#!/usr/bin/env python3
"""Core scanning and rollout engine for gitops-updater."""

from __future__ import annotations

import base64
import json
import logging
import os
import re
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import yaml

logger = logging.getLogger("gitops_updater.engine")

IMAGE_LINE = re.compile(r"^(?P<indent>\s+image:\s+)(?P<image>[^\s#]+)(?P<suffix>\s*(?:#.*)?)$")
WORKLOAD_KINDS = {"Deployment", "StatefulSet", "DaemonSet", "Job", "CronJob"}
TAG_VERSION = re.compile(r"^v?(?P<major>\d+)\.(?P<minor>\d+)\.(?P<patch>\d+)$")
SEMVER_REGEX = re.compile(r"\bv?(?P<major>\d+)(?:\.(?P<minor>\d+))?(?:\.(?P<patch>\d+))?\b")
MANIFEST_LIST_TYPES = {
    "application/vnd.docker.distribution.manifest.list.v2+json",
    "application/vnd.oci.image.index.v1+json",
}


def parse_semver(version: str) -> tuple[int, int, int] | None:
    if not version:
        return None
    m = SEMVER_REGEX.search(version.strip())
    if not m:
        return None
    major = int(m.group("major"))
    minor = int(m.group("minor")) if m.group("minor") is not None else 0
    patch = int(m.group("patch")) if m.group("patch") is not None else 0
    return major, minor, patch


def is_major_change(curr_version: str, latest_version: str) -> bool:
    curr_parsed = parse_semver(curr_version)
    latest_parsed = parse_semver(latest_version)
    if curr_parsed is None or latest_parsed is None:
        return True
    return curr_parsed[0] != latest_parsed[0]


def is_plex_workload(workload: str, repository: str) -> bool:
    wl = (workload or "").lower()
    repo = (repository or "").lower()
    return "plex" in wl or "mediaslave" in wl or "plex" in repo or "pms-docker" in repo


def check_plex_active_streams(
    plex_url: str = "http://192.168.0.10:32400",
    token: str | None = None,
    timeout: float = 5.0,
) -> tuple[bool, str]:
    clean_url = plex_url.rstrip("/")
    if not clean_url.startswith("http://") and not clean_url.startswith("https://"):
        clean_url = f"http://{clean_url}"
    endpoint = f"{clean_url}/status/sessions"

    headers = {"Accept": "application/json", "User-Agent": "gitops-updater/1.0"}
    if token:
        headers["X-Plex-Token"] = token

    try:
        req = Request(endpoint, headers=headers)
        with urlopen(req, timeout=timeout) as resp:
            content_type = resp.headers.get("Content-Type", "")
            raw = resp.read().decode("utf-8", errors="replace")
            if "json" in content_type or raw.strip().startswith("{"):
                data = json.loads(raw)
                container = data.get("MediaContainer", {})
                size = container.get("size", 0)
                metadata = container.get("Metadata", [])
                if size == 0 and not metadata:
                    return False, "No active streams"
                stream_details = []
                for item in metadata:
                    user = item.get("User", {}).get("title") or "User"
                    player = item.get("Player", {}).get("title") or item.get("Player", {}).get("product") or "Device"
                    grandparent = item.get("grandparentTitle")
                    title = item.get("title") or "Media"
                    media_name = f"{grandparent} - {title}" if grandparent else title
                    stream_details.append(f"{user} on {player} watching '{media_name}'")
                detail_text = "; ".join(stream_details) if stream_details else f"{size} stream(s) active"
                return True, detail_text

            size_match = re.search(r'size="(\d+)"', raw)
            if size_match:
                size = int(size_match.group(1))
                if size == 0:
                    return False, "No active streams"
                return True, f"{size} stream(s) active in Plex"

            return False, "No active streams"
    except Exception as exc:
        logger.warning(f"Failed to query Plex streaming status from {endpoint}: {exc}")
        return True, f"Failed to reach Plex server at {endpoint}: {exc}"


