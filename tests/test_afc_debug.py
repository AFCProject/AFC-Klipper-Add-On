# AFCProject Automated Filament Changer
#
# Copyright (C) 2026 AFCProject
#
# This file may be distributed under the terms of the GNU GPLv3 license.
"""Unit tests for troubleshooting/afc_debug.py."""
from __future__ import annotations

import datetime
import json
import subprocess
import zipfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

import pytest

from troubleshooting import afc_debug as dbg

NOW = datetime.datetime(2026, 3, 4, 5, 6, 7)


def make_cfg(tmp_path: Path, **overrides: Any) -> dbg.Config:
    values: Dict[str, Any] = dict(
        printer_log_dir=tmp_path / "logs",
        printer_config_dir=tmp_path / "config",
        klipper_dir=tmp_path / "klipper",
        afc_path=tmp_path / "afc",
        afc_config_dir=tmp_path / "config" / "AFC",
        klipper_venv=tmp_path / "venv",
        moonraker_config=tmp_path / "config" / "moonraker.conf",
        moonraker_address="http://localhost",
        moonraker_port=7125,
        klipper_service="klipper",
        assume_yes=True,
        zip_only=False,
        redact=True)
    values.update(overrides)
    return dbg.Config(**values)


class FakeRun:
    """Stand-in for subprocess.run keyed on the first matching command prefix."""

    def __init__(self, results: Optional[Dict[str, Any]] = None) -> None:
        self.results = results or {}
        self.calls: List[List[str]] = []
        self.kwargs: List[Dict[str, Any]] = []

    def __call__(self, cmd: List[str], **kwargs: Any) -> Any:
        self.calls.append(cmd)
        self.kwargs.append(kwargs)
        result = self.results.get(" ".join(cmd), (0, ""))
        if isinstance(result, BaseException):
            raise result
        code, out = result
        return SimpleNamespace(returncode=code, stdout=out.encode())


class TestRunCommand:
    def test_returns_stripped_output(self, monkeypatch):
        fake = FakeRun({"echo hi": (0, "hi\n\n")})
        monkeypatch.setattr(dbg.subprocess, "run", fake)
        assert dbg.run_command(["echo", "hi"]) == "hi"

    def test_nonzero_exit_returns_none(self, monkeypatch):
        monkeypatch.setattr(dbg.subprocess, "run", FakeRun({"x": (1, "out")}))
        assert dbg.run_command(["x"]) is None

    def test_oserror_returns_none(self, monkeypatch):
        monkeypatch.setattr(dbg.subprocess, "run", FakeRun({"x": OSError("no")}))
        assert dbg.run_command(["x"]) is None

    def test_timeout_returns_none(self, monkeypatch):
        err = subprocess.TimeoutExpired("x", 1)
        monkeypatch.setattr(dbg.subprocess, "run", FakeRun({"x": err}))
        assert dbg.run_command(["x"]) is None

    def test_merge_stderr_flag(self, monkeypatch):
        fake = FakeRun()
        monkeypatch.setattr(dbg.subprocess, "run", fake)
        dbg.run_command(["a"], merge_stderr=True)
        dbg.run_command(["b"])
        assert fake.kwargs[0]["stderr"] == subprocess.STDOUT
        assert fake.kwargs[1]["stderr"] == subprocess.DEVNULL

    def test_cwd_and_timeout_passed(self, monkeypatch, tmp_path):
        fake = FakeRun()
        monkeypatch.setattr(dbg.subprocess, "run", fake)
        dbg.run_command(["a"], cwd=tmp_path, timeout=5)
        assert fake.kwargs[0]["cwd"] == tmp_path
        assert fake.kwargs[0]["timeout"] == 5


class TestCommandOutput:
    def test_success(self, monkeypatch):
        monkeypatch.setattr(dbg.subprocess, "run", FakeRun({"u": (0, "linux")}))
        assert dbg.command_output(["u"]) == "linux"

    def test_failure_uses_default(self, monkeypatch):
        monkeypatch.setattr(dbg.subprocess, "run", FakeRun({"u": (1, "")}))
        assert dbg.command_output(["u"]) == "Unknown"
        assert dbg.command_output(["u"], default="none") == "none"

    def test_empty_success_not_replaced(self, monkeypatch):
        monkeypatch.setattr(dbg.subprocess, "run", FakeRun({"u": (0, "")}))
        assert dbg.command_output(["u"]) == ""


class TestExpand:
    def test_expands_home(self, monkeypatch):
        monkeypatch.setenv("HOME", "/home/test")
        assert dbg._expand("~/x") == Path("/home/test/x")


class TestHelpFormatter:
    def test_wide_option_column(self):
        formatter = dbg._help_formatter("prog")
        assert formatter._max_help_position == 34
        assert formatter._width == 100


class TestBuildParser:
    def test_usage_without_prog_env(self, monkeypatch):
        monkeypatch.delenv("AFC_PROG", raising=False)
        assert dbg.build_parser().usage == "afc-debug.sh [options]"

    def test_usage_with_prog_env(self, monkeypatch):
        monkeypatch.setenv("AFC_PROG", "./x.sh")
        assert dbg.build_parser().usage == "./x.sh [options]"

    def test_prog_from_environment(self, monkeypatch):
        monkeypatch.setenv("AFC_PROG", "./afc-debug.sh")
        assert dbg.build_parser().prog == "./afc-debug.sh"

    def test_defaults(self, monkeypatch):
        monkeypatch.delenv("AFC_PROG", raising=False)
        ns = dbg.build_parser().parse_args([])
        assert ns.moonraker_address == "http://localhost"
        assert ns.moonraker_port == 7125
        assert ns.moonraker_config is None
        assert ns.klipper_dir == "~/klipper"
        assert ns.klipper_venv == "~/klippy-env/bin"
        assert ns.klipper_service == "klipper"
        assert ns.printer_config_dir == "~/printer_data/config"
        assert ns.log_dir == "~/printer_data/logs"
        assert ns.afc_config_dir is None
        assert ns.afc_path is None
        assert ns.yes is False
        assert ns.zip is False
        assert ns.no_redact is False

    def test_short_flags(self):
        ns = dbg.build_parser().parse_args(
            ["-a", "h", "-n", "9", "-m", "m", "-k", "k", "-y", "y", "-s", "s", "-p", "p",
             "-l", "l", "-c", "c", "-r", "r", "-f", "-z"])
        assert (ns.moonraker_address, ns.moonraker_port, ns.moonraker_config) == ("h", 9, "m")
        assert (ns.klipper_dir, ns.klipper_venv, ns.klipper_service) == ("k", "y", "s")
        assert (ns.printer_config_dir, ns.log_dir) == ("p", "l")
        assert (ns.afc_config_dir, ns.afc_path) == ("c", "r")
        assert ns.yes is True
        assert ns.zip is True


