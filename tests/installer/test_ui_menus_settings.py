from pathlib import Path
from typing import List, Sequence, Tuple

import pytest

from installer.platform_core.context import Context
from installer.platform_core.errors import InstallerExit
from installer.ui_menus.main_menu import MainMenu
from installer.ui_menus.settings import (
    SETTINGS,
    parse_moonraker_address,
    set_klipper_dir,
    set_klipper_service,
    set_moonraker_address,
    set_moonraker_config_file,
    set_printer_config_dir,
)
from installer.ui_menus.state import MenuState
from installer.unit_registry.selection import UnitSelection
from tests.installer.test_ui_menus_basic import fake_deps


def make_ctx(tmp_path: Path) -> Context:
    ctx = Context.create(afc_path=tmp_path / "afc", home=tmp_path)
    ctx.printer_config_dir.mkdir(parents=True)
    ctx.klipper_dir.mkdir(parents=True)
    return ctx


class TestSetPrinterConfigDir:
    def test_updates_the_directory(self, tmp_path: Path) -> None:
        ctx = make_ctx(tmp_path)
        other = tmp_path / "other"
        other.mkdir()
        message = set_printer_config_dir(ctx, f" {other} ")
        assert message == f"Printer config directory set to {other}"
        assert ctx.printer_config_dir == other
        assert ctx.afc_config_dir == other / "AFC"

    def test_the_default_moonraker_config_moves_with_it(self, tmp_path: Path) -> None:
        ctx = make_ctx(tmp_path)
        other = tmp_path / "other"
        other.mkdir()
        set_printer_config_dir(ctx, str(other))
        assert ctx.moonraker_config_file == other / "moonraker.conf"

    def test_a_chosen_moonraker_config_stays_put(self, tmp_path: Path) -> None:
        ctx = make_ctx(tmp_path)
        ctx.moonraker_config_file = tmp_path / "mine.conf"
        other = tmp_path / "other"
        other.mkdir()
        set_printer_config_dir(ctx, str(other))
        assert ctx.moonraker_config_file == tmp_path / "mine.conf"

    def test_a_missing_directory_is_rejected_and_nothing_changes(self, tmp_path: Path) -> None:
        ctx = make_ctx(tmp_path)
        before = (ctx.printer_config_dir, ctx.moonraker_config_file)
        with pytest.raises(ValueError) as exc:
            set_printer_config_dir(ctx, str(tmp_path / "nope"))
        assert str(exc.value) == f"Directory not found: {tmp_path / 'nope'}"
        assert (ctx.printer_config_dir, ctx.moonraker_config_file) == before

    def test_a_tilde_is_expanded(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HOME", str(tmp_path))
        ctx = make_ctx(tmp_path)
        (tmp_path / "cfg").mkdir()
        set_printer_config_dir(ctx, "~/cfg")
        assert ctx.printer_config_dir == tmp_path / "cfg"


class TestSetKlipperDir:
    def test_updates_the_directory_and_the_extras_path(self, tmp_path: Path) -> None:
        ctx = make_ctx(tmp_path)
        other = tmp_path / "klipper2"
        other.mkdir()
        assert set_klipper_dir(ctx, str(other)) == f"Klipper directory set to {other}"
        assert ctx.klipper_dir == other
        assert ctx.klipper_extra_path == other / "klippy" / "extras"

    def test_a_missing_directory_is_rejected(self, tmp_path: Path) -> None:
        ctx = make_ctx(tmp_path)
        with pytest.raises(ValueError):
            set_klipper_dir(ctx, str(tmp_path / "nope"))
        assert ctx.klipper_dir == tmp_path / "klipper"

    def test_the_prior_install_flag_is_rechecked_for_the_new_directory(
        self, tmp_path: Path
    ) -> None:
        ctx = make_ctx(tmp_path)
        (ctx.afc_path / "extras").mkdir(parents=True)
        (ctx.afc_path / "extras" / "AFC.py").write_text("")
        other = tmp_path / "klipper2"
        (other / "klippy" / "extras").mkdir(parents=True)
        (other / "klippy" / "extras" / "AFC.py").symlink_to(ctx.afc_path / "extras" / "AFC.py")
        ctx.prior_installation = False
        set_klipper_dir(ctx, str(other))
        assert ctx.prior_installation is True
        clean = tmp_path / "klipper3"
        clean.mkdir()
        set_klipper_dir(ctx, str(clean))
        assert ctx.prior_installation is False


class TestSetMoonrakerConfigFile:
    def test_a_new_file_in_an_existing_folder_is_accepted(self, tmp_path: Path) -> None:
        ctx = make_ctx(tmp_path)
        target = tmp_path / "printer_data" / "moon.conf"
        message = set_moonraker_config_file(ctx, str(target))
        assert message == f"Moonraker config file set to {target}"
        assert ctx.moonraker_config_file == target

    def test_a_missing_folder_is_rejected(self, tmp_path: Path) -> None:
        ctx = make_ctx(tmp_path)
        before = ctx.moonraker_config_file
        with pytest.raises(ValueError) as exc:
            set_moonraker_config_file(ctx, str(tmp_path / "nope" / "moon.conf"))
        assert str(exc.value) == f"Directory not found: {tmp_path / 'nope'}"
        assert ctx.moonraker_config_file == before


class TestSetKlipperService:
    @pytest.mark.parametrize("name", ["klipper", "klipper_2", "klipper-x", "kl.ipper@a:b"])
    def test_valid_names(self, tmp_path: Path, name: str) -> None:
        ctx = make_ctx(tmp_path)
        assert set_klipper_service(ctx, f" {name} ") == f"Klipper service name set to {name}"
        assert ctx.klipper_service == name

    @pytest.mark.parametrize("name", ["", "two words", "a/b", "$(x)", "a;b"])
    def test_invalid_names_are_rejected(self, tmp_path: Path, name: str) -> None:
        ctx = make_ctx(tmp_path)
        with pytest.raises(ValueError) as exc:
            set_klipper_service(ctx, name)
        assert str(exc.value) == "A service name may only use letters, digits and . _ @ : -"
        assert ctx.klipper_service == "klipper"


class TestParseMoonrakerAddress:
    @pytest.mark.parametrize(
        "text,expected",
        [
            ("printer", ("http://printer", "7125")),
            ("printer:80", ("http://printer", "80")),
            ("http://printer", ("http://printer", "7125")),
            ("https://printer:8443", ("https://printer", "8443")),
            ("192.168.1.5:7126", ("http://192.168.1.5", "7126")),
            (" my-printer.local ", ("http://my-printer.local", "7125")),
            ("printer:0080", ("http://printer", "80")),
            ("printer:1", ("http://printer", "1")),
            ("printer:65535", ("http://printer", "65535")),
        ],
    )
    def test_valid(self, text: str, expected: Tuple[str, str]) -> None:
        assert parse_moonraker_address(text, "7125") == expected

    @pytest.mark.parametrize(
        "text,reason",
        [
            ("ftp://printer", "The address must start with http:// or https://"),
            ("printer:abc", "The port must be a number from 1 to 65535"),
            ("printer:0", "The port must be a number from 1 to 65535"),
            ("printer:70000", "The port must be a number from 1 to 65535"),
            ("printer:²", "The port must be a number from 1 to 65535"),
            ("pr inter", "The host may only use letters, digits and . _ -"),
            ("", "The host may only use letters, digits and . _ -"),
            ("http://pri/nter", "The host may only use letters, digits and . _ -"),
        ],
    )
    def test_invalid(self, text: str, reason: str) -> None:
        with pytest.raises(ValueError) as exc:
            parse_moonraker_address(text, "7125")
        assert str(exc.value) == reason


class TestSetMoonrakerAddress:
    def test_updates_the_address_and_the_port(self, tmp_path: Path) -> None:
        ctx = make_ctx(tmp_path)
        message = set_moonraker_address(ctx, "https://printer:8443")
        assert message == "Moonraker address set to https://printer:8443"
        assert (ctx.moonraker_address, ctx.moonraker_port) == ("https://printer", "8443")
        assert ctx.moonraker == "https://printer:8443"

    def test_without_a_port_the_current_one_is_kept(self, tmp_path: Path) -> None:
        ctx = make_ctx(tmp_path)
        ctx.moonraker_port = "9000"
        set_moonraker_address(ctx, "printer")
        assert (ctx.moonraker_address, ctx.moonraker_port) == ("http://printer", "9000")

    def test_a_bad_value_changes_nothing(self, tmp_path: Path) -> None:
        ctx = make_ctx(tmp_path)
        with pytest.raises(ValueError):
            set_moonraker_address(ctx, "printer:99999")
        assert (ctx.moonraker_address, ctx.moonraker_port) == ("http://localhost", "7125")


class TestSettingsTable:
    def test_only_the_editable_rows_are_listed(self) -> None:
        assert sorted(SETTINGS) == ["1", "2", "3", "4", "6"]
        assert [SETTINGS[k].noun for k in sorted(SETTINGS)] == [
            "printer config directory", "klipper directory", "moonraker config file",
            "klipper service name", "moonraker address",
        ]


def build(
    tmp_path: Path, answers: Sequence[str]
) -> Tuple[MainMenu, MenuState, List[str]]:
    ctx = make_ctx(tmp_path)
    state = MenuState(ctx=ctx, selection=UnitSelection())
    deps, _, prompter = fake_deps(answers)
    menu = MainMenu(state, deps)
    return menu, state, prompter.prompts


class TestMainMenuEditing:
    def test_a_number_prompts_and_applies_the_value(self, tmp_path: Path) -> None:
        other = tmp_path / "other"
        other.mkdir()
        menu, state, prompts = build(tmp_path, ["1", str(other)])
        assert menu.step() == "main"
        assert prompts == [
            "Enter your choice: ", "Enter new printer config directory (blank to keep): "
        ]
        assert state.ctx.printer_config_dir == other
        assert state.message == f"Printer config directory set to {other}"

    def test_the_new_value_shows_on_the_next_draw(self, tmp_path: Path) -> None:
        menu, state, _ = build(tmp_path, ["4", "klipper2"])
        menu.step()
        texts = ["".join(s.text for s in r) for r in menu.view().top]
        assert "      4. Klipper Service Name     : klipper2 " in texts

    @pytest.mark.parametrize("blank", ["", "   "])
    def test_a_blank_answer_keeps_the_value(self, tmp_path: Path, blank: str) -> None:
        menu, state, _ = build(tmp_path, ["4", blank])
        menu.step()
        assert state.ctx.klipper_service == "klipper"
        assert state.message == "The klipper service name was left unchanged."

    def test_a_bad_answer_keeps_the_value_and_says_why(self, tmp_path: Path) -> None:
        menu, state, _ = build(tmp_path, ["4", "bad name"])
        menu.step()
        assert state.ctx.klipper_service == "klipper"
        assert state.message == (
            "The klipper service name was not changed. "
            "A service name may only use letters, digits and . _ @ : -"
        )

    def test_end_of_input_at_the_prompt_exits_one(self, tmp_path: Path) -> None:
        menu, state, _ = build(tmp_path, ["1"])
        with pytest.raises(InstallerExit) as exc:
            menu.step()
        assert exc.value.code == 1
        assert state.ctx.printer_config_dir == tmp_path / "printer_data" / "config"

    def test_the_branch_row_is_not_editable(self, tmp_path: Path) -> None:
        menu, state, prompts = build(tmp_path, ["5"])
        menu.step()
        assert prompts == ["Enter your choice: "]
        assert state.ctx.branch == "main"
