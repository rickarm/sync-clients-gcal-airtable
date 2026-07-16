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

# On failure: alert via Alfred/Telegram + drop a Things task so a silent
# scheduled-run failure surfaces. Both best-effort; never change $RC.
if [[ $RC -ne 0 ]]; then
  ts() { date '+%Y-%m-%d %H:%M:%S'; }

  # Read only the specific keys we need — python-dotenv owns the full .env
  # (values may contain chars bash `source` can't parse; see header note).
  get_env() { grep -E "^$1=" "$HOME/.env" 2>/dev/null | head -1 | cut -d= -f2-; }
  ALFRED_URL="$(get_env ALFRED_URL)"; ALFRED_URL="${ALFRED_URL:-http://127.0.0.1:8200}"
  TOKEN="$(get_env THINGS_AGENT_API_KEY)"; TOKEN="${TOKEN:-$(get_env ALFRED_API_KEY)}"

  # --- Alfred → Telegram alert ---
  if [[ -n "$TOKEN" ]]; then
    LOG_TAIL="$(tail -n 20 "$LAUNCHD_LOG" | jq -Rs 'split("\n") | map(select(. != ""))')"
    PAYLOAD="$(jq -n \
      --arg s "gcal-airtable-sync" \
      --arg t "FAIL" \
      --arg d "Scheduled session sync exited $RC" \
      --argjson tail "$LOG_TAIL" \
      '{service:$s, transition:$t, detail:$d, log_tail:$tail}')"
    if curl -fsS -X POST "$ALFRED_URL/alert" \
         -H "Authorization: Bearer $TOKEN" \
         -H "Content-Type: application/json" \
         --max-time 5 -d "$PAYLOAD" >/dev/null 2>&1; then
      echo "[$(ts)] failure alert pushed to Alfred"
    else
      echo "[$(ts)] failure alert push FAILED"
    fi
  else
    echo "[$(ts)] no Alfred token in ~/.env; skipping Telegram alert"
  fi

  # --- Things task (URL scheme; add needs no auth token) ---
  T_TITLE="GCal→Airtable sync failed (exit $RC)"
  T_NOTES="Scheduled run_sync.sh exited $RC on $(date '+%Y-%m-%d %H:%M'). Check logs/launchd.log for detail. — from Claude"
  ADD_URL="$(T_TITLE="$T_TITLE" T_NOTES="$T_NOTES" python3 -c 'import os,urllib.parse; print("things:///add?"+urllib.parse.urlencode({"title":os.environ["T_TITLE"],"notes":os.environ["T_NOTES"],"when":"today"}))')"
  if open "$ADD_URL" >/dev/null 2>&1; then
    echo "[$(ts)] Things failure task created"
  else
    echo "[$(ts)] Things task creation via open FAILED"
  fi

  # --- GitHub issue (deduped by the sync-failure label) ---
  GH_TOKEN_VAL="$(get_env GH_TOKEN)"
  if [[ -n "$GH_TOKEN_VAL" ]] && command -v gh >/dev/null 2>&1; then
    GH_REPO="rickarm/sync-clients-gcal-airtable"
    GH_BODY="Scheduled \`run_sync.sh\` exited $RC on $(date '+%Y-%m-%d %H:%M %Z').

Last 20 log lines:
\`\`\`
$(tail -n 20 "$LAUNCHD_LOG")
\`\`\`
— filed automatically by run_sync.sh"
    EXISTING="$(GH_TOKEN="$GH_TOKEN_VAL" gh issue list -R "$GH_REPO" --state open --label sync-failure --json number -q '.[0].number' 2>/dev/null)"
    if [[ -n "$EXISTING" ]]; then
      GH_TOKEN="$GH_TOKEN_VAL" gh issue comment "$EXISTING" -R "$GH_REPO" -b "$GH_BODY" >/dev/null 2>&1 \
        && echo "[$(ts)] commented on existing failure issue #$EXISTING" \
        || echo "[$(ts)] GH issue comment FAILED"
    else
      GH_TOKEN="$GH_TOKEN_VAL" gh issue create -R "$GH_REPO" \
        --title "Scheduled sync failed (exit $RC)" \
        --label sync-failure \
        --body "$GH_BODY" >/dev/null 2>&1 \
        && echo "[$(ts)] GH failure issue created" \
        || echo "[$(ts)] GH issue create FAILED"
    fi
  else
    echo "[$(ts)] no GH_TOKEN in ~/.env or gh CLI missing; skipping GitHub issue"
  fi
fi

exit $RC
