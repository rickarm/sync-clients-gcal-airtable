from pathlib import Path

import kb_links as k

KB = Path("/kb")


def write_note(tmp_path, name, body):
    d = tmp_path / "coaching" / "client-notes"
    d.mkdir(parents=True, exist_ok=True)
    p = d / name
    p.write_text(body)
    return p


NOTE = (
    "# Vic Mileham: 2026-09-28\n\n"
    "**Client:** Vic Mileham (VP of Talent, [[COMPANY-DBT-Labs]])\n"
    "**Date:** 2026-09-28\n"
    "**Recording:** [Drive session folder](https://drive.google.com/drive/folders/1UuqWr3PYoaeKL2zpwXpueoUY1H72d3Jg)\n"
)


def rec(rid, utc, name, **fields):
    return {"id": rid, "fields": {k.F_TIME: utc, k.F_NAME: [name], **fields}}


def test_parse_note_reads_date_names_and_folder(tmp_path):
    n = k.parse_note(write_note(tmp_path, "Vic-2026-09-28.md", NOTE), tmp_path)
    assert n.rel_path == "coaching/client-notes/Vic-2026-09-28.md"
    assert n.date == "2026-09-28"
    assert n.names == frozenset({"vic"})
    assert n.folder_id == "1UuqWr3PYoaeKL2zpwXpueoUY1H72d3Jg"


def test_parse_note_without_recording(tmp_path):
    n = k.parse_note(write_note(tmp_path, "Vic-2026-01-05.md", "**Client:** Vic Mileham\n"), tmp_path)
    assert n.folder_id is None


def test_parse_note_skips_non_session_files(tmp_path):
    assert k.parse_note(write_note(tmp_path, "index.md", "x"), tmp_path) is None


def test_match_uses_client_line_name(tmp_path):
    n = k.parse_note(write_note(tmp_path, "Meg-2026-05-01.md", "**Client:** Megan Pittman\n"), tmp_path)
    status, r = k.match_session(n, [rec("r1", "2026-05-01T17:00:00.000Z", "Megan Pittman")])
    assert (status, r["id"]) == ("matched", "r1")


def test_pick_transcript_prefers_transcript_md():
    files = [{"Path": "zoom.vtt", "ID": "a"}, {"Path": "transcript.json", "ID": "b"}, {"Path": "transcript.md", "ID": "c"}]
    assert k.pick_transcript(files) == "c"


def test_pick_transcript_falls_back_to_txt():
    files = [{"Path": "coaching-brett-levenson-2026-04-07.txt", "ID": "t"}, {"Path": "audio.m4a", "ID": "a"}]
    assert k.pick_transcript(files) == "t"


def test_pick_transcript_ignores_provenance_and_media():
    files = [{"Path": "zoom.vtt.provenance.json", "ID": "p"}, {"Path": "x.mp4", "ID": "v"}]
    assert k.pick_transcript(files) is None


def test_pacific_date():
    assert k.pacific_date("2026-09-29T02:30:00.000Z") == "2026-09-28"


def test_match_uses_pacific_date():
    n = k.Note("coaching/client-notes/Vic-2026-09-28.md", "2026-09-28", frozenset({"vic"}), "F")
    status, r = k.match_session(n, [rec("r1", "2026-09-29T02:30:00.000Z", "Vic Mileham")])
    assert (status, r["id"]) == ("matched", "r1")


def test_match_picks_person_not_company():
    n = k.Note("coaching/client-notes/Vic-2026-09-28.md", "2026-09-28", frozenset({"vic"}), "F")
    rows = [rec("k", "2026-09-28T19:00:00.000Z", "Kaitlyn Henry"), rec("v", "2026-09-28T16:30:00.000Z", "Vic Mileham")]
    status, r = k.match_session(n, rows)
    assert (status, r["id"]) == ("matched", "v")


def test_match_no_row_and_ambiguous():
    n = k.Note("coaching/client-notes/Vic-2026-09-28.md", "2026-09-28", frozenset({"vic"}), "F")
    assert k.match_session(n, [])[0] == "no-row"
    two = [rec("a", "2026-09-28T16:00:00.000Z", "Vic Mileham"), rec("b", "2026-09-28T20:00:00.000Z", "Vic Mileham")]
    assert k.match_session(n, two)[0] == "ambiguous"


def test_plan_fields_fills_blanks_only():
    n = k.Note("coaching/client-notes/Vic-2026-09-28.md", "2026-09-28", frozenset({"vic"}), "F")
    status, fields = k.plan_fields(n, "T", rec("v", "x", "Vic"), "2026-09-28T20:00:00Z")
    assert status == "write"
    assert fields == {
        k.F_KB: "coaching/client-notes/Vic-2026-09-28.md",
        k.F_FOLDER: "https://drive.google.com/drive/folders/F",
        k.F_TRANSCRIPT: "https://drive.google.com/file/d/T/view",
        k.F_SYNCED: "2026-09-28T20:00:00Z",
    }


def test_plan_fields_already_linked():
    n = k.Note("coaching/client-notes/Vic-2026-09-28.md", "2026-09-28", frozenset({"vic"}), "F")
    r = rec("v", "x", "Vic", **{k.F_KB: n.rel_path, k.F_FOLDER: "https://drive.google.com/drive/folders/F",
                               k.F_TRANSCRIPT: "https://drive.google.com/file/d/T/view"})
    assert k.plan_fields(n, "T", r, "now") == ("already-linked", {})


def test_plan_fields_conflict_writes_nothing():
    n = k.Note("coaching/client-notes/Vic-2026-09-28.md", "2026-09-28", frozenset({"vic"}), "F")
    r = rec("v", "x", "Vic", **{k.F_KB: "coaching/client-notes/Other-2026-09-28.md"})
    assert k.plan_fields(n, "T", r, "now") == ("conflict", {})


def test_plan_fields_without_recording_links_note_only():
    n = k.Note("coaching/client-notes/Vic-2026-01-05.md", "2026-01-05", frozenset({"vic"}), None)
    status, fields = k.plan_fields(n, None, rec("v", "x", "Vic"), "now")
    assert status == "write" and set(fields) == {k.F_KB, k.F_SYNCED}
