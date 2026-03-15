#!/usr/bin/env python3
"""
session_sync.py — Google Calendar → Airtable Sessions sync

Syncs coaching sessions from Google Calendar into Airtable.
Airtable is the system of record. This script is the system of transformation.

Principles:
- Idempotent: Calendar Event ID is the unique key; re-running is always safe
- No silent overwrites: existing records are only patched where fields are blank
- Deterministic creation: Sessions created only with exactly one unambiguous client match
- No guessing: attendee email → Contacts.Email (exact match) → linked Client

Usage:
  # Dry run (safe, no writes)
  python session_sync.py --dry-run --weeks 4

  # Apply (writes to Airtable)
  python session_sync.py --apply --weeks 4

  # Diagnose no-match cases
  python session_sync.py --dry-run --weeks 4 --report-no-match

  # See what would be created
  python session_sync.py --dry-run --weeks 4 --report-create

  # Backfill longer window
  python session_sync.py --apply --weeks 12

Required env vars (in .env):
  AIRTABLE_PAT
  AIRTABLE_BASE_ID

Optional env vars:
  AIRTABLE_SESSIONS_TABLE   (default: "Sessions")
  AIRTABLE_CONTACTS_TABLE   (default: "Contacts")
  SELF_EMAILS               (comma-separated, excluded from attendee matching)
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

import requests
from dateutil import parser as dtparser
from dotenv import load_dotenv
from google.auth.transport.requests import Request as GoogleRequest
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

load_dotenv(dotenv_path=".env")

SCOPES = ["https://www.googleapis.com/auth/calendar.readonly"]

DEFAULT_WEEKS = 4

FIELD_MAP: Dict[str, Dict[str, str]] = {
    "sessions": {
        "calendar_event_id": "Calendar Event ID",
        "session_time_utc": "SessionTimeDate (UTC)",
        "matched_attendee_email": "Matched Attendee Email",
        "matched_contact_link": "Matched Contact",
        "client_link": "Client",
    },
    "contacts": {
        "email": "Email",
        "client_link": "Company",
    },
}


# ---------------------------------------------------------------------------
# Preflight
# ---------------------------------------------------------------------------

def preflight_required_env() -> None:
    required = ["AIRTABLE_PAT", "AIRTABLE_BASE_ID"]
    missing = [k for k in required if not (os.getenv(k) or "").strip()]
    if missing:
        raise SystemExit(
            "Missing required env vars: "
            + ", ".join(missing)
            + "\nTip: create a .env file in the project root and rerun.\n"
            + "Run `make doctor` to diagnose."
        )


# ---------------------------------------------------------------------------
# Google Calendar
# ---------------------------------------------------------------------------

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


def list_calendar_events(
    service, calendar_id: str, time_min: str, time_max: str
) -> List[Dict[str, Any]]:
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
    """Returns ISO 8601 UTC string, or None if all-day."""
    start = event.get("start", {})
    dt = start.get("dateTime")
    if not dt:
        return None  # all-day event
    d = dtparser.isoparse(dt).astimezone(timezone.utc)
    return d.isoformat().replace("+00:00", "Z")


def utc_compact(start_utc_iso: str) -> str:
    """Returns YYYYMMDDTHHMMSSZ format for use in Calendar Event ID."""
    d = dtparser.isoparse(start_utc_iso).astimezone(timezone.utc)
    return d.strftime("%Y%m%dT%H%M%SZ")


def compute_calendar_event_id(event: Dict[str, Any]) -> Optional[str]:
    """iCalUID + '_' + event_start_utc_compact — the unique key for a session."""
    ical = event.get("iCalUID")
    start_utc = event_start_utc_iso(event)
    if not ical or not start_utc:
        return None
    return f"{ical}_{utc_compact(start_utc)}"


def parse_self_emails(env_val: str) -> List[str]:
    return [p.strip().lower() for p in (env_val or "").split(",") if p.strip()]


def extract_attendee_emails(
    event: Dict[str, Any],
    *,
    exclude_emails: Optional[List[str]] = None,
) -> List[str]:
    """Returns deduplicated, lowercased attendee emails, excluding self."""
    exclude = set(exclude_emails or [])
    out: List[str] = []
    seen: set = set()
    for a in (event.get("attendees") or []):
        e = str(a.get("email") or "").strip().lower()
        if not e or e in exclude or e in seen:
            continue
        seen.add(e)
        out.append(e)
    return out


# ---------------------------------------------------------------------------
# Airtable
# ---------------------------------------------------------------------------

def airtable_headers(pat: str) -> Dict[str, str]:
    return {"Authorization": f"Bearer {pat}", "Content-Type": "application/json"}


def escape_formula_string(s: str) -> str:
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


def airtable_create_records(
    pat: str, base_id: str, table: str, records: List[Dict[str, Any]]
) -> Dict[str, Any]:
    url = f"https://api.airtable.com/v0/{base_id}/{requests.utils.quote(table)}"
    resp = requests.post(url, headers=airtable_headers(pat), json={"records": records}, timeout=30)
    if resp.status_code >= 300:
        raise RuntimeError(f"Airtable CREATE failed ({resp.status_code}): {resp.text}")
    return resp.json()


def airtable_patch_records(
    pat: str, base_id: str, table: str, records: List[Dict[str, Any]]
) -> Dict[str, Any]:
    url = f"https://api.airtable.com/v0/{base_id}/{requests.utils.quote(table)}"
    resp = requests.patch(url, headers=airtable_headers(pat), json={"records": records}, timeout=30)
    if resp.status_code >= 300:
        raise RuntimeError(f"Airtable PATCH failed ({resp.status_code}): {resp.text}")
    return resp.json()


# ---------------------------------------------------------------------------
# Matching
# ---------------------------------------------------------------------------

def find_contact_by_email(
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
    formula = f'{{{f_email}}}="{escape_formula_string(email)}"'

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


def resolve_unique_client(
    attendee_emails: List[str],
    pat: str,
    base_id: str,
    contacts_table: str,
    contact_cache: Dict[str, Optional[Dict[str, Any]]],
) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """
    Returns (matched_email, contact_record_id, client_record_id).
    Only returns a result when exactly one unique client matches.
    """
    f_client = FIELD_MAP["contacts"]["client_link"]
    matches: List[Tuple[str, str, str]] = []

    for email in attendee_emails:
        contact = find_contact_by_email(email, contact_cache, pat, base_id, contacts_table)
        if not contact:
            continue
        client_link = contact.get("fields", {}).get(f_client)
        if isinstance(client_link, list) and len(client_link) == 1:
            matches.append((email, contact["id"], client_link[0]))

    # Deduplicate by client ID — only succeed if exactly one unique client
    by_client: Dict[str, Tuple[str, str, str]] = {m[2]: m for m in matches}
    unique = list(by_client.values())

    if len(unique) == 1:
        return unique[0]
    return None, None, None


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser(
        description="Sync Google Calendar coaching sessions into Airtable."
    )
    ap.add_argument("--weeks", type=int, default=DEFAULT_WEEKS, help="Lookback window in weeks")
    ap.add_argument("--calendar-id", default="primary")
    ap.add_argument("--credentials", default="credentials.json")
    ap.add_argument("--token", default="token.json")
    ap.add_argument(
        "--include-no-attendees",
        action="store_true",
        help="Allow session creation for events without attendees (unusual).",
    )
    ap.add_argument(
        "--report-no-match",
        action="store_true",
        help="Print events that could not be matched to a unique client.",
    )
    ap.add_argument(
        "--report-create",
        action="store_true",
        help="Print details for sessions that would be / were created.",
    )

    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--apply", action="store_true")

    args = ap.parse_args()

    preflight_required_env()

    pat = os.getenv("AIRTABLE_PAT", "").strip()
    base_id = os.getenv("AIRTABLE_BASE_ID", "").strip()
    sessions_table = os.getenv("AIRTABLE_SESSIONS_TABLE", "Sessions").strip()
    contacts_table = os.getenv("AIRTABLE_CONTACTS_TABLE", "Contacts").strip()
    self_emails = parse_self_emails(os.getenv("SELF_EMAILS", ""))

    # Field name aliases
    f_ceid = FIELD_MAP["sessions"]["calendar_event_id"]
    f_time = FIELD_MAP["sessions"]["session_time_utc"]
    f_email = FIELD_MAP["sessions"]["matched_attendee_email"]
    f_contact = FIELD_MAP["sessions"]["matched_contact_link"]
    f_client = FIELD_MAP["sessions"]["client_link"]

    # Google Calendar
    service = load_google_service(args.credentials, args.token)
    now_utc = datetime.now(timezone.utc)
    start_utc = now_utc - timedelta(weeks=args.weeks)
    time_min = start_utc.isoformat().replace("+00:00", "Z")
    time_max = now_utc.isoformat().replace("+00:00", "Z")

    print(f"\nFetching calendar events ({args.weeks} weeks)...")
    events = list_calendar_events(service, args.calendar_id, time_min, time_max)
    print(f"Found {len(events)} events.")

    # In-memory caches
    existing_session_cache: Dict[str, Dict[str, Any]] = {}
    contact_cache: Dict[str, Optional[Dict[str, Any]]] = {}

    # Pending writes
    to_create: List[Dict[str, Any]] = []
    to_patch: List[Dict[str, Any]] = []

    # Counters
    n_created = 0
    n_patched = 0
    n_already_present = 0
    n_skipped = 0
    n_no_match = 0

    # Report details
    create_details: List[Dict] = []
    no_match_details: List[Tuple[str, str, List[str]]] = []

    def get_existing_session(ceid: str) -> Optional[Dict[str, Any]]:
        if ceid in existing_session_cache:
            return existing_session_cache[ceid]
        formula = f'{{{f_ceid}}}="{escape_formula_string(ceid)}"'
        found = airtable_list_records(
            pat=pat,
            base_id=base_id,
            table=sessions_table,
            filter_by_formula=formula,
            fields=[f_ceid, f_time, f_email, f_contact, f_client],
            page_size=2,
        )
        rec = found[0] if found else None
        if rec:
            existing_session_cache[ceid] = rec
        return rec

    for ev in events:
        if ev.get("status") == "cancelled":
            continue

        start_iso = event_start_utc_iso(ev)
        if not start_iso:
            continue  # all-day

        ceid = compute_calendar_event_id(ev)
        if not ceid:
            continue

        summary = ev.get("summary", "(no title)")
        attendees = extract_attendee_emails(ev, exclude_emails=self_emails)

        if not attendees and not args.include_no_attendees:
            n_skipped += 1
            continue

        existing = get_existing_session(ceid)

        # ------------------------------------------------------------------
        # EXISTING SESSION: fill blanks only, never overwrite
        # ------------------------------------------------------------------
        if existing:
            fields = existing.get("fields", {}) or {}
            patch: Dict[str, Any] = {}

            if not fields.get(f_time):
                patch[f_time] = start_iso

            matched_email = matched_contact_id = matched_client_id = None
            if attendees:
                matched_email, matched_contact_id, matched_client_id = resolve_unique_client(
                    attendees, pat, base_id, contacts_table, contact_cache
                )

            if matched_email and not fields.get(f_email):
                patch[f_email] = matched_email
            if matched_contact_id and not fields.get(f_contact):
                patch[f_contact] = [matched_contact_id]
            if matched_client_id and not fields.get(f_client):
                patch[f_client] = [matched_client_id]

            if patch:
                n_patched += 1
                if args.apply:
                    to_patch.append({"id": existing["id"], "fields": patch})
            else:
                n_already_present += 1
            continue

        # ------------------------------------------------------------------
        # NEW SESSION: require unambiguous client match
        # ------------------------------------------------------------------
        matched_email, matched_contact_id, matched_client_id = None, None, None
        if attendees:
            matched_email, matched_contact_id, matched_client_id = resolve_unique_client(
                attendees, pat, base_id, contacts_table, contact_cache
            )

        if not matched_client_id:
            n_no_match += 1
            if args.report_no_match:
                no_match_details.append((start_iso, summary, attendees))
            continue

        create_fields: Dict[str, Any] = {f_ceid: ceid, f_time: start_iso}
        if matched_email:
            create_fields[f_email] = matched_email
        if matched_contact_id:
            create_fields[f_contact] = [matched_contact_id]
        if matched_client_id:
            create_fields[f_client] = [matched_client_id]

        n_created += 1
        if args.apply:
            to_create.append({"fields": create_fields})

        if args.report_create:
            create_details.append({
                "start_utc": start_iso,
                "summary": summary,
                "ceid": ceid,
                "attendees": attendees,
                "matched_email": matched_email,
                "matched_contact_id": matched_contact_id,
                "matched_client_id": matched_client_id,
            })

    # Execute writes in batches of 10 (Airtable API limit)
    if args.apply:
        for i in range(0, len(to_create), 10):
            airtable_create_records(pat, base_id, sessions_table, to_create[i:i + 10])
        for i in range(0, len(to_patch), 10):
            airtable_patch_records(pat, base_id, sessions_table, to_patch[i:i + 10])

    # Summary
    mode_label = "DRY RUN" if args.dry_run else "APPLIED"
    print(f"\n--- Sync Summary [{mode_label}] ---")
    print(f"Lookback:              {args.weeks} weeks")
    print(f"Calendar:              {args.calendar_id}")
    print(f"Events fetched:        {len(events)}")
    print(f"Sessions created:      {n_created}")
    print(f"Sessions patched:      {n_patched}  (blanks filled)")
    print(f"Already complete:      {n_already_present}  (no changes)")
    print(f"Skipped (no attendees):{n_skipped}")
    print(f"No unique match:       {n_no_match}  (not created)")
    print("-----------------------------------\n")

    if args.report_create and create_details:
        print("--- Sessions created / would create ---")
        for row in create_details[:200]:
            print(f"  {row['start_utc']} | {row['summary']}")
            print(f"    ceid:    {row['ceid']}")
            print(f"    email:   {row['matched_email']}")
            print(f"    contact: {row['matched_contact_id']}")
            print(f"    client:  {row['matched_client_id']}")
        if len(create_details) > 200:
            print(f"  ... ({len(create_details) - 200} more)")
        print()

    if args.report_no_match and no_match_details:
        print("--- Events with no unique client match ---")
        for start_iso, title, emails in no_match_details[:200]:
            print(f"  {start_iso} | {title}")
            print(f"    attendees: {', '.join(emails)}")
        if len(no_match_details) > 200:
            print(f"  ... ({len(no_match_details) - 200} more)")
        print()
        print("Tip: set SELF_EMAILS in .env to exclude your own addresses from matching.")


if __name__ == "__main__":
    main()
