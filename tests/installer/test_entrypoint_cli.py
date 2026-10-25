import io
from pathlib import Path
from typing import List, Tuple

import pytest

from installer.cli import CliArgs, build_context, help_text, parse_args, requested_branch
from installer.platform_core.errors import InstallerExit

VAR_NAMES = {
    "moonraker_address": "moonraker_address", "klipper_dir": "klipper_dir",
    "moonraker_config_file": "moonraker_config_file", "moonraker_port": "moonraker_port",
    "klipper_service": "klipper_service", "branch": "branch",
    "printer_config_dir": "printer_config_dir", "klipper_venv": "klipper_venv",
}


def parse(argv: List[str], prog: str = "install-afc.sh") -> Tuple[CliArgs, str, str, int]:
    out, err = io.StringIO(), io.StringIO()
    code = 0
    args = CliArgs()
    try:
        args = parse_args(argv, prog, out, err)
    except InstallerExit as exc:
        code = exc.code
    return args, out.getvalue(), err.getvalue(), code






class TestParseArgsErrors:
    @pytest.mark.parametrize(
        "argv,message",
        [
            (["-x"], "illegal option -- x"),
            (["-t", "-q", "-t"], "illegal option -- q"),
            (["-tz"], "illegal option -- z"),
            (["-a"], "option requires an argument -- a"),
            (["-t", "-y"], "option requires an argument -- y"),
            (["-b", "--upload-pack=x"], "invalid branch name -- --upload-pack=x"),
        ],
    )
    def test_exit_one_with_the_getopts_message(self, argv: List[str], message: str) -> None:
        _, out, err, code = parse(argv)
        assert (code, out) == (1, "")
        assert err == f"install-afc.sh: {message}\n"

    def test_help_stops_at_once_even_before_a_bad_flag(self) -> None:
        _, out, err, code = parse(["-t", "-h", "-x"])
        assert (code, err) == (0, "")
        assert out == help_text("install-afc.sh")

    def test_an_earlier_bad_flag_wins_over_help(self) -> None:
        _, out, _, code = parse(["-x", "-h"])
        assert (code, out) == (1, "")

    @pytest.mark.parametrize(
        "argv,expected",
        [
            (["-a", "http://printer", "-k", "/k", "-m", "/m.conf", "-n", "8080", "-s", "svc"],
             CliArgs(moonraker_address="http://printer", klipper_dir="/k",
                     moonraker_config_file="/m.conf", moonraker_port="8080",
                     klipper_service="svc")),
            (["-ty", "/venv"], CliArgs(klipper_venv="/venv", test_mode=True)),
            (["-a/glued", "-n", "1"], CliArgs(moonraker_address="/glued", moonraker_port="1")),
            (["-a", ""], CliArgs(moonraker_address="")),
            (["-b", "one", "-b", "two"], CliArgs(branch="two")),
            (["-t", "stop", "-b", "ignored"], CliArgs(test_mode=True)),
            (["--", "-t"], CliArgs()),
            (["-"], CliArgs()),
            (["-", "-t"], CliArgs()),
            (["-n", "-t"], CliArgs(moonraker_port="-t")),
        ],
    )
    def test_values_clusters_and_where_parsing_stops(
        self, argv: List[str], expected: CliArgs
    ) -> None:
        args, out, err, code = parse(argv)
        assert (args, out, err, code) == (expected, "", "", 0)