class TestParseArgs:
    def test_defaults_derived(self, monkeypatch):
        monkeypatch.setenv("HOME", "/home/test")
        cfg = dbg.parse_args([])
        repo = Path(dbg.__file__).resolve().parent.parent
        assert cfg.printer_config_dir == Path("/home/test/printer_data/config")
        assert cfg.printer_log_dir == Path("/home/test/printer_data/logs")
        assert cfg.afc_config_dir == Path("/home/test/printer_data/config/AFC")
        assert cfg.moonraker_config == Path("/home/test/printer_data/config/moonraker.conf")
        assert cfg.afc_path == repo
        assert cfg.klipper_dir == Path("/home/test/klipper")
        assert cfg.klipper_venv == Path("/home/test/klippy-env/bin")
        assert cfg.assume_yes is False
        assert cfg.zip_only is False
        assert cfg.redact is True

    def test_overrides(self):
        cfg = dbg.parse_args(["-p", "/pc", "-c", "/afc/cfg", "-m", "/m.conf", "-r", "/repo",
                              "-l", "/logs", "-s", "kl", "-n", "8080", "-a", "10.0.0.2",
                              "-f", "-z"])
        assert cfg.printer_config_dir == Path("/pc")
        assert cfg.afc_config_dir == Path("/afc/cfg")
        assert cfg.moonraker_config == Path("/m.conf")
        assert cfg.afc_path == Path("/repo")
        assert cfg.printer_log_dir == Path("/logs")
        assert cfg.klipper_service == "kl"
        assert cfg.moonraker_port == 8080
        assert cfg.moonraker_address == "10.0.0.2"
        assert cfg.assume_yes is True
        assert cfg.zip_only is True

    def test_no_redact_flag_disables_redaction(self):
        assert dbg.parse_args(["--no-redact"]).redact is False

    def test_config_dir_follows_printer_config_dir(self):
        cfg = dbg.parse_args(["-p", "/pc"])
        assert cfg.afc_config_dir == Path("/pc/AFC")
        assert cfg.moonraker_config == Path("/pc/moonraker.conf")


class TestPrepout:
    def test_header_only(self):
        sep = "=" * 64
        assert dbg.prepout("Title") == f"\n{sep}\nTitle\n{sep}\n"

    def test_with_lines(self):
        sep = "=" * 64
        assert dbg.prepout("T", "a", "b") == f"\n{sep}\nT\n{sep}\na\nb\n"


class TestFileSection:
    def test_missing_file(self, tmp_path):
        path = tmp_path / "nope"
        assert dbg.file_section("n", path) == dbg.prepout("n") + f"File not found: {path}\n"

    def test_plain_file(self, tmp_path):
        path = tmp_path / "f"
        path.write_text("hello\n")
        assert dbg.file_section("n", path) == dbg.prepout("n") + "hello\n"

    def test_null_bytes_warn(self, tmp_path):
        path = tmp_path / "f"
        path.write_bytes(b"x\0y")
        expected = dbg.prepout("n") + f"WARNING: Null bytes detected in {path}\n" + "xy"
        assert dbg.file_section("n", path) == expected

    def test_invalid_utf8_replaced(self, tmp_path):
        path = tmp_path / "f"
        path.write_bytes(b"\xff")
        assert dbg.file_section("n", path) == dbg.prepout("n") + "\ufffd"

    def test_directory_is_not_file(self, tmp_path):
        assert "File not found" in dbg.file_section("n", tmp_path)


class TestSudo:
    def test_success(self, monkeypatch):
        fake = FakeRun()
        monkeypatch.setattr(dbg.subprocess, "run", fake)
        dbg._sudo(["systemctl", "stop", "klipper"])
        assert fake.calls == [["sudo", "systemctl", "stop", "klipper"]]

    def test_nonzero_raises(self, monkeypatch):
        monkeypatch.setattr(dbg.subprocess, "run", FakeRun({"sudo ls": (1, "")}))
        with pytest.raises(dbg.DebugError, match="Command failed: sudo ls"):
            dbg._sudo(["ls"])

    def test_oserror_raises(self, monkeypatch):
        monkeypatch.setattr(dbg.subprocess, "run", FakeRun({"sudo ls": OSError("boom")}))
        with pytest.raises(dbg.DebugError, match="Unable to run sudo: boom"):
            dbg._sudo(["ls"])


class TestIsKlipperRunningSystemd:
    LIST = "systemctl list-units --type=service"

    def test_list_fails(self, monkeypatch):
        monkeypatch.setattr(dbg.subprocess, "run", FakeRun({self.LIST: (1, "")}))
        assert dbg.is_klipper_running_systemd("klipper") is False

    def test_service_not_listed(self, monkeypatch):
        monkeypatch.setattr(dbg.subprocess, "run", FakeRun({self.LIST: (0, "other.service")}))
        assert dbg.is_klipper_running_systemd("klipper") is False

    def test_listed_and_active(self, monkeypatch):
        fake = FakeRun({self.LIST: (0, "klipper.service")})
        monkeypatch.setattr(dbg.subprocess, "run", fake)
        assert dbg.is_klipper_running_systemd("klipper") is True
        assert fake.calls[-1] == ["systemctl", "is-active", "--quiet", "klipper"]

    def test_listed_but_inactive(self, monkeypatch):
        fake = FakeRun({self.LIST: (0, "klipper.service"),
                        "systemctl is-active --quiet klipper": (3, "")})
        monkeypatch.setattr(dbg.subprocess, "run", fake)
        assert dbg.is_klipper_running_systemd("klipper") is False


class TestIsKlipperRunningService:
    def test_running(self, monkeypatch):
        monkeypatch.setattr(dbg.subprocess, "run", FakeRun())
        assert dbg.is_klipper_running_service("klipper") is True

    def test_not_running(self, monkeypatch):
        monkeypatch.setattr(dbg.subprocess, "run", FakeRun({"service klipper status": (3, "")}))
        assert dbg.is_klipper_running_service("klipper") is False


def which_only(*names: str):
    return lambda name: f"/bin/{name}" if name in names else None


class TestStopKlipper:
    def test_systemd_running(self, monkeypatch, capsys):
        monkeypatch.setattr(dbg.shutil, "which", which_only("systemctl", "service"))
        monkeypatch.setattr(dbg, "is_klipper_running_systemd", lambda s: True)
        sudo: List[List[str]] = []
        monkeypatch.setattr(dbg, "_sudo", sudo.append)
        dbg.stop_klipper("klipper")
        assert sudo == [["systemctl", "stop", "klipper"]]
        assert capsys.readouterr().out == "Stopping Klipper using systemd...\n"

    def test_systemd_already_stopped(self, monkeypatch, capsys):
        monkeypatch.setattr(dbg.shutil, "which", which_only("systemctl"))
        monkeypatch.setattr(dbg, "is_klipper_running_systemd", lambda s: False)
        sudo: List[List[str]] = []
        monkeypatch.setattr(dbg, "_sudo", sudo.append)
        dbg.stop_klipper("klipper")
        assert sudo == []
        assert capsys.readouterr().out == "Klipper is already stopped (systemd).\n"

    def test_service_running(self, monkeypatch, capsys):
        monkeypatch.setattr(dbg.shutil, "which", which_only("service"))
        monkeypatch.setattr(dbg, "is_klipper_running_service", lambda s: True)
        sudo: List[List[str]] = []
        monkeypatch.setattr(dbg, "_sudo", sudo.append)
        dbg.stop_klipper("klipper")
        assert sudo == [["service", "klipper", "stop"]]
        assert capsys.readouterr().out == "Stopping Klipper using service...\n"

    def test_service_already_stopped(self, monkeypatch, capsys):
        monkeypatch.setattr(dbg.shutil, "which", which_only("service"))
        monkeypatch.setattr(dbg, "is_klipper_running_service", lambda s: False)
        sudo: List[List[str]] = []
        monkeypatch.setattr(dbg, "_sudo", sudo.append)
        dbg.stop_klipper("klipper")
        assert sudo == []
        assert capsys.readouterr().out == "Klipper is already stopped (service).\n"

    def test_no_manager(self, monkeypatch):
        monkeypatch.setattr(dbg.shutil, "which", which_only())
        with pytest.raises(dbg.DebugError, match="Unable to detect systemd or service"):
            dbg.stop_klipper("klipper")


