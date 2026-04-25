# openwebui-markdown-sync

A lightweight .NET containerised tool that syncs Markdown documentation from locally-cloned GitHub repositories into [Open WebUI](https://github.com/open-webui/open-webui) Knowledge Bases. It is designed to run as a one-shot Docker container — on a schedule or on demand — and only uploads files that are new or have changed since the last run.

## How it works

1. The container scans a root directory that contains one or more cloned GitHub documentation repositories (e.g. `microsoft/vscode-docs`, `dotnet/docs`).
2. For each subdirectory it finds, it locates or creates a matching Knowledge Base in Open WebUI (named after the repository folder).
3. It computes an MD5 hash for every `.md` file and compares it against a persisted state file. Only new or modified files are uploaded.
4. Uploaded files are batch-attached to their Knowledge Base via the Open WebUI API.
5. The updated state is saved so subsequent runs only process what has actually changed.

## Markdown repositories

The repositories synced by this tool are GitHub-hosted documentation sources — primarily from the [GitHub Docs](https://github.com/github/docs) ecosystem and other open-source documentation projects. They are cloned locally and placed in the `markdown-repos/` subdirectory inside this project. That folder is volume-mounted into the container and excluded from version control via `.gitignore`. Each repository's folder name becomes the name of the corresponding Knowledge Base in Open WebUI, so keeping repository folder names descriptive is recommended.

### Cloning repositories

Clone repositories using SSH to avoid credential prompts in automated tasks:

```bash
cd /path/to/openwebui-markdown-sync/markdown-repos
git clone git@github.com:org/repo-name.git
```

### Git safe.directory

If your scheduled tasks run as `root` (common on Synology and in Docker contexts), Git will refuse to operate on repositories owned by a different user unless they are marked as safe. Run the following for each cloned repository:

```bash
sudo git config --global --add safe.directory /path/to/openwebui-markdown-sync/markdown-repos/repo-name
```

Or to trust all repositories under the `markdown-repos/` folder at once:

```bash
sudo git config --global --add safe.directory '*'
```

> **Note:** Using `*` is convenient but disables the ownership check globally for root. Use per-repo entries if you prefer a stricter security posture.

## Prerequisites

| Requirement | Notes |
| -- | -- |
| **Docker / Docker Compose** | Tested on Synology DSM with Container Manager |
| **Open WebUI** | Must be running and reachable at `WEBUI_URL`; v0.3+ recommended |
| **Open WebUI API key** | Generate one in Open WebUI → Settings → Account → API Keys |
| **Cloned markdown repositories** | One or more directories of `.md` files under `ROOT_REPOS_PATH` |
| **.NET 10 runtime** | Provided by the Docker image; no local install needed |

## Configuration

Copy `.env.example` (or create `.env`) and populate the following variables:

| Variable | Description | Default |
| -- | -- | -- |
| `WEBUI_URL` | Base URL of your Open WebUI instance | `http://localhost:3000` |
| `API_KEY` | Bearer token for the Open WebUI API | *(required)* |
| `STATE_FILE` | Path inside the container where sync state is persisted | `/data/sync_state.json` |
| `ROOT_REPOS_PATH` | Host path (mounted into the container) containing the cloned repositories | *(required)* |

> **Security note:** The `.env` file contains your API key and is excluded from version control via `.gitignore`. Never commit it.

## Running

### Initial build

Build the image once before scheduling:

```bash
docker compose build
```

### On-demand sync

Use `--rm` so the container is automatically removed after each run, keeping things clean:

```bash
docker compose run --rm openwebui-markdown-sync
```

### Scheduled syncing on Synology

Set up two **Synology Task Scheduler** tasks (Control Panel → Task Scheduler → Create → Scheduled Task → User-defined script). Run them under a user with Docker access (e.g. your admin account).

#### Task 1 — Pull latest docs from GitHub

Create one task per repository, or a single script that loops over all of them:

| Setting | Value |
| -- | -- |
| **Task name** | `git-pull-markdown-repos` |
| **Schedule** | e.g. daily at 02:00 |
| **User** | Your Synology user |

**Script:**

```bash
#!/bin/bash
for repo in /path/to/openwebui-markdown-sync/markdown-repos/*/; do
    git -C "$repo" pull --ff-only
done
```

#### Task 2 — Run the sync container

| Setting | Value |
| -- | -- |
| **Task name** | `openwebui-markdown-sync` |
| **Schedule** | e.g. daily at 02:30 (after Task 1 completes) |
| **User** | Your Synology user |

**Script:**

```bash
#!/bin/bash
cd /path/to/openwebui-markdown-sync
docker compose run --rm openwebui-markdown-sync
```

> **Tip:** Give Task 2 a 30-minute offset from Task 1 to ensure the pulls have finished before the sync runs. For large repositories you may need a longer gap.

### Scheduled syncing on Linux

Add two cron entries via `crontab -e`:

```cron
# Task 1 — Pull latest docs from GitHub (runs daily at 02:00)
0 2 * * * for repo in /path/to/openwebui-markdown-sync/markdown-repos/*/; do git -C "$repo" pull --ff-only; done

# Task 2 — Run the sync container (runs daily at 02:30, after pulls complete)
30 2 * * * cd /path/to/openwebui-markdown-sync && docker compose run --rm openwebui-markdown-sync
```

Redirect output to a log file if you want to keep a history:

```cron
0 2 * * * for repo in /path/to/openwebui-markdown-sync/markdown-repos/*/; do git -C "$repo" pull --ff-only >> /var/log/openwebui-markdown-sync.log 2>&1; done
30 2 * * * cd /path/to/openwebui-markdown-sync && docker compose run --rm openwebui-markdown-sync >> /var/log/openwebui-markdown-sync.log 2>&1
```

## State persistence

Sync state is written to `./data/sync_state.json` on the host (mounted at `/data` inside the container). This file records the MD5 hash of every previously uploaded file so incremental runs are fast. Do not delete it unless you want a full re-upload on the next run.

## Project structure

```bash
compose.yaml          # Docker Compose service definition (not committed)
compose.yaml.example  # Generic template to copy from
app/
  Program.cs          # All sync logic
  app.csproj          # .NET 10 project file
  Dockerfile          # Multi-stage build (SDK → runtime)
markdown-repos/       # Cloned documentation repositories (not committed)
data/                 # Created at runtime; holds sync_state.json
.env                  # Local config (not committed)
.env.example          # Template for .env
```
