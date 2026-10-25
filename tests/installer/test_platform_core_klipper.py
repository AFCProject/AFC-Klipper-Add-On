import json
import os
import shutil
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Callable, Dict, Iterator, List, Optional

import pytest

from installer.platform_core.context import Context
from installer.platform_core.errors import InstallerExit
from installer.platform_core.klipper_links import (
    check_init_symlink,
    exclude_from_klipper_git,
    link_extensions,
    unlink_extensions,
)
from installer.platform_core.system import (
    CommandRunner,
    query_printer_status,
    restart_klipper,
    restart_service,
    u1_restart_klipper,
    u1_write_debug_file,
)
from tests.installer.fakes import (
    FakeCommandRunner,
    PrinterTree,
    RecordingConsole,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git required")
EXCLUDE = Path(".git") / "info" / "exclude"
NOT_IDLE_GENERIC = (
    "WARNING",
    "Your printer is not idle/ready, or its status could not be confirmed. Automatic "
    "Klipper restart has been skipped; please restart the Klipper service manually "
    "once the printer is idle.",
)
NOT_IDLE_SNAPMAKER = (
    "WARNING",
    "Please manually restart klipper through <ip_address>/firmware-config panel, "
    "or fully power-cycle printer.",
)
STOCK_HEADER = (
    "# Package definition for the extras directory\n"
    "#\n"
    "# Copyright (C) 2018  Kevin O'Connor <kevin@koconnor.net>\n"
    "#\n"
    "# This file may be distributed under the terms of the GNU GPLv3 license.\n"
)


def make_ctx(tree: PrinterTree, **overrides: object) -> Context:
    return Context.create(tree.afc_path, home=tree.home, **overrides)  # type: ignore[arg-type]


def links_in(directory: Path) -> Dict[str, str]:
    return {p.name: os.readlink(p) for p in sorted(directory.iterdir()) if p.is_symlink()}


# --- link / unlink ---------------------------------------------------------------------


class TestLinkExtensions:
    def test_links_every_py_file_except_init(self, printer_tree: PrinterTree) -> None:
        ctx = make_ctx(printer_tree)
        assert link_extensions(ctx) is None
        extras = printer_tree.afc_path / "extras"
        assert links_in(printer_tree.klipper_extras) == {
            "AFC.py": str(extras / "AFC.py"),
            "AFC_lane.py": str(extras / "AFC_lane.py"),
            "AFC_unit.py": str(extras / "AFC_unit.py"),
        }
        assert not (printer_tree.klipper_extras / "__init__.py").is_symlink()

    def test_relinking_replaces_existing_links_and_regular_files(
        self, printer_tree: PrinterTree
    ) -> None:
        ctx = make_ctx(printer_tree)
        link_extensions(ctx)
        (printer_tree.klipper_extras / "AFC.py").unlink()
        (printer_tree.klipper_extras / "AFC.py").write_text("stale regular file")
        (printer_tree.klipper_extras / "AFC_lane.py").unlink()
        (printer_tree.klipper_extras / "AFC_lane.py").symlink_to("/elsewhere")
        link_extensions(ctx)
        links = links_in(printer_tree.klipper_extras)
        extras = printer_tree.afc_path / "extras"
        assert links["AFC.py"] == str(extras / "AFC.py")
        assert links["AFC_lane.py"] == str(extras / "AFC_lane.py")

    def test_ignores_non_py_files_and_names_ending_in_init(
        self, printer_tree: PrinterTree
    ) -> None:
        extras = printer_tree.afc_path / "extras"
        (extras / "notes.txt").write_text("")
        (extras / "foo__init__.py").write_text("")
        link_extensions(make_ctx(printer_tree))
        assert "notes.txt" not in links_in(printer_tree.klipper_extras)
        assert "foo__init__.py" not in links_in(printer_tree.klipper_extras)

    def test_missing_klipper_extras_returns_a_message_that_is_never_shown(
        self, printer_tree: PrinterTree
    ) -> None:
        # this text is never shown to the user; callers must not print it
        ctx = make_ctx(printer_tree, klipper_dir=printer_tree.home / "nowhere")
        assert link_extensions(ctx) == (
            "AFC Klipper extensions not installed; Klipper extras directory not found."
        )


class TestUnlinkExtensions:
    def test_removes_the_linked_files_but_keeps_init_and_others(
        self, printer_tree: PrinterTree
    ) -> None:
        ctx = make_ctx(printer_tree)
        link_extensions(ctx)
        (printer_tree.klipper_extras / "klipper_own.py").write_text("keep")
        unlink_extensions(ctx, RecordingConsole())
        assert sorted(p.name for p in printer_tree.klipper_extras.iterdir()) == [
            "__init__.py",
            "klipper_own.py",
        ]

    def test_removes_a_regular_file_with_the_same_name_like_rm_f(
        self, printer_tree: PrinterTree
    ) -> None:
        (printer_tree.klipper_extras / "AFC.py").write_text("not a link")
        unlink_extensions(make_ctx(printer_tree), RecordingConsole())
        assert not (printer_tree.klipper_extras / "AFC.py").exists()

    def test_missing_names_are_fine(self, printer_tree: PrinterTree) -> None:
        unlink_extensions(make_ctx(printer_tree), RecordingConsole())

    def test_missing_extras_directory_is_an_error_exit(
        self, printer_tree: PrinterTree
    ) -> None:
        ctx = make_ctx(printer_tree, klipper_dir=printer_tree.home / "nowhere")
        console = RecordingConsole()
        with pytest.raises(InstallerExit) as exc:
            unlink_extensions(ctx, console)
        assert exc.value.code == 1
        assert console.messages == [
            (
                "ERROR",
                "AFC Klipper extensions not uninstalled; Klipper extras directory not found.",
            )
        ]


# --- exclude from klipper git ------------------------------------------------------------


class TestExcludeFromKlipperGit:
    def lines(self, tree: PrinterTree) -> List[str]:
        return (tree.klipper_dir / EXCLUDE).read_text().splitlines()

    def test_adds_each_extension_relative_to_klippy_extras(
        self, printer_tree: PrinterTree
    ) -> None:
        exclude_from_klipper_git(make_ctx(printer_tree), RecordingConsole())
        assert sorted(self.lines(printer_tree)) == [
            "klippy/extras/AFC.py",
            "klippy/extras/AFC_lane.py",
            "klippy/extras/AFC_unit.py",
        ]

    def test_running_twice_does_not_duplicate_entries(self, printer_tree: PrinterTree) -> None:
        ctx = make_ctx(printer_tree)
        exclude_from_klipper_git(ctx, RecordingConsole())
        exclude_from_klipper_git(ctx, RecordingConsole())
        assert len(self.lines(printer_tree)) == 3

    def test_existing_entries_are_kept_and_not_repeated(self, printer_tree: PrinterTree) -> None:
        (printer_tree.klipper_dir / EXCLUDE).write_text("*.pyc\nklippy/extras/AFC.py\n")
        exclude_from_klipper_git(make_ctx(printer_tree), RecordingConsole())
        lines = self.lines(printer_tree)
        assert lines[:2] == ["*.pyc", "klippy/extras/AFC.py"]
        assert sorted(lines[2:]) == ["klippy/extras/AFC_lane.py", "klippy/extras/AFC_unit.py"]

    def test_match_is_a_whole_line_not_a_substring(self, printer_tree: PrinterTree) -> None:
        (printer_tree.klipper_dir / EXCLUDE).write_text("# klippy/extras/AFC.py\n")
        exclude_from_klipper_git(make_ctx(printer_tree), RecordingConsole())
        assert "klippy/extras/AFC.py" in self.lines(printer_tree)

    def test_walks_subdirectories_and_flattens_to_the_basename(
        self, printer_tree: PrinterTree
    ) -> None:
        sub = printer_tree.afc_path / "extras" / "sub"
        sub.mkdir()
        (sub / "deep.py").write_text("")
        (sub / "__init__.py").write_text("")
        exclude_from_klipper_git(make_ctx(printer_tree), RecordingConsole())
        lines = self.lines(printer_tree)
        assert "klippy/extras/deep.py" in lines
        assert not any("__init__" in line for line in lines)

    def test_same_name_in_two_directories_is_listed_once(
        self, printer_tree: PrinterTree
    ) -> None:
        sub = printer_tree.afc_path / "extras" / "sub"
        sub.mkdir()
        (sub / "AFC.py").write_text("")
        exclude_from_klipper_git(make_ctx(printer_tree), RecordingConsole())
        assert self.lines(printer_tree).count("klippy/extras/AFC.py") == 1

    def test_creates_the_exclude_file_when_only_the_directory_exists(
        self, printer_tree: PrinterTree
    ) -> None:
        (printer_tree.klipper_dir / EXCLUDE).unlink()
        exclude_from_klipper_git(make_ctx(printer_tree), RecordingConsole())
        assert len(self.lines(printer_tree)) == 3

    def test_a_missing_trailing_newline_is_not_repaired(
        self, printer_tree: PrinterTree
    ) -> None:
        (printer_tree.klipper_dir / EXCLUDE).write_text("foo")
        exclude_from_klipper_git(make_ctx(printer_tree), RecordingConsole())
        assert self.lines(printer_tree)[0].startswith("fooklippy/extras/")

    def test_klipper_without_a_git_directory_is_an_error_exit(
        self, printer_tree: PrinterTree
    ) -> None:
        shutil.rmtree(printer_tree.klipper_dir / ".git")
        console = RecordingConsole()
        with pytest.raises(InstallerExit) as exc:
            exclude_from_klipper_git(make_ctx(printer_tree), console)
        assert exc.value.code == 1
        path = printer_tree.klipper_dir / EXCLUDE
        assert console.messages == [
            ("ERROR", f"Unable to update {path}: No such file or directory")
        ]


# --- __init__.py symlink repair ----------------------------------------------------------


class TestCheckInitSymlink:
    def init_link(self, tree: PrinterTree) -> Path:
        return tree.klipper_extras / "__init__.py"

    def point_at_afc(self, tree: PrinterTree) -> None:
        self.init_link(tree).unlink()
        self.init_link(tree).symlink_to(tree.afc_path / "extras" / "__init__.py")

    def test_regular_file_is_left_alone(self, printer_tree: PrinterTree) -> None:
        runner = FakeCommandRunner()
        check_init_symlink(make_ctx(printer_tree), runner)
        assert runner.calls == []
        assert self.init_link(printer_tree).read_text().startswith("# stock klipper")

    def test_symlink_to_some_other_file_is_left_alone(self, printer_tree: PrinterTree) -> None:
        other = printer_tree.home / "other.py"
        other.write_text("x")
        self.init_link(printer_tree).unlink()
        self.init_link(printer_tree).symlink_to(other)
        runner = FakeCommandRunner()
        check_init_symlink(make_ctx(printer_tree), runner)
        assert runner.calls == []
        assert self.init_link(printer_tree).is_symlink()

    def test_symlink_to_afc_init_is_removed_and_restored_with_git(
        self, printer_tree: PrinterTree
    ) -> None:
        self.point_at_afc(printer_tree)
        runner = FakeCommandRunner()
        check_init_symlink(make_ctx(printer_tree), runner)
        assert runner.calls == [
            [
                "git", "-C", str(printer_tree.klipper_dir),
                "checkout", "--", "klippy/extras/__init__.py",
            ]
        ]
        assert not self.init_link(printer_tree).exists()

    def test_when_git_fails_the_stock_header_is_written(
        self, printer_tree: PrinterTree
    ) -> None:
        self.point_at_afc(printer_tree)
        failing = subprocess.CompletedProcess([], 1, stdout="", stderr="")
        runner = FakeCommandRunner(
            results={
                (
                    "git", "-C", str(printer_tree.klipper_dir),
                    "checkout", "--", "klippy/extras/__init__.py",
                ): failing
            }
        )
        check_init_symlink(make_ctx(printer_tree), runner)
        link = self.init_link(printer_tree)
        assert not link.is_symlink()
        assert link.read_text() == STOCK_HEADER

    def test_klipper_extras_symlinked_wholesale_to_afc_leaves_afc_init_intact(
        self, printer_tree: PrinterTree
    ) -> None:
        # __init__.py resolves to AFC's file but is not itself a symlink: only the
        # directory is. Both conditions must hold before anything is removed.
        shutil.rmtree(printer_tree.klipper_extras)
        printer_tree.klipper_extras.symlink_to(printer_tree.afc_path / "extras")
        afc_init = printer_tree.afc_path / "extras" / "__init__.py"
        runner = FakeCommandRunner()
        check_init_symlink(make_ctx(printer_tree), runner)
        assert runner.calls == []
        assert afc_init.is_file()

    def test_no_init_file_at_all_does_nothing(self, printer_tree: PrinterTree) -> None:
        self.init_link(printer_tree).unlink()
        runner = FakeCommandRunner()
        check_init_symlink(make_ctx(printer_tree), runner)
        assert runner.calls == []

    @needs_git
    def test_with_real_git_the_committed_file_is_restored(
        self, printer_tree: PrinterTree
    ) -> None:
        klipper = printer_tree.klipper_dir
        env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
               "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
        shutil.rmtree(klipper / ".git")
        for cmd in (["init", "-q"], ["add", "klippy/extras/__init__.py"],
                    ["commit", "-q", "-m", "stock"]):
            subprocess.run(["git", "-C", str(klipper), *cmd], check=True, env=env,
                           capture_output=True)
        committed = self.init_link(printer_tree).read_text()
        self.point_at_afc(printer_tree)
        check_init_symlink(make_ctx(printer_tree), CommandRunner())
        assert not self.init_link(printer_tree).is_symlink()
        assert self.init_link(printer_tree).read_text() == committed


# --- service restart and Moonraker status -------------------------------------------------


class TestRestartService:
    def test_uses_systemctl_when_available(self) -> None:
        runner = FakeCommandRunner(existing=["systemctl"])
        restart_service(runner, "moonraker")
        assert runner.calls == [["sudo", "systemctl", "restart", "moonraker"]]

    def test_falls_back_to_the_service_command(self) -> None:
        runner = FakeCommandRunner()
        restart_service(runner, "moonraker")
        assert runner.calls == [["sudo", "service", "moonraker", "restart"]]

    def test_a_failing_restart_exits_with_the_command_status(self) -> None:
        failing = subprocess.CompletedProcess([], 5, stdout="", stderr="")
        runner = FakeCommandRunner(
            results={("sudo", "service", "klipper", "restart"): failing}
        )
        with pytest.raises(InstallerExit) as exc:
            restart_service(runner, "klipper")
        assert exc.value.code == 5


class _Handler(BaseHTTPRequestHandler):
    body = b""
    status = 200
    seen: List[str] = []

    def do_GET(self) -> None:  # noqa: N802
        type(self).seen.append(self.path)
        self.send_response(type(self).status)
        self.end_headers()
        self.wfile.write(type(self).body)

    def log_message(self, *args: object) -> None:
        pass


@pytest.fixture
def moonraker() -> Iterator[Callable[[bytes, int], str]]:
    servers: List[HTTPServer] = []

    def start(body: bytes, status: int = 200) -> str:
        handler = type("H", (_Handler,), {"body": body, "status": status, "seen": []})
        server = HTTPServer(("127.0.0.1", 0), handler)
        servers.append(server)
        threading.Thread(
            target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
        ).start()
        start.handler = handler  # type: ignore[attr-defined]
        return f"http://127.0.0.1:{server.server_address[1]}"

    yield start  # type: ignore[misc]
    for server in servers:
        server.shutdown()
        server.server_close()


def idle_json(state: object) -> bytes:
    return json.dumps({"result": {"status": {"idle_timeout": {"state": state}}}}).encode()


class TestQueryPrinterStatus:
    @pytest.mark.parametrize("state", ["Ready", "Idle", "idle", "Printing "])
    def test_any_state_but_printing_counts_as_idle(
        self, moonraker: Callable[[bytes, int], str], state: str
    ) -> None:
        assert query_printer_status(moonraker(idle_json(state), 200)) is True

    def test_printing_is_not_idle(self, moonraker: Callable[[bytes, int], str]) -> None:
        assert query_printer_status(moonraker(idle_json("Printing"), 200)) is False

    @pytest.mark.parametrize("state", [None, "", "null"])
    def test_null_or_empty_state_is_unknown_so_not_idle(
        self, moonraker: Callable[[bytes, int], str], state: Optional[str]
    ) -> None:
        assert query_printer_status(moonraker(idle_json(state), 200)) is False

    @pytest.mark.parametrize(
        "body",
        [b"not json", b"{}", b'{"result": {}}', b'{"result": {"status": {}}}', b"[]", b"null"],
    )
    def test_malformed_or_incomplete_responses_are_not_idle(
        self, moonraker: Callable[[bytes, int], str], body: bytes
    ) -> None:
        assert query_printer_status(moonraker(body, 200)) is False

    def test_the_body_is_parsed_even_on_an_http_error_status_like_curl_s(
        self, moonraker: Callable[[bytes, int], str]
    ) -> None:
        assert query_printer_status(moonraker(idle_json("Ready"), 500)) is True

    def test_a_moonraker_error_object_is_not_idle(
        self, moonraker: Callable[[bytes, int], str]
    ) -> None:
        body = b'{"error": {"code": 404, "message": "Not Found"}}'
        assert query_printer_status(moonraker(body, 404)) is False

    @pytest.mark.parametrize("url", ["file:///etc/hostname", "ftp://host", "localhost"])
    def test_a_non_http_address_is_never_requested(self, url: str) -> None:
        assert query_printer_status(url) is False

    def test_connection_refused_is_not_idle(self) -> None:
        assert query_printer_status("http://127.0.0.1:1") is False

    def test_requests_the_idle_timeout_object(
        self, moonraker: Callable[[bytes, int], str]
    ) -> None:
        url = moonraker(idle_json("Ready"), 200)
        query_printer_status(url)
        seen = moonraker.handler.seen  # type: ignore[attr-defined]
        assert seen == ["/printer/objects/query?idle_timeout"]


class TestU1RestartKlipper:
    def test_non_root_uses_su(self) -> None:
        runner = FakeCommandRunner()
        u1_restart_klipper(runner, euid=1000)
        assert runner.calls == [["su", "root", "-c", "/etc/init.d/S60klipper restart"]]

    def test_root_runs_the_init_script_directly(self) -> None:
        runner = FakeCommandRunner()
        u1_restart_klipper(runner, euid=0)
        assert runner.calls == [["/etc/init.d/S60klipper", "restart"]]


class TestU1WriteDebugFile:
    def test_non_root_announces_and_uses_su(self, tmp_path: Path) -> None:
        console, runner = RecordingConsole(), FakeCommandRunner()
        u1_write_debug_file(console, runner, euid=1000, oem_dir=tmp_path)
        assert console.messages == [("OTHER", "Writing .debug file to /oem/ directory")]
        assert runner.calls == [["su", "root", "-c", f"touch {tmp_path}/.debug"]]
        assert not (tmp_path / ".debug").exists()

    def test_root_creates_the_file_when_missing(self, tmp_path: Path) -> None:
        console, runner = RecordingConsole(), FakeCommandRunner()
        u1_write_debug_file(console, runner, euid=0, oem_dir=tmp_path)
        assert console.messages == [("OTHER", "Adding /oem/.debug file")]
        assert (tmp_path / ".debug").is_file()
        assert runner.calls == []

    def test_root_does_nothing_when_the_file_exists(self, tmp_path: Path) -> None:
        (tmp_path / ".debug").write_text("keep")
        console = RecordingConsole()
        u1_write_debug_file(console, FakeCommandRunner(), euid=0, oem_dir=tmp_path)
        assert console.messages == []
        assert (tmp_path / ".debug").read_text() == "keep"


class TestRestartKlipper:
    def idle(self, _url: str) -> bool:
        return True

    def busy(self, _url: str) -> bool:
        return False

    def test_idle_restarts_the_klipper_service_with_systemctl(
        self, printer_tree: PrinterTree
    ) -> None:
        runner = FakeCommandRunner(existing=["systemctl"])
        console = RecordingConsole()
        restart_klipper(make_ctx(printer_tree), console, runner, self.idle)
        assert runner.calls == [["sudo", "systemctl", "restart", "klipper"]]
        assert console.messages == []

    def test_idle_without_systemctl_uses_the_service_command(
        self, printer_tree: PrinterTree
    ) -> None:
        runner = FakeCommandRunner()
        restart_klipper(make_ctx(printer_tree), RecordingConsole(), runner, self.idle)
        assert runner.calls == [["sudo", "service", "klipper", "restart"]]

    def test_the_literal_service_name_klipper_is_used_not_the_dash_s_value(
        self, printer_tree: PrinterTree
    ) -> None:
        runner = FakeCommandRunner(existing=["systemctl"])
        ctx = make_ctx(printer_tree, klipper_service="klipper-2")
        restart_klipper(ctx, RecordingConsole(), runner, self.idle)
        assert runner.calls == [["sudo", "systemctl", "restart", "klipper"]]

    def test_idle_snapmaker_restarts_through_the_u1_script(
        self, printer_tree: PrinterTree
    ) -> None:
        ctx = make_ctx(printer_tree)
        ctx.is_snapmaker = True
        runner = FakeCommandRunner()
        restart_klipper(ctx, RecordingConsole(), runner, self.idle, euid=1000)
        assert runner.calls == [["su", "root", "-c", "/etc/init.d/S60klipper restart"]]

    def test_busy_printer_skips_the_restart_with_a_warning(
        self, printer_tree: PrinterTree
    ) -> None:
        runner, console = FakeCommandRunner(), RecordingConsole()
        restart_klipper(make_ctx(printer_tree), console, runner, self.busy)
        assert runner.calls == []
        assert console.messages == [NOT_IDLE_GENERIC]

    def test_busy_snapmaker_gets_the_firmware_config_hint(
        self, printer_tree: PrinterTree
    ) -> None:
        ctx = make_ctx(printer_tree)
        ctx.is_snapmaker = True
        runner, console = FakeCommandRunner(), RecordingConsole()
        restart_klipper(ctx, console, runner, self.busy)
        assert runner.calls == []
        assert console.messages == [NOT_IDLE_SNAPMAKER]

    def test_the_status_query_receives_the_moonraker_url(
        self, printer_tree: PrinterTree
    ) -> None:
        seen: List[str] = []
        ctx = make_ctx(printer_tree, moonraker_address="http://h", moonraker_port="9")
        restart_klipper(ctx, RecordingConsole(), FakeCommandRunner(),
                        lambda url: seen.append(url) or True)
        assert seen == ["http://h:9"]

    def test_default_status_query_is_the_real_one(self, printer_tree: PrinterTree) -> None:
        ctx = make_ctx(printer_tree, moonraker_address="http://127.0.0.1", moonraker_port="1")
        console, runner = RecordingConsole(), FakeCommandRunner()
        restart_klipper(ctx, console, runner)
        assert runner.calls == []
        assert console.messages == [NOT_IDLE_GENERIC]
