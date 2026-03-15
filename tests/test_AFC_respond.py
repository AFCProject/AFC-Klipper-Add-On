"""
Unit tests for extras/AFC_respond.py

Covers every public method of AFCprompt:
  p_begin, p_text, p_button, p_footer_button, p_cancel_button,
  p_show, p_end, p_button_group_start, p_button_group_end,
  create_custom_p (all combinations of args)
"""

from __future__ import annotations

from unittest.mock import MagicMock, call
import pytest

from extras.AFC_respond import AFCprompt


# ── Helpers ───────────────────────────────────────────────────────────────────

def make_prompt():
    """Return an AFCprompt whose logger.raw() calls are captured."""
    gcmd = MagicMock()
    from tests.conftest import MockLogger
    logger = MockLogger()
    return AFCprompt(gcmd, logger)


def raw_messages(prompt):
    return [m for lvl, m in prompt.logger.messages if lvl == "raw"]


# ── Basic protocol methods ────────────────────────────────────────────────────

class TestAFCpromptBasicMethods:
    def test_p_begin_formats_correctly(self):
        p = make_prompt()
        p.p_begin("My Prompt")
        assert any("prompt_begin My Prompt" in m for m in raw_messages(p))

    def test_p_show_sends_action(self):
        p = make_prompt()
        p.p_show()
        assert any("prompt_show" in m for m in raw_messages(p))

    def test_p_end_sends_action(self):
        p = make_prompt()
        p.p_end()
        assert any("prompt_end" in m for m in raw_messages(p))

    def test_p_button_group_start(self):
        p = make_prompt()
        p.p_button_group_start()
        assert any("prompt_button_group_start" in m for m in raw_messages(p))

    def test_p_button_group_end(self):
        p = make_prompt()
        p.p_button_group_end()
        assert any("prompt_button_group_end" in m for m in raw_messages(p))


# ── p_text ────────────────────────────────────────────────────────────────────

class TestAFCpromptText:
    def test_p_text_single_line(self):
        p = make_prompt()
        p.p_text("Hello world")
        msgs = raw_messages(p)
        assert any("prompt_text Hello world" in m for m in msgs)

    def test_p_text_multiline_sends_separate_messages(self):
        p = make_prompt()
        p.p_text("Line 1\nLine 2\nLine 3")
        msgs = raw_messages(p)
        assert any("Line 1" in m for m in msgs)
        assert any("Line 2" in m for m in msgs)
        assert any("Line 3" in m for m in msgs)


# ── p_button ──────────────────────────────────────────────────────────────────

class TestAFCpromptButton:
    def test_p_button_without_style(self):
        p = make_prompt()
        p.p_button("Load", "LOAD_LANE LANE=lane1")
        msgs = raw_messages(p)
        assert any("Load|LOAD_LANE LANE=lane1" in m for m in msgs)
        # Style must NOT be appended
        assert not any("|primary" in m or "|secondary" in m for m in msgs)

    def test_p_button_with_style(self):
        p = make_prompt()
        p.p_button("Load", "LOAD_LANE LANE=lane1", style="primary")
        msgs = raw_messages(p)
        assert any("Load|LOAD_LANE LANE=lane1|primary" in m for m in msgs)

    def test_p_footer_button_without_style(self):
        p = make_prompt()
        p.p_footer_button("Cancel", "CANCEL")
        msgs = raw_messages(p)
        assert any("prompt_footer_button Cancel|CANCEL" in m for m in msgs)
        assert not any("|primary" in m for m in msgs)

    def test_p_footer_button_with_style(self):
        p = make_prompt()
        p.p_footer_button("Cancel", "CANCEL", style="warning")
        msgs = raw_messages(p)
        assert any("Cancel|CANCEL|warning" in m for m in msgs)

    def test_p_cancel_button_uses_warning_style(self):
        p = make_prompt()
        p.p_cancel_button()
        msgs = raw_messages(p)
        assert any("warning" in m for m in msgs)
        assert any("Cancel" in m for m in msgs)
        assert any("prompt_end" in m for m in msgs)


# ── create_custom_p ───────────────────────────────────────────────────────────