class TestStartKlipper:
    def test_systemd(self, monkeypatch, capsys):
        monkeypatch.setattr(dbg.shutil, "which", which_only("systemctl", "service"))
        sudo: List[List[str]] = []
        monkeypatch.setattr(dbg, "_sudo", sudo.append)
        dbg.start_klipper("kl")
        assert sudo == [["systemctl", "start", "kl"]]
        assert capsys.readouterr().out == "Starting Klipper using systemd...\n"

    def test_service(self, monkeypatch, capsys):
        monkeypatch.setattr(dbg.shutil, "which", which_only("service"))
        sudo: List[List[str]] = []
        monkeypatch.setattr(dbg, "_sudo", sudo.append)
        dbg.start_klipper("kl")
        assert sudo == [["service", "kl", "start"]]
        assert capsys.readouterr().out == "Starting Klipper using service...\n"

    def test_no_manager(self, monkeypatch):
        monkeypatch.setattr(dbg.shutil, "which", which_only())
        with pytest.raises(dbg.DebugError, match="Unable to detect systemd or service"):
            dbg.start_klipper("kl")


class TestKlipperPython:
    def test_venv_python_exists(self, tmp_path):
        (tmp_path / "python").write_text("")
        assert dbg.klipper_python(tmp_path) == str(tmp_path / "python")

    def test_falls_back_to_current_interpreter(self, tmp_path):
        assert dbg.klipper_python(tmp_path) == dbg.sys.executable


class TestCollectCanIds:
    def test_no_interfaces(self, monkeypatch, capsys, tmp_path):
        monkeypatch.setattr(dbg.subprocess, "run", FakeRun({"ip -br link show": (1, "")}))
        assert dbg.collect_can_ids(tmp_path, tmp_path) == []
        assert capsys.readouterr().out == "No CAN interfaces found.\n"

    def test_blank_output_has_no_interfaces(self, monkeypatch, capsys, tmp_path):
        monkeypatch.setattr(dbg.subprocess, "run", FakeRun({"ip -br link show": (0, "\n  \n")}))
        assert dbg.collect_can_ids(tmp_path, tmp_path) == []
        assert capsys.readouterr().out == "No CAN interfaces found.\n"

    def test_collects_responding_interfaces(self, monkeypatch, capsys, tmp_path):
        (tmp_path / "python").write_text("")
        script = str(tmp_path / "scripts" / "canbus_query.py")
        py = str(tmp_path / "python")
        fake = FakeRun({
            "ip -br link show": (0, "lo UNKNOWN\ncan0 UP\neth0 UP\n"),
            f"{py} {script} lo": (1, ""),
            f"{py} {script} can0": (0, "Found uuid=abc\nFound uuid=def"),
            f"{py} {script} eth0": (1, ""),
        })
        monkeypatch.setattr(dbg.subprocess, "run", fake)
        result = dbg.collect_can_ids(tmp_path, tmp_path)
        assert result == ["Interface: can0", "Found uuid=abc", "Found uuid=def", ""]
        assert capsys.readouterr().out == "Querying CAN devices...\n"
        assert fake.kwargs[1]["timeout"] == 30
        assert fake.kwargs[1]["stderr"] == subprocess.STDOUT


class TestGetAfcVersion:
    def test_success(self, monkeypatch, tmp_path):
        p = str(tmp_path)
        fake = FakeRun({f"git -C {p} rev-parse --short HEAD": (0, "abc123"),
                        f"git -C {p} rev-list HEAD --count": (0, "42")})
        monkeypatch.setattr(dbg.subprocess, "run", fake)
        assert dbg.get_afc_version(tmp_path) == "42-abc123"

    def test_hash_fails(self, monkeypatch, tmp_path):
        p = str(tmp_path)
        fake = FakeRun({f"git -C {p} rev-parse --short HEAD": (1, ""),
                        f"git -C {p} rev-list HEAD --count": (0, "42")})
        monkeypatch.setattr(dbg.subprocess, "run", fake)
        assert dbg.get_afc_version(tmp_path) == "Unknown"

    def test_count_fails(self, monkeypatch, tmp_path):
        p = str(tmp_path)
        fake = FakeRun({f"git -C {p} rev-parse --short HEAD": (0, "abc123"),
                        f"git -C {p} rev-list HEAD --count": (1, "")})
        monkeypatch.setattr(dbg.subprocess, "run", fake)
        assert dbg.get_afc_version(tmp_path) == "Unknown"


class TestGetKlipperVersion:
    def test_success(self, monkeypatch, tmp_path):
        fake = FakeRun({f"git -C {tmp_path} describe --tags": (0, "v0.12.0-1")})
        monkeypatch.setattr(dbg.subprocess, "run", fake)
        assert dbg.get_klipper_version(tmp_path) == "v0.12.0-1"

    def test_failure(self, monkeypatch, tmp_path):
        monkeypatch.setattr(dbg.subprocess, "run", FakeRun({
            f"git -C {tmp_path} describe --tags": (128, "")}))
        assert dbg.get_klipper_version(tmp_path) == "Unknown"


class TestReadDistro:
    def test_joins_release_files(self, tmp_path):
        (tmp_path / "b-release").write_text("B\n")
        (tmp_path / "a-release").write_text("A\n")
        (tmp_path / "other").write_text("no")
        assert dbg.read_distro(str(tmp_path)) == "A\nB"

    def test_none_found(self, tmp_path):
        assert dbg.read_distro(str(tmp_path)) == "Unknown"

    def test_unreadable_entry_skipped(self, tmp_path):
        (tmp_path / "a-release").mkdir()
        (tmp_path / "b-release").write_text("B")
        assert dbg.read_distro(str(tmp_path)) == "B"


LSUSB_V = """\
  iProduct                2 USB2.0 Hub
  bDeviceProtocol         1 Single TT
    bAlternateSetting       0
    bInterfaceProtocol      0 Full speed
    bAlternateSetting       1
    bInterfaceProtocol      1 Single TT
"""


class TestSummarizeLsusb:
    def test_marks_active_alternate_setting(self):
        result = dbg.summarize_lsusb(LSUSB_V, "1", "/sys/dev/1-1/")
        assert result == [
            "Path:              /sys/dev/1-1/",
            "  iProduct                2 USB2.0 Hub",
            "  bDeviceProtocol         1 Single TT",
            "    bInterfaceProtocol      0 Full speed\n"
            "    bInterfaceProtocol      1 Single TT (active)",
            "---"]

    def test_empty_input(self):
        assert dbg.summarize_lsusb("", "", "p") == ["Path:              p", "", "", "", "---"]

    def test_blank_alternate_setting_line(self):
        text = "bAlternateSetting\nbInterfaceProtocol 0\n"
        result = dbg.summarize_lsusb(text, "Z", "p")
        assert result[3] == "bInterfaceProtocol 0"

    def test_alternate_setting_without_value_line(self):
        # A bare whitespace-only match cannot happen, so use a line holding only the keyword
        result = dbg.summarize_lsusb("bAlternateSetting", "bAlternateSetting", "p")
        assert result[3] == ""


def make_usb_device(root: Path, name: str, dev_class: str = "09", alt: Optional[str] = " 1\n",
                    bus: str = "1", devnum: str = "4") -> Path:
    dev = root / name
    dev.mkdir()
    (dev / "bDeviceClass").write_text(dev_class + "\n")
    (dev / "busnum").write_text(bus + "\n")
    (dev / "devnum").write_text(devnum + "\n")
    if alt is not None:
        child = dev / f"{name}:1.0"
        child.mkdir()
        (child / "bAlternateSetting").write_text(alt)
    return dev


