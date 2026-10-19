#!/bin/sh
set -e

echo "[gitops-updater] Initializing container runtime..."

mkdir -p /root/.ssh
chmod 700 /root/.ssh

# Configure SSH key from secret mount if present
if [ -f "/etc/gitops-updater/ssh/id_ed25519" ]; then
    echo "[gitops-updater] Installing SSH deploy key..."
    cp /etc/gitops-updater/ssh/id_ed25519 /root/.ssh/id_ed25519
    chmod 600 /root/.ssh/id_ed25519
fi

# Fetch host keys to prevent interactive host verification
ssh-keyscan github.com >> /root/.ssh/known_hosts 2>/dev/null || true
ssh-keyscan gitlab.com >> /root/.ssh/known_hosts 2>/dev/null || true
chmod 644 /root/.ssh/known_hosts

# Configure Git user identity
git config --global user.name "${GIT_USER_NAME:-GitOps Updater}"
git config --global user.email "${GIT_USER_EMAIL:-gitops-updater@noreply.github.com}"
git config --global safe.directory "*"

REPO_DIR="${REPO_DIR:-/workspace/repo}"
GIT_BRANCH="${GIT_BRANCH:-main}"

if [ -z "$GIT_REPO_URL" ]; then
    echo "[gitops-updater] ERROR: GIT_REPO_URL environment variable is required."
    exit 1
fi

mkdir -p "$(dirname "$REPO_DIR")"

if [ ! -d "$REPO_DIR/.git" ]; then
    echo "[gitops-updater] Cloning repository (branch: $GIT_BRANCH) into $REPO_DIR..."
    git clone --branch "$GIT_BRANCH" "$GIT_REPO_URL" "$REPO_DIR"
else
    echo "[gitops-updater] Existing repository detected at $REPO_DIR. Refreshing..."
    cd "$REPO_DIR"
    git fetch origin "$GIT_BRANCH"
    git reset --hard "origin/$GIT_BRANCH"
fi

echo "[gitops-updater] Starting application..."
exec python3 /app/src/main.py
