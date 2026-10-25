import re
from pathlib import Path

import pytest

from installer.platform_core.constants import SNAPMAKER_KLIPPER_VENV
from installer.platform_core.context import Context
from installer.platform_core.platform_info import PlatformInfo, detect_platform

REPO_ROOT = Path(__file__).resolve().parents[2]
AFC = Path("/somewhere/else/AFC-Klipper-Add-On")




class TestContextCreate:
    def test_flags_start_in_their_default_state(self, tmp_path: Path) -> None:
        ctx = Context.create(AFC, home=tmp_path)
        assert ctx.git_install is True
        assert ctx.test_mode is False
        assert ctx.prior_installation is False
        assert ctx.files_updated_or_installed is False
        assert ctx.is_k1_os is False
        assert ctx.is_snapmaker is False
        assert ctx.original_args == []

    def test_afc_path_is_the_argument_and_never_derived_from_home(
        self, tmp_path: Path
    ) -> None:
        ctx = Context.create(AFC, home=tmp_path)
        assert ctx.afc_path == AFC
        assert str(tmp_path) not in str(ctx.afc_path)

    def test_home_defaults_to_the_home_env_var_read_at_call_time(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("HOME", str(tmp_path))
        ctx = Context.create(AFC)
        assert ctx.printer_config_dir == tmp_path / "printer_data" / "config"

    def test_the_home_directory_is_kept_for_the_moonraker_tilde_path(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        assert Context.create(AFC, home=tmp_path).home == tmp_path
        monkeypatch.setenv("HOME", str(tmp_path / "other"))
        assert Context.create(AFC).home == tmp_path / "other"

    def test_overrides_replace_defaults(self, tmp_path: Path) -> None:
        ctx = Context.create(
            AFC,
            home=tmp_path,
            printer_config_dir=Path("/p"),
            klipper_dir=Path("/k"),
            klipper_venv=Path("/v"),
            klipper_service="klipper-2",
            moonraker_address="http://printer",
            moonraker_port="7126",
            branch="DEV",
        )
        assert ctx.printer_config_dir == Path("/p")
        assert ctx.klipper_dir == Path("/k")
        assert ctx.klipper_venv == Path("/v")
        assert ctx.klipper_service == "klipper-2"
        assert ctx.moonraker == "http://printer:7126"
        assert ctx.branch == "DEV"

    def test_moonraker_config_defaults_to_the_overridden_printer_config_dir(
        self, tmp_path: Path
    ) -> None:
        ctx = Context.create(AFC, home=tmp_path, printer_config_dir=Path("/p"))
        assert ctx.moonraker_config_file == Path("/p/moonraker.conf")

    def test_explicit_moonraker_config_is_honored_not_overwritten(
        self, tmp_path: Path
    ) -> None:
        ctx = Context.create(
            AFC, home=tmp_path, moonraker_config_file=Path("/etc/moon.conf")
        )
        assert ctx.moonraker_config_file == Path("/etc/moon.conf")

    def test_backup_date_is_fourteen_digits_and_can_be_injected(
        self, tmp_path: Path
    ) -> None:
        assert re.fullmatch(r"\d{14}", Context.create(AFC, home=tmp_path).backup_date)
        fixed = Context.create(AFC, home=tmp_path, backup_date="20260101120000")
        assert fixed.backup_date == "20260101120000"

    def test_original_args_are_copied_not_shared(self, tmp_path: Path) -> None:
        args = ["-t", "-b", "DEV"]
        ctx = Context.create(AFC, home=tmp_path, original_args=args)
        args.append("-h")
        assert ctx.original_args == ["-t", "-b", "DEV"]
        assert Context.create(AFC, home=tmp_path).original_args == []

    def test_test_mode_and_git_install_overrides(self, tmp_path: Path) -> None:
        ctx = Context.create(AFC, home=tmp_path, test_mode=True, git_install=False)
        assert ctx.test_mode is True
        assert ctx.git_install is False


class TestContextDerivedPaths:
    def test_derived_paths_follow_the_fields_when_they_change(
        self, tmp_path: Path
    ) -> None:
        ctx = Context.create(AFC, home=tmp_path)
        ctx.printer_config_dir = Path("/new/config")
        ctx.klipper_dir = Path("/new/klipper")
        ctx.moonraker_address = "http://h"
        ctx.moonraker_port = "1"
        assert ctx.afc_config_dir == Path("/new/config/AFC")
        assert ctx.afc_file == Path("/new/config/AFC/AFC.cfg")
        assert ctx.klipper_extra_path == Path("/new/klipper/klippy/extras")
        assert ctx.moonraker == "http://h:1"


class TestContextApplyPlatform:
    def test_plain_linux_changes_nothing(self, tmp_path: Path) -> None:
        ctx = Context.create(AFC, home=tmp_path)
        before = ctx.klipper_venv
        ctx.apply_platform(PlatformInfo(False, False))
        assert (ctx.is_k1_os, ctx.is_snapmaker) == (False, False)
        assert ctx.klipper_venv == before

    def test_k1_without_snapmaker_keeps_the_venv(self, tmp_path: Path) -> None:
        ctx = Context.create(AFC, home=tmp_path, klipper_venv=Path("/custom"))
        ctx.apply_platform(PlatformInfo(True, False))
        assert (ctx.is_k1_os, ctx.is_snapmaker) == (True, False)
        assert ctx.klipper_venv == Path("/custom")

    def test_snapmaker_forces_usr_bin_even_over_a_user_supplied_venv(
        self, tmp_path: Path
    ) -> None:
        ctx = Context.create(AFC, home=tmp_path, klipper_venv=Path("/custom"))
        ctx.apply_platform(PlatformInfo(True, True))
        assert (ctx.is_k1_os, ctx.is_snapmaker) == (True, True)
        assert ctx.klipper_venv == Path(SNAPMAKER_KLIPPER_VENV) == Path("/usr/bin")


class TestDetectPlatform:
    def write(self, tmp_path: Path, text: str) -> Path:
        path = tmp_path / "os-release"
        path.write_text(text)
        return path

    def test_missing_file_is_plain_linux(self, tmp_path: Path) -> None:
        assert detect_platform(tmp_path / "nope") == (False, False)

    def test_unreadable_path_is_plain_linux(self, tmp_path: Path) -> None:
        assert detect_platform(tmp_path) == (False, False)  # a directory

    def test_ordinary_distro_is_plain_linux(self, tmp_path: Path) -> None:
        path = self.write(tmp_path, 'NAME="Debian"\nID=debian\n')
        assert detect_platform(path) == PlatformInfo(False, False)

    def test_buildroot_is_k1_but_not_snapmaker(self, tmp_path: Path) -> None:
        path = self.write(tmp_path, "NAME=Buildroot\nID=buildroot\n")
        assert detect_platform(path) == PlatformInfo(True, False)

    def test_buildroot_with_snapmaker_build_info_is_snapmaker(
        self, tmp_path: Path
    ) -> None:
        path = self.write(
            tmp_path, 'ID=buildroot\nRK_BUILD_INFO="snapmaker u1 2026"\n'
        )
        assert detect_platform(path) == PlatformInfo(True, True)

    def test_snapmaker_marker_alone_without_buildroot_is_not_snapmaker(
        self, tmp_path: Path
    ) -> None:
        path = self.write(tmp_path, 'ID=debian\nRK_BUILD_INFO="snapmaker x"\n')
        assert detect_platform(path) == PlatformInfo(False, False)

    def test_match_is_a_substring_like_grep_dash_f(self, tmp_path: Path) -> None:
        path = self.write(tmp_path, "VERSION_ID=buildroot2023\n")
        assert detect_platform(path).is_k1_os is True

    def test_non_utf8_bytes_do_not_break_detection(self, tmp_path: Path) -> None:
        path = tmp_path / "os-release"
        path.write_bytes(b"NAME=\xff\xfe\nID=buildroot\n")
        assert detect_platform(path) == PlatformInfo(True, False)

    def test_default_path_is_etc_os_release(self, monkeypatch: pytest.MonkeyPatch) -> None:
        seen = []
        real_read = Path.read_bytes

        def spy(self: Path) -> bytes:
            seen.append(str(self))
            return real_read(self)

        monkeypatch.setattr(Path, "read_bytes", spy)
        detect_platform()
        assert seen == ["/etc/os-release"]

    def test_result_unpacks_as_a_plain_tuple(self, tmp_path: Path) -> None:
        is_k1, is_snap = detect_platform(tmp_path / "nope")
        assert (is_k1, is_snap) == (False, False)