@dataclass(frozen=True)
class ImageRef:
    file: Path
    line_number: int
    workload: str
    current: str
    repository: str
    tag: str


@dataclass(frozen=True)
class RegistryImage:
    repository: str
    reference: str
    digest: str
    version: str | None

    @property
    def update_reference(self) -> str:
        if self.version:
            return f"{self.repository}:{self.version}"
        return f"{self.repository}@{self.digest}"


@dataclass(frozen=True)
class Update:
    image: ImageRef
    available: RegistryImage
    running_version: str
    latest_version: str
    is_major: bool = False
    is_plex: bool = False


@dataclass(frozen=True)
class RunningImage:
    namespace: str
    pod: str
    container: str
    workload: str
    repository: str
    tag: str
    requested: str
    digest: str
    node_architecture: str


@dataclass(frozen=True)
class DockerHubTag:
    name: str
    digest: str
    architecture_digests: dict[str, str]


@dataclass
class ScanResult:
    scanned_at: datetime
    updates: list[Update] = field(default_factory=list)
    up_to_date_count: int = 0
    rebuilt_count: int = 0
    errors: list[str] = field(default_factory=list)
    all_workloads: list[dict[str, str]] = field(default_factory=list)


@dataclass
class ApplyResult:
    applied_at: datetime
    success: bool
    commit_sha: str | None = None
    updated_workloads: list[str] = field(default_factory=list)
    deferred_workloads: list[str] = field(default_factory=list)
    deferred_reasons: dict[str, str] = field(default_factory=dict)
    error_message: str | None = None


def parse_image(value: str) -> tuple[str, str] | None:
    if "@" in value:
        return None
    image, separator, tag = value.rpartition(":")
    if not separator or "/" in tag:
        image, tag = value, "latest"
    return image, tag


def registry_for(repository: str) -> tuple[str, str]:
    parts = repository.split("/", 1)
    if parts[0] in {"ghcr.io", "lscr.io", "docker.io", "registry.hub.docker.com"}:
        registry, path = parts
    else:
        registry, path = "docker.io", repository
    if registry == "lscr.io":
        registry = "docker.io"
    if registry == "docker.io" and "/" not in path:
        path = f"library/{path}"
    return registry, path


