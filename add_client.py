#!/usr/bin/env python3
"""
add_client.py — Onboard a new coaching client into Airtable + known_clients.json

Creates (or reuses) a Company record, creates the Contact record linked to it,
and adds the client to known_clients.json so session_sync picks them up automatically.

Usage:
  python add_client.py --name "Alice Smith" --email alice@company.com --company "Acme Corp"
  python add_client.py --name "Alice Smith" --email alice@company.com --company "Acme Corp" --rate 1000 --date-signed 2026-05-15
  python add_client.py --name "Alice Smith" --email alice@company.com --company "Acme Corp" --dry-run
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv(dotenv_path=".env")

AIRTABLE_BASE = os.getenv("AIRTABLE_BASE_ID", "").strip()
AIRTABLE_PAT = os.getenv("AIRTABLE_PAT", "").strip()
KNOWN_CLIENTS_FILE = os.getenv("KNOWN_CLIENTS_FILE", "known_clients.json")

COMPANY_TABLE = "Company"
CONTACTS_TABLE = "Contacts"


def headers() -> dict:
    return {"Authorization": f"Bearer {AIRTABLE_PAT}", "Content-Type": "application/json"}


def at_get(table: str, record_id: str) -> dict:
    url = f"https://api.airtable.com/v0/{AIRTABLE_BASE}/{requests.utils.quote(table)}/{record_id}"
    r = requests.get(url, headers=headers(), timeout=30)
    r.raise_for_status()
    return r.json()


def at_search(table: str, formula: str, fields: list[str] | None = None) -> list[dict]:
    url = f"https://api.airtable.com/v0/{AIRTABLE_BASE}/{requests.utils.quote(table)}"
    params: list = [("filterByFormula", formula), ("pageSize", "10")]
    if fields:
        for f in fields:
            params.append(("fields[]", f))
    r = requests.get(url, headers=headers(), params=params, timeout=30)
    r.raise_for_status()
    return r.json().get("records", [])


def at_create(table: str, fields: dict) -> dict:
    url = f"https://api.airtable.com/v0/{AIRTABLE_BASE}/{requests.utils.quote(table)}"
    r = requests.post(url, headers=headers(), json={"fields": fields}, timeout=30)
    if r.status_code >= 300:
        raise RuntimeError(f"Airtable CREATE failed ({r.status_code}): {r.text}")
    return r.json()


def find_company(name: str) -> dict | None:
    """Find existing Company record by name (case-insensitive)."""
    escaped = name.replace('"', '\\"')
    records = at_search(COMPANY_TABLE, f'LOWER({{Client}})=LOWER("{escaped}")', ["Client", "Rate (per session)"])
    return records[0] if records else None


def create_company(name: str, rate: int, date_signed: str, domain: str) -> dict:
    fields: dict = {
        "Client": name,
        "Rate (per session)": rate,
        "Status-Company": "Active",
        "Date signed": date_signed,
        "Billing Model": "Retainer",
    }
    if domain:
        fields["Domain"] = domain
    return at_create(COMPANY_TABLE, fields)


def find_contact(email: str) -> dict | None:
    """Find existing Contact record by email."""
    escaped = email.replace('"', '\\"')
    records = at_search(CONTACTS_TABLE, f'{{Email}}="{escaped}"', ["Email", "Name", "Company"])
    return records[0] if records else None


def create_contact(name: str, email: str, company_record_id: str) -> dict:
    fields = {
        "Name": name,
        "Email": email,
        "Company": [company_record_id],
        "Status Client": "Active-coaching",
    }
    return at_create(CONTACTS_TABLE, fields)


def update_known_clients(email: str, name: str, company_record_id: str, dry_run: bool) -> bool:
    """Add entry to known_clients.json if not already present. Returns True if added."""
    p = Path(KNOWN_CLIENTS_FILE)
    entries: list[dict] = []
    if p.exists():
        with p.open() as f:
            entries = json.load(f)

    existing_emails = {e["email"].strip().lower() for e in entries}
    if email.lower() in existing_emails:
        return False  # already present

    new_entry = {
        "email": email.lower(),
        "name": name,
        "company_record_id": company_record_id,
    }
    entries.append(new_entry)

    if not dry_run:
        with p.open("w") as f:
            json.dump(entries, f, indent=2)
        f.write("\n") if False else None  # avoid trailing newline issue

    return True


def main() -> None:
    ap = argparse.ArgumentParser(description="Onboard a new coaching client into Airtable + known_clients.json")
    ap.add_argument("--name", required=True, help="Client full name (e.g. 'Alice Smith')")
    ap.add_argument("--email", required=True, help="Client work email")
    ap.add_argument("--company", required=True, help="Company name (must match or will be created)")
    ap.add_argument("--rate", type=int, default=1000, help="Session rate in USD (default: 1000)")
    ap.add_argument("--date-signed", default=str(date.today()), help="Engagement start date YYYY-MM-DD (default: today)")
    ap.add_argument("--domain", default="", help="Company website URL (optional)")
    ap.add_argument("--dry-run", action="store_true", help="Show what would happen without writing anything")
    args = ap.parse_args()

    if not AIRTABLE_PAT or not AIRTABLE_BASE:
        sys.exit("Missing AIRTABLE_PAT or AIRTABLE_BASE_ID in .env")

    dry = args.dry_run
    tag = "[DRY RUN] " if dry else ""

    print(f"\n{tag}Onboarding: {args.name} <{args.email}> @ {args.company}")
    print("=" * 60)

    # --- Company ---
    existing_company = find_company(args.company)
    if existing_company:
        company_id = existing_company["id"]
        print(f"  Company:  found existing '{args.company}' → {company_id}")
    else:
        if dry:
            company_id = "dry-run-company-id"
            print(f"  Company:  would CREATE '{args.company}' (rate=${args.rate}/session, signed {args.date_signed})")
        else:
            rec = create_company(args.company, args.rate, args.date_signed, args.domain)
            company_id = rec["id"]
            print(f"  Company:  CREATED '{args.company}' → {company_id}")

    # --- Contact ---
    existing_contact = find_contact(args.email)
    if existing_contact:
        print(f"  Contact:  found existing {args.email} → {existing_contact['id']}")
    else:
        if dry:
            print(f"  Contact:  would CREATE {args.name} <{args.email}> linked to company")
        else:
            rec = create_contact(args.name, args.email, company_id)
            print(f"  Contact:  CREATED {args.name} <{args.email}> → {rec['id']}")

    # --- known_clients.json ---
    added = update_known_clients(args.email, args.name, company_id, dry)
    if added:
        if dry:
            print(f"  known_clients.json: would ADD {args.email}")
        else:
            print(f"  known_clients.json: ADDED {args.email}")
    else:
        print(f"  known_clients.json: already has {args.email}, skipped")

    print()
    if dry:
        print("Dry run complete. Run without --dry-run to apply.")
    else:
        print("Done. New client is ready for session sync.")


if __name__ == "__main__":
    main()
