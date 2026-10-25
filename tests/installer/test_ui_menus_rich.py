import builtins
import re
import io
import sys
from typing import Any, Dict

import pytest

from installer.ui_menus.art import art_name_for_type
from installer.ui_menus.renderer import AnsiRenderer
from installer.ui_menus.rich_renderer import (
    RichRenderer,
    choose_renderer,
    load_rich,
    rich_disabled,
)
from installer.ui_menus.view import (
    ADDITIONAL_OPTIONS_BOX,
    UPDATE_OPTIONS_BOX,
    MenuView,
    row,
)
from installer.unit_registry.registry import UNIT_TYPES
from tests.installer.fakes import RecordingConsole


@pytest.fixture
def without_rich(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make every import of rich fail, as on a machine without it."""
    for name in [m for m in sys.modules if m == "rich" or m.startswith("rich.")]:
        monkeypatch.delitem(sys.modules, name)
    real_import = builtins.__import__

    def blocked(name: str, *args: Any, **kwargs: Any) -> Any:
        if name == "rich" or name.startswith("rich."):
            raise ImportError(f"No module named {name!r}")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", blocked)


class TestWithoutRich:
    def test_load_rich_reports_missing(self, without_rich: None) -> None:
        assert load_rich() is None

    def test_a_terminal_still_gets_the_plain_renderer(self, without_rich: None) -> None:
        renderer = choose_renderer(RecordingConsole(), True)  # type: ignore[arg-type]
        assert isinstance(renderer, AnsiRenderer)


class TestRichSwitch:
    @pytest.mark.parametrize("value", ["1", "true", "yes", "anything"])
    def test_any_real_value_turns_rich_off(self, value: str) -> None:
        assert rich_disabled({"AFC_NO_RICH": value}) is True

    @pytest.mark.parametrize("env", [{}, {"AFC_NO_RICH": ""}, {"AFC_NO_RICH": "0"}])
    def test_unset_empty_or_zero_leaves_rich_on(self, env: Dict[str, str]) -> None:
        assert rich_disabled(env) is False

    def test_the_switch_forces_the_plain_renderer_on_a_terminal_with_rich(self) -> None:
        pytest.importorskip("rich")
        console = RecordingConsole()
        renderer = choose_renderer(console, True, {"AFC_NO_RICH": "1"})  # type: ignore[arg-type]
        assert isinstance(renderer, AnsiRenderer)
        on = choose_renderer(console, True, {"AFC_NO_RICH": "0"})  # type: ignore[arg-type]
        assert isinstance(on, RichRenderer)

    def test_the_process_environment_is_used_by_default(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        pytest.importorskip("rich")
        monkeypatch.setenv("AFC_NO_RICH", "1")
        assert isinstance(choose_renderer(RecordingConsole(), True), AnsiRenderer)  # type: ignore
        monkeypatch.delenv("AFC_NO_RICH")
        assert isinstance(choose_renderer(RecordingConsole(), True), RichRenderer)  # type: ignore


class TestChooseRenderer:
    def test_not_a_terminal_gets_the_plain_renderer_even_with_rich(self) -> None:
        pytest.importorskip("rich")
        renderer = choose_renderer(RecordingConsole(), False)  # type: ignore[arg-type]
        assert isinstance(renderer, AnsiRenderer)

    def test_a_terminal_with_rich_gets_the_rich_renderer(self) -> None:
        pytest.importorskip("rich")
        renderer = choose_renderer(RecordingConsole(), True)  # type: ignore[arg-type]
        assert isinstance(renderer, RichRenderer)


def draw(view: MenuView) -> str:
    rich = load_rich()
    assert rich is not None
    stream = io.StringIO()
    rich_console = rich.Console(
        file=stream, force_terminal=True, width=100, color_system="standard"
    )
    RichRenderer(RecordingConsole(), rich, rich_console).show(view)  # type: ignore[arg-type]
    return stream.getvalue()


class TestRichRenderer:
    def test_clear_goes_through_the_console(self) -> None:
        rich = pytest.importorskip("rich")
        console = RecordingConsole()
        RichRenderer(console, load_rich()).clear()  # type: ignore[arg-type]
        assert console.clears == 1
        assert rich is not None

    def test_screen_contains_the_message_boxes_and_rows(self) -> None:
        pytest.importorskip("rich")
        view = MenuView(
            top=[row("intro line")],
            message="a note",
            options_box=ADDITIONAL_OPTIONS_BOX,
            rows=[row("T. Installation Type: HTLF "), row(("I. Install", "bold_green"))],
        )
        plain = draw(view)
        for needle in ("intro line", "AFC Script Help", "a note", "add an additional unit type",
                       "T. Installation Type: HTLF", "I. Install"):
            assert needle in plain

    @pytest.mark.parametrize(
        "color,code", [("red", "\x1b[31m"), ("green", "\x1b[32m"), ("bold_green", "\x1b[1;32m")]
    )
    def test_segment_colors_become_styles(self, color: str, code: str) -> None:
        pytest.importorskip("rich")
        output = draw(MenuView(options_box=UPDATE_OPTIONS_BOX, rows=[row(("coloured", color))]))
        assert f"{code}coloured" in output

    def test_unknown_color_is_unstyled(self) -> None:
        pytest.importorskip("rich")
        output = draw(MenuView(options_box=UPDATE_OPTIONS_BOX, rows=[row(("plainly", "teal"))]))
        assert "plainly" in output
        assert "\x1b[31mplainly" not in output

    def test_ansi_in_a_message_is_rendered_not_printed_raw(self) -> None:
        pytest.importorskip("rich")
        output = draw(MenuView(message="\x1b[0;31mwarn\x1b[0m", options_box=UPDATE_OPTIONS_BOX))
        assert "warn" in output
        assert "[0;31m" not in output

    @pytest.mark.parametrize("unit", UNIT_TYPES, ids=[u.key for u in UNIT_TYPES])
    def test_every_unit_banner_draws(self, unit) -> None:  # type: ignore[no-untyped-def]
        pytest.importorskip("rich")
        view = MenuView(art=art_name_for_type(unit.display_name), options_box=UPDATE_OPTIONS_BOX)
        assert "▄" in draw(view)

    def test_the_main_and_utilities_banners_draw(self) -> None:
        pytest.importorskip("rich")
        for name in ("main", "utilities"):
            assert "▄" in draw(MenuView(art=name, options_box=UPDATE_OPTIONS_BOX))

    def test_no_art_prints_no_banner(self) -> None:
        pytest.importorskip("rich")
        output = draw(MenuView(art="", message="only", options_box=UPDATE_OPTIONS_BOX))
        assert re.sub(r"\x1b\[[0-9;]*m", "", output).startswith("╭")


class TestBoxText:
    def test_box_titles_are_centered_without_extra_padding(self) -> None:
        pytest.importorskip("rich")
        plain = re.sub(r"\x1b\[[0-9;]*m", "", draw(MenuView(options_box=UPDATE_OPTIONS_BOX)))
        line = next(ln for ln in plain.splitlines() if "AFC Script Help" in ln)
        inner = line.strip("│")
        left = len(inner) - len(inner.lstrip())
        right = len(inner) - len(inner.rstrip())
        assert abs(left - right) <= 1