class RegistryClient:
    def __init__(self, repository: str):
        self.source_repository = repository
        self.registry, self.path = registry_for(repository)
        self._dockerhub_tag_cache: list[DockerHubTag] | None = None
        self.base_url = f"https://{self.registry}/v2/{self.path}"
        headers = {"User-Agent": "gitops-updater/1.0"}
        token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GHCR_TOKEN")
        if self.registry == "ghcr.io":
            auth_headers = dict(headers)
            if token:
                auth_headers["Authorization"] = f"Bearer {token}"
            query = urlencode({"service": "ghcr.io", "scope": f"repository:{self.path}:pull"})
            with urlopen(Request(f"https://ghcr.io/token?{query}", headers=auth_headers), timeout=15) as response:
                registry_token = json.load(response)["token"]
            headers["Authorization"] = f"Bearer {registry_token}"
        if self.registry == "docker.io":
            username = os.environ.get("DOCKERHUB_USERNAME")
            password = os.environ.get("DOCKERHUB_TOKEN")
            credentials = f"&account={username}" if username and password else ""
            if username and password:
                encoded = base64.b64encode(f"{username}:{password}".encode()).decode()
                headers["Authorization"] = f"Basic {encoded}"
            query = urlencode({"service": "registry.docker.io", "scope": f"repository:{self.path}:pull"})
            with urlopen(Request(f"https://auth.docker.io/token?{query}{credentials}", headers=headers), timeout=15) as response:
                registry_token = json.load(response)["token"]
            headers["Authorization"] = f"Bearer {registry_token}"
            self.base_url = f"https://registry-1.docker.io/v2/{self.path}"
        self.headers = headers

    @property
    def repository(self) -> str:
        if self.registry == "docker.io" and self.path.startswith("library/"):
            return self.path.removeprefix("library/")
        return f"{self.registry}/{self.path}"

    def manifest(self, reference: str) -> tuple[dict, str]:
        accept = ", ".join((*MANIFEST_LIST_TYPES, "application/vnd.docker.distribution.manifest.v2+json", "application/vnd.oci.image.manifest.v1+json"))
        request = Request(f"{self.base_url}/manifests/{reference}", headers={**self.headers, "Accept": accept})
        with urlopen(request, timeout=15) as response:
            return json.load(response), response.headers.get("Docker-Content-Digest", "")

    def image(self, reference: str, architecture: str = "amd64") -> RegistryImage:
        manifest, digest = self.manifest(reference)
        if manifest.get("mediaType") in MANIFEST_LIST_TYPES or "manifests" in manifest:
            descriptor = next((item for item in manifest["manifests"] if item.get("platform", {}).get("architecture") == architecture and item.get("platform", {}).get("os") == "linux"), manifest["manifests"][0])
            digest = descriptor["digest"]
            manifest, _ = self.manifest(digest)
        config_digest = manifest.get("config", {}).get("digest")
        labels: dict[str, str] = {}
        if config_digest:
            with urlopen(Request(f"{self.base_url}/blobs/{config_digest}", headers=self.headers), timeout=15) as response:
                config = json.load(response)
            labels = config.get("config", {}).get("Labels", {}) or {}
        version = next((labels.get(key) for key in (
            "org.opencontainers.image.version",
            "org.label-schema.version",
            "com.docker.image.version",
            "version",
        ) if labels.get(key)), None)
        return RegistryImage(self.source_repository, reference, digest, version)

    def dockerhub_tags(self) -> list[DockerHubTag]:
        if self.registry != "docker.io":
            return []
        if self._dockerhub_tag_cache is not None:
            return self._dockerhub_tag_cache
        tags: list[DockerHubTag] = []
        url = f"https://hub.docker.com/v2/repositories/{self.path}/tags?page_size=100"
        while url:
            with urlopen(Request(url, headers={"User-Agent": "gitops-updater/1.0"}), timeout=15) as response:
                payload = json.load(response)
            for item in payload.get("results", []):
                tags.append(DockerHubTag(
                    item["name"],
                    item.get("digest", ""),
                    {image["architecture"]: image["digest"] for image in item.get("images", []) if image.get("digest")},
                ))
            url = payload.get("next") or ""
        self._dockerhub_tag_cache = tags
        return tags

    def image_from_dockerhub_tag(self, reference: str, digest: str, architecture: str = "amd64") -> RegistryImage:
        tags = self.dockerhub_tags()
        matching = [
            tag for tag in tags
            if tag.digest == digest or tag.architecture_digests.get(architecture) == digest
        ]
        versioned = [tag for tag in matching if TAG_VERSION.fullmatch(tag.name)]
        version = max(versioned, key=lambda tag: tuple(int(TAG_VERSION.fullmatch(tag.name).group(name)) for name in ("major", "minor", "patch"))).name if versioned else None
        return RegistryImage(self.source_repository, reference, digest, version)

    def image_from_dockerhub_reference(self, reference: str, architecture: str = "amd64") -> RegistryImage:
        tag = next((tag for tag in self.dockerhub_tags() if tag.name == reference), None)
        if tag is None:
            raise ValueError(f"Docker Hub tag not found: {self.source_repository}:{reference}")
        digest = tag.architecture_digests.get(architecture) or tag.digest
        return self.image_from_dockerhub_tag(reference, digest, architecture)


def kubectl_json(*args: str) -> dict:
    return json.loads(subprocess.check_output(["kubectl", *args], text=True))


def split_image(value: str) -> tuple[str, str | None]:
    image = value.split("@", 1)[0]
    parsed = parse_image(image)
    if not parsed:
        raise ValueError(f"Unsupported image reference: {value}")
    return parsed[0], value.split("@", 1)[1] if "@" in value else None


