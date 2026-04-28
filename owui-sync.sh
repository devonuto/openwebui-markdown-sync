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
OWUI_MODEL="${OWUI_MODEL:-gpt-4o}"    # Any model that has the tool enabled
OWUI_TOOL_ID="${OWUI_TOOL_ID:-local_directory_import}"  # Tool ID from Workspace → Tools

# ── 1. Git pull every immediate subfolder (runs on the host) ─────────────────
for dir in "$HOST_DROP"/*/; do
    [ -d "$dir/.git" ] || continue
    echo "[sync] Pulling $dir"
    git -C "$dir" pull --ff-only
done

# ── 2. Trigger the Open WebUI import tool via the chat completions API ────────
#    The plugin runs inside the container, so we pass CONTAINER_DROP here.
#    The system prompt + terse user message keeps token usage to a minimum:
#    the model should call the tool immediately and reply with only the JSON.
echo "[sync] Triggering Open WebUI import for $CONTAINER_DROP"

# stream=true is required — without it Open WebUI returns the raw model response
# and never executes the tool server-side. With streaming the full tool-call loop
# runs: model → tool execution → final reply.
PAYLOAD=$(cat <<JSON
{
  "model": "$OWUI_MODEL",
  "tool_ids": ["$OWUI_TOOL_ID"],
  "stream": true,
  "messages": [
    {
      "role": "system",
      "content": "You are an automation agent. When asked to import, call import_local_directory immediately and reply with only the raw JSON result. No explanation."
    },
    {
      "role": "user",
      "content": "import"
    }
  ]
}
JSON
)

echo "[sync] POST $OWUI_URL/api/chat/completions model=$OWUI_MODEL tool_ids=[$OWUI_TOOL_ID]"

RESPONSE=$(curl -f -S -X POST "$OWUI_URL/api/chat/completions" \
    -H "Authorization: Bearer $OWUI_API_KEY" \
    -H "Content-Type: application/json" \
    -d "$PAYLOAD")

CURL_EXIT=$?
if [ $CURL_EXIT -ne 0 ]; then
    echo "[sync] ERROR: curl failed (exit $CURL_EXIT)"
    echo "[sync] Raw response: $RESPONSE"
    exit 1
fi

echo "[sync] Raw stream (first 2000 chars):"
echo "${RESPONSE:0:2000}"
echo "---"

# Extract the final assistant content from the SSE stream
# Each chunk is: data: {"choices":[{"delta":{"content":"..."}}]}
# We concatenate all content deltas to reconstruct the full reply.
FINAL=$(echo "$RESPONSE" \
    | grep '^data: ' \
    | grep -v '^data: \[DONE\]' \
    | sed 's/^data: //' \
    | python3 -c "
import sys, json
buf = ''
errors = []
for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    try:
        obj = json.loads(line)
        delta = obj.get('choices', [{}])[0].get('delta', {})
        buf += delta.get('content', '') or ''
    except Exception as e:
        errors.append(str(e))
if errors:
    print('(parse errors:', errors[:3], ')', file=sys.stderr)
print(buf)
")

echo "[sync] Result: $FINAL"
echo "[sync] Done."
