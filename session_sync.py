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
- Auto-onboard: if an attendee email is in known_clients.json but not yet in Airtable
  Contacts, the Contact record is created automatically before the session is filed.

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
  KNOWN_CLIENTS_FILE        (default: "known_clients.json")
"""

from __future__ import annotations

import argparse
import json
import logging
import os
from datetime import datetime, timedelta, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
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
        "name": "Name",
        "client_link": "Company",
        "status": "Status Client",
    },
}


# ---------------------------------------------------------------------------
# Known clients
# ---------------------------------------------------------------------------

def load_known_clients(path: str) -> Dict[str, Dict[str, Any]]:
    """
    Load known_clients.json, keyed by lowercase email.

    File format (array of objects):
      [
        {"email": "alice@co.com", "name": "Alice Smith", "company_record_id": "recXXX"},
        ...
      ]

    Returns {} if the file doesn't exist or is empty.
    """
    p = Path(path)
    if not p.exists():
        return {}
    with p.open() as f:
        entries = json.load(f)
    return {e["email"].strip().lower(): e for e in entries if e.get("email")}


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def setup_logging(verbose: bool = False) -> logging.Logger:
    """Configure file + console logging. File always gets DEBUG; console gets INFO (or DEBUG if verbose)."""
    log_dir = Path(__file__).parent / "logs"
    log_dir.mkdir(exist_ok=True)
    log_path = log_dir / "session_sync.log"

    logger = logging.getLogger("session_sync")
    logger.setLevel(logging.DEBUG)

    # File handler — rotating, 5 MB x 5 backups, always DEBUG
    fh = RotatingFileHandler(log_path, maxBytes=5_000_000, backupCount=5)
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(logging.Formatter(
        "%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    ))

    # Console handler — INFO by default, DEBUG if --verbose
    ch = logging.StreamHandler()
    ch.setLevel(logging.DEBUG if verbose else logging.INFO)
    ch.setFormatter(logging.Formatter("%(message)s"))

    logger.addHandler(fh)
    logger.addHandler(ch)
    return logger


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


def airtable_create_contact(
    email: str,
    name: str,
    company_record_id: str,
    pat: str,
    base_id: str,
    contacts_table: str,
    *,
    dry_run: bool = False,
    logger: Optional[logging.Logger] = None,
) -> Optional[Dict[str, Any]]:
    """
    Create a new Contact record in Airtable for a known client.
    In dry-run mode, logs the intent and returns a synthetic record so the
    rest of the sync can proceed as if the contact exists.
    """
    f_email = FIELD_MAP["contacts"]["email"]
    f_name = FIELD_MAP["contacts"]["name"]
    f_company = FIELD_MAP["contacts"]["client_link"]
    f_status = FIELD_MAP["contacts"]["status"]

    fields = {
        f_email: email,
        f_name: name,
        f_company: [company_record_id],
        f_status: "Active-coaching",
    }

    if dry_run:
        if logger:
            logger.info(f"[DRY RUN] Would auto-create Contact: {name} <{email}>")
        # Return a synthetic record so matching continues normally
        return {
            "id": f"dry-run-contact-{email}",
            "fields": {f_email: email, f_name: name, f_company: [company_record_id]},
        }

    url = f"https://api.airtable.com/v0/{base_id}/{requests.utils.quote(contacts_table)}"
    resp = requests.post(
        url, headers=airtable_headers(pat), json={"fields": fields}, timeout=30
    )
    if resp.status_code >= 300:
        if logger:
            logger.error(f"Failed to auto-create Contact for {email}: {resp.text}")
        return None

    record = resp.json()
    if logger:
        logger.info(f"Auto-created Contact: {name} <{email}> → {record['id']}")
    return record


# ---------------------------------------------------------------------------
# Matching
# ---------------------------------------------------------------------------

def find_contact_by_email(
    email: str,
    cache: Dict[str, Optional[Dict[str, Any]]],
    pat: str,
    base_id: str,
    contacts_table: str,
    *,
    known_clients: Optional[Dict[str, Dict[str, Any]]] = None,
    dry_run: bool = False,
    logger: Optional[logging.Logger] = None,
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

    # Auto-create from known_clients.json if not found in Airtable
    if contact is None and known_clients:
        known = known_clients.get(email.lower())
        if known:
            contact = airtable_create_contact(
                email=email,
                name=known["name"],
                company_record_id=known["company_record_id"],
                pat=pat,
                base_id=base_id,
                contacts_table=contacts_table,
                dry_run=dry_run,
                logger=logger,
            )

    cache[email] = contact
    return contact


def resolve_unique_client(
    attendee_emails: List[str],
    pat: str,
    base_id: str,
    contacts_table: str,
    contact_cache: Dict[str, Optional[Dict[str, Any]]],
    *,
    known_clients: Optional[Dict[str, Dict[str, Any]]] = None,
    dry_run: bool = False,
    logger: Optional[logging.Logger] = None,
) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """
    Returns (matched_email, contact_record_id, client_record_id).
    Only returns a result when exactly one unique client matches.
    """
    f_client = FIELD_MAP["contacts"]["client_link"]
    matches: List[Tuple[str, str, str]] = []

    for email in attendee_emails:
        contact = find_contact_by_email(
            email,
            contact_cache,
            pat,
            base_id,
            contacts_table,
            known_clients=known_clients,
            dry_run=dry_run,
            logger=logger,
        )
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

    ap.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Show DEBUG-level output on console (always written to log file).",
    )

    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--apply", action="store_true")

    args = ap.parse_args()

    logger = setup_logging(args.verbose)

    preflight_required_env()

    pat = os.getenv("AIRTABLE_PAT", "").strip()
    base_id = os.getenv("AIRTABLE_BASE_ID", "").strip()
    sessions_table = os.getenv("AIRTABLE_SESSIONS_TABLE", "Sessions").strip()
    contacts_table = os.getenv("AIRTABLE_CONTACTS_TABLE", "Contacts").strip()
    self_emails = parse_self_emails(os.getenv("SELF_EMAILS", ""))
    known_clients_file = os.getenv("KNOWN_CLIENTS_FILE", "known_clients.json")

    known_clients = load_known_clients(known_clients_file)
    if known_clients:
        logger.info(f"Loaded {len(known_clients)} known client(s) from {known_clients_file}")

    dry_run = args.dry_run

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

    mode_label = "APPLY" if args.apply else "DRY RUN"
    logger.info(f"=== Sync started | mode={mode_label} | weeks={args.weeks} | calendar={args.calendar_id} ===")
    logger.info(f"Fetching calendar events ({args.weeks} weeks)...")
    events = list_calendar_events(service, args.calendar_id, time_min, time_max)
    logger.info(f"Found {len(events)} events.")

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
            logger.debug(f"Skip cancelled: \"{ev.get('summary', '(no title)')}\"")
            continue

        start_iso = event_start_utc_iso(ev)
        if not start_iso:
            logger.debug(f"Skip all-day: \"{ev.get('summary', '(no title)')}\"")
            continue  # all-day

        ceid = compute_calendar_event_id(ev)
        if not ceid:
            logger.debug(f"Skip (no ceid): \"{ev.get('summary', '(no title)')}\"")
            continue

        summary = ev.get("summary", "(no title)")
        attendees = extract_attendee_emails(ev, exclude_emails=self_emails)

        if not attendees and not args.include_no_attendees:
            n_skipped += 1
            logger.debug(f"Skip no-attendees: \"{summary}\" ({start_iso})")
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
                    attendees, pat, base_id, contacts_table, contact_cache,
                    known_clients=known_clients, dry_run=dry_run, logger=logger,
                )

            if matched_email and not fields.get(f_email):
                patch[f_email] = matched_email
            if matched_contact_id and not fields.get(f_contact):
                patch[f_contact] = [matched_contact_id]
            if matched_client_id and not fields.get(f_client):
                patch[f_client] = [matched_client_id]

            if patch:
                n_patched += 1
                logger.debug(f"Patch \"{summary}\" ({start_iso}) | fields={list(patch.keys())}")
                if args.apply:
                    to_patch.append({"id": existing["id"], "fields": patch})
            else:
                n_already_present += 1
                logger.debug(f"Complete \"{summary}\" ({start_iso}) | no changes")
            continue

        # ------------------------------------------------------------------
        # NEW SESSION: require unambiguous client match
        # ------------------------------------------------------------------
        matched_email, matched_contact_id, matched_client_id = None, None, None
        if attendees:
            matched_email, matched_contact_id, matched_client_id = resolve_unique_client(
                attendees, pat, base_id, contacts_table, contact_cache,
                known_clients=known_clients, dry_run=dry_run, logger=logger,
            )

        if not matched_client_id:
            n_no_match += 1
            logger.warning(f"No unique match: \"{summary}\" ({start_iso}) | attendees={', '.join(attendees)}")
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
        logger.info(f"{'Create' if args.apply else 'Would create'}: \"{summary}\" ({start_iso}) | client={matched_client_id} | email={matched_email}")
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
        logger.info(f"Writing to Airtable: {len(to_create)} creates, {len(to_patch)} patches...")
        for i in range(0, len(to_create), 10):
            airtable_create_records(pat, base_id, sessions_table, to_create[i:i + 10])
        for i in range(0, len(to_patch), 10):
            airtable_patch_records(pat, base_id, sessions_table, to_patch[i:i + 10])

    # Summary
    summary_mode = "DRY RUN" if args.dry_run else "APPLIED"
    logger.info(f"\n--- Sync Summary [{summary_mode}] ---")
    logger.info(f"Lookback:              {args.weeks} weeks")
    logger.info(f"Calendar:              {args.calendar_id}")
    logger.info(f"Events fetched:        {len(events)}")
    logger.info(f"Sessions created:      {n_created}")
    logger.info(f"Sessions patched:      {n_patched}  (blanks filled)")
    logger.info(f"Already complete:      {n_already_present}  (no changes)")
    logger.info(f"Skipped (no attendees):{n_skipped}")
    logger.info(f"No unique match:       {n_no_match}  (not created)")
    logger.info("-----------------------------------")
    logger.info("=== Sync complete ===")

    if args.report_create and create_details:
        logger.info("--- Sessions created / would create ---")
        for row in create_details[:200]:
            logger.info(f"  {row['start_utc']} | {row['summary']}")
            logger.info(f"    ceid:    {row['ceid']}")
            logger.info(f"    email:   {row['matched_email']}")
            logger.info(f"    contact: {row['matched_contact_id']}")
            logger.info(f"    client:  {row['matched_client_id']}")
        if len(create_details) > 200:
            logger.info(f"  ... ({len(create_details) - 200} more)")

    if args.report_no_match and no_match_details:
        logger.info("--- Events with no unique client match ---")
        for start_iso, title, emails in no_match_details[:200]:
            logger.info(f"  {start_iso} | {title}")
            logger.info(f"    attendees: {', '.join(emails)}")
        if len(no_match_details) > 200:
            logger.info(f"  ... ({len(no_match_details) - 200} more)")
        logger.info("Tip: set SELF_EMAILS in .env to exclude your own addresses from matching.")


if __name__ == "__main__":
    main()