class TestCreateCustomPrompt:
    def test_minimal_call_begin_and_show(self):
        p = make_prompt()
        p.create_custom_p("My Title")
        msgs = raw_messages(p)
        assert any("prompt_begin My Title" in m for m in msgs)
        assert any("prompt_show" in m for m in msgs)

    def test_with_text(self):
        p = make_prompt()
        p.create_custom_p("Title", text="Some instructions")
        msgs = raw_messages(p)
        assert any("Some instructions" in m for m in msgs)

    def test_with_cancel_footer(self):
        p = make_prompt()
        p.create_custom_p("Title", cancel=True)
        msgs = raw_messages(p)
        assert any("Cancel" in m for m in msgs)

    def test_with_buttons(self):
        p = make_prompt()
        buttons = [
            ("Button A", "CMD_A", "primary"),
            ("Button B", "CMD_B", "secondary"),
        ]
        p.create_custom_p("Title", buttons=buttons)
        msgs = raw_messages(p)
        assert any("Button A" in m for m in msgs)
        assert any("Button B" in m for m in msgs)

    def test_with_footer_buttons(self):
        p = make_prompt()
        footer = [("Back", "BACK_CMD", "info")]
        p.create_custom_p("Title", footer_buttons=footer)
        msgs = raw_messages(p)
        assert any("Back" in m for m in msgs)
        assert any("prompt_footer_button" in m for m in msgs)

    def test_with_groups(self):
        p = make_prompt()
        groups = [
            [("Lane 1", "LOAD_LANE LANE=1", "primary"),
             ("Lane 2", "LOAD_LANE LANE=2", "secondary")],
        ]
        p.create_custom_p("Select Lane", groups=groups)
        msgs = raw_messages(p)
        assert any("prompt_button_group_start" in m for m in msgs)
        assert any("prompt_button_group_end" in m for m in msgs)
        assert any("Lane 1" in m for m in msgs)
        assert any("Lane 2" in m for m in msgs)

    def test_order_begin_text_buttons_show(self):
        """begin must appear before show, text before buttons."""
        p = make_prompt()
        buttons = [("OK", "OK_CMD", None)]
        p.create_custom_p("Order Test", text="instructions", buttons=buttons)
        msgs = raw_messages(p)
        begin_idx = next(i for i, m in enumerate(msgs) if "prompt_begin" in m)
        text_idx = next(i for i, m in enumerate(msgs) if "instructions" in m)
        btn_idx = next(i for i, m in enumerate(msgs) if "OK" in m)
        show_idx = next(i for i, m in enumerate(msgs) if "prompt_show" in m)
        assert begin_idx < text_idx < btn_idx < show_idx

    def test_all_options_combined(self):
        p = make_prompt()
        groups = [[("G1", "G1_CMD", "primary")]]
        buttons = [("B1", "B1_CMD", "secondary")]
        footer = [("Back", "BACK", "info")]
        p.create_custom_p(
            "Full Prompt",
            text="Details here",
            buttons=buttons,
            cancel=True,
            groups=groups,
            footer_buttons=footer,
        )
        msgs = raw_messages(p)
        assert any("Full Prompt" in m for m in msgs)
        assert any("Details here" in m for m in msgs)
        assert any("G1" in m for m in msgs)
        assert any("B1" in m for m in msgs)
        assert any("Back" in m for m in msgs)
        assert any("Cancel" in m for m in msgs)
        assert any("prompt_show" in m for m in msgs)


# ── example_prompt ────────────────────────────────────────────────────────────

class TestExamplePrompt:
    def _make_prompt_with_mock(self):
        p = make_prompt()
        p.prompt = MagicMock()  # example_prompt delegates to self.prompt
        return p

    def test_four_items_creates_one_group(self):
        p = self._make_prompt_with_mock()
        p.example_prompt(["a", "b", "c", "d"])
        _, _, _, _, groups, _ = p.prompt.create_custom_p.call_args[0]
        assert len(groups) == 1
        assert len(groups[0]) == 4

    def test_five_items_creates_two_groups(self):
        p = self._make_prompt_with_mock()
        p.example_prompt(["a", "b", "c", "d", "e"])
        _, _, _, _, groups, _ = p.prompt.create_custom_p.call_args[0]
        assert len(groups) == 2
        assert len(groups[0]) == 4
        assert len(groups[1]) == 1

    def test_eight_items_creates_two_groups_of_four(self):
        p = self._make_prompt_with_mock()
        p.example_prompt(["a", "b", "c", "d", "e", "f", "g", "h"])
        _, _, _, _, groups, _ = p.prompt.create_custom_p.call_args[0]
        assert len(groups) == 2
        assert all(len(g) == 4 for g in groups)

    def test_button_styles_alternate_primary_secondary(self):
        p = self._make_prompt_with_mock()
        p.example_prompt(["x", "y"])
        _, _, _, _, groups, _ = p.prompt.create_custom_p.call_args[0]
        styles = [btn[2] for btn in groups[0]]
        assert styles[0] == "primary"
        assert styles[1] == "secondary"

    def test_button_command_contains_item_name(self):
        p = self._make_prompt_with_mock()
        p.example_prompt(["lane1"])
        _, _, _, _, groups, _ = p.prompt.create_custom_p.call_args[0]
        cmd = groups[0][0][1]
        assert "lane1" in cmd

    def test_back_button_is_passed_as_footer(self):
        p = self._make_prompt_with_mock()
        p.example_prompt(["x"])
        _, _, _, _, _, back = p.prompt.create_custom_p.call_args[0]
        assert len(back) == 1
        assert back[0][0] == "Back"

    def test_cancel_is_enabled(self):
        p = self._make_prompt_with_mock()
        p.example_prompt(["x"])
        _, _, _, cancel, _, _ = p.prompt.create_custom_p.call_args[0]
        assert cancel is True
