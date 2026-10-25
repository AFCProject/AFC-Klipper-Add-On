import io
import re
from pathlib import Path

import pytest

from installer.platform_core.console import BOLD_GREEN, GREEN, MENU_GREEN, NC, RED, Console
from installer.platform_core.toggles import default_toggles
from installer.ui_menus import art as art_module
from installer.ui_menus.art import art_name_for_type, load_art
from installer.ui_menus.renderer import AnsiRenderer
from installer.ui_menus.state import MenuState
from installer.ui_menus import view as view_module
from installer.ui_menus.view import (
    BOX_WIDTH,
    centered,
    HELP_BOX,
    UPDATE_OPTIONS_BOX,
    MenuView,
    Segment,
    row,
)
from installer.platform_core.context import Context
from installer.unit_registry.registry import UNIT_TYPES
from installer.unit_registry.selection import UnitSelection
from tests.installer.fakes import RecordingConsole
from tests.installer.paths import REPO_ROOT

UNIT_KEYS = [unit.key for unit in UNIT_TYPES]
SETUP = (
    f'set -e; export afc_path="{REPO_ROOT}"; cd "{REPO_ROOT}"; '
    "source include/units/registry.sh; source include/constants.sh; source include/colors.sh; "
)


def build_state(tmp_path: Path) -> MenuState:
    ctx = Context.create(afc_path=tmp_path, home=tmp_path)
    return MenuState(ctx=ctx, selection=UnitSelection())


