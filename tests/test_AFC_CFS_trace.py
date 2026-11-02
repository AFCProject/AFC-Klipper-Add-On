"""Tests for the CFS trace writer and cfs_trace_summary.py (tools/ in the
standalone repo, utilities/ in AFC)."""
from __future__ import annotations

import importlib.util
import io
import json
import os
from unittest.mock import MagicMock

import pytest

from extras.AFC_CFS import CFSTrace

_ROOT = os.path.join(os.path.dirname(os.path.realpath(__file__)), "..")
TOOL = next((p for p in (os.path.join(_ROOT, d, "cfs_trace_summary.py")
                         for d in ("tools", "utilities")) if os.path.isfile(p)), None)
if TOOL is None:
    pytest.skip("cfs_trace_summary.py not found", allow_module_level=True)
_spec = importlib.util.spec_from_file_location("cfs_trace_summary", TOOL)
summary = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(summary)


def _read(path):
    with open(path, encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def test_disabled_without_path_is_a_noop(tmp_path):
    trace = CFSTrace(None, 1024, 1, MagicMock())
    assert trace.start() is False
    trace.event("tx", fn="X")
    assert trace.written == 0 and trace.dropped == 0


def test_writes_json_lines_with_timestamps(tmp_path):
    path = tmp_path / "sub" / "trace.jsonl"
    trace = CFSTrace(str(path), 1024 * 1024, 1, MagicMock())
    assert trace.start()
    trace.event("tx", fn="GET_BOX_STATE", data=b"\x01".hex())
    trace.event("load_end", ok=True, seconds=12.5)
    trace.stop()
    lines = _read(path)
    assert [e["ev"] for e in lines] == ["tx", "load_end"]
    assert all("ts" in e and "t" in e for e in lines)
    assert lines[1]["seconds"] == 12.5


def test_rotates_at_max_size_and_keeps_backups(tmp_path):
    path = tmp_path / "trace.jsonl"
    trace = CFSTrace(str(path), 2000, 2, MagicMock())
    trace.start()
    for i in range(400):
        trace.event("tx", fn="EXTRUDE_PROCESS", data="0105" * 4, i=i)
    trace.stop()
    assert os.path.exists(str(path) + ".1")
    events, files, bad = summary.load(str(path))
    assert bad == 0
    seen = [e["i"] for e in events]
    assert seen == sorted(seen)                          # backups read oldest first
    assert not os.path.exists(str(path) + ".3")          # never more than `backups`
    total = sum(os.path.getsize(f) for f in files)
    assert total < 3 * 2000 + 4000


def test_full_queue_drops_instead_of_blocking(tmp_path):
    import queue
    trace = CFSTrace(str(tmp_path / "t.jsonl"), 1024 * 1024, 0, MagicMock())
    trace._queue = queue.Queue(maxsize=5)
    trace.enabled = True                                 # no writer thread: queue fills
    for _ in range(15):
        trace.event("tx")
    assert trace.dropped == 10


def test_summary_reports_failures_and_link_problems():
    t = 1_760_000_000.0
    events = [
        {"ts": t, "ev": "trace_start", "settings": {"stage7_arg": 3, "load_attempts": 2,
                                                    "stage5_timeout": 60, "stage5_stall_time": 8}},
        {"ts": t + 1, "ev": "online", "version": "113100008730633225CMPN"},
        {"ts": t + 2, "ev": "load_start", "lane": "lane2", "slot": "B"},
        {"ts": t + 3, "ev": "tx", "fn": "EXTRUDE_PROCESS"},
        {"ts": t + 3.2, "ev": "rx", "fn": "EXTRUDE_PROCESS", "st": "EXTRUDE_ERR8", "ms": 40.0},
        {"ts": t + 4, "ev": "timeout", "fn": "EXTRUDE_PROCESS", "attempt": 1},
        {"ts": t + 9, "ev": "stage5_result", "lane": "lane2", "slot": "B", "reached": False,
         "attempt": 1, "detail": "measuring wheel stalled at 310.0mm", "seconds": 7.0},
        {"ts": t + 20, "ev": "load_end", "lane": "lane2", "slot": "B", "ok": False,
         "attempts": 2, "seconds": 18.0},
        {"ts": t + 20, "ev": "fail", "lane": "lane2", "message": "load failed after 2 attempt(s)"},
        {"ts": t + 21, "ev": "mark", "text": "swapped spool"},
    ]
    buf = io.StringIO()
    summary.summarize(events, out=buf)
    text = buf.getvalue()
    assert "Box firmware: 113100008730633225CMPN" in text
    assert "Loads: 0 ok, 1 failed" in text
    assert "measuring wheel stalled" in text
    assert "EXTRUDE_ERR8" in text
    assert "timeouts by command: EXTRUDE_PROCESS x1" in text
    assert "swapped spool" in text


def test_summary_cli_filters_events(tmp_path, capsys):
    path = tmp_path / "t.jsonl"
    path.write_text("\n".join(json.dumps(e) for e in [
        {"ts": 1.0, "ev": "tx", "fn": "A"},
        {"ts": 2.0, "ev": "fail", "message": "boom"},
    ]) + "\n{truncated", encoding="utf-8")
    assert summary.main([str(path), "--events", "fail"]) == 0
    out = capsys.readouterr().out.strip().splitlines()
    assert len(out) == 1 and json.loads(out[0])["message"] == "boom"
