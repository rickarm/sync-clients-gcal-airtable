#!/usr/bin/env python3
"""
Google Calendar -> Airtable Sessions sync (last N weeks)

What it does:
- Fetch events from Google Calendar for the past N weeks (default 4)
- For each event:
  - Skip cancelled / all-day
  - Extract attendee emails (optionally excluding your own emails)
  - Compute Calendar Event ID = iCalUID + "_" + event_start_utc_fmt (YYYYMMDDTHHMMSSZ)
  - Find existing Sessions row by Calendar Event ID
    - If exists: PATCH missing fields (NO overwrites):
        - ALWAYS fill SessionTimeDate (UTC) if blank
        - Best-effort fill Matched Attendee Email / Matched Contact / Client if blank
    - If does not exist: CREATE a new Session ONLY if it uniquely matches exactly one Client via Contacts

Idempotent:
- Calendar Event ID is the unique key; re-running is safe.

Requirements:
  pip install google-api-python-client google-auth google-auth-oauthlib python-dateutil requests python-dotenv

Google OAuth:
- Place credentials.json next to this script (Desktop OAuth Client)
- token.json will be created on first run

Airtable:
- Uses Airtable REST API with a PAT

Usage:
  # Dry run
  python sync_last_4_weeks.py --dry-run --weeks 4 --calendar-id primary

  # Apply
  python sync_last_4_weeks.py --apply --weeks 4 --calendar-id primary

  # Diagnose no-match cases
  python sync_last_4_weeks.py --dry-run --weeks 4 --calendar-id primary --report-no-match

Env vars:
  AIRTABLE_PAT
  AIRTABLE_BASE_ID
  AIRTABLE_SESSIONS_TABLE   (default "Sessions")
  AIRTABLE_CONTACTS_TABLE   (default "Contacts")

Optional env vars:
  SELF_EMAILS="rick@example.com,other@domain.com"   # excluded from attendee matching
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

import requests
from dateutil import parser as dtparser

from dotenv import load_dotenv

from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request as GoogleRequest
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

def preflight_required_env():
    required = ["AIRTABLE_PAT", "AIRTABLE_BASE_ID"]
    missing = [k for k in required if not (os.getenv(k) or "").strip()]
    if missing:
        raise SystemExit(
            "Missing required env vars: "
            + ", ".join(missing)
            + "\nTip: create a .env file in the project root and rerun."
        )

# Force dotenv path so python-dotenv doesn't rely on stack inspection (can break on some Python versions)
load_dotenv(dotenv_path=".env")

SCOPES = ["https://www.googleapis.com/auth/calendar.readonly"]

# ----------------------------
# Configure Airtable field names
# ----------------------------
FIELD_MAP: Dict[str, Dict[str, str]] = {
    "sessions": {
        "calendar_event_id": "Calendar Event ID",
        "session_time_utc": "SessionTimeDate (UTC)",     # write ISO 8601 Z
        "matched_attendee_email": "Matched Attendee Email",
        "matched_contact_link": "Matched Contact",       # linked record (list of record IDs)
        "client_link": "Client",                         # linked record (list of record IDs)
    },
    "contacts": {
        "email": "Email",
        "client_link": "Client",                         # linked record (list of record IDs)
    },
}

DEFAULT_WEEKS = 4


# ----------------------------
# Google Calendar helpers
# ----------------------------
def load_google_service(credentials_path: str, token_path: str):
    creds: Optional[Credentials] = None
    if os.path.exists(token_path):
        creds = Credentials.from_authorized_user_file(token_path, SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(GoogleRequest())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(credentials_path, SCOPES)
            creds = flow.run_local_server(port=0)
        with open(token_path, "w") as f:
            f.write(creds.to_json())

    return build("calendar", "v3", credentials=creds)


def list_events(service, calendar_id: str, time_min: str, time_max: str) -> List[Dict[str, Any]]:
    events: List[Dict[str, Any]] = []
    page_token = None
    while True:
        resp = (
            service.events()
            .list(
                calendarId=calendar_id,
                timeMin=time_min,
                timeMax=time_max,
                singleEvents=True,
                orderBy="startTime",
                maxResults=2500,
                pageToken=page_token,
            )
            .execute()
        )
        events.extend(resp.get("items", []) or [])
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    return events


def event_start_utc_iso(event: Dict[str, Any]) -> Optional[str]:
    start = event.get("start", {})
    dt = start.get("dateTime")
    if not dt:
        return None  # all-day
    d = dtparser.isoparse(dt).astimezone(timezone.utc)
    return d.isoformat().replace("+00:00", "Z")


def utc_fmt_for_id(start_utc_iso: str) -> str:
    d = dtparser.isoparse(start_utc_iso).astimezone(timezone.utc)
    return d.strftime("%Y%m%dT%H%M%SZ")


def compute_calendar_event_id(event: Dict[str, Any]) -> Optional[str]:
    ical = event.get("iCalUID")
    start_utc = event_start_utc_iso(event)
    if not ical or not start_utc:
        return None
    return f"{ical}_{utc_fmt_for_id(start_utc)}"


def _csv_emails(env_val: str) -> List[str]:
    out = []
    for part in (env_val or "").split(","):
        p = part.strip().lower()
        if p:
            out.append(p)
    return out


def extract_attendee_emails(
    event: Dict[str, Any],
    *,
    exclude_emails: Optional[List[str]] = None,
) -> List[str]:
    """
    Returns attendee emails for an event.
    - Lowercases
    - Removes duplicates
    - Optionally excludes your own emails (SELF_EMAILS)
    """
    exclude = set((exclude_emails or []))
    attendees = event.get("attendees", []) or []

    out: List[str] = []
    seen = set()

    for a in attendees:
        e = a.get("email")
        if not e:
            continue
        e = str(e).strip().lower()
        if not e or e in exclude:
            continue
        if e in seen:
            continue
        seen.add(e)
        out.append(e)

    return out


# ----------------------------
# Airtable helpers
# ----------------------------
def airtable_headers(pat: str) -> Dict[str, str]:
    return {"Authorization": f"Bearer {pat}", "Content-Type": "application/json"}


def escape_airtable_formula_string(s: str) -> str:
    return s.replace('"', '\\"')


def airtable_list_records(
    pat: str,
    base_id: str,
    table: str,
    *,
    filter_by_formula: Optional[str] = None,
    fields: Optional[List[str]] = None,
    page_size: int = 100,
) -> List[Dict[str, Any]]:
    url = f"https://api.airtable.com/v0/{base_id}/{requests.utils.quote(table)}"
    params: Dict[str, Any] = {"pageSize": page_size}
    if filter_by_formula:
        params["filterByFormula"] = filter_by_formula

    records: List[Dict[str, Any]] = []
    offset = None

    while True:
        if fields:
            tuples = list(params.items())
            for f in fields:
                tuples.append(("fields[]", f))
            if offset:
                tuples.append(("offset", offset))
            resp = requests.get(url, headers=airtable_headers(pat), params=tuples, timeout=30)
        else:
            p = dict(params)
            if offset:
                p["offset"] = offset
            resp = requests.get(url, headers=airtable_headers(pat), params=p, timeout=30)

        if resp.status_code >= 300:
            raise RuntimeError(f"Airtable LIST failed ({resp.status_code}): {resp.text}")

        data = resp.json()
        records.extend(data.get("records", []))
        offset = data.get("offset")
        if not offset:
            break

    return records


def airtable_create_records(pat: str, base_id: str, table: str, records: List[Dict[str, Any]]) -> Dict[str, Any]:
    url = f"https://api.airtable.com/v0/{base_id}/{requests.utils.quote(table)}"
    payload = {"records": records}
    resp = requests.post(url, headers=airtable_headers(pat), json=payload, timeout=30)
    if resp.status_code >= 300:
        raise RuntimeError(f"Airtable CREATE failed ({resp.status_code}): {resp.text}")
    return resp.json()


def airtable_patch_records(pat: str, base_id: str, table: str, records: List[Dict[str, Any]]) -> Dict[str, Any]:
    url = f"https://api.airtable.com/v0/{base_id}/{requests.utils.quote(table)}"
    payload = {"records": records}
    resp = requests.patch(url, headers=airtable_headers(pat), json=payload, timeout=30)
    if resp.status_code >= 300:
        raise RuntimeError(f"Airtable PATCH failed ({resp.status_code}): {resp.text}")
    return resp.json()


# ----------------------------
# Matching logic
# ----------------------------
def find_contact_by_email_cached(
    email: str,
    cache: Dict[str, Optional[Dict[str, Any]]],
    pat: str,
    base_id: str,
    contacts_table: str,
) -> Optional[Dict[str, Any]]:
    if email in cache:
        return cache[email]

    f_email = FIELD_MAP["contacts"]["email"]
    f_client = FIELD_MAP["contacts"]["client_link"]

    formula = f'{{{f_email}}}="{escape_airtable_formula_string(email)}"'
    records = airtable_list_records(
        pat=pat,
        base_id=base_id,
        table=contacts_table,
        filter_by_formula=formula,
        fields=[f_email, f_client],
        page_size=10,
    )

    contact = records[0] if records else None
    cache[email] = contact
    return contact


def choose_unique_client_match(
    attendee_emails: List[str],
    pat: str,
    base_id: str,
    contacts_table: str,
    contact_cache: Dict[str, Optional[Dict[str, Any]]],
) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """
    Returns (matched_attendee_email, matched_contact_record_id, matched_client_record_id)
    Only returns a match if it is unambiguous (exactly one unique client).
    """
    matches: List[Tuple[str, str, str]] = []

    f_client = FIELD_MAP["contacts"]["client_link"]

    for email in attendee_emails:
        c = find_contact_by_email_cached(email, contact_cache, pat, base_id, contacts_table)
        if not c:
            continue

        client_link = c.get("fields", {}).get(f_client)
        if isinstance(client_link, list) and len(client_link) == 1:
            matches.append((email, c["id"], client_link[0]))

    # Deduplicate by client id
    by_client: Dict[str, Tuple[str, str, str]] = {}
    for m in matches:
        by_client[m[2]] = m

    unique = list(by_client.values())
    if len(unique) == 1:
        return unique[0]

    return None, None, None


# ----------------------------
# Probing helpers (debug schema without meta API)
# ----------------------------
def probe_table_fields(pat: str, base_id: str, table: str) -> List[str]:
    """Fetch 1 record and return its field keys (best-effort)."""
    recs = airtable_list_records(pat, base_id, table, page_size=1)
    if not recs:
        return []
    fields = recs[0].get("fields", {}) or {}
    return sorted(fields.keys())


# ----------------------------
# Sync
# ----------------------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--weeks", type=int, default=DEFAULT_WEEKS)
    ap.add_argument("--calendar-id", default="primary")
    ap.add_argument("--credentials", default="credentials.json")
    ap.add_argument("--token", default="token.json")
    ap.add_argument(
       "--report-create",
        action="store_true",
        help="Print details for records that would be CREATED (dry-run/apply).",
    )

    create_report_rows = []  # list of dicts for pretty printing

    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--apply", action="store_true")

    ap.add_argument("--include-no-attendees", action="store_true",
                    help="If set, events without attendees can still create Sessions (usually not desired).")
    ap.add_argument("--report-no-match", action="store_true",
                    help="Print details for events that do not resolve to a unique client match.")
    ap.add_argument("--probe-sessions-fields", action="store_true")
    ap.add_argument("--probe-contacts-fields", action="store_true")

    args = ap.parse_args()

    pat = os.getenv("AIRTABLE_PAT", "").strip()
    base_id = os.getenv("AIRTABLE_BASE_ID", "").strip()
    sessions_table = os.getenv("AIRTABLE_SESSIONS_TABLE", "Sessions").strip()
    contacts_table = os.getenv("AIRTABLE_CONTACTS_TABLE", "Contacts").strip()
    self_emails = _csv_emails(os.getenv("SELF_EMAILS", ""))

    preflight_required_env()

    if not pat or not base_id:
        print("Missing AIRTABLE_PAT or AIRTABLE_BASE_ID env vars.", file=sys.stderr)
        sys.exit(1)

    # Field aliases
    ses_ceid_field = FIELD_MAP["sessions"]["calendar_event_id"]
    ses_time_field = FIELD_MAP["sessions"]["session_time_utc"]
    ses_matched_email_field = FIELD_MAP["sessions"]["matched_attendee_email"]
    ses_matched_contact_field = FIELD_MAP["sessions"]["matched_contact_link"]
    ses_client_field = FIELD_MAP["sessions"]["client_link"]

    if args.probe_sessions_fields:
        try:
            fields = probe_table_fields(pat, base_id, sessions_table)
            print("\nSessions fields (from first record):")
            for f in fields:
                print(" -", f)
            print()
        except Exception as e:
            print(f"Probe Sessions failed: {e}", file=sys.stderr)

    if args.probe_contacts_fields:
        try:
            fields = probe_table_fields(pat, base_id, contacts_table)
            print("\nContacts fields (from first record):")
            for f in fields:
                print(" -", f)
            print()
        except Exception as e:
            print(f"Probe Contacts failed: {e}", file=sys.stderr)

    service = load_google_service(args.credentials, args.token)

    now_utc = datetime.now(timezone.utc)
    start_utc = now_utc - timedelta(weeks=args.weeks)
    time_min = start_utc.isoformat().replace("+00:00", "Z")
    time_max = now_utc.isoformat().replace("+00:00", "Z")

    events = list_events(service, args.calendar_id, time_min, time_max)

    # In-memory caches
    existing_by_ceid: Dict[str, Dict[str, Any]] = {}
    contact_cache: Dict[str, Optional[Dict[str, Any]]] = {}

    # Pending writes
    to_create: List[Dict[str, Any]] = []
    to_patch: List[Dict[str, Any]] = []

    # Counters
    created = 0
    patched = 0
    already_present = 0
    skipped = 0
    no_match = 0

    # For report mode
    no_match_details: List[Tuple[str, str, List[str]]] = []

    # Helper: lookup existing session by CEID (cached)
    def get_existing_session_by_ceid(ceid: str) -> Optional[Dict[str, Any]]:
        if ceid in existing_by_ceid:
            return existing_by_ceid[ceid]

        formula = f'{{{ses_ceid_field}}}="{escape_airtable_formula_string(ceid)}"'
        found = airtable_list_records(
            pat=pat,
            base_id=base_id,
            table=sessions_table,
            filter_by_formula=formula,
            fields=[
                ses_ceid_field,
                ses_time_field,
                ses_matched_email_field,
                ses_matched_contact_field,
                ses_client_field,
            ],
            page_size=2,
        )
        rec = found[0] if found else None
        if rec:
            existing_by_ceid[ceid] = rec
        return rec

    for ev in events:
        if ev.get("status") == "cancelled":
            continue

        start_iso = event_start_utc_iso(ev)
        if not start_iso:
            # all-day
            continue

        ceid = compute_calendar_event_id(ev)
        if not ceid:
            continue

        summary = ev.get("summary", "(no title)")
        attendee_emails = extract_attendee_emails(ev, exclude_emails=self_emails)

        if not attendee_emails and not args.include_no_attendees:
            skipped += 1
            continue

        existing = get_existing_session_by_ceid(ceid)

        # --------------------------------------------------
        # EXISTING SESSION: always patch missing UTC time,
        # and best-effort fill matched fields if blank.
        # --------------------------------------------------
        if existing:
            fields = existing.get("fields", {}) or {}
            patch_fields: Dict[str, Any] = {}

            # ALWAYS fill SessionTimeDate (UTC) if blank
            if not fields.get(ses_time_field):
                patch_fields[ses_time_field] = start_iso

            # Best-effort: try to match attendee -> contact/client
            matched_email = matched_contact_id = matched_client_id = None
            if attendee_emails:
                matched_email, matched_contact_id, matched_client_id = choose_unique_client_match(
                    attendee_emails, pat, base_id, contacts_table, contact_cache
                )

            # Fill matched fields if missing
            if matched_email and not fields.get(ses_matched_email_field):
                patch_fields[ses_matched_email_field] = matched_email

            if matched_contact_id and not fields.get(ses_matched_contact_field):
                patch_fields[ses_matched_contact_field] = [matched_contact_id]

            if matched_client_id and not fields.get(ses_client_field):
                patch_fields[ses_client_field] = [matched_client_id]

            if patch_fields:
                patched += 1
                if args.apply:
                    to_patch.append({"id": existing["id"], "fields": patch_fields})
            else:
                already_present += 1

            continue

        # --------------------------------------------------
        # NEW SESSION: require unique client match (deterministic)
        # --------------------------------------------------
        matched_email, matched_contact_id, matched_client_id = (None, None, None)
        if attendee_emails:
            matched_email, matched_contact_id, matched_client_id = choose_unique_client_match(
                attendee_emails, pat, base_id, contacts_table, contact_cache
            )

        if not matched_client_id:
            no_match += 1
            if args.report_no_match:
                no_match_details.append((start_iso, summary, attendee_emails))
            continue

        create_fields: Dict[str, Any] = {
            ses_ceid_field: ceid,
            ses_time_field: start_iso,
        }
        if matched_email:
            create_fields[ses_matched_email_field] = matched_email
        if matched_contact_id:
            create_fields[ses_matched_contact_field] = [matched_contact_id]
        if matched_client_id:
            create_fields[ses_client_field] = [matched_client_id]

        if args.report_create:
            create_report_rows.append({
                "start_utc": start_iso,
                "summary": summary,
                "ceid": ceid,
                "attendees": attendee_emails,
                "matched_email": matched_email,
                "matched_contact_id": matched_contact_id,
                "matched_client_id": matched_client_id,
            })

        created += 1
        if args.apply:
            to_create.append({"fields": create_fields})

    # Execute Airtable writes in batches of 10 (Airtable API limit)
    if args.apply:
        for i in range(0, len(to_create), 10):
            batch = to_create[i:i + 10]
            if batch:
                airtable_create_records(pat, base_id, sessions_table, batch)

        for i in range(0, len(to_patch), 10):
            batch = to_patch[i:i + 10]
            if batch:
                airtable_patch_records(pat, base_id, sessions_table, batch)

    # Report
    print("\n--- Sync Summary ---")
    print(f"Lookback weeks: {args.weeks}")
    print(f"Calendar: {args.calendar_id}")
    print(f"Events fetched: {len(events)}")
    print(f"{'Would create Sessions' if args.dry_run else 'Created Sessions'}: {created}")
    print(f"{'Would patch Sessions (filled blanks)' if args.dry_run else 'Patched Sessions (filled blanks)'}: {patched}")
    print(f"Already present (no changes): {already_present}")
    print(f"Skipped (no attendees): {skipped}")
    print(f"Not created (no unique client match): {no_match}")
    print("--------------------\n")

    if args.report_create and create_report_rows:
        print("\n--- Would-CREATE details ---")
        for row in create_report_rows[:200]:
            print(f"{row['start_utc']} | {row['summary']}")
            print(f"  ceid: {row['ceid']}")
            print(f"  attendees: {', '.join(row['attendees'])}")
            print(f"  matched_email: {row['matched_email']}")
            print(f"  matched_contact_id: {row['matched_contact_id']}")
            print(f"  matched_client_id: {row['matched_client_id']}")
        if len(create_report_rows) > 200:
            print(f"... ({len(create_report_rows) - 200} more)")
        print()

    if args.report_no_match and no_match_details:
        print("\n--- No-unique-client-match details ---")
        for start_iso, title, emails in no_match_details[:200]:
            print(f"{start_iso} | {title}")
            print("  attendees:", ", ".join(emails))
        if len(no_match_details) > 200:
            print(f"... ({len(no_match_details) - 200} more)")
        print()

        print("Tip: if you want to reduce noise, set SELF_EMAILS so your own address is excluded from attendees.")


if __name__ == "__main__":
    main()