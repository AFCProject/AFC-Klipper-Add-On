"""
Runs the CFS unit and lanes against a simulated CFS box on Klipper's real
reactor (see tests/cfs_reactor_harness.py). Each scenario runs in its own
subprocess because conftest mocks chelper and the real reactor needs it.

Skipped unless KLIPPER_PATH points at a Klipper (or Kalico) checkout, e.g.
    KLIPPER_PATH=~/klipper ./run-tests.sh --unit
"""
from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
HARNESS = os.path.join(HERE, "cfs_reactor_harness.py")
# KLIPPER_PATH, or a Klipper clone at the repository root (where AFC's klippy
# integration tests expect it)
KLIPPER_PATH = os.environ.get("KLIPPER_PATH", "") or os.path.join(HERE, "..", "klipper")

pytestmark = pytest.mark.skipif(
    not os.path.isfile(os.path.join(KLIPPER_PATH, "klippy", "reactor.py")),
    reason="no Klipper checkout (set KLIPPER_PATH or clone Klipper to ./klipper)",
)

SCENARIOS = [
    "connect_addressed",
    "connect_discovery",
    "retry_and_offline",
    "garbage_resync",
    "poll_presence",
    "load_unload",
    "slot_switch",
    "stall_fails_cleanly",
    "empty_slot",
    "offline",
    "trace_recording",
    "slow_stage0",
    "late_reply_resync",
    "eject_part_fed",
    "unload_still_gripped",
    "box_boots_late",
    "offline_prep_then_online",
    "two_boxes_by_uid",
    "two_boxes_load_while_polling",
    "stage5_reply_lost",
    "rfid_insert_reads_tag",
    "rfid_no_tag_box_busy",
    "rfid_box_silent_longer",
    "rfid_spoolman_id",
    "rfid_load_waits_for_tag_read",
    "rfid_read_at_startup",
    "rfid_startup_off_by_default",
    "rfid_not_during_print",
    "wrong_box_at_address",
    "single_box_without_uid",
]


@pytest.mark.parametrize("scenario", SCENARIOS)
def test_cfs_on_real_reactor(scenario):
    env = dict(os.environ)
    env["KLIPPER_PATH"] = KLIPPER_PATH
    env["PYTHONPATH"] = os.pathsep.join(p for p in sys.path if p)
    proc = subprocess.run([sys.executable, HARNESS, scenario], capture_output=True,
                          text=True, timeout=120, env=env)
    assert proc.returncode == 0, proc.stderr
    result = json.loads(proc.stdout.strip().splitlines()[-1])
    assert "exception" not in result, result["exception"]
    failed = {k: v for k, v in result["checks"].items() if not v}
    assert not failed, "failed checks {} (info: {})".format(list(failed), result["info"])