def running_images() -> list[RunningImage]:
    nodes = {node["metadata"]["name"]: node["status"]["nodeInfo"]["architecture"] for node in kubectl_json("get", "nodes", "-o", "json")["items"]}
    result: list[RunningImage] = []
    for pod in kubectl_json("get", "pods", "--all-namespaces", "-o", "json")["items"]:
        if pod.get("status", {}).get("phase") != "Running":
            continue
        statuses = {status["name"]: status for status in pod.get("status", {}).get("containerStatuses", [])}
        for container in pod.get("spec", {}).get("containers", []):
            status = statuses.get(container["name"])
            if not status or "imageID" not in status:
                continue
            repository, _ = split_image(container["image"])
            parsed = parse_image(container["image"].split("@", 1)[0])
            if not parsed:
                continue
            _, tag = parsed
            digest = status["imageID"].rsplit("@", 1)[-1]
            workload = pod.get("metadata", {}).get("labels", {}).get("app") or pod.get("metadata", {}).get("labels", {}).get("app.kubernetes.io/name")
            if not workload:
                workload = pod["metadata"]["name"].rsplit("-", 2)[0]
            result.append(RunningImage(pod["metadata"]["namespace"], pod["metadata"]["name"], container["name"], workload, repository, tag, container["image"], digest, nodes.get(pod["spec"].get("nodeName"), "amd64")))
    return result


def canonical_repository(repository: str) -> str:
    if repository.startswith("docker.io/"):
        return repository.removeprefix("docker.io/")
    if "/" not in repository.split(":", 1)[0]:
        return f"docker.io/{repository}"
    return repository


def latest_reference(tag: str) -> str:
    if re.fullmatch(r"v\d+", tag):
        return tag
    return "latest"


def find_images(root: Path, apps_dir_name: str = "apps") -> list[ImageRef]:
    images: list[ImageRef] = []
    apps_dir = root / apps_dir_name
    if not apps_dir.exists():
        logger.warning(f"Manifests directory {apps_dir} does not exist.")
        return []
    for path in sorted(apps_dir.rglob("*.yaml")):
        try:
            documents = list(yaml.safe_load_all(path.read_text()))
        except yaml.YAMLError:
            continue
        if not any(document and document.get("kind") in WORKLOAD_KINDS for document in documents):
            continue
        for line_number, line in enumerate(path.read_text().splitlines(), 1):
            match = IMAGE_LINE.match(line)
            if not match:
                continue
            parsed = parse_image(match.group("image"))
            if parsed:
                repository, tag = parsed
                images.append(ImageRef(path, line_number, path.parent.name, match.group("image"), repository, tag))
    return images


def manifest_reference(update: Update) -> str:
    tag = latest_reference(update.image.tag)
    if tag == "latest" or re.fullmatch(r"v\d+", tag):
        return f"{update.image.repository}:{tag}"
    return update.image.current


def replace_image(update: Update) -> None:
    path = update.image.file
    lines = path.read_text().splitlines(keepends=True)
    index = update.image.line_number - 1
    line = lines[index]
    lines[index] = line.replace(update.image.current, manifest_reference(update), 1)
    path.write_text("".join(lines))


