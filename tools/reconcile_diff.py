#!/usr/bin/env python3
"""Year-by-year reconciliation of Crossbeam sessions: Google Calendar vs Airtable.

Reads crossbeam_sessions.csv (produced by tools/crossbeam_sessions_full.py) and pulls
the Airtable Sessions rows linked to Crossbeam, then buckets every session:

  matched       calendar_event_id present on both sides (clean)
  drift         same event (iCalUID) + same Pacific date, but the UTC time differs
                -> a rescheduled/retimed session; the Airtable key is stale
  calendar_only a calendar session with no Airtable record (candidate to ADD)
  airtable_only an Airtable record whose key is not on the live calendar
                (stale / deleted / duplicate -> candidate to review)

Prints a per-year table and writes reconcile_diff.csv (one row per item, with a
`bucket` column and both sides' key/time so you can drill into any year).

Read-only. Needs AIRTABLE_PAT + AIRTABLE_BASE_ID in .env (same as session_sync.py).
Run from the repo root:  .venv/bin/python tools/reconcile_diff.py
"""
import csv
import os
import re
from zoneinfo import ZoneInfo

import requests
from dateutil import parser as dtp
from dotenv import load_dotenv

load_dotenv(".env")
PT = ZoneInfo("America/Los_Angeles")
STAMP = re.compile(r"_\d{8}T\d{6}Z$")
SESSION_EMAILS = {"bob@crossbeam.com", "lbarnett@crossbeam.com"}

PAT = os.getenv("AIRTABLE_PAT", "").strip()
BASE = os.getenv("AIRTABLE_BASE_ID", "").strip()
TABLE = os.getenv("AIRTABLE_SESSIONS_TABLE", "Sessions").strip()
if not PAT or not BASE:
    raise SystemExit("Missing AIRTABLE_PAT / AIRTABLE_BASE_ID in .env")

F_CEID = "Calendar Event ID"
F_UTC = "SessionTimeDate (UTC)"
F_EMAIL = "Matched Attendee Email"
F_PTF = "SessionDateTime (PT)"   # primary formula, fallback date for legacy rows


def uid_of(ceid):
    return STAMP.sub("", ceid) if ceid else ""


def pt_date_from_utc(iso):
    return dtp.isoparse(iso).astimezone(PT).date().isoformat() if iso else ""


# ---- Airtable side: all Sessions linked to Crossbeam ----
def fetch_airtable():
    url = f"https://api.airtable.com/v0/{BASE}/{requests.utils.quote(TABLE)}"
    headers = {"Authorization": f"Bearer {PAT}"}
    params = [("filterByFormula", 'FIND("Crossbeam", ARRAYJOIN({Client}))'),
              ("fields[]", F_CEID), ("fields[]", F_UTC),
              ("fields[]", F_EMAIL), ("fields[]", F_PTF), ("pageSize", "100")]
    out, offset = [], None
    while True:
        p = list(params) + ([("offset", offset)] if offset else [])
        r = requests.get(url, headers=headers, params=p, timeout=30)
        r.raise_for_status()
        data = r.json()
        for rec in data.get("records", []):
            fx = rec.get("fields", {})
            ceid = fx.get(F_CEID, "")
            utc = fx.get(F_UTC, "")
            ptf = fx.get(F_PTF, "")
            pt_date = pt_date_from_utc(utc) if utc else (ptf[:10] if ptf else "")
            out.append({"id": rec["id"], "ceid": ceid, "utc": utc,
                        "pt_date": pt_date, "email": fx.get(F_EMAIL, "")})
        offset = data.get("offset")
        if not offset:
            break
    return out


# ---- Calendar side ----
def load_calendar():
    with open("crossbeam_sessions.csv") as f:
        rows = [r for r in csv.DictReader(f) if r["calendar_event_id"]]  # timed only
    for r in rows:
        r["year"] = r["date_pt"][:4]
        r["is_session"] = any(e in SESSION_EMAILS for e in r["crossbeam_attendees"].split(";"))
    return rows


