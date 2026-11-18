#!/usr/bin/env python3
"""Delete orphan package versions from a user's GHCR package.

Strategy (digest-driven, registry-derived):
  1. Query GitHub packages API for all versions (id, name, created_at, manifests[]).
  2. Fetch live tag -> digest mapping from the GHCR registry list endpoint.
  3. Determine the PROTECTED digest set: any digest that a PROTECTED tag points at.
  4. For each version:
       - If ANY of its manifest digests is in the protected set -> SKIP (release).
       - Otherwise it is pure orphan debris -> DELETE it.
  5. Report what was deleted / skipped.

Env:
  GH_TOKEN    GitHub API token with packages:write
  PKG         package name (e.g. "ascender-monitor")
  PROTECTED   optional: space-separated tag names to protect. When unset,
              every semver tag (1.0, 1.2.0, ...) and `latest` in the
              registry is protected automatically.
"""
import json
import os
import re
import sys
import urllib.error
import urllib.request

TOKEN = os.environ["GH_TOKEN"]
PKG = os.environ["PKG"]
API = "https://api.github.com".rstrip("/")
REGISTRY = "https://ghcr.io"


def reg_bearer(pkg: str):
    import base64
    req = urllib.request.Request(
        f"{REGISTRY}/token?service=ghcr.io&scope=repository:awdball/{pkg}:pull",
        headers={"Authorization": "Basic " + base64.b64encode(f"awdball:{TOKEN}".encode()).decode()},
    )
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read())["token"]


def gh(method: str, path: str):
    req = urllib.request.Request(
        f"{API}{path}",
        method=method,
        headers={"Authorization": f"Bearer {TOKEN}", "Accept": "application/vnd.github+json"},
    )
    with urllib.request.urlopen(req) as resp:
        return resp.status, resp.read()


# --- Step 1: versions from packages API ---
status, data = gh("GET", f"/user/packages/container/{PKG}/versions?per_page=100")
versions = json.loads(data)
print(f"versions: {len(versions)}")

# Build: digest -> set of version_ids (a version may have multiple manifests/digests)
digest_to_vids = {}
vid_info = {}
for v in versions:
    vid = v["id"]
    name = v.get("name", "")
    created = v.get("created_at", "")[:10]
    digests = [m.get("digest", "") for m in (v.get("manifests") or []) if m.get("digest")]
    vid_info[vid] = {"name": name, "created": created, "created_at": v.get("created_at", ""), "digests": digests}
    for d in digests:
        digest_to_vids.setdefault(d, set()).add(vid)
    # Also index by the version's own primary digest (name is often a digest)
    if name.startswith("sha256:"):
        digest_to_vids.setdefault(name, set()).add(vid)

# --- Step 2: live tag -> digest from registry ---
_BEARER_CACHE = {}

def _bearer():
    if PKG not in _BEARER_CACHE:
        _BEARER_CACHE[PKG] = reg_bearer(PKG)
    return _BEARER_CACHE[PKG]

def reg_list():
    req = urllib.request.Request(f"{REGISTRY}/v2/awdball/{PKG}/tags/list?n=200",
                                 headers={"Authorization": f"Bearer {_bearer()}"})
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read()).get("tags", [])

def reg_digest(tag):
    """Return the index digest for a tag from the registry."""
    req = urllib.request.Request(f"{REGISTRY}/v2/awdball/{PKG}/manifests/{tag}", headers={
        "Authorization": f"Bearer {_bearer()}",
        "Accept": "application/vnd.oci.image.index.v1+json, application/vnd.docker.distribution.manifest.list.v2+json, application/vnd.oci.image.manifest.v1+json, application/vnd.docker.distribution.manifest.v2+json"
    })
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.headers.get("Docker-Content-Digest", "")
    except Exception:
        return ""

tags = reg_list()
print(f"tags: {len(tags)}")

# --- Step 3: protected digest set ---
# PROTECTED env (explicit) takes precedence; by default every semver tag
# (1.0, 1.2.0, ...) and `latest` present in the registry is protected.
PROTECTED = set(os.environ.get("PROTECTED", "").split()) or {
    t for t in tags if t == "latest" or re.fullmatch(r"\d+(\.\d+)+", t)
}
if os.environ.get("PROTECTED"):
    print(f"PROTECTED (explicit): {sorted(PROTECTED)}")