def add_rollout_marker(path: Path, value: str, annotation_key: str = "gitops.updater/rollout-timestamp") -> None:
    lines = path.read_text().splitlines(keepends=True)
    template_index = next(index for index, line in enumerate(lines) if line.strip() == "template:")
    template_indent = len(lines[template_index]) - len(lines[template_index].lstrip())
    metadata_index = next(
        index for index in range(template_index + 1, len(lines))
        if lines[index].strip() == "metadata:" and len(lines[index]) - len(lines[index].lstrip()) == template_indent + 2
    )
    metadata_indent = template_indent + 2
    annotations_index = next(
        (index for index in range(metadata_index + 1, len(lines))
         if lines[index].strip() == "annotations:" and len(lines[index]) - len(lines[index].lstrip()) == metadata_indent + 2),
        None,
    )
    marker = annotation_key
    if annotations_index is None:
        lines[metadata_index + 1:metadata_index + 1] = [
            " " * (metadata_indent + 2) + "annotations:\n",
            " " * (metadata_indent + 4) + f"{marker}: {value!r}\n",
        ]
    else:
        annotation_end = next(
            (index for index in range(annotations_index + 1, len(lines))
             if lines[index].strip() and len(lines[index]) - len(lines[index].lstrip()) <= metadata_indent + 2),
            len(lines),
        )
        marker_index = next(
            (index for index in range(annotations_index + 1, annotation_end) if marker + ":" in lines[index]),
            None,
        )
        marker_line = " " * (metadata_indent + 4) + f"{marker}: {value!r}\n"
        if marker_index is None:
            lines[annotation_end:annotation_end] = [marker_line]
        else:
            lines[marker_index] = marker_line
    path.write_text("".join(lines))


