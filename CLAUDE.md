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

## Scheduled Run (launchd)

`com.rickarmbrust.gcal-airtable-sync` on the mini runs `run_sync.sh` **Mon 8am + Fri 2pm**
(`session_sync.py --apply --weeks 4 --calendar-id primary`). The plist is tracked in-repo
(`com.rickarmbrust.gcal-airtable-sync.plist`) — edit it, copy to `~/Library/LaunchAgents/`,
then `launchctl bootout`+`load -w` to change the schedule. On non-zero exit `run_sync.sh`
fires three best-effort alerts (none alter the exit code): Alfred/Telegram, a Things "Today"
task, and a GitHub issue deduped by the `sync-failure` label. Logs: `logs/launchd.log`.

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
tools/
  crossbeam_sessions_full.py  # Audit: export all calendar events for a client domain (rich columns + join key)
  reconcile_diff.py           # Audit: year-by-year diff of calendar vs Airtable Sessions
  dump_crossbeam_events.py    # Audit: minimal event/iCalUID dump (superseded by crossbeam_sessions_full.py)
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
| `Count Against Package?` | Single select: Auto / Count / Exclude. Set "Exclude" on intros/kickoffs so they don't draw down a prepaid package. Blank behaves like Auto/Count (billable). The sync does not set this field. |
| `Counts vs Package` | Formula: `IF({Count Against Package?} = 'Exclude', 0, 1)`. Feeds the Company `# Billable Sessions` rollup. |

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
| `Sessions Purchased (All Time)` | Rollup of Payments → Sessions Purchased |
| `# Sessions (All Time)` | Plain count of all linked Sessions. All-time history only — do NOT use for the prepaid balance. |
| `# Billable Sessions` | Rollup: `SUM(values)` of Sessions `Counts vs Package`. Sessions marked Exclude don't count. |
| `Paid Sessions Remaining` | Formula: blank for Retainer, else `MAX(0, {Sessions Purchased (All Time)} - {# Billable Sessions})`. `Payment Status` reads this. |

**Prepaid balance chain (issue #10):** `Count Against Package?` → `Counts vs Package` (Sessions formula) → `# Billable Sessions` (Company rollup) → `Paid Sessions Remaining` → `Payment Status`. To exempt an intro/kickoff from billing, set `Count Against Package?` = Exclude on the Sessions row — nothing else needs touching.

**Known issue:** Client A, Client B, and Client C each have duplicate Contact records (same email, same Company link). Sync works (first match wins) — don't create more.

## Matching Logic

1. Extract attendee emails from calendar event (excluding SELF_EMAILS)
2. For each email, look up exact match in Contacts.Email
3. Contact must link to exactly one Company
4. That Company must be a real coaching client — i.e. it has a `Billing Model` set. Companies without one (referral sources, BD/relationship contacts filed under a non-client company) are skipped, even though the Contact→Company link is unique. This prevents phantom sessions (e.g. a coffee with a VC who referred a client).
5. If exactly one unique billable Client matches across all attendees → create/update
6. If zero or multiple Clients → skip (no create, log in no-match report)

## Client Audit / Reconciliation Tools (`tools/`)

Reusable scripts for auditing a client's Sessions records against Google Calendar and
billing history. Built for the July 2026 Crossbeam reconciliation (PR #9); to reuse for
another client, edit the constants at the top of each script (`DOMAIN` / `TARGETS` /
`SESSION_EMAILS`, and `TIME_MIN` for the relationship start).

### Audit workflow (repeatable)

```bash
# 1. Export the calendar side (rich columns incl. the exact Calendar Event ID join key)
.venv/bin/python tools/crossbeam_sessions_full.py     # → crossbeam_sessions.csv

# 2. Diff against Airtable, year by year
.venv/bin/python tools/reconcile_diff.py              # → per-year table + reconcile_diff.csv
```

`reconcile_diff.py` buckets every session:
- **matched** — `Calendar Event ID` identical on both sides (clean)
- **drift** — same event (iCalUID) + same Pacific date but different time ⇒ the meeting was
  rescheduled after sync; the Airtable key/time is stale. Fix by re-pointing the record's
  `Calendar Event ID` + `SessionTimeDate (UTC)` to the live calendar values.
- **calendar_only** — session on the calendar with no Airtable record ⇒ add candidate
  (check the `is_session` flag: the wide domain net also catches intros/scheduling noise)
- **airtable_only** — Airtable record whose key isn't on the live calendar ⇒ stale or
  duplicate; review manually before deleting (never auto-delete)

For a full billing reconciliation, also pull the client's Wave statement and compare the
Payments table invoice-by-invoice (see the Crossbeam Billing Notes on the Company record
for a worked example — the "debt" turned out to be two pre-Airtable 2021 invoices that
were never entered).

### Hard-won lessons (July 2026 Crossbeam audit)

- **`iCalUID` is only available from the Google Calendar API** (via `token.json`) — calendar
  MCP integrations don't expose it, and it **cannot be derived from the event `id`**:
  recurring instances sometimes key on the master UID, sometimes on an `_R...` instance UID
  (both exist in the same series). Never guess or reconstruct keys — always export them.
- **Legacy date-only Sessions rows** (blank `Calendar Event ID` + blank UTC time) are
  invisible to the sync's idempotency check — a long-window `--apply` will duplicate every
  one of them. Backfill their keys (by matching on date) before any historical apply.
- **Reschedules strand Airtable keys**: the key embeds the start time, so a moved event no
  longer matches its record. This surfaces as `drift`, not as a duplicate.
- **Cap calendar exports at *now*** — future recurring instances otherwise show up as
  false "missing" sessions (`crossbeam_sessions_full.py` does this; override with `END=`).
- **Tool outputs contain client emails** — all `*.csv` outputs are gitignored; never commit
  them. Ship scripts, not data.

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
