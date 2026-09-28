"""Link KB session notes and their Drive transcripts to Airtable Sessions rows.

Idempotent and fill-blank-only: a non-blank field with a different value is reported as a conflict and left alone.
Never creates Sessions rows (the calendar sync owns them). See CLAUDE.md "KB session links".
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

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
TRANSCRIPT_EXTS = (".md", ".txt", ".srt", ".vtt")
TRANSCRIPT_PREFS = [
    lambda p: p == "transcript.md",
    lambda p: "transcript" in p and p.endswith(TRANSCRIPT_EXTS),
    lambda p: p.endswith(".md"),
    lambda p: p.endswith(".txt"),
    lambda p: p.endswith(".srt"),
    lambda p: p.endswith(".vtt"),
]


@dataclass(frozen=True)
class Note:
    rel_path: str
    date: str
    names: frozenset[str]
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
    conflicts = {k: have[k] for k, v in want.items() if have.get(k) and have[k] != v}
    if conflicts:
        return "conflict", conflicts
    todo = {k: v for k, v in want.items() if not have.get(k)}
    if not todo:
        return "already-linked", {}
    todo[F_SYNCED] = now_iso
    return "write", todo


REPO = Path(__file__).resolve().parent
RCLONE = "/opt/homebrew/bin/rclone" if Path("/opt/homebrew/bin/rclone").exists() else "rclone"
REMOTE = os.environ.get("KB_LINKS_DRIVE_REMOTE", "rickdrive:")
TABLE = os.environ.get("AIRTABLE_SESSIONS_TABLE", "Sessions")
STATUSES = ["write", "already-linked", "no-row", "ambiguous", "conflict", "rclone-error"]


def load_env() -> None:
    from dotenv import load_dotenv

    load_dotenv(REPO / ".env")


def list_sessions(pat: str, base: str) -> list[dict]:
    from session_sync import airtable_list_records

    return airtable_list_records(pat, base, TABLE, fields=[F_TIME, F_NAME, F_KB, F_FOLDER, F_TRANSCRIPT])


def list_folder(folder_id: str) -> list[dict]:
    r = subprocess.run(
        [RCLONE, "lsjson", REMOTE, "--drive-root-folder-id", folder_id, "-R", "--files-only"],
        capture_output=True, text=True, timeout=120,
    )
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip()[-200:])
    return json.loads(r.stdout)


def patch(pat: str, base: str, rows: list[dict]) -> None:
    from session_sync import airtable_patch_records

    for i in range(0, len(rows), 10):  # Airtable allows 10 records per request
        airtable_patch_records(pat, base, TABLE, rows[i : i + 10])


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--apply", action="store_true", help="write to Airtable (default: dry run)")
    ap.add_argument("--note", action="append", type=Path, help="only this note (repeatable)")
    ap.add_argument("--days", type=int, help="only notes dated within the last N days")
    ap.add_argument("--kb", type=Path, default=Path(os.environ.get("KB_DIR", Path.home() / "Dev" / "kb")))
    a = ap.parse_args(argv)
    load_env()
    pat, base = os.environ.get("AIRTABLE_PAT", ""), os.environ.get("AIRTABLE_BASE_ID", "")
    kb = a.kb.resolve()
    paths = [p.resolve() for p in a.note] if a.note else sorted((kb / "coaching" / "client-notes").glob("*.md"))
    cutoff = (date.today() - timedelta(days=a.days)).isoformat() if a.days is not None else None
    notes = []
    skipped = 0
    for p in paths:
        try:
            n = parse_note(p, kb)
        except ValueError:
            n = None
        if n is None:
            skipped += 1
            print(f"skip {p} (not a client session note)")
            continue
        if not cutoff or n.date >= cutoff:
            notes.append(n)
    try:
        records = list_sessions(pat, base)
    except (RuntimeError, requests.RequestException) as e:
        print(f"AIRTABLE FAIL: {e}")
        return 1
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    counts = dict.fromkeys(STATUSES, 0)
    rows = []
    matches = [(n, *match_session(n, records)) for n in notes]
    dup_counts: dict[str, int] = {}
    for _, status, r in matches:
        if status == "matched":
            dup_counts[r["id"]] = dup_counts.get(r["id"], 0) + 1
    for n, status, r in matches:
        if status == "matched" and dup_counts[r["id"]] > 1:
            status = "ambiguous"
        if status != "matched":
            counts[status] += 1
            print(f"{status} {n.rel_path}")
            continue
        tid = None
        if n.folder_id:
            try:
                tid = pick_transcript(list_folder(n.folder_id))
            except (RuntimeError, subprocess.TimeoutExpired, json.JSONDecodeError, OSError) as e:
                counts["rclone-error"] += 1
                print(f"rclone-error {n.rel_path}: {e}")
        status, fields = plan_fields(n, tid, r, now)
        counts[status] += 1
        if status == "conflict":
            print(f"conflict {n.rel_path} -> {r['id']} (fields: {', '.join(fields)})")
            continue
        print(f"{status} {n.rel_path} -> {r['id']}")
        if status == "write":
            rows.append({"id": r["id"], "fields": fields})
    if a.apply and rows:
        try:
            patch(pat, base, rows)
        except (RuntimeError, requests.RequestException) as e:
            print(f"AIRTABLE FAIL: {e}")
            return 1
    result = "RESULT: " + " ".join(f"{s}={counts[s]}" for s in STATUSES) + f" skipped={skipped}"
    print(result + ("" if a.apply else " (dry run)"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
