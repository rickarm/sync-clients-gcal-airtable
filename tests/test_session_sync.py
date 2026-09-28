import json
from datetime import date, datetime, timezone

import pytest

import session_sync as s

F_CEID, F_TIME = "Calendar Event ID", "SessionTimeDate (UTC)"
# 2026-09-28 06:00 Pacific (PDT, UTC-7)
NOW = datetime(2026, 9, 28, 13, 0, tzinfo=timezone.utc)
END_OF_TODAY = datetime(2026, 9, 29, 7, 0, tzinfo=timezone.utc)


def test_window_caps_at_now_by_default():
    _, end, _ = s.compute_window(NOW, 4, date(2026, 9, 28))
    assert end == NOW


def test_include_today_extends_to_end_of_local_day():
    start, end, label = s.compute_window(NOW, 4, date(2026, 9, 27), date(2026, 9, 28), include_today=True)
    assert start == datetime(2026, 9, 27, 7, 0, tzinfo=timezone.utc)
    assert end == END_OF_TODAY
    assert label == "2026-09-27 to 2026-09-28"


def test_include_today_never_reaches_tomorrow():
    _, end, _ = s.compute_window(NOW, 4, date(2026, 9, 28), date(2026, 10, 5), include_today=True)
    assert end == END_OF_TODAY


def test_include_today_with_weeks():
    start, end, label = s.compute_window(NOW, 4, include_today=True)
    assert (start, end) == (datetime(2026, 8, 31, 13, 0, tzinfo=timezone.utc), END_OF_TODAY)
    assert label == "4 weeks + rest of today"


def test_future_start_still_rejected():
    with pytest.raises(ValueError):
        s.compute_window(NOW, 4, date(2026, 9, 29), include_today=True)


def rec(rid, ceid, t):
    return {"id": rid, "fields": {F_CEID: ceid, F_TIME: t}}


def test_find_stale_flags_only_in_window_rows_with_no_live_event():
    recs = [
        rec("live", "a_1", "2026-09-28T17:00:00.000Z"),
        rec("gone", "b_1", "2026-09-28T19:00:00.000Z"),
        rec("old", "c_1", "2026-08-01T17:00:00.000Z"),
        {"id": "legacy", "fields": {F_TIME: "2026-09-28T18:00:00.000Z"}},
        {"id": "notime", "fields": {F_CEID: "d_1"}},
    ]
    start = datetime(2026, 9, 27, 7, 0, tzinfo=timezone.utc)
    out = s.find_stale_sessions(recs, {"a_1"}, start, END_OF_TODAY, F_CEID, F_TIME)
    assert [r["id"] for r in out] == ["gone"]


def row(rid):
    return {"id": rid, "time": "2026-09-28 10:00", "client": ["X"][0], "ceid": rid + "_c"}


def test_write_stale_file_reports_only_new_rows(tmp_path):
    p = tmp_path / "stale.json"
    assert [r["id"] for r in s.write_stale_file(p, [row("r1")])] == ["r1"]
    assert (tmp_path / "stale.json.new").read_text() == "2026-09-28 10:00 | X | r1\n"

    assert [r["id"] for r in s.write_stale_file(p, [row("r1"), row("r2")])] == ["r2"]
    assert "r2" in (tmp_path / "stale.json.new").read_text()

    assert s.write_stale_file(p, [row("r1")]) == []
    assert not (tmp_path / "stale.json.new").exists()
    assert [r["id"] for r in json.loads(p.read_text())] == ["r1"]