class TestUsbHubDetails:
    def test_hub_summarised(self, monkeypatch, tmp_path):
        dev = make_usb_device(tmp_path, "1-1")
        fake = FakeRun({"lsusb -v -s 1:4": (0, LSUSB_V)})
        monkeypatch.setattr(dbg.subprocess, "run", fake)
        result = dbg.usb_hub_details(str(tmp_path))
        assert result[0] == f"Path:              {dev}/"
        assert result[3].endswith("Single TT (active)")
        assert result[3].startswith("    bInterfaceProtocol      0 Full speed\n")

    def test_non_hub_skipped(self, monkeypatch, tmp_path):
        make_usb_device(tmp_path, "1-1", dev_class="ff")
        fake = FakeRun()
        monkeypatch.setattr(dbg.subprocess, "run", fake)
        assert dbg.usb_hub_details(str(tmp_path)) == []
        assert fake.calls == []

    def test_missing_attribute_files_skipped(self, monkeypatch, tmp_path):
        (tmp_path / "usb1").mkdir()
        monkeypatch.setattr(dbg.subprocess, "run", FakeRun())
        assert dbg.usb_hub_details(str(tmp_path)) == []

    def test_lsusb_failure_skipped(self, monkeypatch, tmp_path):
        make_usb_device(tmp_path, "1-1")
        monkeypatch.setattr(dbg.subprocess, "run", FakeRun({"lsusb -v -s 1:4": (1, "")}))
        assert dbg.usb_hub_details(str(tmp_path)) == []

    def test_no_alternate_setting_files(self, monkeypatch, tmp_path):
        make_usb_device(tmp_path, "1-1", alt=None)
        monkeypatch.setattr(dbg.subprocess, "run", FakeRun({"lsusb -v -s 1:4": (0, LSUSB_V)}))
        result = dbg.usb_hub_details(str(tmp_path))
        assert "(active)" not in result[3]

    def test_unreadable_alternate_setting_skipped(self, monkeypatch, tmp_path):
        dev = make_usb_device(tmp_path, "1-1", alt=None)
        (dev / "1-1:1.0").mkdir()
        (dev / "1-1:1.0" / "bAlternateSetting").mkdir()
        monkeypatch.setattr(dbg.subprocess, "run", FakeRun({"lsusb -v -s 1:4": (0, LSUSB_V)}))
        result = dbg.usb_hub_details(str(tmp_path))
        assert "(active)" not in result[3]

    def test_regular_files_ignored(self, monkeypatch, tmp_path):
        (tmp_path / "version").write_text("x")
        monkeypatch.setattr(dbg.subprocess, "run", FakeRun())
        assert dbg.usb_hub_details(str(tmp_path)) == []


class TestCollectUsbTopology:
    def test_combines_tree_and_hubs(self, monkeypatch):
        monkeypatch.setattr(dbg.subprocess, "run", FakeRun({"lsusb -tv": (0, "TREE\0")}))
        monkeypatch.setattr(dbg, "usb_hub_details", lambda: ["h1", "h2"])
        assert dbg.collect_usb_topology() == "TREE\n===\nh1\nh2"

    def test_lsusb_missing(self, monkeypatch):
        monkeypatch.setattr(dbg.subprocess, "run", FakeRun({"lsusb -tv": OSError()}))
        monkeypatch.setattr(dbg, "usb_hub_details", lambda: [])
        assert dbg.collect_usb_topology() == "lsusb not available\n==="


class TestFormatStatValue:
    @pytest.mark.parametrize("value,expected", [
        (3.14159, "3.14"),
        (12, "12"),
        (2.0, "2"),
        ("7.456", "7.46"),
        ("-1.5", "-1.5"),
        (True, "true"),
        (False, "false"),
        (None, "null"),
        ("hello", "hello"),
        ("1.2.3", "1.2.3"),
        ("-", "-"),
        ("9" * 400, "9" * 400),
        ("0.125", "0.13"),
        ("-0.125", "-0.13"),
        ("2.675", "2.68"),
        ("-0.001", "0"),
        ("100.5", "100.5"),
        ("100.504", "100.5"),
        ("12345678901234567890", "12345678901234567890"),
        (".", "."),
    ])
    def test_values(self, value, expected):
        assert dbg.format_stat_value(value) == expected


class TestFlattenScalars:
    def test_scalar(self):
        assert dbg.flatten_scalars(5, "k") == [("k", 5)]

    def test_nested(self):
        data = {"a": {"b": 1}, "c": [10, {"d": 2}], "e": "x"}
        assert dbg.flatten_scalars(data) == [("a.b", 1), ("c.0", 10), ("c.1.d", 2), ("e", "x")]


class FakeResponse:
    def __init__(self, body: bytes) -> None:
        self.body = body

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *args: Any) -> None:
        return None

    def read(self) -> bytes:
        return self.body


class TestFetchStats:
    def test_success_with_scheme(self, monkeypatch):
        urls: List[str] = []

        def fake_urlopen(url: str, timeout: float) -> FakeResponse:
            urls.append(url)
            return FakeResponse(b'{"result": {"value": {"a": 1}}}')

        monkeypatch.setattr(dbg.urllib.request, "urlopen", fake_urlopen)
        assert dbg.fetch_stats("http://localhost/", 7125) == {"result": {"value": {"a": 1}}}
        assert urls == ["http://localhost:7125/server/database/item?namespace=afc_stats"]

    def test_adds_scheme_when_missing(self, monkeypatch):
        urls: List[str] = []

        def fake_urlopen(url: str, timeout: float) -> FakeResponse:
            urls.append(url)
            return FakeResponse(b"{}")

        monkeypatch.setattr(dbg.urllib.request, "urlopen", fake_urlopen)
        dbg.fetch_stats("10.0.0.2", 80)
        assert urls == ["http://10.0.0.2:80/server/database/item?namespace=afc_stats"]

    def test_connection_error(self, monkeypatch):
        def fake_urlopen(url: str, timeout: float) -> FakeResponse:
            raise OSError("down")

        monkeypatch.setattr(dbg.urllib.request, "urlopen", fake_urlopen)
        assert dbg.fetch_stats("h", 1) is None

    def test_invalid_json(self, monkeypatch):
        monkeypatch.setattr(dbg.urllib.request, "urlopen",
                            lambda url, timeout: FakeResponse(b"not json"))
        assert dbg.fetch_stats("h", 1) is None


class TestStatsReport:
    HEADER = (f"{'=' * 64}\nAFC STATS REPORT - 2026-03-04 05:06:07\n{'=' * 64}\n")

    def test_formats_aligned_rows(self, monkeypatch):
        data = {"result": {"value": {"lane1": {"cycles": 3, "dist": 1.239}, "x": "ok"}}}
        monkeypatch.setattr(dbg, "fetch_stats", lambda a, p: data)
        expected = (self.HEADER + "lane1.cycles  3\nlane1.dist    1.24\nx             ok\n\n")
        assert dbg.stats_report("h", 1, NOW) == expected

    def test_fetch_failure(self, monkeypatch):
        monkeypatch.setattr(dbg, "fetch_stats", lambda a, p: None)
        expected = self.HEADER + "Unable to retrieve AFC stats from Moonraker.\n\n"
        assert dbg.stats_report("h", 1, NOW) == expected

    def test_missing_value(self, monkeypatch):
        monkeypatch.setattr(dbg, "fetch_stats", lambda a, p: {"result": {}})
        assert "Unable to retrieve" in dbg.stats_report("h", 1, NOW)

    def test_empty_value_dict(self, monkeypatch):
        monkeypatch.setattr(dbg, "fetch_stats", lambda a, p: {"result": {"value": {}}})
        assert dbg.stats_report("h", 1, NOW) == self.HEADER + "\n"


