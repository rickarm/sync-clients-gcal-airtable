"""Link KB session notes and their Drive transcripts to Airtable Sessions rows.

Idempotent and fill-blank-only: a non-blank field with a different value is reported as a conflict and left alone.
Never creates Sessions rows (the calendar sync owns them). See CLAUDE.md "KB session links".
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

F_TIME = "SessionTimeDate (UTC)"
F_NAME = "Name (from Matched Contact)"
F_KB = "KB File Path"
F_FOLDER = "Drive Folder"
F_TRANSCRIPT = "Transcript"
F_SYNCED = "Last Synced to KB At"

PACIFIC = ZoneInfo("America/Los_Angeles")
NOTE_RE = re.compile(r"^([A-Za-z]+)-(\d{4}-\d{2}-\d{2})\.md$")
FOLDER_RE = re.compile(r"^\*\*Recording:\*\*.*drive\.google\.com/drive/folders/([A-Za-z0-9_-]+)", re.M)
CLIENT_RE = re.compile(r"^\*\*Client:\*\*\s*([A-Za-z]+)", re.M)
# Most preferred first. Media and provenance files are never transcripts.
TRANSCRIPT_PREFS = [
    lambda p: p == "transcript.md",
    lambda p: p.endswith(".md"),
    lambda p: p.endswith(".txt"),
    lambda p: p.endswith(".srt"),
    lambda p: p.endswith(".vtt"),
]


@dataclass(frozen=True)
class Note:
    rel_path: str
    date: str
    names: frozenset
    folder_id: str | None


def parse_note(path: Path, kb_root: Path) -> Note | None:
    m = NOTE_RE.match(path.name)
    if not m:
        return None
    text = path.read_text(errors="replace")
    names = {m.group(1).lower()}
    c = CLIENT_RE.search(text)
    if c:
        names.add(c.group(1).lower())
    f = FOLDER_RE.search(text)
    return Note(path.relative_to(kb_root).as_posix(), m.group(2), frozenset(names), f.group(1) if f else None)


def pick_transcript(files: list[dict]) -> str | None:
    names = sorted(files, key=lambda f: (f["Path"].count("/"), f["Path"]))
    for pref in TRANSCRIPT_PREFS:
        for f in names:
            base = f["Path"].rsplit("/", 1)[-1].lower()
            if "provenance" not in base and pref(base):
                return f["ID"]
    return None


def pacific_date(utc_iso: str) -> str:
    return datetime.fromisoformat(utc_iso.replace("Z", "+00:00")).astimezone(PACIFIC).date().isoformat()


def match_session(note: Note, records: list[dict]) -> tuple[str, dict | None]:
    hits = []
    for r in records:
        f = r.get("fields", {})
        if not f.get(F_TIME) or pacific_date(f[F_TIME]) != note.date:
            continue
        firsts = {str(n).split()[0].lower() for n in f.get(F_NAME, []) if str(n).strip()}
        if firsts & note.names:
            hits.append(r)
    if not hits:
        return "no-row", None
    if len(hits) > 1:
        return "ambiguous", None
    return "matched", hits[0]


def plan_fields(note: Note, transcript_id: str | None, record: dict, now_iso: str) -> tuple[str, dict]:
    want = {F_KB: note.rel_path}
    if note.folder_id:
        want[F_FOLDER] = f"https://drive.google.com/drive/folders/{note.folder_id}"
    if transcript_id:
        want[F_TRANSCRIPT] = f"https://drive.google.com/file/d/{transcript_id}/view"
    have = record.get("fields", {})
    if any(have.get(k) and have[k] != v for k, v in want.items()):
        return "conflict", {}
    todo = {k: v for k, v in want.items() if not have.get(k)}
    if not todo:
        return "already-linked", {}
    todo[F_SYNCED] = now_iso
    return "write", todo