class TestLoadArt:
    def test_escape_markers_become_real_escapes(self, tmp_path: Path, monkeypatch) -> None:
        (tmp_path / "demo.txt").write_bytes(b"\\e[31mred\\e[m\r\nplain\n")
        monkeypatch.setattr(art_module, "ART_DIR", tmp_path)
        art_module.load_art.cache_clear()
        try:
            assert load_art("demo") == "\x1b[31mred\x1b[m\r\nplain\n"
        finally:
            art_module.load_art.cache_clear()

    def test_missing_art_is_empty(self) -> None:
        assert load_art("NoSuchUnit") == ""

    @pytest.mark.parametrize("name", ["", "../main", "a/b", "main.txt", "x y"])
    def test_malformed_names_are_empty(self, name: str) -> None:
        assert load_art(name) == ""

    def test_a_name_cannot_reach_outside_the_art_directory(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        (tmp_path / "art").mkdir()
        (tmp_path / "secret.txt").write_text("outside")
        monkeypatch.setattr(art_module, "ART_DIR", tmp_path / "art")
        art_module.load_art.cache_clear()
        try:
            assert load_art("../secret") == ""
        finally:
            art_module.load_art.cache_clear()

    def test_unreadable_art_is_empty(self, tmp_path: Path, monkeypatch) -> None:
        (tmp_path / "broken.txt").mkdir()
        monkeypatch.setattr(art_module, "ART_DIR", tmp_path)
        art_module.load_art.cache_clear()
        try:
            assert load_art("broken") == ""
        finally:
            art_module.load_art.cache_clear()

    def test_every_unit_type_has_a_banner_file(self) -> None:
        for key in UNIT_KEYS:
            assert load_art(key) != "", key


class TestArtNameForType:
    @pytest.mark.parametrize("unit", UNIT_TYPES, ids=[u.key for u in UNIT_TYPES])
    def test_is_the_unit_key(self, unit) -> None:  # type: ignore[no-untyped-def]
        assert art_name_for_type(unit.display_name) == unit.key

    def test_unknown_type_has_no_art(self) -> None:
        assert art_name_for_type("Mystery Box") == ""


def all_boxes() -> "list[tuple[str, tuple]]":  # type: ignore[type-arg]
    return [
        (name, getattr(view_module, name))
        for name in dir(view_module)
        if name.endswith("_BOX") and isinstance(getattr(view_module, name), tuple)
    ]


class TestArtEndings:
    @pytest.mark.parametrize("name", [*UNIT_KEYS, "main", "utilities"])
    def test_a_banner_ends_on_a_fresh_line(self, name: str) -> None:
        art = load_art(name)
        assert art.endswith("\n"), name
        assert not art.endswith(" "), name

    def test_the_first_box_under_the_main_banner_starts_in_the_first_column(self) -> None:
        text = AnsiRenderer.render(MenuView(art="main", options_box=UPDATE_OPTIONS_BOX))
        plain = re.sub(r"\x1b\[[0-9;]*m", "", text)
        box_tops = [line for line in plain.split("\n") if "▄" * BOX_WIDTH in line]
        assert box_tops
        assert all(line.startswith("▄" * BOX_WIDTH) for line in box_tops)


class TestBoxes:
    @pytest.mark.parametrize("name,box", all_boxes(), ids=[n for n, _ in all_boxes()])
    def test_every_line_fits_between_the_borders_exactly(  # type: ignore[no-untyped-def]
        self, name: str, box
    ) -> None:
        for line in box:
            assert len(line.left) + len(line.right) == BOX_WIDTH - 2, (name, line.left.strip())

    def test_there_are_boxes_to_check(self) -> None:
        assert len(all_boxes()) >= 8

    def test_centered_text_has_equal_margins_within_one_column(self) -> None:
        line = centered("Hello there")
        text = line.left + line.right
        assert text.strip() == "Hello there"
        assert abs((len(text) - len(text.lstrip())) - (len(text) - len(text.rstrip()))) <= 1
        assert len(text) == BOX_WIDTH - 2

    def test_a_line_as_wide_as_the_box_has_no_padding(self) -> None:
        text = "x" * (BOX_WIDTH - 2)
        line = centered(text)
        assert (line.left, line.right) == (text, "")


class TestRow:
    def test_plain_and_colored_parts(self) -> None:
        assert row("A. ", ("Unknown", "red")) == (Segment("A. "), Segment("Unknown", "red"))

    def test_no_parts_is_a_blank_row(self) -> None:
        assert row() == ()


class TestMenuState:
    def test_defaults(self, tmp_path: Path) -> None:
        state = build_state(tmp_path)
        assert (state.message, state.update_message, state.unit_message) == ("", "", "")
        assert state.toggles == {
            "afc_includes": True, "tip_forming": False, "toolhead_cutter": True,
            "hub_cutter": False, "kick_macro": True, "park_macro": True,
            "poop_macro": True, "wipe_macro": True, "tip_cut_servo": False,
        }

    def test_each_state_gets_its_own_toggles(self, tmp_path: Path) -> None:
        first = build_state(tmp_path)
        second = build_state(tmp_path)
        first.toggles["tip_forming"] = True
        assert second.toggles["tip_forming"] is False
        assert default_toggles()["tip_forming"] is False


class TestAnsiRenderer:
    def test_screen_layout_and_colored_segments(self) -> None:
        view = MenuView(
            art="",
            top=[row("intro")],
            message="msg",
            options_box=UPDATE_OPTIONS_BOX[:1],
            rows=[
                row("A. pin : ", ("Unknown", "red"), " "),
                row("ok ", ("yes", "green")),
                row(("I. go", "bold_green")),
            ],
        )
        bar = "▄" * 87
        end = "▀" * 87
        head = HELP_BOX[0]
        opt = UPDATE_OPTIONS_BOX[0]
        expected = (
            "intro\n"
            f"{MENU_GREEN}{bar} \n█{NC}{head.left}{MENU_GREEN}{head.right}█\n"
            f"{MENU_GREEN}{end}{NC} \n"
            "msg\n"
            f"{MENU_GREEN}{bar} \n█{NC}{opt.left}{MENU_GREEN}{opt.right}█\n"
            f"{MENU_GREEN}{end}{NC} \n"
            f"A. pin : {RED}Unknown{NC} \n"
            f"ok {GREEN}yes{NC}\n"
            f"{BOLD_GREEN}I. go{NC}\n"
        )
        assert AnsiRenderer.render(view) == expected

    def test_unknown_color_name_is_left_uncolored(self) -> None:
        view = MenuView(rows=[row(("plain", "chartreuse"))])
        assert AnsiRenderer.render(view).endswith("plain\n")

    def test_art_is_printed_first(self) -> None:
        text = AnsiRenderer.render(MenuView(art="main"))
        assert text.startswith(load_art("main"))

    def test_show_writes_the_rendered_text_to_the_console(self) -> None:
        console = RecordingConsole()
        view = MenuView(message="hi", options_box=UPDATE_OPTIONS_BOX)
        AnsiRenderer(console).show(view)  # type: ignore[arg-type]
        assert console.written == [AnsiRenderer.render(view)]

    def test_clear_goes_through_the_console(self) -> None:
        console = RecordingConsole()
        AnsiRenderer(console).clear()  # type: ignore[arg-type]
        assert console.clears == 1


class TestConsoleWrite:
    def test_writes_text_verbatim_and_flushes(self) -> None:
        class Flushing(io.StringIO):
            flushes = 0

            def flush(self) -> None:
                Flushing.flushes += 1

        stream = Flushing()
        Console(stream).write("a\x1b[0mb")
        assert stream.getvalue() == "a\x1b[0mb"
        assert Flushing.flushes == 1
