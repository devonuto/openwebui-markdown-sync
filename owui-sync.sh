#!/usr/bin/env bash
# owui-sync.sh — Pull all git repos in HOST_DROP, then trigger Open WebUI import.
#
# HOST_DROP      — path on the NAS/host where the drop folder lives (used for git pulls)
# CONTAINER_DROP — path to the same folder as seen from inside the Open WebUI container
#                  (used in the API call; must match allowed_base_dirs in the Valve)

set -euo pipefail

HOST_DROP="${HOST_DROP:-/host/path/to/drop}"
CONTAINER_DROP="${CONTAINER_DROP:-/app/backend/data/drop}"
OWUI_URL="${OWUI_URL:-http://localhost:3000}"
OWUI_API_KEY="${OWUI_API_KEY:-}"       # Set via environment or replace here
OWUI_MODEL="${OWUI_MODEL:-gpt-5.4-nano}"    # Any model that has the tool enabled
OWUI_TOOL_ID="${OWUI_TOOL_ID:-local_directory_import}"  # Tool ID from Workspace → Tools

# ── 1. Git pull every immediate subfolder (runs on the host) ─────────────────
for dir in "$HOST_DROP"/*/; do
    [ -d "$dir/.git" ] || continue
    echo "[sync] Pulling $dir"
    git -C "$dir" pull --ff-only
done

# ── 2. Trigger the Open WebUI import tool via the chat completions API ────────
#    The plugin runs inside the container, so we pass CONTAINER_DROP here.
echo "[sync] Triggering Open WebUI import for $CONTAINER_DROP"
curl -s -X POST "$OWUI_URL/api/chat/completions" \
    -H "Authorization: Bearer $OWUI_API_KEY" \
    -H "Content-Type: application/json" \
    -d @- <<JSON
{
  "model": "$OWUI_MODEL",
  "tool_ids": ["$OWUI_TOOL_ID"],
  "messages": [
    {
      "role": "user",
      "content": "Import the documents in $CONTAINER_DROP into the knowledge bases."
    }
  ]
}
JSON

echo "[sync] Done."