cal = load_calendar()
at = fetch_airtable()
cal_ceids = {r["calendar_event_id"] for r in cal}
at_by_ceid = {r["ceid"]: r for r in at if r["ceid"]}
# index for drift detection: (iCalUID, pt_date) -> ceid, on each side
cal_uid_date = {(uid_of(r["calendar_event_id"]), r["date_pt"]): r["calendar_event_id"] for r in cal}
at_uid_date = {(uid_of(r["ceid"]), r["pt_date"]): r["ceid"] for r in at if r["ceid"]}

diff = []
for r in cal:
    ceid = r["calendar_event_id"]
    key = (uid_of(ceid), r["date_pt"])
    if ceid in at_by_ceid:
        bucket = "matched"
    elif key in at_uid_date:
        bucket = "drift"          # same event+date, different time -> stale Airtable key
    else:
        bucket = "calendar_only"
    diff.append({"year": r["year"], "bucket": bucket, "side": "calendar",
                 "date_pt": r["date_pt"], "start_utc": r["start_utc"],
                 "attendees": r["crossbeam_attendees"], "is_session": r["is_session"],
                 "calendar_event_id": ceid,
                 "airtable_ceid": at_uid_date.get(key, "") if bucket == "drift" else "",
                 "airtable_id": "", "summary": r["summary"]})
for r in at:
    ceid = r["ceid"]
    if ceid and ceid in cal_ceids:
        continue  # already matched from calendar side
    key = (uid_of(ceid), r["pt_date"]) if ceid else None
    if ceid and key in cal_uid_date:
        continue  # its drift partner already emitted on calendar side
    bucket = "airtable_only"
    yr = (r["pt_date"] or "")[:4]
    diff.append({"year": yr, "bucket": bucket, "side": "airtable",
                 "date_pt": r["pt_date"], "start_utc": r["utc"],
                 "attendees": r["email"], "is_session": r["email"] in SESSION_EMAILS,
                 "calendar_event_id": "", "airtable_ceid": ceid,
                 "airtable_id": r["id"], "summary": ""})

# ---- per-year table ----
years = sorted({d["year"] for d in diff if d["year"]})
buckets = ["matched", "drift", "calendar_only", "airtable_only"]
print(f"Calendar timed Crossbeam events: {len(cal)}  |  Airtable Crossbeam rows: {len(at)}\n")
hdr = f'{"year":6}' + "".join(f"{b:>15}" for b in buckets) + f'{"cal_only(session)":>20}'
print(hdr)
print("-" * len(hdr))
for y in years:
    dy = [d for d in diff if d["year"] == y]
    counts = {b: sum(1 for d in dy if d["bucket"] == b) for b in buckets}
    conly_sess = sum(1 for d in dy if d["bucket"] == "calendar_only" and d["is_session"])
    print(f"{y:6}" + "".join(f'{counts[b]:>15}' for b in buckets) + f"{conly_sess:>20}")
tot = {b: sum(1 for d in diff if d["bucket"] == b) for b in buckets}
print("-" * len(hdr))
print(f'{"ALL":6}' + "".join(f'{tot[b]:>15}' for b in buckets))
print("\nNote: calendar_only includes non-coaching Crossbeam events (intros, scheduling by")
print("other @crossbeam.com staff). The cal_only(session) column counts only those with")
print("bob@ or lbarnett@ present — those are the real add candidates.")

cols = ["year", "bucket", "side", "date_pt", "start_utc", "attendees", "is_session",
        "calendar_event_id", "airtable_ceid", "airtable_id", "summary"]
with open("reconcile_diff.csv", "w", newline="") as f:
    w = csv.DictWriter(f, fieldnames=cols)
    w.writeheader()
    w.writerows(sorted(diff, key=lambda d: (d["year"], d["bucket"], d["date_pt"])))
print(f"\nWrote reconcile_diff.csv ({len(diff)} rows).")
