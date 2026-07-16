# sync-clients-gcal-airtable

Syncs coaching sessions from Google Calendar into an Airtable Sessions table. Safe and idempotent — re-running never overwrites populated fields. New sessions are only created when there is an unambiguous one-to-one match between a calendar attendee and an Airtable Client record.

## How Matching Works

1. Reads events from Google Calendar for the last N weeks
2. Extracts attendee emails (excluding your own via `SELF_EMAILS`)
3. Looks up each email in `Contacts.Email` (exact match)
4. If exactly one unique Client is found → creates or patches the Session record
5. Ambiguous or unmatched events are skipped and reported

## Key Files

| File | Purpose |
|---|---|
| `session_sync.py` | Main script (primary entry point) |
| `sync_last_4_weeks.py` | Original version (kept for reference) |
| `Makefile` | Convenience targets: `make dryrun`, `make report`, `make apply` |
| `requirements.txt` | Python dependencies |
| `credentials.json` | Google OAuth Desktop Client credentials (not committed) |
| `token.json` | Auto-generated on first auth (not committed) |
| `CLAUDE.md` | Developer guide with schema, matching logic, troubleshooting |
| `README.md` | Full documentation |
| `scripts/bootstrap.sh` | Sets up the Python virtual environment |
| `scripts/doctor.py` | Environment health check (`make doctor`) |

## Common Commands

```bash
make doctor        # Check environment setup
make dryrun        # Preview changes, no writes
make report        # Dry run + show why events aren't matching
APPLY=1 make apply # Write to Airtable

# Backfill a longer window
python session_sync.py --apply --weeks 12 --calendar-id primary
```

## Airtable Schema

**Sessions:** `Calendar Event ID`, `SessionTimeDate (UTC)`, `Matched Attendee Email`, `Matched Contact`, `Client`, `Count Against Package?` (set "Exclude" on intros so they don't draw down a prepaid package — see CLAUDE.md)

**Contacts:** `Email`, `Client`
