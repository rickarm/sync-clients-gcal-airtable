# Session Sync: Google Calendar → Airtable

## What This Project Does

Syncs coaching sessions from Google Calendar into the Airtable Sessions table.
Airtable is the system of record. This tool is the system of transformation.

## Core Principles

- **Airtable is the system of record** — never destructive, never guessing
- **Idempotent** — re-running is always safe, produces the same result
- **Deterministic creation** — new Sessions are only created when there is exactly one unambiguous client match via Contacts
- **No silent overwrites** — existing records are only patched where fields are blank

## Development Workflow

See `KB-Development-Workflow.md` in the Knowledge Base for the full workflow. Summary:

1. Bugs and features are tracked as **GitHub Issues**
2. Claude works on a **feature branch** (worktrees for isolation in local sessions)
3. Claude pushes the branch and opens a **Pull Request**
4. Rick reviews and merges the PR
5. Adding the `claude` label to an issue triggers Claude via GitHub Actions

**CI / merge gating:** `main` merge-gating is a GitHub **ruleset** ("CI-test"), NOT classic branch protection — `gh api repos/rickarm/sync-clients-gcal-airtable/branches/main/protection` returns 404. Inspect/edit via `gh api repos/rickarm/sync-clients-gcal-airtable/rulesets`. The required status-check `context` must equal the Actions **check-run name** (the job name `test`), not `ci / test` — a mismatch shows "Expected — Waiting for status to be reported" forever even though CI passed. Use the keyring-authed `gh` (repo+workflow scopes) for ruleset edits; the `GH_TOKEN` PAT in `~/.env` can't read/write protection.

## Project Structure

```
session_sync.py          # Main script (primary entry point)
add_client.py            # New client onboarding: creates Company + Contact in Airtable, adds to known_clients.json
known_clients.json       # Email → company_record_id map; auto-creates Contact on first sync if missing from Airtable
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
AIRTABLE_CLIENTS_TABLE=Company         # default (the Company/Clients table)
SELF_EMAILS=rick@example.com,...  # excluded from attendee matching
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

# Onboard a new client (creates Company if needed, Contact, updates known_clients.json)
python add_client.py --name "Alice Smith" --email alice@co.com --company "Acme Corp"
python add_client.py --name "Alice Smith" --email alice@co.com --company "Acme Corp" --dry-run
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
| `Company` | Linked record → Company (note: field is called "Company" not "Client") |

### Company table
| Field | Purpose |
|---|---|
| `Client` | Primary field — company/client name (note: primary field is called "Client" not "Name") |
| `Billing Model` | "Prepaid Sessions" / "Retainer". **Presence = this is a real coaching client.** Used to exclude non-client companies (referral/BD contacts) from session matching. |
| `Rate (per session)` | Billing rate in USD |
| `Status-Company` | "Active" for current clients |

**Known issue:** Client A, Client B, and Client C each have duplicate Contact records (same email, same Company link). Sync works (first match wins) — don't create more.

## Matching Logic

1. Extract attendee emails from calendar event (excluding SELF_EMAILS)
2. For each email, look up exact match in Contacts.Email
3. Contact must link to exactly one Company
4. That Company must be a real coaching client — i.e. it has a `Billing Model` set. Companies without one (referral sources, BD/relationship contacts filed under a non-client company) are skipped, even though the Contact→Company link is unique. This prevents phantom sessions (e.g. a coffee with a VC who referred a client).
5. If exactly one unique billable Client matches across all attendees → create/update
6. If zero or multiple Clients → skip (no create, log in no-match report)

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

**Verifying changes to matching logic:**
`make dryrun` shows already-synced events as "Already complete" and does NOT re-run client matching for them — so it won't catch a regression in matching on existing sessions. To verify matching changes, unit-test the function directly against live Airtable (e.g. call `company_is_billable_client` / `resolve_unique_client` with real record IDs) or test against a fresh, unsynced event.