class FakeSocket:
    def __init__(self, reply: List[bytes]) -> None:
        self.reply = list(reply)
        self.sent = b""
        self.shutdown_how: Optional[int] = None

    def __enter__(self) -> "FakeSocket":
        return self

    def __exit__(self, *args: Any) -> None:
        return None

    def sendall(self, data: bytes) -> None:
        self.sent += data

    def shutdown(self, how: int) -> None:
        self.shutdown_how = how

    def recv(self, size: int) -> bytes:
        return self.reply.pop(0) if self.reply else b""


class TestTermbinReachable:
    def test_reachable(self, monkeypatch):
        calls: List[Any] = []

        def fake_connect(addr: Any, timeout: float) -> FakeSocket:
            calls.append((addr, timeout))
            return FakeSocket([])

        monkeypatch.setattr(dbg.socket, "create_connection", fake_connect)
        assert dbg.termbin_reachable() is True
        assert calls == [(("termbin.com", 9999), 3)]

    def test_unreachable(self, monkeypatch):
        def fake_connect(addr: Any, timeout: float) -> FakeSocket:
            raise OSError("no route")

        monkeypatch.setattr(dbg.socket, "create_connection", fake_connect)
        assert dbg.termbin_reachable() is False


class TestUploadToTermbin:
    def test_returns_url_and_strips_nulls(self, monkeypatch):
        sock = FakeSocket([b"https://termbin.com/", b"abc\0\n"])
        monkeypatch.setattr(dbg.socket, "create_connection", lambda a, timeout: sock)
        assert dbg.upload_to_termbin(b"da\0ta") == "https://termbin.com/abc"
        assert sock.sent == b"data"
        assert sock.shutdown_how == dbg.socket.SHUT_WR

    def test_connection_error(self, monkeypatch):
        def fake_connect(addr: Any, timeout: float) -> FakeSocket:
            raise OSError("x")

        monkeypatch.setattr(dbg.socket, "create_connection", fake_connect)
        assert dbg.upload_to_termbin(b"data") == ""


class TestUploadFile:
    def test_success(self, monkeypatch, tmp_path):
        path = tmp_path / "klippy.log"
        path.write_bytes(b"log")
        sent: List[bytes] = []
        monkeypatch.setattr(dbg, "upload_to_termbin", lambda d: sent.append(d) or "http://u")
        assert dbg.upload_file(path) == "klippy.log: http://u"
        assert sent == [b"log"]

    def test_failure(self, monkeypatch, tmp_path):
        path = tmp_path / "klippy.log"
        path.write_bytes(b"log")
        monkeypatch.setattr(dbg, "upload_to_termbin", lambda d: "")
        assert dbg.upload_file(path) == "klippy.log: Upload failed"


class TestShouldUpload:
    @pytest.mark.parametrize("name", [
        "klippy.log.config.1", "klippy.log.shutdown.1", "AFC.log", "klippy.log"])
    def test_selected(self, name):
        assert dbg.should_upload(name) is True

    def test_each_condition_independent(self):
        assert dbg.should_upload("a_config_b") is True
        assert dbg.should_upload("a_shutdown_b") is True
        assert dbg.should_upload("a_AFC_b") is True
        assert dbg.should_upload("a_klippy.log") is True

    def test_other_names_skipped(self):
        assert dbg.should_upload("klippy.log.1") is False
        assert dbg.should_upload("random.txt") is False


class TestExtractKlipperLogs:
    def test_missing_klippy_log(self, tmp_path, capsys):
        cfg = make_cfg(tmp_path)
        temp = tmp_path / "t"
        temp.mkdir()
        dbg.extract_klipper_logs(cfg, temp)
        out = capsys.readouterr().out
        assert out == ("Extracting Klipper logs...\n"
                       f"File not found: {cfg.printer_log_dir / 'klippy.log'}\n")
        assert list(temp.iterdir()) == []

    def test_copies_logs_and_runs_extractor(self, monkeypatch, tmp_path, capsys):
        cfg = make_cfg(tmp_path)
        cfg.printer_log_dir.mkdir()
        (cfg.printer_log_dir / "klippy.log").write_text("k")
        (cfg.printer_log_dir / "AFC.log").write_text("a")
        (cfg.printer_log_dir / "AFC.log.1").write_text("a1")
        (cfg.printer_log_dir / "other.log").write_text("o")
        temp = tmp_path / "t"
        temp.mkdir()
        fake = FakeRun()
        monkeypatch.setattr(dbg.subprocess, "run", fake)
        dbg.extract_klipper_logs(cfg, temp)
        assert sorted(p.name for p in temp.iterdir()) == ["AFC.log", "AFC.log.1", "klippy.log"]
        script = str(cfg.klipper_dir / "scripts" / "logextract.py")
        assert fake.calls == [[dbg.sys.executable, script, "./klippy.log"]]
        assert fake.kwargs[0]["cwd"] == temp
        assert capsys.readouterr().out == "Extracting Klipper logs...\n"

    def test_extractor_failure_warns(self, monkeypatch, tmp_path, capsys):
        cfg = make_cfg(tmp_path)
        cfg.printer_log_dir.mkdir()
        (cfg.printer_log_dir / "klippy.log").write_text("k")
        temp = tmp_path / "t"
        temp.mkdir()
        script = str(cfg.klipper_dir / "scripts" / "logextract.py")
        fake = FakeRun({f"{dbg.sys.executable} {script} ./klippy.log": (1, "")})
        monkeypatch.setattr(dbg.subprocess, "run", fake)
        dbg.extract_klipper_logs(cfg, temp)
        assert capsys.readouterr().out == (
            "Extracting Klipper logs...\nKlipper log extraction failed.\n")


class TestSystemSections:
    def test_builds_all_sections(self, monkeypatch, tmp_path, capsys):
        cfg = make_cfg(tmp_path)
        monkeypatch.setattr(dbg, "collect_can_ids", lambda d, v: ["Interface: can0", "id", ""])
        monkeypatch.setattr(dbg, "read_distro", lambda: "DISTRO")
        monkeypatch.setattr(dbg, "get_afc_version", lambda p: "9-abc")
        monkeypatch.setattr(dbg, "get_klipper_version", lambda p: "v1")
        monkeypatch.setattr(dbg, "collect_usb_topology", lambda: "USB")
        monkeypatch.setattr(dbg.subprocess, "run", FakeRun({
            "uname -a": (0, "Linux x"),
            "uptime": (0, "up 1 day"),
            "ls -l /dev/serial/by-id/": (0, "serial-entry")}))
        sep = "=" * 64
        expected = (
            f"\n{sep}\nSystem Information\n{sep}\n"
            "Distro: DISTRO\nKernel: Linux x\nUptime: up 1 day\n"
            f"\n{sep}\nVersions\n{sep}\n"
            "AFC Version: 9-abc\nKlipper Version: v1\n"
            f"\n{sep}\nUSB Topology\n{sep}\nUSB\n"
            f"\n{sep}\nSerial IDs\n{sep}\nserial-entry\n"
            f"\n{sep}\nCAN Bus IDs\n{sep}\nInterface: can0\nid\n\n")
        assert dbg.system_sections(cfg) == expected
        assert capsys.readouterr().out == ""

    def test_no_serial_and_no_can(self, monkeypatch, tmp_path):
        cfg = make_cfg(tmp_path)
        monkeypatch.setattr(dbg, "collect_can_ids", lambda d, v: [])
        monkeypatch.setattr(dbg, "collect_usb_topology", lambda: "USB")
        monkeypatch.setattr(dbg.subprocess, "run", FakeRun({
            "ls -l /dev/serial/by-id/": (2, "")}))
        text = dbg.system_sections(cfg)
        assert text.endswith("CAN Bus IDs\n" + "=" * 64 + "\nNo CAN devices found.\n")
        assert "No serial devices found.\n" in text


