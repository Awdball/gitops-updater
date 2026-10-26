# GitOps Updater (`gitops-updater`)

[![Build and Publish Docker Image](https://github.com/Awdball/gitops-updater/actions/workflows/docker-publish.yml/badge.svg)](https://github.com/Awdball/gitops-updater/actions/workflows/docker-publish.yml)
[![Container Image](https://img.shields.io/badge/GHCR-ghcr.io%2Fawdball%2Fgitops--updater-blue?logo=docker)](https://github.com/Awdball/gitops-updater/pkgs/container/gitops-updater)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

**GitOps Updater** is a lightweight Kubernetes ChatOps bot. It monitors upstream container registries for image updates, automatically applies minor and patch releases, and sends Telegram alerts with one-tap approval buttons for major version upgrades:

```text
⚡ Auto-Applied Image Updates (Production)

The following updates fall within the current version line and were applied automatically:
• sonarr: 4.0.9 ➔ 4.0.10
• tautulli: v2.18.2 ➔ v2.18.3

Commit: abc1234
🔄 Flux reconciliation triggered.
```

When a major version change is detected (e.g. `postgres: 15.4 ➔ 16.0`), the bot sends an approval prompt:

```text
🔔 Major Version Updates Available (Production)

• postgres: 15.4 ➔ 16.0

[ 🚀 Apply Updates ]   [ ❌ Dismiss ]
```

### 🛡️ Plex Streaming Protection
Before applying **any** update to Plex (whether automated or manual), GitOps Updater queries Plex's active sessions. If any client is actively streaming, the Plex update is safely postponed, leaving active viewers uninterrupted while allowing other workloads to roll out.

When you tap **Apply**, the bot commits rollout annotations directly to your GitOps repository (Flux / Argo CD) and triggers an immediate reconciliation. 

**No SSH. No VPN. Zero open inbound ports.**

---

## How It Works

```
 ┌──────────────────────┐        ┌──────────────────────┐
 │ Container Registries │        │  Kubernetes Cluster  │
 │ (Docker Hub, GHCR)   │        │   (Pods & Digests)   │
 └──────────┬───────────┘        └──────────┬───────────┘
            │                               │
            └───────────────┬───────────────┘
                            ▼
              ┌───────────────────────────┐
              │      gitops-updater       │
              └─────────────┬─────────────┘
                            │ Outbound Long-Polling
                            ▼
                     ┌─────────────┐
                     │  Telegram   │ ──► [ 🚀 Apply Updates ]
                     └──────┬──────┘
                            │ Tap "Apply"
                            ▼
               ┌─────────────────────────┐
               │    GitOps Repository    │ ──► Triggers Flux / Argo CD
               │   (GitHub, GitLab)      │
               └─────────────────────────┘
```

1. **Scans Workloads:** Inspects your running pods and checks upstream registries for new digests and semantic version labels.
2. **Pushes to Telegram:** Alerts your authorized Telegram chat with pending updates.
3. **Rolls Out via GitOps:** When you approve, it updates pod annotations, commits to `main`, and triggers reconciliation.

### Why not Watchtower or Renovate?
- **Watchtower** modifies containers directly on nodes, bypassing Git. GitOps controllers (Flux/Argo) see this as drift and immediately revert it.
- **Renovate / Flux Image Automation** require rewriting Git tags to pinned semver digests and fail on mutable tags like `:latest` or `:v4`.
- **GitOps Updater** allows you to keep `:latest` or `:vX` in your manifests while safely rolling out new releases through Git.

<details>
<summary><b>🔍 Deep Dive: How Workloads & Versions Are Discovered</b></summary>

<br>

### 1. In-Cluster Workload Discovery
When a scan runs (either scheduled or via `/check`), `gitops-updater`:
- **Discovers Running Pods:** Queries the Kubernetes API (`/api/v1/pods`) across all namespaces to inspect running containers.
- **Extracts Live Image Digests:** Retrieves the exact image digest running in the container runtime via `.status.containerStatuses[*].imageID` (`sha256:...`).
- **Detects Node Architecture:** Inspects the node hosting each pod (`.spec.nodeName`) to identify target CPU architecture (`amd64` vs. `arm64`), ensuring multi-architecture images resolve the matching platform manifest.
- **Correlates with Git Manifests:** Parses YAML files in your GitOps manifests directory (`APPS_DIR`) to match live pods back to their source Deployment, StatefulSet, or DaemonSet declarations.

### 2. Upstream Version Resolution
For each tracked container, `gitops-updater` queries the upstream OCI registry (`docker.io`, `ghcr.io`, `lscr.io`, etc.):
- **Resolves Multi-Arch Manifests:** Pulls the manifest for the tag channel (e.g. `:latest` or `:v4`) and resolves the manifest list / OCI index to match the running node's architecture.
- **Primary Resolution (OCI Labels):** Downloads the container config blob and reads standard OCI metadata labels:
  - `org.opencontainers.image.version`
  - `org.label-schema.version`
  - `com.docker.image.version`
  - `version`
- **Fallback Resolution (Registry Tag Matching):** If the image author did not embed OCI labels in the image config, the bot automatically queries the registry's tags API, matches the running digest against existing tags, and extracts the corresponding semantic version tag (`vX.Y.Z`).

### 3. Decision Matrix
Comparing the live runtime state against upstream registry metadata:
- **`current`:** Live digest matches upstream registry digest. Workload is up to date.
- **`same version; rebuilt`:** Digest differs, but semantic version string is unchanged (e.g., base OS security rebuild).
- **`update available`:** Newer semantic version detected (e.g., `4.0.9` ➔ `4.0.10`). Pushed to Telegram with `[ 🚀 Apply Updates ]`.

</details>

---

## 3-Minute Quickstart

### 1. Get Telegram Credentials (60 seconds)
1. Message **[@BotFather](https://t.me/BotFather)** on Telegram, send `/newbot`, and copy your **Bot Token**.
2. Message **[@userinfobot](https://t.me/userinfobot)** and copy your numerical **Id** (this whitelists your phone).

### 2. Create the Kubernetes Secret
Provide your Telegram credentials and your Git deploy SSH private key:

```bash
kubectl create secret generic gitops-updater-secret \
  --namespace default \
  --from-literal=TELEGRAM_BOT_TOKEN="123456789:ABCdefGHI..." \
  --from-literal=TELEGRAM_ALLOWED_CHAT_IDS="123456789" \
  --from-file=id_ed25519=~/.ssh/your_gitops_deploy_key
```

### 3. Deploy `gitops-updater`
Apply the deployment manifest to your cluster:

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: gitops-updater
  namespace: default
spec:
  replicas: 1
  strategy:
    type: Recreate
  selector:
    matchLabels:
      app.kubernetes.io/name: gitops-updater
  template:
    metadata:
      labels:
        app.kubernetes.io/name: gitops-updater
    spec:
      serviceAccountName: gitops-updater
      containers:
        - name: gitops-updater
          image: ghcr.io/awdball/gitops-updater:latest
          imagePullPolicy: Always
          envFrom:
            - secretRef:
                name: gitops-updater-secret
          env:
            - name: GIT_REPO_URL
              value: "git@github.com:your-username/your-gitops-repo.git"
            - name: GIT_BRANCH
              value: "main"
            - name: CLUSTER_NAME
              value: "Home Cluster"
            - name: SCHEDULE_TIME
              value: "04:00"
            - name: TZ
              value: "America/Chicago"
          volumeMounts:
            - name: ssh-key
              mountPath: /etc/gitops-updater/ssh
              readOnly: true
            - name: workspace
              mountPath: /workspace
      volumes:
        - name: ssh-key
          secret:
            secretName: gitops-updater-secret
            items:
              - key: id_ed25519
                path: id_ed25519
                mode: 256
        - name: workspace
          emptyDir: {}
```
*(Also apply the standard RBAC manifest from [rbac.yaml](https://github.com/Awdball/gitops-updater/blob/main/rbac.yaml) to allow reading pods/nodes and annotating Flux).*

### 4. Start Using It
Open your bot in Telegram and send:
- `/check` - Run an immediate scan of all cluster workloads.
- `/status` - View current cluster image status and scheduled crawl time.
- `/apply` - Apply pending updates directly.

---

## Configuration Settings

### Essential Settings
| Variable | Default | Description |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | *Required* | API token from `@BotFather`. |
| `TELEGRAM_ALLOWED_CHAT_IDS` | *Required* | Comma-separated User IDs allowed to access the bot. |
| `GIT_REPO_URL` | *Required* | SSH clone URL for your GitOps repo. |
| `GIT_BRANCH` | `main` | Git branch to monitor and commit updates to. |
| `CLUSTER_NAME` | `Kubernetes Cluster` | Display name shown in Telegram notifications. |
| `SCHEDULE_TIME` | `04:00` | Daily scan time (`HH:MM`). |
| `TZ` | `UTC` | Timezone for the daily schedule (e.g., `America/New_York`). |

<details>
<summary><b>Advanced Settings (Rate-limiting, Pacing, Flux)</b></summary>

<br>

| Variable | Default | Description |
|---|---|---|
| `PLEX_URL` | `http://192.168.0.10:32400` | URL of the Plex Media Server for session and streaming validation. |
| `PLEX_TOKEN` | *None* | Optional `X-Plex-Token` if authentication is required to access `/status/sessions`. |
| `SCHEDULE_DELAY_SECONDS` | `120` | Delay (seconds) between workload checks during the daily scheduled crawl to prevent Docker Hub throttling. |
| `MANUAL_DELAY_SECONDS` | `0.5` | Delay (seconds) between workload checks during on-demand `/check`. |
| `SKIP_REPOSITORIES` | `""` | Comma-separated list of image names to ignore (e.g. local images like `my-app:cpu`). |
| `APPS_DIR` | `apps` | Directory in your Git repo where workload manifests live. |
| `ROLLOUT_ANNOTATION_KEY` | `gitops.updater/rollout-timestamp` | Annotation injected into pod templates to trigger redeployments. |
| `FLUX_ENABLED` | `true` | Whether to automatically trigger Flux reconciliation after commit. |
| `FLUX_NAMESPACE` | `flux-system` | Namespace where Flux resources reside. |
| `FLUX_GIT_REPOSITORIES` | *Auto-discover* | Comma-separated GitRepositories to annotate (auto-detected if blank). |
| `FLUX_KUSTOMIZATIONS` | *Auto-discover* | Comma-separated Kustomizations to annotate (auto-detected if blank). |
| `NOTIFY_ON_NO_UPDATES` | `false` | Whether to send a Telegram message if the daily check finds 0 updates. |
| `GIT_USER_NAME` | `GitOps Updater` | Author name for Git commits. |
| `GIT_USER_EMAIL` | `gitops-updater@noreply.github.com` | Author email for Git commits. |

</details>

---

## Security

- **Strict Access Control:** Only Telegram accounts whose numerical IDs are listed in `TELEGRAM_ALLOWED_CHAT_IDS` can interact with the bot. Unauthorized users receive an immediate rejection and are logged.
- **Outbound Only:** The bot uses long-polling (`getUpdates`). It never listens on a public port and never requires webhook routing or firewall openings.
- **Deploy Key Isolation:** The container only requires write access to your designated GitOps repository.

---

## License

MIT License. See [LICENSE](LICENSE) for details.
