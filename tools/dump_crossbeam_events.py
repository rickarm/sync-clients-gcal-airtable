#!/usr/bin/env python3
"""Fast, read-only dump of Crossbeam calendar events with their iCalUID.
Reuses the repo's existing token.json (calendar.readonly). Writes crossbeam_events.csv.

Run from the repo root:  .venv/bin/python tools/dump_crossbeam_events.py
"""
import csv
from datetime import timezone
from dateutil import parser as dtp
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

SCOPES = ["https://www.googleapis.com/auth/calendar.readonly"]
creds = Credentials.from_authorized_user_file("token.json", SCOPES)
if not creds.valid and creds.expired and creds.refresh_token:
    creds.refresh(Request())
svc = build("calendar", "v3", credentials=creds)

TARGETS = {"bob@crossbeam.com", "lbarnett@crossbeam.com"}
rows, page = [], None
while True:
    resp = svc.events().list(
        calendarId="primary",
        timeMin="2021-01-01T00:00:00Z",
        timeMax="2026-07-02T00:00:00Z",
        singleEvents=True, orderBy="startTime",
        maxResults=2500, pageToken=page,
    ).execute()
    for ev in resp.get("items", []):
        if ev.get("status") == "cancelled":
            continue
        atts = {(a.get("email") or "").lower() for a in ev.get("attendees", [])}
        hit = TARGETS & atts
        if not hit:
            continue
        start = ev.get("start", {}).get("dateTime")
        if not start:            # all-day, skip
            continue
        d = dtp.isoparse(start).astimezone(timezone.utc)
        # EXACT same key session_sync.py computes: iCalUID + "_" + UTC-compact-start
        ceid = f'{ev["iCalUID"]}_{d.strftime("%Y%m%dT%H%M%SZ")}'
        rows.append((d.isoformat().replace("+00:00", "Z"),
                     ";".join(sorted(hit)), ev["iCalUID"], ceid,
                     (ev.get("summary") or "").replace("\n", " ")))
    page = resp.get("nextPageToken")
    if not page:
        break

rows.sort()
with open("crossbeam_events.csv", "w", newline="") as f:
    w = csv.writer(f)
    w.writerow(["start_utc", "attendee", "icaluid", "calendar_event_id", "summary"])
    w.writerows(rows)
print(f"Wrote {len(rows)} Crossbeam events to crossbeam_events.csv")