class TestConfigSections:
    def test_includes_afc_and_moonraker_files(self, tmp_path):
        cfg = make_cfg(tmp_path)
        (cfg.afc_config_dir / "sub").mkdir(parents=True)
        (cfg.afc_config_dir / "AFC.cfg").write_text("afc\n")
        (cfg.afc_config_dir / "sub" / "unit.cfg").write_text("unit\n")
        cfg.moonraker_config.write_text("moon\n")
        expected = (dbg.prepout("AFC.cfg") + "afc\n"
                    + dbg.prepout("unit.cfg") + "unit\n"
                    + dbg.prepout(str(cfg.moonraker_config)) + "moon\n")
        assert dbg.config_sections(cfg) == expected

    def test_missing_dir_and_moonraker(self, tmp_path):
        cfg = make_cfg(tmp_path)
        expected = (f"File not found: {cfg.afc_config_dir}\n"
                    f"File not found: {cfg.moonraker_config}\n")
        assert dbg.config_sections(cfg) == expected


class TestIsPublicIp:
    @pytest.mark.parametrize("text", ["8.8.8.8", "1.1.1.1/32", "2606:4700:4700::1111",
                                      "93.184.216.34"])
    def test_public_addresses(self, text):
        assert dbg._is_public_ip(text) is True

    @pytest.mark.parametrize("text", ["10.1.2.3", "172.16.0.9", "192.168.1.20", "127.0.0.1",
                                      "169.254.1.1", "100.64.0.1", "0.0.0.0", "192.168.1.0/24",
                                      "::1", "fe80::1", "fd12:3456::1", "2001:db8::1"])
    def test_private_and_reserved_addresses(self, text):
        assert dbg._is_public_ip(text) is False

    @pytest.mark.parametrize("text", ["999.1.1.1", "12:34:56", "aa:bb:cc:dd:ee:ff", "nope"])
    def test_invalid_text(self, text):
        assert dbg._is_public_ip(text) is False


class TestRedactIps:
    def test_public_ipv4_replaced_and_counted(self):
        assert dbg._redact_ips("peer 8.8.8.8 up") == ("peer <public-ip> up", 1)

    def test_private_ipv4_kept(self):
        text = "trusted_clients:\n  192.168.1.0/24\n  10.0.0.5\n  127.0.0.1\n"
        assert dbg._redact_ips(text) == (text, 0)

    def test_public_network_with_prefix_replaced(self):
        assert dbg._redact_ips("allow 8.8.8.0/24 now") == ("allow <public-ip> now", 1)

    def test_port_and_trailing_period_preserved(self):
        assert dbg._redact_ips("at 8.8.8.8:7125.") == ("at <public-ip>:7125.", 1)

    def test_invalid_octets_kept(self):
        assert dbg._redact_ips("addr 999.8.8.8") == ("addr 999.8.8.8", 0)

    def test_version_like_text_with_leading_letter_kept(self):
        assert dbg._redact_ips("v8.8.8.8") == ("v8.8.8.8", 0)

    def test_public_ipv6_replaced(self):
        text = "dns 2606:4700:4700::1111 ok"
        assert dbg._redact_ips(text) == ("dns <public-ip> ok", 1)

    def test_local_ipv6_kept(self):
        text = "fe80::1 fd12:3456::1 ::1"
        assert dbg._redact_ips(text) == (text, 0)

    def test_times_and_macs_kept(self):
        text = "at 12:34:56.789 mac aa:bb:cc:dd:ee:ff"
        assert dbg._redact_ips(text) == (text, 0)

    def test_counts_every_match(self):
        text = "8.8.8.8 1.1.1.1 10.0.0.1 2606:4700:4700::1111"
        assert dbg._redact_ips(text) == (
            "<public-ip> <public-ip> 10.0.0.1 <public-ip>", 3)


class TestRedactText:
    def test_nothing_to_redact(self):
        text = "[mcu]\nserial: /dev/serial/by-id/usb-Klipper_stm32\nhost: 192.168.1.5\n"
        assert dbg.redact_text(text) == (text, {})

    @pytest.mark.parametrize("line, expected", [
        ("api_key: abc123", "api_key: <redacted>"),
        ("api_key = abc123", "api_key = <redacted>"),
        ("password: hunter 2", "password: <redacted>"),
        ("PASSWD=x", "PASSWD=<redacted>"),
        ("access_token: a.b-c", "access_token: <redacted>"),
        ("client_secret=zz", "client_secret=<redacted>"),
        ("apikey: k", "apikey: <redacted>"),
        ("passphrase: p", "passphrase: <redacted>"),
        ("Authorization: Basic Zm9vOmJhcg==", "Authorization: <redacted>"),
        ('"api_key": "abc"', '"api_key": <redacted>'),
        ("db_credentials: x:y", "db_credentials: <redacted>"),
    ])
    def test_key_value_secrets(self, line, expected):
        assert dbg.redact_text(line) == (expected, {"secret value": 1})

    def test_key_without_value_kept(self):
        assert dbg.redact_text("api_key:\nnext: 1") == ("api_key:\nnext: 1", {})

    def test_value_does_not_cross_lines(self):
        assert dbg.redact_text("token: x\nkeep: me") == ("token: <redacted>\nkeep: me",
                                                         {"secret value": 1})

    def test_non_http_notifier_url_redacted(self):
        assert dbg.redact_text("url: tgram://12345:AAEbot/9876") == (
            "url: <redacted>", {"secret value": 1})

    def test_http_url_kept(self):
        text = "url: https://example.com/hook"
        assert dbg.redact_text(text) == (text, {})

    def test_stream_url_key_not_treated_as_url_secret(self):
        text = "stream_url: rtsp://cam/stream"
        assert dbg.redact_text(text) == (text, {})

    def test_url_credentials_redacted_user_kept(self):
        text = "origin: https://bob:s3cret@github.com/x.git"
        assert dbg.redact_text(text) == (
            "origin: https://bob:<redacted>@github.com/x.git", {"secret value": 1})

    def test_url_without_password_kept(self):
        text = "origin: https://github.com/x.git user@host"
        assert dbg.redact_text(text) == (text, {})

    def test_bearer_token_redacted(self):
        assert dbg.redact_text("sent Bearer abcdef123456 ok") == (
            "sent Bearer <redacted> ok", {"secret value": 1})

    def test_short_bearer_word_kept(self):
        text = "Bearer shaft"
        assert dbg.redact_text(text) == (text, {})

    @pytest.mark.parametrize("token", [
        "ghp_" + "A" * 36,
        "github_pat_" + "b1" * 15,
        "xoxb-1234567890-abcdef",
        "AKIA" + "Z" * 16,
        "eyJhbGci.eyJzdWIi.c2lnbmF0dXJl",
    ])
    def test_token_shapes(self, token):
        assert dbg.redact_text(f"found {token} here") == (
            "found <redacted> here", {"secret value": 1})

    def test_public_ip_counted_separately(self):
        text, counts = dbg.redact_text("password: x\nhost 8.8.8.8\npeer 192.168.0.2\n")
        assert text == "password: <redacted>\nhost <public-ip>\npeer 192.168.0.2\n"
        assert counts == {"secret value": 1, "public IP address": 1}

    def test_secret_counts_accumulate_across_rules(self):
        text, counts = dbg.redact_text("token: a\nurl: x://y\nBearer abcdefgh\n")
        assert text == "token: <redacted>\nurl: <redacted>\nBearer <redacted>\n"
        assert counts == {"secret value": 3}


