# Local Directory Import — Open WebUI Tool Plugin

An [Open WebUI Tool plugin](https://docs.openwebui.com/features/extensibility/plugin/) that bulk-imports files from a local server-side drop folder into Open WebUI Knowledge Bases.

## How it works

Point the tool at a drop folder on the server's local filesystem. Each **immediate subfolder** of that folder is treated as a target Knowledge Base — the KB is created automatically if it does not already exist. All files found recursively inside each subfolder are then:

1. Read and hashed with SHA-256 (the source file is never modified or renamed) — if a matching hash already exists in the database the file is skipped
2. Copied into Open WebUI's upload directory (`UPLOAD_DIR`)
3. Registered as a file record in the database (with its SHA-256 hash stored)
4. Linked to the corresponding Knowledge Base
5. Vectorized via the retrieval pipeline

The tool returns a JSON summary with per-KB breakdowns (discovered / imported / linked / processed / skipped / failed counts, plus per-file status).

### Example folder layout

```bash
/data/drop/
├── project-alpha/          →  KB: "project-alpha"
│   ├── notes.md
│   └── specs/
│       └── design.pdf
└── onboarding/             →  KB: "onboarding"
    ├── handbook.docx
    └── faq.txt
```

## Installation

1. In Open WebUI, go to **Workspace → Tools**.
2. Click **+** to add a new tool.
3. Paste the contents of [`local_directory_import.py`](local_directory_import.py) into the editor.
4. Save.

## Configuration (Valves)

| Valve | Type | Default | Description |
| -- | -- | -- | -- |
| `allowed_base_dirs` | `list[str]` | `["/app/backend/data/drop"]` | Absolute paths that are permitted as drop-folder roots. The `drop_folder` argument must resolve to a path inside one of these directories. |

> **Note:** The default matches the standard Open WebUI Docker volume mount path. Adjust if your setup differs.

## Usage

The tool exposes a single function that the LLM (or a user with admin rights) can invoke:

```bash
import_local_directory(drop_folder: str) → str (JSON)
```

### **Parameters**

| Parameter | Description |
| -- | -- |
| `drop_folder` | Absolute path to the root folder containing the per-KB subfolders. |

### **Example prompt**

> Import the documents in `/data/drop` into the knowledge bases.

### **Example response (truncated)**

```json
{
  "drop_folder": "/data/drop",
  "total_discovered": 3,
  "total_imported": 2,
  "total_linked": 2,
  "total_processed": 2,
  "total_skipped": 1,
  "total_failed": 0,
  "knowledge_bases": [
    {
      "kb_name": "project-alpha",
      "knowledge_id": "kb_abc123",
      "kb_created": false,
      "discovered": 2,
      "imported": 1,
      "linked": 1,
      "processed": 1,
      "skipped": 1,
      "failed": 0,
      "files": [...]
    }
  ]
}
```

## Access control

- **Admin only.** The tool rejects calls from any user whose role is not `admin`.
- **Allow-list path security.** The `drop_folder` is resolved and checked against `allowed_base_dirs` before any file I/O is performed, preventing path traversal.

## Limitations

- **Local filesystem only.** Not compatible with S3, GCS, or Azure Blob storage backends.
- The drop folder must be accessible to the Open WebUI server process.
- Deduplication is hash-based: a file that moves to a different path but whose content is unchanged will be skipped. A renamed file with the same content is treated as the same file.

## Automated sync with scheduled tasks

A common pattern is to keep each subfolder as a git repository, pull the latest commits on a schedule, then trigger the import so Open WebUI's knowledge bases stay in sync automatically.

### The sync script

[`owui-sync.sh`](owui-sync.sh) is included in this repo. Copy it to the server and make it executable:

```bash
cp owui-sync.sh /usr/local/bin/owui-sync.sh
chmod +x /usr/local/bin/owui-sync.sh
```

## **Required environment variables**

| Variable | Description |
| -- | -- |
| `HOST_DROP` | Path to the drop folder on the **host** (NAS/server). Used by the git pull loop. |
| `CONTAINER_DROP` | Path to the same drop folder as seen **inside the container**. Sent in the API call and must match `allowed_base_dirs` in the Valve. |
| `OWUI_URL` | Base URL of your Open WebUI instance. |
| `OWUI_API_KEY` | API key for an admin account (`Settings → Account → API Keys`). |
| `OWUI_MODEL` | Any model that has the Local Directory Import tool enabled. |
| `OWUI_TOOL_ID` | The tool's ID as shown in **Workspace → Tools**. |

### Cron (Linux)

Edit the crontab for the user that has read access to the drop folder:

```bash
crontab -e
```

Run every night at 02:00:

```cron
0 2 * * * HOST_DROP=/host/path/to/drop CONTAINER_DROP=/app/backend/data/drop OWUI_URL=http://your-owui-host:3000 OWUI_API_KEY=sk-... OWUI_MODEL=gpt-4o OWUI_TOOL_ID=local_directory_import /usr/local/bin/owui-sync.sh >> /var/log/owui-sync.log 2>&1
```

Or store the variables in a file (e.g. `/etc/owui-sync.env`) and source it:

```cron
0 2 * * * . /etc/owui-sync.env && /usr/local/bin/owui-sync.sh >> /var/log/owui-sync.log 2>&1
```

### Synology NAS Task Scheduler

1. Open **DSM → Control Panel → Task Scheduler**.
2. Click **Create → Scheduled Task → User-defined script**.
3. Fill in the **General** tab:
   - Task name: `OWUI Knowledge Sync`
   - User: an account with read access to the drop folder
4. Fill in the **Schedule** tab to your preferred recurrence (e.g. daily at 02:00).
5. On the **Task Settings** tab, paste the following into **Run command**:

    ```bash
    export HOST_DROP=/host/path/to/drop
    export CONTAINER_DROP=/app/backend/data/drop
    export OWUI_URL=http://your-owui-host:3000
    export OWUI_API_KEY=sk-...
    export OWUI_MODEL=gpt-5.4
    export OWUI_TOOL_ID=local_directory_import
    bash /usr/local/bin/owui-sync.sh >> /var/log/owui-sync.log 2>&1
    ```

6. Click **OK**. You can immediately test it with **Action → Run**.

> **Synology note:** If Open WebUI runs in a Docker container on the same NAS, use the container's bridge IP (e.g. `http://172.17.0.1:3000`) or the NAS LAN IP rather than `localhost`, since the script runs outside the container network.

---

## Development

Run the unit tests (no live Open WebUI instance required):

```bash
pytest test_local_directory_import.py
```

## License

MIT
