#!/usr/bin/env bash
# Entry point for the launchd weekly gcal→airtable sync.
#
# Does NOT source .env via bash. python-dotenv inside session_sync.py reads it
# directly, which handles quoting and special characters that bash's `source`
# cannot parse (e.g. unescaped parentheses in values).

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

LOG_DIR="$SCRIPT_DIR/logs"
mkdir -p "$LOG_DIR"
LAUNCHD_LOG="$LOG_DIR/launchd.log"

exec >> "$LAUNCHD_LOG" 2>&1

echo "[$(date '+%Y-%m-%d %H:%M:%S')] run_sync.sh starting"

"$SCRIPT_DIR/.venv/bin/python" session_sync.py \
  --apply \
  --weeks 4 \
  --calendar-id primary

RC=$?
echo "[$(date '+%Y-%m-%d %H:%M:%S')] run_sync.sh done (exit $RC)"
exit $RC
