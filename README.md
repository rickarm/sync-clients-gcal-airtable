# Google Calendar → Airtable Sessions Sync (Last N Weeks)

This repo contains **`sync_last_4_weeks.py`**, a safe, idempotent sync that backfills (or updates missing fields for) coaching sessions from **Google Calendar** into your **Airtable Sessions** table.

## What the script does

For events in the last **N weeks** (default **4**):

- Reads events from a Google Calendar (default `primary`).
- Skips:
  - cancelled events
  - all-day events (no `dateTime`)
  - events with no attendees (unless you opt in)
- Extracts attendee emails (optionally excluding your own emails).
- Computes a deterministic **Calendar Event ID**:

```
Calendar Event ID = iCalUID + "_" + event_start_utc_fmt
where event_start_utc_fmt = YYYYMMDDTHHMMSSZ
```

Then, for each event:

### If a matching Session already exists
Finds an existing Sessions record by **Calendar Event ID** and **only fills blanks** (never overwrites populated fields):

- Always fills **`SessionTimeDate (UTC)`** if blank
- Best-effort fills **`Matched Attendee Email`**, **`Matched Contact`**, and **`Client`** if blank (only when it can uniquely match to exactly one client)

### If no matching Session exists
Creates a new Sessions record **only if** the event can be matched **unambiguously** to **exactly one Client** via Contacts:

- Attendee email → `Contacts.Email` (exact match)
- Contact must have exactly one linked `Client`
- If multiple clients match, or none match → **no create**

## Why it’s safe to re-run

- **Idempotent key:** `Calendar Event ID`
- **No silent overwrites:** existing records are only patched where fields are blank
- **Deterministic creation:** sessions are created only when there is exactly one unambiguous match

---

## Prerequisites

### Python packages

```bash
pip install google-api-python-client google-auth google-auth-oauthlib python-dateutil requests python-dotenv
```

### Google Calendar OAuth files

Place these next to the script:

- `credentials.json` — Google OAuth Desktop Client credentials
- `token.json` — created automatically on first run

**Scopes used:** Calendar read-only

### Airtable

You’ll need:

- An Airtable **Personal Access Token (PAT)**
- Your **Base ID**
- Your table names for **Sessions** and **Contacts**

---

## Airtable schema assumptions

The script is configured to write to these field names (see `FIELD_MAP` inside the script):

### Sessions table

| Purpose | Airtable Field Name |
|---|---|
| Unique key | `Calendar Event ID` |
| Event time (UTC) | `SessionTimeDate (UTC)` |
| Matched attendee email | `Matched Attendee Email` |
| Link to Contact record | `Matched Contact` |
| Link to Client record | `Client` |

### Contacts table

| Purpose | Airtable Field Name |
|---|---|
| Email (exact match) | `Email` |
| Link to Client record | `Client` |

If your Airtable field names differ, update the `FIELD_MAP` dictionary in `sync_last_4_weeks.py`.

---

## Setup

### 1) Create a `.env` file

In the same directory as the script, create a `.env` file:

```dotenv
AIRTABLE_PAT=pat_xxxxxxxxxxxxxxxxx
AIRTABLE_BASE_ID=appxxxxxxxxxxxxxx

# Optional (defaults shown)
AIRTABLE_SESSIONS_TABLE=Sessions
AIRTABLE_CONTACTS_TABLE=Contacts

# Optional: exclude your own emails from attendee matching
SELF_EMAILS=rick@rickarmbrust.com,other@domain.com
```

### 2) Put OAuth credentials next to the script

- `credentials.json`
- (optional) `token.json` (auto-generated on first auth)

---

## Usage

### Dry run (recommended)
Shows what would be created/patched without writing to Airtable:

```bash
python sync_last_4_weeks.py --dry-run --weeks 4 --calendar-id primary
```

### Apply (writes to Airtable)

```bash
python sync_last_4_weeks.py --apply --weeks 4 --calendar-id primary
```

### Diagnose events that won’t create (no unique client match)

```bash
python sync_last_4_weeks.py --dry-run --weeks 4 --calendar-id primary --report-no-match
```

### Print details for sessions that would be created

```bash
python sync_last_4_weeks.py --dry-run --weeks 4 --calendar-id primary --report-create
```

### Include events with no attendees (not typical)

```bash
python sync_last_4_weeks.py --dry-run --weeks 4 --calendar-id primary --include-no-attendees
```

---

## Output / reporting

At the end you’ll see a summary like:

- Events fetched
- Would create / created
- Would patch / patched
- Already present (no changes)
- Skipped (no attendees)
- Not created (no unique client match)

If you pass `--report-create` or `--report-no-match`, you’ll also get up to 200 detail lines for quick debugging.

---

## Common workflows

### Backfill the last month safely

1) Dry run + see no-match cases

```bash
python sync_last_4_weeks.py --dry-run --weeks 4 --calendar-id primary --report-no-match
```

2) If no-match noise is high, set `SELF_EMAILS` in `.env` and rerun.

3) Apply

```bash
python sync_last_4_weeks.py --apply --weeks 4 --calendar-id primary
```

### Fix missing UTC times / links on existing sessions

If you already imported Sessions but some rows are missing `SessionTimeDate (UTC)` or the matched link fields, just run:

```bash
python sync_last_4_weeks.py --apply --weeks 4 --calendar-id primary
```

Because patching only fills blanks, this is safe to run repeatedly.

---

## Troubleshooting

### “Missing required env vars”
- Ensure `.env` exists next to the script and contains `AIRTABLE_PAT` and `AIRTABLE_BASE_ID`.

### Google auth problems
- Ensure `credentials.json` is present.
- Delete `token.json` to re-auth (it will be recreated).

### Airtable errors (401/403)
- Confirm your PAT has access to the base.
- Confirm the base ID is correct.

### Nothing is getting created
Most commonly:
- Attendee emails don’t match `Contacts.Email` exactly
- Contacts have 0 or >1 linked Client
- Multiple attendees map to different clients (ambiguous)

Use:

```bash
python sync_last_4_weeks.py --dry-run --weeks 4 --calendar-id primary --report-no-match
```

---

## Notes

- All timestamps written to Airtable are in **UTC** (`SessionTimeDate (UTC)`), as ISO 8601 with `Z`.
- The script writes to Airtable in batches of **10 records** (Airtable API limit).

