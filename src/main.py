#!/usr/bin/env python3
"""Main entrypoint for gitops-updater bot."""

import logging
import os
import sys
import time
from pathlib import Path

from bot import TelegramBot
from updater import GitOpsEngine


def main() -> int:
    log_level = os.environ.get("LOG_LEVEL", "INFO").upper()
    logging.basicConfig(
        level=getattr(logging, log_level, logging.INFO),
        format="[%(asctime)s] [%(levelname)s] [%(name)s] %(message)s",
    )
    logger = logging.getLogger("gitops_updater")

    while True:
        token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
        allowed_ids_raw = os.environ.get("TELEGRAM_ALLOWED_CHAT_IDS", "").strip()

        if not token or token == "PENDING_SETUP" or not allowed_ids_raw or allowed_ids_raw == "0":
            logger.warning(
                "TELEGRAM_BOT_TOKEN or TELEGRAM_ALLOWED_CHAT_IDS is not configured yet. "
                "Waiting for credentials to be configured... (checking every 30s)"
            )
            time.sleep(30)
            continue
        break

    try:
        allowed_chat_ids = {int(x.strip()) for x in allowed_ids_raw.split(",") if x.strip()}
    except ValueError as exc:
        logger.error(f"Invalid TELEGRAM_ALLOWED_CHAT_IDS format: {exc}. Must be comma-separated integers.")
        return 1

    cluster_name = os.environ.get("CLUSTER_NAME", "Kubernetes Cluster").strip()
    repo_dir_str = os.environ.get("REPO_DIR", "/workspace/repo")
    repo_dir = Path(repo_dir_str).resolve()
    if not repo_dir.exists():
        logger.error(f"Git repository directory {repo_dir} does not exist.")
        return 1

    apps_dir_name = os.environ.get("APPS_DIR", "apps").strip()
    rollout_annotation_key = os.environ.get("ROLLOUT_ANNOTATION_KEY", "gitops.updater/rollout-timestamp").strip()
    
    skip_repos_raw = os.environ.get("SKIP_REPOSITORIES", "").strip()
    skip_repositories = {x.strip() for x in skip_repos_raw.split(",") if x.strip()}

    schedule_time = os.environ.get("SCHEDULE_TIME", "04:00").strip()
    try:
        schedule_delay = float(os.environ.get("SCHEDULE_DELAY_SECONDS", "120").strip())
    except ValueError:
        schedule_delay = 120.0
    try:
        manual_delay = float(os.environ.get("MANUAL_DELAY_SECONDS", "0.5").strip())
    except ValueError:
        manual_delay = 0.5
    notify_on_no_updates = os.environ.get("NOTIFY_ON_NO_UPDATES", "false").lower() in {"1", "true", "yes"}

    flux_enabled = os.environ.get("FLUX_ENABLED", "true").lower() in {"1", "true", "yes"}
    flux_namespace = os.environ.get("FLUX_NAMESPACE", "flux-system").strip()
    
    flux_git_repos_raw = os.environ.get("FLUX_GIT_REPOSITORIES", "").strip()
    flux_git_repositories = [x.strip() for x in flux_git_repos_raw.split(",") if x.strip()] or None

    flux_kustomizations_raw = os.environ.get("FLUX_KUSTOMIZATIONS", "").strip()
    flux_kustomizations = [x.strip() for x in flux_kustomizations_raw.split(",") if x.strip()] or None

    plex_url = os.environ.get("PLEX_URL", "http://192.168.0.10:32400").strip()
    plex_token = os.environ.get("PLEX_TOKEN", "").strip() or None

    logger.info(f"Initializing GitOpsEngine for repository: {repo_dir}")
    logger.info(f"Cluster: '{cluster_name}', Manifests directory: '{apps_dir_name}'")
    logger.info(f"Rollout annotation key: '{rollout_annotation_key}'")
    logger.info(f"Plex streaming check URL: '{plex_url}'")
    logger.info("Auto-apply policy: minor/patch updates applied automatically; major version changes require Telegram approval.")
    if skip_repositories:
        logger.info(f"Ignored image repositories: {skip_repositories}")
    logger.info(f"Scheduled scan time: {schedule_time} (delay between workloads: {schedule_delay}s)")
    logger.info(f"Flux reconciliation enabled: {flux_enabled} (namespace: '{flux_namespace}')")

    engine = GitOpsEngine(
        repo_dir=repo_dir,
        apps_dir_name=apps_dir_name,
        rollout_annotation_key=rollout_annotation_key,
        skip_repositories=skip_repositories,
        flux_enabled=flux_enabled,
        flux_namespace=flux_namespace,
        flux_git_repositories=flux_git_repositories,
        flux_kustomizations=flux_kustomizations,
        plex_url=plex_url,
        plex_token=plex_token,
    )
    bot = TelegramBot(
        token=token,
        allowed_chat_ids=allowed_chat_ids,
        engine=engine,
        cluster_name=cluster_name,
        schedule_time_str=schedule_time,
        schedule_delay_seconds=schedule_delay,
        manual_delay_seconds=manual_delay,
        notify_on_no_updates=notify_on_no_updates,
    )

    try:
        bot.start()
    except KeyboardInterrupt:
        logger.info("Shutdown requested.")
        bot.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