def refresh_flux_workloads(
    namespace: str = "flux-system",
    gitrepositories: list[str] | None = None,
    kustomizations: list[str] | None = None,
) -> None:
    now = str(int(datetime.now(timezone.utc).timestamp()))
    
    # Auto-discover if not explicitly configured
    if gitrepositories is None:
        try:
            res = kubectl_json("-n", namespace, "get", "gitrepositories", "-o", "json")
            gitrepositories = [item["metadata"]["name"] for item in res.get("items", [])]
        except Exception:
            gitrepositories = []

    if kustomizations is None:
        try:
            res = kubectl_json("-n", namespace, "get", "kustomizations", "-o", "json")
            kustomizations = [item["metadata"]["name"] for item in res.get("items", [])]
        except Exception:
            kustomizations = []

    for gitrepo in gitrepositories:
        subprocess.run([
            "kubectl", "-n", namespace, "annotate", "gitrepository",
            gitrepo, f"reconcile.fluxcd.io/requestedAt={now}", "--overwrite",
        ], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    for kustomization in kustomizations:
        subprocess.run([
            "kubectl", "-n", namespace, "annotate", "kustomization",
            kustomization, f"reconcile.fluxcd.io/requestedAt={now}", "--overwrite",
        ], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    logger.info(f"Triggered Flux reconciliation in {namespace} (gitrepositories: {gitrepositories}, kustomizations: {kustomizations})")


class GitOpsEngine:
    def __init__(
        self,
        repo_dir: Path,
        apps_dir_name: str = "apps",
        rollout_annotation_key: str = "gitops.updater/rollout-timestamp",
        skip_repositories: set[str] | None = None,
        flux_enabled: bool = True,
        flux_namespace: str = "flux-system",
        flux_git_repositories: list[str] | None = None,
        flux_kustomizations: list[str] | None = None,
        plex_url: str = "http://192.168.0.10:32400",
        plex_token: str | None = None,
    ):
        self.repo_dir = repo_dir
        self.apps_dir_name = apps_dir_name
        self.rollout_annotation_key = rollout_annotation_key
        self.skip_repositories = skip_repositories or set()
        self.flux_enabled = flux_enabled
        self.flux_namespace = flux_namespace
        self.flux_git_repositories = flux_git_repositories
        self.flux_kustomizations = flux_kustomizations
        self.plex_url = plex_url
        self.plex_token = plex_token

    def is_plex_streaming(self) -> tuple[bool, str]:
        """Validates if any client is actively streaming on Plex."""
        return check_plex_active_streams(self.plex_url, self.plex_token)

    def ensure_latest_git(self) -> None:
        """Pulls and resets to latest remote branch before scanning or applying."""
        try:
            subprocess.run(["git", "fetch", "origin", "main"], cwd=self.repo_dir, check=True, capture_output=True)
            subprocess.run(["git", "reset", "--hard", "origin/main"], cwd=self.repo_dir, check=True, capture_output=True)
        except subprocess.CalledProcessError as exc:
            logger.warning(f"Git fetch/reset failed: {exc.stderr.decode() if exc.stderr else exc}")

    def check_updates(self, delay_seconds: float = 0.0) -> ScanResult:
        self.ensure_latest_git()
        scan_time = datetime.now(timezone.utc)
        result = ScanResult(scanned_at=scan_time)

        try:
            running = running_images()
        except Exception as exc:
            msg = f"Unable to inspect running K3s containers: {exc}"
            logger.error(msg)
            result.errors.append(msg)
            return result

        refs = find_images(self.repo_dir, self.apps_dir_name)
        registry_cache: dict[tuple[str, str, str], RegistryImage] = {}

        checked_workloads = 0
        canonical_skips = {canonical_repository(repo) for repo in self.skip_repositories}

        for item in running:
            if canonical_repository(item.repository) in canonical_skips:
                continue
            matching_refs = [
                img for img in refs
                if img.workload == item.workload and canonical_repository(img.repository) == canonical_repository(item.repository)
            ]
            if not matching_refs:
                continue

            if checked_workloads > 0 and delay_seconds > 0:
                logger.debug(f"Paced crawl: sleeping {delay_seconds}s before inspecting {item.workload}...")
                time.sleep(delay_seconds)
            checked_workloads += 1

            try:
                client = RegistryClient(item.repository)
                current_key = (item.repository, item.digest, item.node_architecture)
                channel = latest_reference(item.tag)
                latest_key = (item.repository, channel, item.node_architecture)

                current = registry_cache.setdefault(
                    current_key,
                    client.image(item.digest, item.node_architecture),
                )
                latest = registry_cache.setdefault(
                    latest_key,
                    client.image(channel, item.node_architecture),
                )

                # Generic Fallback: If image lacks OCI metadata labels, inspect Docker Hub tags for semver tags matching digest
                if (current.version is None or latest.version is None) and client.registry == "docker.io":
                    if current.version is None:
                        current = client.image_from_dockerhub_tag(item.requested, item.digest, item.node_architecture)
                        registry_cache[current_key] = current
                    if latest.version is None:
                        try:
                            latest = client.image_from_dockerhub_reference(channel, item.node_architecture)
                            registry_cache[latest_key] = latest
                        except ValueError:
                            pass

                if current.version is None or latest.version is None:
                    continue

                current_version = current.version
                latest_version = latest.version
                same_version = current_version == latest_version

                if item.digest == latest.digest:
                    state = "current"
                    result.up_to_date_count += 1
                elif same_version:
                    state = "same version; rebuilt"
                    result.rebuilt_count += 1
                else:
                    state = "update available"

                result.all_workloads.append({
                    "workload": f"{item.namespace}/{item.workload}",
                    "container": item.container,
                    "running": current_version,
                    "latest": latest_version,
                    "state": state,
                })

                if state != "update available":
                    continue

                is_major = is_major_change(current_version, latest_version)
                is_plex = is_plex_workload(item.workload, item.repository)

                for ref in matching_refs:
                    if ref.current != latest.update_reference:
                        result.updates.append(Update(
                            image=ref,
                            available=latest,
                            running_version=current_version,
                            latest_version=latest_version,
                            is_major=is_major,
                            is_plex=is_plex,
                        ))

            except (HTTPError, URLError, TimeoutError, json.JSONDecodeError, KeyError, IndexError, ValueError) as exc:
                err_msg = f"{item.repository}: unable to query registry ({exc})"
                logger.warning(err_msg)
                result.errors.append(err_msg)

        # Deduplicate updates by (file, line_number)
        seen: set[tuple[Path, int]] = set()
        deduped: list[Update] = []
        for upd in result.updates:
            key = (upd.image.file, upd.image.line_number)
            if key not in seen:
                seen.add(key)
                deduped.append(upd)
        result.updates = deduped
        return result

    def apply_updates(self, updates_to_apply: list[Update] | None = None) -> ApplyResult:
        now = datetime.now(timezone.utc)
        self.ensure_latest_git()

        if updates_to_apply is None:
            scan = self.check_updates()
            updates_to_apply = scan.updates

        if not updates_to_apply:
            return ApplyResult(
                applied_at=now,
                success=True,
                commit_sha=None,
                updated_workloads=[],
                deferred_workloads=[],
                deferred_reasons={},
                error_message="No pending updates found to apply.",
            )

        # Validate Plex streaming status if any Plex update is in the batch
        deferred_workloads: list[str] = []
        deferred_reasons: dict[str, str] = {}
        applicable_updates: list[Update] = []

        for update in updates_to_apply:
            if update.is_plex or is_plex_workload(update.image.workload, update.image.repository):
                is_streaming, stream_reason = self.is_plex_streaming()
                if is_streaming:
                    logger.warning(f"Plex update deferred due to active streaming: {stream_reason}")
                    deferred_workloads.append(f"{update.image.workload} ({update.running_version} -> {update.latest_version})")
                    deferred_reasons[update.image.workload] = stream_reason
                    continue
            applicable_updates.append(update)

        if not applicable_updates:
            return ApplyResult(
                applied_at=now,
                success=True,
                commit_sha=None,
                updated_workloads=[],
                deferred_workloads=deferred_workloads,
                deferred_reasons=deferred_reasons,
                error_message="All candidate updates deferred (Plex active streaming detected)." if deferred_workloads else "No updates to apply.",
            )

        changed_files: set[Path] = set()
        workloads_updated: list[str] = []

        for update in applicable_updates:
            replace_image(update)
            changed_files.add(update.image.file)
            workloads_updated.append(f"{update.image.workload} ({update.running_version} -> {update.latest_version})")

        marker = now.isoformat(timespec="seconds")
        for path in changed_files:
            add_rollout_marker(path, marker, self.rollout_annotation_key)

        # Validate kustomize
        try:
            subprocess.run(
                ["kubectl", "kustomize", self.apps_dir_name],
                cwd=self.repo_dir,
                check=True,
                capture_output=True,
                text=True,
            )
        except subprocess.CalledProcessError as exc:
            err = f"Kustomize validation failed: {exc.stderr}"
            logger.error(err)
            subprocess.run(["git", "reset", "--hard", "HEAD"], cwd=self.repo_dir)
            return ApplyResult(
                applied_at=now,
                success=False,
                deferred_workloads=deferred_workloads,
                deferred_reasons=deferred_reasons,
                error_message=err,
            )

        # Git commit and push
        try:
            rel_files = [str(p.relative_to(self.repo_dir)) for p in changed_files]
            subprocess.run(["git", "add", *rel_files], cwd=self.repo_dir, check=True)
            commit_msg = (
                "Update container images [gitops-updater]\n\n" +
                "\n".join(f"- {w}" for w in workloads_updated)
            )
            subprocess.run(["git", "commit", "-m", commit_msg], cwd=self.repo_dir, check=True)
            sha = subprocess.check_output(["git", "rev-parse", "--short", "HEAD"], cwd=self.repo_dir, text=True).strip()
            subprocess.run(["git", "push", "origin", "main"], cwd=self.repo_dir, check=True)
            
            if self.flux_enabled:
                refresh_flux_workloads(
                    namespace=self.flux_namespace,
                    gitrepositories=self.flux_git_repositories,
                    kustomizations=self.flux_kustomizations,
                )

            return ApplyResult(
                applied_at=now,
                success=True,
                commit_sha=sha,
                updated_workloads=workloads_updated,
                deferred_workloads=deferred_workloads,
                deferred_reasons=deferred_reasons,
            )
        except subprocess.CalledProcessError as exc:
            err = f"Git commit/push or reconciliation failed: {exc}"
            logger.error(err)
            return ApplyResult(
                applied_at=now,
                success=False,
                deferred_workloads=deferred_workloads,
                deferred_reasons=deferred_reasons,
                error_message=err,
            )
