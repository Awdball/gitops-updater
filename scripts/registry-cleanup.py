#!/usr/bin/env python3
"""Remove legacy orphan tags from this repository's GHCR package.

Background
----------
The pre-1.1 publishing setup pushed a branch-name ``main`` tag and per-push
``sha-*`` dev tags to GHCR on every build of ``main``. Those tags are
development debris: they have no version identity and make the registry page
look unprofessional.

GHCR's registry API has no DELETE method (``DELETE /v2/<repo>/manifests/<tag>``
returns HTTP 405 even for owners), so orphan tags must be removed through the
GitHub packages REST API, which requires an authenticated token (the CI job's
GITHUB_TOKEN is sufficient with the declared ``packages: write`` permission).

In the GitHub packages model a *version* is a unique manifest digest and
deleting a version removes that digest together with every tag that points at
it. For that reason this script never touches a version whose id or digest is
shared with the protected release tags (``PROTECTED``) - doing so would wipe
the current release out of the registry.

Usage (CI)
----------
The script is driven entirely by environment variables:

  GH_TOKEN   GitHub API token (CI: ``secrets.GITHUB_TOKEN``)
  PKG        package name (CI: ``github.event.repository.name``)
  ORPHANS    space-separated tag names to delete
  PROTECTED  space-separated tag names whose versions must survive
  API_BASE   optional override of the GitHub API base (for local testing)

Exit status: 0 when all present orphans were deleted (or were absent),
1 when any deletion was rejected by the API.
"""

import json
import os
import sys
import urllib.error
import urllib.request


def main() -> int:
    token = os.environ["GH_TOKEN"]
    pkg = os.environ["PKG"]
    orphans = os.environ.get("ORPHANS", "").split()
    protected = set(os.environ.get("PROTECTED", "").split())
    api_base = os.environ.get("API_BASE", "https://api.github.com").rstrip("/")

    def call(method: str, url: str):
        req = urllib.request.Request(
            url,
            method=method,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
            },
        )
        with urllib.request.urlopen(req) as resp:
            return resp.status, resp.read()

    status, data = call("GET", f"{api_base}/user/packages/container/{pkg}/versions?per_page=100")
    versions = json.loads(data)

    protected_ids = set()
    protected_digests = set()
    for version in versions:
        if version.get("name") in protected:
            protected_ids.add(version["id"])
            for manifest in version.get("manifests") or []:
                if manifest.get("digest"):
                    protected_digests.add(manifest["digest"])

    failed = []
    for tag in orphans:
        version = next((v for v in versions if v.get("name") == tag), None)
        if version is None:
            print(f"  {tag}: absent (already clean)")
            continue
        digests = [m.get("digest") for m in version.get("manifests") or []]
        if version["id"] in protected_ids or any(d in protected_digests for d in digests):
            print(f"  {tag}: SKIPPED - shares its manifest with a protected release tag")
            continue
        url = f"{api_base}/user/packages/container/{pkg}/versions/{version['id']}"
        try:
            status, body = call("DELETE", url)
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                print(f"  {tag}: already removed")
                continue
            detail = exc.read()[:200].decode(errors="replace")
            print(f"  {tag}: FAILED HTTP {exc.code}: {detail}", file=sys.stderr)
            failed.append(tag)
            continue
        if status in (200, 202):
            print(f"  {tag}: deleted (HTTP {status})")
        else:
            detail = body[:200].decode(errors="replace")
            print(f"  {tag}: FAILED HTTP {status}: {detail}", file=sys.stderr)
            failed.append(tag)

    if failed:
        print(f"registry cleanup FAILED for: {', '.join(failed)}", file=sys.stderr)
        return 1
    print("registry cleanup complete")
    return 0


if __name__ == "__main__":
    sys.exit(main())