class TestDescribeCounts:
    def test_singular(self):
        assert dbg.describe_counts({"secret value": 1}) == "1 secret value"

    def test_plural_and_joined(self):
        counts = {"secret value": 2, "public IP address": 3}
        assert dbg.describe_counts(counts) == "2 secret values, 3 public IP addresses"

    def test_empty(self):
        assert dbg.describe_counts({}) == ""


class TestRedactFile:
    def test_rewrites_when_replaced(self, tmp_path):
        path = tmp_path / "a.log"
        path.write_text("password: x\nok 10.0.0.1\n")
        assert dbg.redact_file(path) == {"secret value": 1}
        assert path.read_text() == "password: <redacted>\nok 10.0.0.1\n"

    def test_untouched_when_clean(self, tmp_path):
        path = tmp_path / "a.log"
        data = b"clean \xff bytes\n"
        path.write_bytes(data)
        assert dbg.redact_file(path) == {}
        assert path.read_bytes() == data


class TestRedactCollected:
    def test_disabled_returns_report_unchanged(self, tmp_path, capsys):
        (tmp_path / "klippy.log").write_text("password: x\n")
        cfg = make_cfg(tmp_path, redact=False)
        assert dbg.redact_collected(cfg, "token: y", tmp_path) == "token: y"
        assert (tmp_path / "klippy.log").read_text() == "password: x\n"
        assert capsys.readouterr().out == (
            "Redaction disabled. Secrets and public IP addresses will be included.\n")

    def test_nothing_found(self, tmp_path, capsys):
        (tmp_path / "klippy.log").write_text("all good\n")
        cfg = make_cfg(tmp_path)
        assert dbg.redact_collected(cfg, "report", tmp_path) == "report"
        assert capsys.readouterr().out == (
            "Redacting secrets and public IP addresses (best effort)...\nNothing to redact.\n")

    def test_redacts_report_and_files_with_summary(self, tmp_path, capsys):
        (tmp_path / "klippy.log").write_text("peer 8.8.8.8\n")
        (tmp_path / "AFC.log").write_text("fine\n")
        cfg = make_cfg(tmp_path)
        result = dbg.redact_collected(cfg, "api_key: k\n", tmp_path)
        assert result == (
            "api_key: <redacted>\n"
            f"\n{'=' * 64}\nRedaction\n{'=' * 64}\n"
            "afc_debug.log: 1 secret value\n"
            "klippy.log: 1 public IP address\n")
        assert (tmp_path / "klippy.log").read_text() == "peer <public-ip>\n"
        assert (tmp_path / "AFC.log").read_text() == "fine\n"
        assert capsys.readouterr().out == (
            "Redacting secrets and public IP addresses (best effort)...\n"
            "Redacted afc_debug.log: 1 secret value\n"
            "Redacted klippy.log: 1 public IP address\n")

    def test_only_files_changed_still_summarised(self, tmp_path):
        (tmp_path / "klippy.log").write_text("token: z\n")
        result = dbg.redact_collected(make_cfg(tmp_path), "clean", tmp_path)
        assert result.startswith("clean\n")
        assert result.endswith("klippy.log: 1 secret value\n")
        assert "afc_debug.log" not in result


class TestWriteZip:
    def test_archive_contents(self, monkeypatch, tmp_path):
        monkeypatch.setenv("HOME", str(tmp_path))
        temp = tmp_path / "t"
        temp.mkdir()
        (temp / "klippy.log").write_text("k")
        path = dbg.write_zip("rep\0ort", temp, NOW)
        assert path == tmp_path / "afc_debug_logs_20260304_050607.zip"
        with dbg.zipfile.ZipFile(path) as archive:
            assert sorted(archive.namelist()) == ["afc_debug.log", "klippy.log"]
            assert archive.read("afc_debug.log") == b"report"
            assert archive.read("klippy.log") == b"k"


class TestConfirm:
    def test_assume_yes_skips_prompt(self, monkeypatch):
        monkeypatch.setattr("builtins.input", lambda p: pytest.fail("prompted"))
        assert dbg.confirm(True) is True

    @pytest.mark.parametrize("answer", ["y", "Y", "yes"])
    def test_yes(self, monkeypatch, answer):
        monkeypatch.setattr("builtins.input", lambda p: answer)
        assert dbg.confirm(False) is True

    @pytest.mark.parametrize("answer", ["n", "N", "no"])
    def test_no(self, monkeypatch, answer):
        monkeypatch.setattr("builtins.input", lambda p: answer)
        assert dbg.confirm(False) is False

    @pytest.mark.parametrize("answer", ["maybe", ""])
    def test_invalid(self, monkeypatch, answer):
        monkeypatch.setattr("builtins.input", lambda p: answer)
        with pytest.raises(dbg.DebugError, match="Please answer y or n."):
            dbg.confirm(False)

    def test_eof_is_invalid(self, monkeypatch):
        def raise_eof(prompt: str) -> str:
            raise EOFError

        monkeypatch.setattr("builtins.input", raise_eof)
        with pytest.raises(dbg.DebugError, match="Please answer y or n."):
            dbg.confirm(False)

    def test_prompt_text(self, monkeypatch):
        prompts: List[str] = []
        monkeypatch.setattr("builtins.input", lambda p: prompts.append(p) or "y")
        dbg.confirm(False)
        assert prompts == ["Do you wish to continue? [y/n]: "]


class TestSaveZip:
    def test_writes_zip_and_prints(self, monkeypatch, tmp_path, capsys):
        monkeypatch.setenv("HOME", str(tmp_path))
        temp = tmp_path / "t"
        temp.mkdir()
        (temp / "a.log").write_text("a")
        dbg.save_zip("rep", temp, NOW, "Because")
        zip_path = tmp_path / "afc_debug_logs_20260304_050607.zip"
        assert zip_path.is_file()
        assert capsys.readouterr().out == (
            "Because. Creating a zip archive of logs instead.\n"
            f"Logs have been saved to {zip_path}\n"
            "Please share this file with the AFCProject support team.\n")