class TestParseArgs:
    def test_defaults(self) -> None:
        assert parse([])[0] == CliArgs()

    def test_each_flag_sets_its_own_field(self) -> None:
        args, _, _, _ = parse(
            ["-a", "A", "-k", "K", "-m", "M", "-n", "N", "-s", "S", "-b", "B", "-p", "P",
             "-y", "Y", "-t"]
        )
        assert args == CliArgs(
            moonraker_address="A", klipper_dir="K", moonraker_config_file="M",
            moonraker_port="N", klipper_service="S", branch="B", printer_config_dir="P",
            klipper_venv="Y", test_mode=True,
        )

    def test_help_writes_to_the_given_stream_and_exits_zero(self) -> None:
        _, out, err, code = parse(["-h"], prog="./x.sh")
        assert (code, err) == (0, "")
        assert out == help_text("./x.sh")

    def test_default_streams_are_stdout_and_stderr(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        with pytest.raises(InstallerExit):
            parse_args(["-h"], "p")
        with pytest.raises(InstallerExit):
            parse_args(["-x"], "p")
        captured = capsys.readouterr()
        assert captured.out == help_text("p")
        assert captured.err == "p: illegal option -- x\n"


class TestHelpText:
    def test_has_every_option_and_ends_with_a_newline(self) -> None:
        text = help_text("p")
        for flag in ("-a ", "-k ", "-m ", "-n ", "-s ", "-p ", "-b ", "-y ", "-t ", "-h "):
            assert f"\n  {flag}" in text
        assert text.endswith("[-h] \n")


class TestRequestedBranch:
    @pytest.mark.parametrize(
        "argv,expected",
        [
            ([], ""),
            (["-b", "dev"], "dev"),
            (["-t", "-b", "one", "-b", "two"], "two"),
            (["-bdev"], "dev"),
            (["-x", "-b", "dev"], ""),
            (["-b"], ""),
            (["-a", "-b", "dev"], ""),
            (["stop", "-b", "dev"], ""),
            (["-b", "-evil"], ""),
            (["-b", "dev", "-b", "-evil"], "dev"),
        ],
    )
    def test_quiet_scan(self, argv: List[str], expected: str) -> None:
        assert requested_branch(argv) == expected

    def test_nothing_is_printed(self, capsys: pytest.CaptureFixture[str]) -> None:
        requested_branch(["-x", "-h", "-b", "dev"])
        captured = capsys.readouterr()
        assert (captured.out, captured.err) == ("", "")


class TestBuildContext:
    def test_defaults(self, tmp_path: Path) -> None:
        ctx = build_context(CliArgs(), tmp_path / "afc", tmp_path, [])
        assert ctx.afc_path == tmp_path / "afc"
        assert ctx.printer_config_dir == tmp_path / "printer_data" / "config"
        assert ctx.klipper_dir == tmp_path / "klipper"
        assert ctx.klipper_venv == tmp_path / "klippy-env" / "bin"
        assert ctx.moonraker_config_file == tmp_path / "printer_data" / "config" / "moonraker.conf"
        assert (ctx.klipper_service, ctx.branch) == ("klipper", "main")
        assert (ctx.moonraker_address, ctx.moonraker_port) == ("http://localhost", "7125")
        assert ctx.test_mode is False
        assert ctx.original_args == []

    def test_every_flag_overrides_its_value(self, tmp_path: Path) -> None:
        args = CliArgs(
            moonraker_address="http://mr", klipper_dir="/k", moonraker_config_file="/m.conf",
            moonraker_port="80", klipper_service="svc", branch="dev", printer_config_dir="/pc",
            klipper_venv="/venv", test_mode=True,
        )
        ctx = build_context(args, tmp_path / "afc", tmp_path, ["-t", "-b", "dev"])
        assert ctx.moonraker_address == "http://mr"
        assert ctx.klipper_dir == Path("/k")
        assert ctx.moonraker_config_file == Path("/m.conf")
        assert ctx.moonraker_port == "80"
        assert ctx.klipper_service == "svc"
        assert ctx.branch == "dev"
        assert ctx.printer_config_dir == Path("/pc")
        assert ctx.klipper_venv == Path("/venv")
        assert ctx.test_mode is True
        assert ctx.original_args == ["-t", "-b", "dev"]

    def test_the_moonraker_config_follows_the_printer_config_dir(self, tmp_path: Path) -> None:
        ctx = build_context(CliArgs(printer_config_dir="/pc"), tmp_path / "afc", tmp_path, [])
        assert ctx.moonraker_config_file == Path("/pc/moonraker.conf")

    def test_an_explicit_moonraker_config_is_honored_over_the_default(
        self, tmp_path: Path
    ) -> None:
        args = CliArgs(printer_config_dir="/pc", moonraker_config_file="/elsewhere/mr.conf")
        ctx = build_context(args, tmp_path / "afc", tmp_path, [])
        assert ctx.moonraker_config_file == Path("/elsewhere/mr.conf")

    def test_an_empty_moonraker_config_falls_back_to_the_default(self, tmp_path: Path) -> None:
        ctx = build_context(CliArgs(moonraker_config_file=""), tmp_path / "afc", tmp_path, [])
        assert ctx.moonraker_config_file == tmp_path / "printer_data" / "config" / "moonraker.conf"

    def test_empty_path_flags_fall_back_to_the_defaults(self, tmp_path: Path) -> None:
        args = CliArgs(printer_config_dir="", klipper_dir="", klipper_venv="")
        ctx = build_context(args, tmp_path / "afc", tmp_path, [])
        assert ctx.printer_config_dir == tmp_path / "printer_data" / "config"
        assert ctx.klipper_dir == tmp_path / "klipper"
        assert ctx.klipper_venv == tmp_path / "klippy-env" / "bin"

    def test_an_explicitly_empty_text_flag_is_kept(self, tmp_path: Path) -> None:
        ctx = build_context(CliArgs(branch="", klipper_service=""), tmp_path / "afc", tmp_path, [])
        assert (ctx.branch, ctx.klipper_service) == ("", "")

    def test_afc_path_is_the_script_dir_not_under_home(self, tmp_path: Path) -> None:
        ctx = build_context(CliArgs(), Path("/opt/AFC"), tmp_path, [])
        assert ctx.afc_path == Path("/opt/AFC")