protected_digests = set()
for t in sorted(PROTECTED):
    d = reg_digest(t)
    if d:
        protected_digests.add(d)
        print(f"  PROTECTED {t:<10} digest={d[:20]}...")
    else:
        print(f"  PROTECTED {t:<10} (no digest found)")

# --- Step 3b: protect the platform children of every protected multi-arch index.
# The child (per-arch) manifests are untagged and each has its own version id;
# without this, deleting "untagged" versions orphans the index it belongs to.
from urllib.error import HTTPError

def reg_manifest_ref(tag):
    req = urllib.request.Request(
        f"{REGISTRY}/v2/awdball/{PKG}/manifests/{tag}",
        headers={
            "Authorization": f"Bearer {_bearer()}",
            "Accept": "application/vnd.oci.image.index.v1+json, application/vnd.docker.distribution.manifest.list.v2+json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())
    except Exception:
        return None

children_of = []
for t in sorted(PROTECTED):
    ref = reg_manifest_ref(t)
    if ref and "manifests" in ref:  # multi-arch index
        for m in ref["manifests"]:
            if m.get("digest"):
                protected_digests.add(m["digest"])
                children_of.append((t, m["digest"][:20]))
if children_of:
    for t, d in children_of:
        print(f"  PROTECTED-CHILD of {t:<8} digest={d}...")

# --- Step 3c: AGE GUARD.
# Never touch manifests created within the last 6 hours: a live CI push may not
# yet be visible as "tagged" in the API (eventual consistency), and GitHub's
# GC is asynchronous after deletes. Orphan dev junk is always much older.
from datetime import datetime, timedelta, timezone
AGC_CUTOFF = datetime.now(timezone.utc) - timedelta(hours=6)

# --- Step 4: classify each version ---
to_delete = []
to_keep = []
for vid, info in sorted(vid_info.items(), key=lambda x: x[1]["created"], reverse=True):
    name = info["name"]
    digests = set(info["digests"])
    # Also check the version's name-as-digest
    if name.startswith("sha256:"):
        digests.add(name)
    if digests & protected_digests:
        to_keep.append((vid, info))
        continue
    # Age guard: skip anything created within the last 6 hours (in-flight CI).
    try:
        created_dt = datetime.fromisoformat(info["created_at"].replace("Z", "+00:00"))
        if created_dt > AGC_CUTOFF:
            print(f"  AGE-GUARD skip {vid} (created {info['created_at']}, name={name[:24]})")
            to_keep.append((vid, info))
            continue
    except Exception:
        pass
    to_delete.append((vid, info))

print(f"\n=== DELETE ({len(to_delete)}) ===")
for vid, info in to_delete:
    print(f"  {vid}  {info['created']}  name={info['name'][:24]}  digests={[d[:16] for d in info['digests']]}")
print(f"\n=== KEEP  ({len(to_keep)}) ===")
for vid, info in to_keep:
    print(f"  {vid}  {info['created']}  name={info['name'][:24]}  digests={[d[:16] for d in info['digests']]}")

if "--dry-run" in sys.argv:
    print("\ndry-run complete")
    sys.exit(0)

# --- Step 5: execute deletions ---
failed = []
for vid, info in to_delete:
    try:
        status, body = gh("DELETE", f"/user/packages/container/{PKG}/versions/{vid}")
        if status in (200, 202, 204):
            print(f"  DELETED {vid} ({info['name'][:30]})")
        else:
            print(f"  FAILED  {vid} HTTP {status}: {body[:200].decode(errors='replace')}")
            failed.append(vid)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            print(f"  ALREADY GONE {vid}")
        else:
            detail = exc.read()[:200].decode(errors="replace")
            print(f"  FAILED  {vid} HTTP {exc.code}: {detail}")
            failed.append(vid)

print(f"\n{'CLEANUP OK' if not failed else f'CLEANUP FAILED: {failed}'}")
sys.exit(1 if failed else 0)
