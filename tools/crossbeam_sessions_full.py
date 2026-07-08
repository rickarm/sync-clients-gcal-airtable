#!/usr/bin/env python3
"""Rich, read-only export of all Crossbeam-related calendar events since 2020.

Writes crossbeam_sessions.csv with every column useful for a robust match against
the Airtable Sessions table. The key join column is `calendar_event_id`
(iCalUID + "_" + UTC-compact-start) — EXACTLY what session_sync.py stores in the
Airtable "Calendar Event ID" field. Secondary keys: start_utc / date_pt.

Run from the repo root:  .venv/bin/python tools/crossbeam_sessions_full.py
Read-only: no Airtable or Calendar writes.
"""
import csv
from datetime import timezone
from zoneinfo import ZoneInfo

from dateutil import parser as dtp
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

SCOPES = ["https://www.googleapis.com/auth/calendar.readonly"]
PT = ZoneInfo("America/Los_Angeles")
DOMAIN = "@crossbeam.com"          # "Crossbeam related" = anyone at this domain
TIME_MIN = "2020-01-01T00:00:00Z"
TIME_MAX = "2026-12-31T23:59:59Z"

creds = Credentials.from_authorized_user_file("token.json", SCOPES)
if not creds.valid and creds.expired and creds.refresh_token:
    creds.refresh(Request())
svc = build("calendar", "v3", credentials=creds)


def compact(d):  # YYYYMMDDTHHMMSSZ
    return d.strftime("%Y%m%dT%H%M%SZ")


rows, page = [], None
while True:
    resp = svc.events().list(
        calendarId="primary", timeMin=TIME_MIN, timeMax=TIME_MAX,
        singleEvents=True, orderBy="startTime", maxResults=2500,
        showDeleted=False, pageToken=page,
    ).execute()
    for ev in resp.get("items", []):
        atts = ev.get("attendees", []) or []
        emails = [(a.get("email") or "").lower() for a in atts]
        org = (ev.get("organizer", {}).get("email") or "").lower()
        creator = (ev.get("creator", {}).get("email") or "").lower()
        cb = sorted({e for e in emails + [org, creator] if e.endswith(DOMAIN)})
        if not cb:
            continue
        start = ev.get("start", {})
        s_dt = start.get("dateTime")
        if not s_dt:                      # all-day; keep but no precise key
            date_only = start.get("date", "")
            rows.append({
                "calendar_event_id": "", "icaluid": ev.get("iCalUID", ""),
                "google_event_id": ev.get("id", ""),
                "recurring_event_id": ev.get("recurringEventId", ""),
                "start_utc": "", "end_utc": "", "start_pt": date_only,
                "date_pt": date_only, "duration_min": "",
                "crossbeam_attendees": ";".join(cb),
                "all_attendee_emails": ";".join(sorted(e for e in emails if e)),
                "num_attendees": len(atts), "summary": (ev.get("summary") or "").replace("\n", " "),
                "status": ev.get("status", ""), "event_type": ev.get("eventType", ""),
                "organizer_email": org, "location": (ev.get("location") or "").replace("\n", " "),
                "created": ev.get("created", ""), "updated": ev.get("updated", ""),
            })
            continue
        sd = dtp.isoparse(s_dt).astimezone(timezone.utc)
        e_dt = ev.get("end", {}).get("dateTime")
        ed = dtp.isoparse(e_dt).astimezone(timezone.utc) if e_dt else None
        dur = int((ed - sd).total_seconds() // 60) if ed else ""
        icaluid = ev.get("iCalUID", "")
        rows.append({
            "calendar_event_id": f"{icaluid}_{compact(sd)}" if icaluid else "",
            "icaluid": icaluid,
            "google_event_id": ev.get("id", ""),
            "recurring_event_id": ev.get("recurringEventId", ""),
            "start_utc": sd.isoformat().replace("+00:00", "Z"),
            "end_utc": ed.isoformat().replace("+00:00", "Z") if ed else "",
            "start_pt": sd.astimezone(PT).isoformat(),
            "date_pt": sd.astimezone(PT).date().isoformat(),
            "duration_min": dur,
            "crossbeam_attendees": ";".join(cb),
            "all_attendee_emails": ";".join(sorted(e for e in emails if e)),
            "num_attendees": len(atts),
            "summary": (ev.get("summary") or "").replace("\n", " "),
            "status": ev.get("status", ""),
            "event_type": ev.get("eventType", ""),
            "organizer_email": org,
            "location": (ev.get("location") or "").replace("\n", " "),
            "created": ev.get("created", ""),
            "updated": ev.get("updated", ""),
        })
    page = resp.get("nextPageToken")
    if not page:
        break

rows.sort(key=lambda r: (r["start_utc"] or r["date_pt"]))
cols = ["calendar_event_id", "icaluid", "google_event_id", "recurring_event_id",
        "start_utc", "end_utc", "start_pt", "date_pt", "duration_min",
        "crossbeam_attendees", "all_attendee_emails", "num_attendees",
        "summary", "status", "event_type", "organizer_email", "location", "created", "updated"]
with open("crossbeam_sessions.csv", "w", newline="") as f:
    w = csv.DictWriter(f, fieldnames=cols)
    w.writeheader()
    w.writerows(rows)
print(f"Wrote {len(rows)} Crossbeam-related events to crossbeam_sessions.csv "
      f"({sum(1 for r in rows if r['calendar_event_id'])} timed, "
      f"{sum(1 for r in rows if not r['calendar_event_id'])} all-day/no-key)")
