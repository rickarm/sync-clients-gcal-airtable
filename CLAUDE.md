# Session Sync: Google Calendar → Airtable

## What This Project Does

Syncs coaching sessions from Google Calendar into the Airtable Sessions table.
Airtable is the system of record. This tool is the system of transformation.

## Core Principles

- **Airtable is the system of record** — never destructive, never guessing
- **Idempotent** — re-running is always safe, produces the same result
- **Deterministic creation** — new Sessions are only created when there is exactly one unambiguous client match via Contacts
- **No silent overwrites** — existing records are only patched where fields are blank

## Project Structure

```
session_sync.py          # Main script (primary entry point)
sync_last_4_weeks.py     # Original script (kept for reference)
credentials.json         # Google OAuth Desktop Client credentials
token.json               # Auto-generated on first auth (do not commit)
.env                     # Local secrets (do not commit)
requirements.txt         # Python dependencies
scripts/
  bootstrap.sh           # Venv setup
  doctor.py              # Environment health check
Makefile                 # Convenience targets
```

## Environment Variables

Required (in `.env`):
```
AIRTABLE_PAT=pat_...
AIRTABLE_BASE_ID=app...
```

Optional:
```
AIRTABLE_SESSIONS_TABLE=Sessions       # default
AIRTABLE_CONTACTS_TABLE=Contacts       # default
SELF_EMAILS=rick@rickarmbrust.com,...  # excluded from attendee matching
```

## Common Commands

```bash
# Health check
make doctor

# Dry run (safe, no writes)
make dryrun

# Dry run with no-match report (diagnose why sessions aren't being created)
make report

# Dry run with create report (see what would be created)
python session_sync.py --dry-run --weeks 4 --report-create

# Apply (writes to Airtable)
APPLY=1 make apply

# Backfill longer window
python session_sync.py --apply --weeks 12 --calendar-id primary
```

## Airtable Schema

### Sessions table
| Field | Purpose |
|---|---|
| `Calendar Event ID` | Unique key (iCalUID + "_" + start UTC) |
| `SessionTimeDate (UTC)` | Event start time in UTC |
| `Matched Attendee Email` | Email used to identify the client |
| `Matched Contact` | Linked record → Contacts |
| `Client` | Linked record → Clients |

### Contacts table
| Field | Purpose |
|---|---|
| `Email` | Exact match against attendee emails |
| `Client` | Linked record → Clients |

## Matching Logic

1. Extract attendee emails from calendar event (excluding SELF_EMAILS)
2. For each email, look up exact match in Contacts.Email
3. Contact must link to exactly one Client
4. If exactly one unique Client matches across all attendees → create/update
5. If zero or multiple Clients → skip (no create, log in no-match report)

## What This Tool Does NOT Do

- Does not invent or guess sessions
- Does not overwrite populated Airtable fields
- Does not use title-based matching
- Does not touch notes, billing, or CRM data beyond session counts

## Google Auth

- `credentials.json` must be present (Google OAuth Desktop Client)
- `token.json` is auto-created on first run (delete to re-auth)
- Scope: `calendar.readonly`

## Troubleshooting

**Nothing is being created:**
Run `make report` to see no-match details. Most common causes:
- Attendee email doesn't match `Contacts.Email` exactly
- Contact has 0 or >1 linked Client
- Multiple attendees map to different clients

**Auth errors:**
Delete `token.json` and rerun — it will re-prompt for Google auth.

**Airtable 401/403:**
Confirm PAT is valid and has access to the base.