class TestDeliver:
    def make_temp(self, tmp_path: Path) -> Path:
        temp = tmp_path / "t"
        temp.mkdir()
        (temp / "klippy.log").write_text("k")
        (temp / "klippy.log.config.1").write_text("c")
        (temp / "notes.txt").write_text("n")
        return temp

    def test_zip_mode(self, monkeypatch, tmp_path, capsys):
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setattr(dbg, "termbin_reachable", lambda: pytest.fail("checked"))
        cfg = make_cfg(tmp_path, zip_only=True)
        dbg.deliver(cfg, "rep", self.make_temp(tmp_path), NOW)
        zip_path = tmp_path / "afc_debug_logs_20260304_050607.zip"
        assert zip_path.is_file()
        assert capsys.readouterr().out == (
            "Zip mode selected. Creating a zip archive of logs instead.\n"
            f"Logs have been saved to {zip_path}\n"
            "Please share this file with the AFCProject support team.\n")

    def test_unreachable_falls_back_to_zip(self, monkeypatch, tmp_path, capsys):
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setattr(dbg, "termbin_reachable", lambda: False)
        dbg.deliver(make_cfg(tmp_path), "rep", self.make_temp(tmp_path), NOW)
        out = capsys.readouterr().out
        assert out.startswith(
            "Failed to connect to termbin.com. Creating a zip archive of logs instead.\n")

    def test_upload_success(self, monkeypatch, tmp_path, capsys):
        monkeypatch.setattr(dbg, "termbin_reachable", lambda: True)
        sent: List[bytes] = []

        def fake_upload(data: bytes) -> str:
            sent.append(data)
            return f"http://u/{len(sent)}"

        monkeypatch.setattr(dbg, "upload_to_termbin", fake_upload)
        dbg.deliver(make_cfg(tmp_path), "rep", self.make_temp(tmp_path), NOW)
        assert sent[:2] == [b"k", b"c"]
        trailer = dbg.prepout("Uploaded Extracted Klippy Configuration and Shutdown Logs",
                              "klippy.log: http://u/1", "klippy.log.config.1: http://u/2")
        assert sent[2] == ("rep" + trailer).encode()
        assert len(sent) == 3
        assert capsys.readouterr().out == (
            'Uploading "klippy.log" to termbin...\n'
            'Uploading "klippy.log.config.1" to termbin...\n'
            "Uploading logs to termbin.com...\n"
            "Logs are available at http://u/3\n"
            "Please share this URL with the AFCProject support team.\n")

    def test_no_uploadable_files_omits_trailer(self, monkeypatch, tmp_path, capsys):
        monkeypatch.setattr(dbg, "termbin_reachable", lambda: True)
        sent: List[bytes] = []
        monkeypatch.setattr(dbg, "upload_to_termbin", lambda d: sent.append(d) or "http://u")
        temp = tmp_path / "t"
        temp.mkdir()
        (temp / "notes.txt").write_text("n")
        dbg.deliver(make_cfg(tmp_path), "rep", temp, NOW)
        assert sent == [b"rep"]
        assert capsys.readouterr().out == (
            "Uploading logs to termbin.com...\n"
            "Logs are available at http://u\n"
            "Please share this URL with the AFCProject support team.\n")

    def test_main_upload_failure_falls_back_to_zip(self, monkeypatch, tmp_path, capsys):
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setattr(dbg, "termbin_reachable", lambda: True)
        monkeypatch.setattr(dbg, "upload_to_termbin", lambda d: "")
        temp = tmp_path / "t"
        temp.mkdir()
        (temp / "notes.txt").write_text("n")
        dbg.deliver(make_cfg(tmp_path), "rep", temp, NOW)
        zip_path = tmp_path / "afc_debug_logs_20260304_050607.zip"
        with zipfile.ZipFile(zip_path) as archive:
            assert sorted(archive.namelist()) == ["afc_debug.log", "notes.txt"]
            assert archive.read("afc_debug.log") == b"rep"
        assert capsys.readouterr().out == (
            "Uploading logs to termbin.com...\n"
            "Failed to upload logs to termbin.com. Creating a zip archive of logs instead.\n"
            f"Logs have been saved to {zip_path}\n"
            "Please share this file with the AFCProject support team.\n")

    def test_failed_upload_zip_excludes_url_trailer(self, monkeypatch, tmp_path):
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setattr(dbg, "termbin_reachable", lambda: True)
        monkeypatch.setattr(dbg, "upload_to_termbin", lambda d: "")
        dbg.deliver(make_cfg(tmp_path), "rep", self.make_temp(tmp_path), NOW)
        zip_path = tmp_path / "afc_debug_logs_20260304_050607.zip"
        with zipfile.ZipFile(zip_path) as archive:
            assert archive.read("afc_debug.log") == b"rep"


class TestRun:
    def patch_pipeline(self, monkeypatch, events: List[str]) -> None:
        monkeypatch.setattr(dbg, "stop_klipper", lambda s: events.append("stop"))
        monkeypatch.setattr(dbg, "start_klipper", lambda s: events.append("start"))
        monkeypatch.setattr(dbg, "extract_klipper_logs", lambda c, t: events.append("extract"))
        monkeypatch.setattr(dbg, "system_sections", lambda c: "SYS|")
        monkeypatch.setattr(dbg, "stats_report", lambda a, p, n: "STATS|")
        monkeypatch.setattr(dbg, "config_sections", lambda c: "CFG")
        monkeypatch.setattr(dbg, "redact_collected", lambda c, r, t: r + "|REDACTED")

    def test_full_run(self, monkeypatch, tmp_path, capsys):
        events: List[str] = []
        self.patch_pipeline(monkeypatch, events)
        delivered: List[Any] = []
        monkeypatch.setattr(dbg, "deliver",
                            lambda c, r, t, n: delivered.append((r, t.is_dir())))
        assert dbg.run(make_cfg(tmp_path)) == 0
        assert events == ["stop", "extract", "start"]
        assert delivered == [("SYS|STATS|CFG|REDACTED", True)]
        out = capsys.readouterr().out
        assert out.startswith(
            "This script will collect diagnostic information and upload it to AFCProject "
            "support.\n"
            "Please review the script if you have concerns about its contents.\n"
            "Klipper will be stopped during this process. Do not run this while printing.\n"
            "\nTemporary directory created at ")
        assert out.endswith("Gathering system information...\nGathering AFC statistics\n")

    def test_declined_does_nothing(self, monkeypatch, tmp_path):
        events: List[str] = []
        self.patch_pipeline(monkeypatch, events)
        monkeypatch.setattr(dbg, "confirm", lambda a: False)
        assert dbg.run(make_cfg(tmp_path)) == 0
        assert events == []

    def test_klipper_restarted_after_failure(self, monkeypatch, tmp_path):
        events: List[str] = []
        self.patch_pipeline(monkeypatch, events)

        def boom(c: Any, r: Any, t: Any, n: Any) -> None:
            raise RuntimeError("fail")

        monkeypatch.setattr(dbg, "deliver", boom)
        with pytest.raises(RuntimeError):
            dbg.run(make_cfg(tmp_path))
        assert events == ["stop", "extract", "start"]

    def test_stop_failure_skips_start(self, monkeypatch, tmp_path):
        events: List[str] = []
        self.patch_pipeline(monkeypatch, events)

        def bad_stop(s: str) -> None:
            raise dbg.DebugError("nope")

        monkeypatch.setattr(dbg, "stop_klipper", bad_stop)
        with pytest.raises(dbg.DebugError):
            dbg.run(make_cfg(tmp_path))
        assert events == []


class TestMain:
    def test_returns_run_code(self, monkeypatch):
        seen: List[dbg.Config] = []
        monkeypatch.setattr(dbg, "run", lambda c: seen.append(c) or 0)
        assert dbg.main(["-f", "-z"]) == 0
        assert seen[0].assume_yes is True
        assert seen[0].zip_only is True

    def test_debug_error_prints_and_returns_one(self, monkeypatch, capsys):
        def bad_run(cfg: dbg.Config) -> int:
            raise dbg.DebugError("something broke")

        monkeypatch.setattr(dbg, "run", bad_run)
        assert dbg.main([]) == 1
        assert capsys.readouterr().out == "something broke\n"
