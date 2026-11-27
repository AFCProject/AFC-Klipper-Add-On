"""
Unit tests for extras/AFC_button.py

Covers:
  - AFCButton.__init__: subclasses GCodeButton, reads lane_id/long_press_duration,
    resolves is_adc_button/analog_min/analog_max/pullup, loads short/long press templates
  - AFCButton.lane_obj: resolved-lane property
  - AFCButton._handle_ready: lane lookup on klippy:ready
  - AFCButton.get_resistance: ADC resistance calculation
  - AFCButton.button_callback: calibration suppression, press/release tracking,
    custom short/long press gcode override, embedded short/long press logic
  - AFCButton._run_custom_press_gcode: render/run a custom press gcode template
  - load_config_prefix: Klipper config entry point
"""

from __future__ import annotations

from typing import Any, Optional
from unittest.mock import MagicMock
import pytest

from extras.AFC_button import AFCButton, load_config_prefix


# ── Helpers ───────────────────────────────────────────────────────────────────

class _FakeTemplate:
    """Stand-in for Klipper's gcode_macro.TemplateWrapper: a fixed script
    that renders to itself (or raises, if configured to), with a `.name`
    matching the real "<section>:<option>" naming."""

    def __init__(self, name: str, script: str) -> None:
        self.name = name
        self.script = script
        self.render_error: Optional[Exception] = None

    def render(self, context: Any = None) -> str:
        if self.render_error is not None:
            raise self.render_error
        return self.script


class _FakeGCodeMacro:
    """Stand-in for Klipper's gcode_macro object: load_template(config,
    option, default) returns a _FakeTemplate whose script is read straight
    from config, exactly like the real PrinterGCodeMacro.load_template."""

    def __init__(self) -> None:
        self.templates: dict = {}

    def load_template(self, config, option, default=None):
        script = config.get(option) if default is None else config.get(option, default)
        name = f"{config.get_name()}:{option}"
        template = _FakeTemplate(name, script)
        self.templates[option] = template
        return template


class _ButtonsNoDebounce:
    """Stand-in for a buttons module that predates register_debounce_button/
    register_debounce_adc_button (e.g. u1-klipper's buttons.py), exposing
    only the older register_buttons/register_adc_button API."""

    def __init__(self) -> None:
        self.register_adc_button = MagicMock()
        self.register_buttons = MagicMock()


def _make_button(lane_id: str="lane1", long_press_duration: float=1.2,
                 pin: str="PA0", analog_range: Optional[str]=None,
                 analog_pullup_resistor: Optional[float]=None,
                 short_press_gcode: str="", long_press_gcode: str="",
                 buttons_obj: Optional[Any]=None) -> AFCButton:
    """Build an AFCButton via its real __init__ (chained through
    GCodeButton.__init__), then fire klippy:ready exactly as Klipper would
    so lane_obj resolves the same way it does at runtime.

    :param lane_id: lane name, also this button's config suffix
    :param long_press_duration: long_press_duration config value
    :param pin: pin config value
    :param analog_range: "min,max" string to configure an ADC button, or
        None for a plain digital button
    :param analog_pullup_resistor: analog_pullup_resistor config value,
        only meaningful when analog_range is set
    :param short_press_gcode: short_press_gcode config value
    :param long_press_gcode: long_press_gcode config value
    :param buttons_obj: buttons object to register against, or None for a
        plain MagicMock exposing the full debounce API
    :return AFCButton: constructed button, with lane_obj already resolved
    """
    from tests.conftest import MockAFC, MockConfig, MockLogger, MockPrinter

    afc = MockAFC()
    afc.logger = MockLogger()
    printer = MockPrinter(afc=afc)
    printer._objects["buttons"] = buttons_obj if buttons_obj is not None else MagicMock()
    printer._objects["gcode_macro"] = _FakeGCodeMacro()

    lane = MagicMock()
    lane.name = lane_id
    afc.lanes = {lane_id: lane}
    afc.function.button_calibration_active = False

    values: dict = {"pin": pin, "long_press_duration": long_press_duration,
                    "short_press_gcode": short_press_gcode,
                    "long_press_gcode": long_press_gcode}
    if analog_range is not None:
        values["analog_range"] = analog_range
        if analog_pullup_resistor is not None:
            values["analog_pullup_resistor"] = analog_pullup_resistor

    config = MockConfig(name=f"AFC_button {lane_id}", printer=printer, values=values)
    btn = AFCButton(config)
    printer.send_event("klippy:ready")
    return btn


class _FakeMcuAdc:
    """Stand-in for Klipper's MCU_adc: get_last_value() returns (value,
    timestamp) or (timestamp, value) depending on `value_first`, mirroring
    the two Klipper fork orderings get_resistance() has to disambiguate."""

    def __init__(self, value: float, timestamp: float=42.0, value_first: bool=True) -> None:
        self.value = value
        self.timestamp = timestamp
        self.value_first = value_first

    def get_last_value(self):
        if self.value_first:
            return (self.value, self.timestamp)
        return (self.timestamp, self.value)


def _register_adc_sample(btn: AFCButton, value: float, timestamp: float=42.0,
                         value_first: bool=True) -> None:
    """Register a fake query_adc sample for `btn`'s pin, as
    MCU_ADC_buttons.__init__ would via query_adc.register_adc().

    :param btn: ADC button whose pin the sample is registered for
    :param value: ADC ratio (0-1) to report
    :param timestamp: companion timestamp value
    :param value_first: whether get_last_value() returns (value, timestamp)
        (True) or (timestamp, value) (False)
    """
    query_adc = MagicMock()
    query_adc.adc = {f"adc_button:{btn.pin.strip()}": _FakeMcuAdc(value, timestamp, value_first)}
    btn.printer._objects["query_adc"] = query_adc


# __init__

class TestAFCButtonInit:
    def test_sets_fullname(self) -> None:
        btn = _make_button("lane1")
        assert btn.fullname == "AFC_button lane1"

    def test_registers_klippy_ready_handler(self) -> None:
        btn = _make_button("lane1")
        assert "klippy:ready" in btn.printer._event_handlers

    def test_sets_lane_id_from_config_name(self) -> None:
        btn = _make_button("lane_x")
        assert btn.lane_id == "lane_x"

    def test_long_press_duration_default(self) -> None:
        from tests.conftest import MockAFC, MockConfig, MockPrinter
        afc = MockAFC()
        printer = MockPrinter(afc=afc)
        printer._objects["buttons"] = MagicMock()
        printer._objects["gcode_macro"] = _FakeGCodeMacro()
        config = MockConfig(name="AFC_button lane1", printer=printer,
                            values={"pin": "PA0"})
        btn = AFCButton(config)
        assert btn.long_press_duration == 1.2

    def test_long_press_duration_custom(self) -> None:
        btn = _make_button("lane1", long_press_duration=2.5)
        assert btn.long_press_duration == 2.5

    def test_is_adc_button_false_without_analog_range(self) -> None:
        btn = _make_button("lane1")
        assert btn.is_adc_button is False

    def test_analog_min_max_pullup_defaults_without_analog_range(self) -> None:
        btn = _make_button("lane1")
        assert (btn.analog_min, btn.analog_max, btn.pullup) == (0.0, 0.0, 4700.0)

    def test_is_adc_button_true_with_analog_range(self) -> None:
        btn = _make_button("lane1", analog_range="500,1500")
        assert btn.is_adc_button is True

    def test_analog_min_max_parsed_from_analog_range(self) -> None:
        btn = _make_button("lane1", analog_range="500,1500")
        assert (btn.analog_min, btn.analog_max) == (500.0, 1500.0)

    def test_analog_range_with_an_empty_field_is_rejected(self) -> None:
        """Klipper's real getfloatlist() rejects an empty field rather than silently skipping it,
        so `500,,1500` must fail to construct instead of being parsed as a valid pair."""
        with pytest.raises(ValueError):
            _make_button("lane1", analog_range="500,,1500")

    def test_analog_min_max_parsed_from_file_backed_config(self) -> None:
        """analog_range read via MockConfig.fileconfig (as a real printer.cfg value would be)
        must parse the same way as one passed through the values dict."""
        from tests.conftest import MockAFC, MockConfig, MockLogger, MockPrinter

        afc = MockAFC()
        afc.logger = MockLogger()
        printer = MockPrinter(afc=afc)
        printer._objects["buttons"] = MagicMock()
        printer._objects["gcode_macro"] = _FakeGCodeMacro()
        lane = MagicMock()
        lane.name = "lane1"
        afc.lanes = {"lane1": lane}
        afc.function.button_calibration_active = False

        config = MockConfig(name="AFC_button lane1", printer=printer,
                            values={"pin": "PA0", "long_press_duration": 1.2,
                                   "short_press_gcode": "", "long_press_gcode": ""})
        config.fileconfig.set(config.section, "analog_range", "500,1500")
        btn = AFCButton(config)
        printer.send_event("klippy:ready")

        assert (btn.analog_min, btn.analog_max) == (500.0, 1500.0)

    def test_pullup_default_with_analog_range(self) -> None:
        btn = _make_button("lane1", analog_range="500,1500")
        assert btn.pullup == 4700.0

    def test_pullup_custom_with_analog_range(self) -> None:
        btn = _make_button("lane1", analog_range="500,1500", analog_pullup_resistor=2200.0)
        assert btn.pullup == 2200.0

    def test_short_press_template_loaded_from_config(self) -> None:
        btn = _make_button("lane1", short_press_gcode="TEST_MACRO")
        assert btn.short_press_template.script == "TEST_MACRO"

    def test_long_press_template_loaded_from_config(self) -> None:
        btn = _make_button("lane1", long_press_gcode="TEST_MACRO")
        assert btn.long_press_template.script == "TEST_MACRO"

    def test_sets_name_from_config_name(self) -> None:
        btn = _make_button("lane_x")
        assert btn.name == "lane_x"

    def test_sets_pin_from_config(self) -> None:
        btn = _make_button("lane1", pin="PB3")
        assert btn.pin == "PB3"

    def test_registers_debounce_button_for_digital_pin(self) -> None:
        from tests.conftest import MockAFC, MockConfig, MockLogger, MockPrinter

        afc = MockAFC()
        afc.logger = MockLogger()
        printer = MockPrinter(afc=afc)
        printer._objects["buttons"] = MagicMock()
        printer._objects["gcode_macro"] = _FakeGCodeMacro()
        lane = MagicMock()
        lane.name = "lane1"
        afc.lanes = {"lane1": lane}
        afc.function.button_calibration_active = False
        config = MockConfig(name="AFC_button lane1", printer=printer,
                            values={"pin": "PA0", "long_press_duration": 1.2,
                                   "short_press_gcode": "", "long_press_gcode": ""})

        btn = AFCButton(config)
        printer.send_event("klippy:ready")

        buttons_mock = printer._objects["buttons"]
        buttons_mock.register_debounce_button.assert_called_once_with(
            btn.pin, btn.button_callback, config)
        buttons_mock.register_debounce_adc_button.assert_not_called()

    def test_registers_debounce_adc_button_for_adc_pin(self) -> None:
        from tests.conftest import MockAFC, MockConfig, MockLogger, MockPrinter

        afc = MockAFC()
        afc.logger = MockLogger()
        printer = MockPrinter(afc=afc)
        printer._objects["buttons"] = MagicMock()
        printer._objects["gcode_macro"] = _FakeGCodeMacro()
        lane = MagicMock()
        lane.name = "lane1"
        afc.lanes = {"lane1": lane}
        afc.function.button_calibration_active = False
        config = MockConfig(name="AFC_button lane1", printer=printer,
                            values={"pin": "PA0", "long_press_duration": 1.2,
                                   "short_press_gcode": "", "long_press_gcode": "",
                                   "analog_range": "500,1500",
                                   "analog_pullup_resistor": 2200.0})

        btn = AFCButton(config)
        printer.send_event("klippy:ready")

        buttons_mock = printer._objects["buttons"]
        buttons_mock.register_debounce_adc_button.assert_called_once_with(
            btn.pin, 500.0, 1500.0, 2200.0, btn.button_callback, config)
        buttons_mock.register_debounce_button.assert_not_called()

    def test_falls_back_to_register_buttons_for_digital_pin(self) -> None:
        buttons_obj = _ButtonsNoDebounce()
        btn = _make_button("lane1", buttons_obj=buttons_obj)
        buttons_obj.register_buttons.assert_called_once_with([btn.pin], btn.button_callback)

    def test_falls_back_to_register_adc_button_for_adc_pin(self) -> None:
        buttons_obj = _ButtonsNoDebounce()
        btn = _make_button("lane1", analog_range="500,1500",
                           analog_pullup_resistor=2200.0, buttons_obj=buttons_obj)
        buttons_obj.register_adc_button.assert_called_once_with(
            btn.pin, 500.0, 1500.0, 2200.0, btn.button_callback)


# ── lane_obj property ─────────────────────────────────────────────────────────

class TestLaneObjProperty:
    def test_returns_resolved_lane(self) -> None:
        btn = _make_button("lane1")
        assert btn.lane_obj is btn.afc.lanes["lane1"]

    def test_raises_when_not_yet_resolved(self) -> None:
        btn = _make_button("lane1")
        btn._lane_obj = None
        from configfile import error as KlipperError
        with pytest.raises(KlipperError):
            btn.lane_obj


# ── _handle_ready ─────────────────────────────────────────────────────────────

class TestHandleReady:
    def test_handle_ready_sets_lane_obj_when_found(self) -> None:
        btn = _make_button("lane1")
        btn._lane_obj = None
        btn._handle_ready()
        assert btn.lane_obj is btn.afc.lanes["lane1"]

    def test_handle_ready_raises_when_lane_not_found(self) -> None:
        btn = _make_button("missing_lane")
        btn.afc.lanes = {}
        btn._lane_obj = None
        from configfile import error as KlipperError
        with pytest.raises(KlipperError):
            btn._handle_ready()


# _get_adc_ratio

class TestGetAdcRatio:
    def test_returns_none_when_not_adc_button(self) -> None:
        btn = _make_button("lane1")
        assert btn._get_adc_ratio() is None

    def test_returns_none_when_no_sample_registered(self) -> None:
        btn = _make_button("lane1", analog_range="500,1500")
        btn.printer._objects["query_adc"] = MagicMock(adc={})
        assert btn._get_adc_ratio() is None

    def test_returns_value_when_value_is_first_element(self) -> None:
        btn = _make_button("lane1", analog_range="500,1500")
        _register_adc_sample(btn, value=0.680790, timestamp=332.776, value_first=True)
        assert btn._get_adc_ratio() == pytest.approx(0.680790)

    def test_returns_value_when_value_is_second_element(self) -> None:
        """Independent coverage of the inline conditional's false branch: a
        fork whose get_last_value() returns (timestamp, value) instead of
        (value, timestamp) must still resolve to the ratio, not the
        timestamp."""
        btn = _make_button("lane1", analog_range="500,1500")
        _register_adc_sample(btn, value=0.680790, timestamp=332.776, value_first=False)
        assert btn._get_adc_ratio() == pytest.approx(0.680790)


# resistance_at_ratio

class TestResistanceAtRatio:
    def test_computes_resistance_from_ratio_and_pullup(self) -> None:
        btn = _make_button("lane1", analog_range="500,1500",
                           analog_pullup_resistor=4700.0)
        result = btn.resistance_at_ratio(0.95)
        expected = 4700.0 * 0.95 / (1.0 - 0.95)
        assert result == pytest.approx(expected, rel=1e-6)

    def test_uses_this_button_s_own_pullup(self) -> None:
        btn = _make_button("lane1", analog_range="500,1500",
                           analog_pullup_resistor=100.0)
        result = btn.resistance_at_ratio(0.99999)
        expected = 100.0 * 0.99999 / (1.0 - 0.99999)
        assert result == pytest.approx(expected, rel=1e-6)

    def test_half_ratio_equals_pullup(self) -> None:
        """Independent of the source's own formula: a voltage divider at the
        midpoint ratio (0.5) splits evenly, so the unknown resistance must
        equal the pullup exactly, a known identity rather than a
        restatement of resistance_at_ratio's formula."""
        btn = _make_button("lane1", analog_range="500,1500",
                           analog_pullup_resistor=4700.0)
        assert btn.resistance_at_ratio(0.5) == pytest.approx(4700.0, rel=1e-6)


class TestRatioAtResistance:
    def test_computes_ratio_from_resistance_and_pullup(self) -> None:
        btn = _make_button("lane1", analog_range="500,1500",
                           analog_pullup_resistor=4700.0)
        result = btn.ratio_at_resistance(89300.0)
        expected = 89300.0 / (89300.0 + 4700.0)
        assert result == pytest.approx(expected, rel=1e-6)

    def test_uses_this_button_s_own_pullup(self) -> None:
        btn = _make_button("lane1", analog_range="500,1500",
                           analog_pullup_resistor=100.0)
        result = btn.ratio_at_resistance(469995300.0)
        expected = 469995300.0 / (469995300.0 + 100.0)
        assert result == pytest.approx(expected, rel=1e-6)

    def test_resistance_equal_to_pullup_gives_half_ratio(self) -> None:
        """Independent of the source's own formula: a voltage divider with
        the unknown resistance equal to the pullup splits evenly, so the
        ratio must be exactly 0.5, a known identity rather than a
        restatement of ratio_at_resistance's formula."""
        btn = _make_button("lane1", analog_range="500,1500",
                           analog_pullup_resistor=4700.0)
        assert btn.ratio_at_resistance(4700.0) == pytest.approx(0.5, rel=1e-6)

    def test_is_the_inverse_of_resistance_at_ratio(self) -> None:
        btn = _make_button("lane1", analog_range="500,1500",
                           analog_pullup_resistor=4700.0)
        resistance = btn.resistance_at_ratio(0.95)
        result = btn.ratio_at_resistance(resistance)
        assert result == pytest.approx(0.95, rel=1e-6)


# get_resistance

class TestGetResistance:
    def test_returns_none_when_not_adc_button(self) -> None:
        btn = _make_button("lane1")
        assert btn.get_resistance() is None

    def test_returns_none_when_no_sample_registered(self) -> None:
        btn = _make_button("lane1", analog_range="500,1500")
        btn.printer._objects["query_adc"] = MagicMock(adc={})
        assert btn.get_resistance() is None

    def test_computes_resistance_with_value_first_order(self) -> None:
        btn = _make_button("lane1", analog_range="500,1500",
                           analog_pullup_resistor=4700.0)
        _register_adc_sample(btn, value=0.680790, timestamp=332.776, value_first=True)
        result = btn.get_resistance()
        expected = 4700.0 * 0.680790 / (1.0 - 0.680790)
        assert result == pytest.approx(expected, rel=1e-6)

    def test_computes_resistance_with_timestamp_first_order(self) -> None:
        """Some Klipper forks return (timestamp, value) instead of (value,
        timestamp); get_resistance must pick whichever element is in [0, 1]
        regardless of position."""
        btn = _make_button("lane1", analog_range="500,1500",
                           analog_pullup_resistor=4700.0)
        _register_adc_sample(btn, value=0.680790, timestamp=332.776, value_first=False)
        result = btn.get_resistance()
        expected = 4700.0 * 0.680790 / (1.0 - 0.680790)
        assert result == pytest.approx(expected, rel=1e-6)

    def test_clamps_value_near_one(self) -> None:
        """value=1.0 is a valid, in-range ADC ratio (0.0 <= 1.0 <= 1.0), so
        _get_adc_ratio() selects it as-is; the out-of-range timestamp (10.0)
        only confirms it was correctly rejected as the other field. The clamp
        under test here is get_resistance()'s own max(0.00001, min(0.99999,
        value))."""
        btn = _make_button("lane1", analog_range="500,1500",
                           analog_pullup_resistor=4700.0)
        _register_adc_sample(btn, value=1.0, timestamp=10.0, value_first=True)
        result = btn.get_resistance()
        expected = 4700.0 * 0.99999 / (1.0 - 0.99999)
        assert result == pytest.approx(expected, rel=1e-6)


# is_adc_saturated

class TestIsAdcSaturated:
    def test_false_when_not_adc_button(self) -> None:
        btn = _make_button("lane1")
        assert btn.is_adc_saturated() is False

    def test_false_when_no_sample_registered(self) -> None:
        btn = _make_button("lane1", analog_range="500,1500")
        btn.printer._objects["query_adc"] = MagicMock(adc={})
        assert btn.is_adc_saturated() is False

    def test_false_when_ratio_well_below_ceiling(self) -> None:
        btn = _make_button("lane1", analog_range="500,1500")
        _register_adc_sample(btn, value=0.680790, timestamp=332.776, value_first=True)
        assert btn.is_adc_saturated() is False

    def test_true_when_ratio_at_or_above_ceiling(self) -> None:
        btn = _make_button("lane1", analog_range="500,1500")
        _register_adc_sample(btn, value=0.995, timestamp=10.0, value_first=True)
        assert btn.is_adc_saturated() is True

    def test_true_with_timestamp_first_order(self) -> None:
        """Mirrors get_resistance's own order-independent handling."""
        btn = _make_button("lane1", analog_range="500,1500")
        _register_adc_sample(btn, value=0.995, timestamp=10.0, value_first=False)
        assert btn.is_adc_saturated() is True


# _run_custom_press_gcode

class TestRunCustomPressGcode:
    def test_empty_script_returns_false_and_does_not_run(self) -> None:
        btn = _make_button("lane1")
        btn.afc.run_script = MagicMock()
        template = _FakeTemplate("AFC_button lane1:short_press_gcode", "")
        assert btn._run_custom_press_gcode(template) is False
        btn.afc.run_script.assert_not_called()

    def test_nonempty_script_runs_and_returns_true(self) -> None:
        btn = _make_button("lane1")
        btn.afc.run_script = MagicMock()
        template = _FakeTemplate("AFC_button lane1:short_press_gcode", "MY_MACRO")
        assert btn._run_custom_press_gcode(template) is True
        btn.afc.run_script.assert_called_once_with("MY_MACRO", gcmd="force_acquire")

    def test_render_exception_reports_afc_error_and_returns_true(self) -> None:
        btn = _make_button("lane1")
        btn.afc.run_script = MagicMock()
        btn.afc.error = MagicMock()
        template = _FakeTemplate("AFC_button lane1:short_press_gcode", "")
        template.render_error = RuntimeError("bad template")
        assert btn._run_custom_press_gcode(template) is True
        btn.afc.run_script.assert_not_called()
        btn.afc.error.AFC_error.assert_called_once()
        call_args = btn.afc.error.AFC_error.call_args
        assert "lane1" in call_args[0][0]
        assert "AFC_button lane1:short_press_gcode" in call_args[0][0]
        assert call_args[1] == {"pause": False, "gcmd": "callback"}

    def test_render_exception_logs_traceback_to_debug_only(self) -> None:
        btn = _make_button("lane1")
        btn.afc.run_script = MagicMock()
        btn.afc.error = MagicMock()
        btn.afc.logger.debug = MagicMock()
        template = _FakeTemplate("AFC_button lane1:short_press_gcode", "")
        template.render_error = RuntimeError("bad template")
        btn._run_custom_press_gcode(template)
        btn.afc.logger.debug.assert_called_once()
        call_args = btn.afc.logger.debug.call_args
        assert call_args[0][0] == "lane1: error running AFC_button lane1:short_press_gcode"
        assert call_args[1]["only_debug"] is True
        assert "bad template" in call_args[1]["traceback"]
        assert btn.afc.logger.messages == []

    def test_run_script_exception_reports_afc_error_and_returns_true(self) -> None:
        btn = _make_button("lane1")
        btn.afc.run_script = MagicMock(side_effect=RuntimeError("boom"))
        btn.afc.error = MagicMock()
        template = _FakeTemplate("AFC_button lane1:long_press_gcode", "MY_MACRO")
        assert btn._run_custom_press_gcode(template) is True
        btn.afc.error.AFC_error.assert_called_once()


# button_callback

class TestButtonCallback:
    # calibration suppression

    def test_calibration_active_suppresses_press(self) -> None:
        btn = _make_button("lane1")
        btn.afc.function.button_calibration_active = True
        btn.button_callback(100.0, True)
        assert btn._press_time is None
        assert btn.afc.logger.messages == []

    def test_calibration_active_suppresses_release(self) -> None:
        btn = _make_button("lane1")
        btn._press_time = 100.0
        btn.afc.function.button_calibration_active = True
        btn.afc.CHANGE_TOOL = MagicMock()
        btn.button_callback(100.5, False)
        assert btn._press_time is None
        btn.afc.CHANGE_TOOL.assert_not_called()
        assert btn.afc.logger.messages == []

    def test_calibration_active_clears_stale_press_time(self) -> None:
        """A press recorded just before calibration starts must not survive
        into a release handled after calibration has already begun."""
        btn = _make_button("lane1")
        btn._press_time = 50.0
        btn.afc.function.button_calibration_active = True
        btn.button_callback(100.0, True)
        assert btn._press_time is None
        assert btn.afc.logger.messages == []

    # press/release tracking

    def test_press_stores_time(self) -> None:
        btn = _make_button("lane1")
        btn.button_callback(100.0, True)
        assert btn._press_time == 100.0
        assert btn.afc.logger.messages == []

    def test_release_with_no_press_does_nothing(self) -> None:
        btn = _make_button("lane1")
        btn._press_time = None
        btn.afc.error = MagicMock()
        btn.button_callback(101.0, False)
        btn.afc.error.AFC_error.assert_not_called()
        assert btn.afc.logger.messages == []

    def test_too_fast_release_ignored(self) -> None:
        btn = _make_button("lane1")
        btn._press_time = 100.0
        btn.afc.function.is_printing.return_value = False
        btn.afc.function.get_current_lane_obj.return_value = None
        btn.afc.CHANGE_TOOL = MagicMock()
        btn.button_callback(100.03, False)  # 0.03s < 0.05s threshold
        btn.afc.CHANGE_TOOL.assert_not_called()
        assert btn.afc.logger.messages == []

    def test_press_time_cleared_after_release(self) -> None:
        btn = _make_button("lane1")
        btn._press_time = 100.0
        btn.afc.function.is_printing.return_value = False
        btn.afc.function.get_current_lane_obj.return_value = None
        btn.afc.CHANGE_TOOL = MagicMock()
        btn.button_callback(100.2, False)
        assert btn._press_time is None
        assert btn.afc.logger.messages == [
            ("info", "lane1: Short press detected."),
            ("info", "Loading tool to lane1."),
        ]

    def test_raises_when_lane_obj_unresolved(self) -> None:
        """The embedded short-press logic must have already logged both
        messages by the time CHANGE_TOOL's self.lane_obj access raises,
        since that's the last thing it does before calling CHANGE_TOOL."""
        btn = _make_button("lane1")
        btn._press_time = 100.0
        btn._lane_obj = None
        btn.afc.function.is_printing.return_value = False
        btn.afc.CHANGE_TOOL = MagicMock()
        from configfile import error as KlipperError
        with pytest.raises(KlipperError):
            btn.button_callback(100.3, False)
        btn.afc.CHANGE_TOOL.assert_not_called()
        assert btn.afc.logger.messages == [
            ("info", "lane1: Short press detected."),
            ("info", "Loading tool to lane1."),
        ]

    # printing guard

    def test_error_logged_when_printing(self) -> None:
        btn = _make_button("lane1")
        btn._press_time = 100.0
        btn.afc.function.is_printing.return_value = True
        btn.afc.error = MagicMock()
        btn.button_callback(101.0, False)
        btn.afc.error.AFC_error.assert_called_once_with(
            "Cannot use buttons while printer is actively moving or homing", False,
            gcmd="callback")
        assert btn.afc.logger.messages == []

    # short press, embedded logic (no custom gcode configured)

    def test_short_press_no_active_lane_loads_this_lane(self) -> None:
        btn = _make_button("lane1")
        btn._press_time = 100.0
        btn.afc.function.is_printing.return_value = False
        btn.afc.function.get_current_lane_obj.return_value = None
        btn.afc.CHANGE_TOOL = MagicMock()
        btn.button_callback(100.3, False)  # 0.3s < long_press_duration (1.2s)
        btn.afc.CHANGE_TOOL.assert_called_once_with(btn.lane_obj, gcmd="callback")
        assert btn.afc.logger.messages == [
            ("info", "lane1: Short press detected."),
            ("info", "Loading tool to lane1."),
        ]

    def test_short_press_with_this_lane_active_unloads(self) -> None:
        btn = _make_button("lane1")
        btn._press_time = 100.0
        btn.afc.function.is_printing.return_value = False
        cur_lane = MagicMock()
        cur_lane.name = "lane1"
        btn.afc.function.get_current_lane_obj.return_value = cur_lane
        btn.afc.TOOL_UNLOAD = MagicMock(return_value=True)
        btn.button_callback(100.3, False)
        btn.afc.TOOL_UNLOAD.assert_called_once_with(cur_lane, gcmd="callback")
        btn.afc.afc_stats.increase_unload_error_count.assert_not_called()
        assert btn.afc.logger.messages == [
            ("info", "lane1: Short press detected."),
            ("info", "Unloading tool from lane1."),
        ]

    def test_short_press_unload_failure_increases_unload_error_count(self) -> None:
        btn = _make_button("lane1")
        btn._press_time = 100.0
        btn.afc.function.is_printing.return_value = False
        cur_lane = MagicMock()
        cur_lane.name = "lane1"
        btn.afc.function.get_current_lane_obj.return_value = cur_lane
        btn.afc.TOOL_UNLOAD = MagicMock(return_value=False)
        btn.button_callback(100.3, False)
        btn.afc.afc_stats.increase_unload_error_count.assert_called_once_with(btn.afc)
        assert btn.afc.logger.messages == [
            ("info", "lane1: Short press detected."),
            ("info", "Unloading tool from lane1."),
        ]

    def test_short_press_unload_failure_with_no_afc_stats_does_not_raise(self) -> None:
        btn = _make_button("lane1")
        btn._press_time = 100.0
        btn.afc.function.is_printing.return_value = False
        cur_lane = MagicMock()
        cur_lane.name = "lane1"
        btn.afc.function.get_current_lane_obj.return_value = cur_lane
        btn.afc.TOOL_UNLOAD = MagicMock(return_value=False)
        btn.afc.afc_stats = None
        btn.button_callback(100.3, False)
        assert btn.afc.logger.messages == [
            ("info", "lane1: Short press detected."),
            ("info", "Unloading tool from lane1."),
        ]

    def test_short_press_with_different_lane_active_loads_this_lane(self) -> None:
        btn = _make_button("lane1")
        btn._press_time = 100.0
        btn.afc.function.is_printing.return_value = False
        cur_lane = MagicMock()
        cur_lane.name = "lane2"
        btn.afc.function.get_current_lane_obj.return_value = cur_lane
        btn.afc.CHANGE_TOOL = MagicMock()
        btn.button_callback(100.3, False)
        btn.afc.CHANGE_TOOL.assert_called_once_with(btn.lane_obj, gcmd="callback")
        assert btn.afc.logger.messages == [
            ("info", "lane1: Short press detected."),
            ("info", "Loading tool to lane1."),
        ]

    # short press, custom gcode override

    def test_short_press_custom_gcode_overrides_embedded_logic(self) -> None:
        """Must return before the embedded logic's own logger.info calls,
        not just skip calling CHANGE_TOOL."""
        btn = _make_button("lane1", short_press_gcode="MY_SHORT_MACRO")
        btn._press_time = 100.0
        btn.afc.function.is_printing.return_value = False
        btn.afc.function.get_current_lane_obj.return_value = None
        btn.afc.CHANGE_TOOL = MagicMock()
        btn.afc.run_script = MagicMock()
        btn.button_callback(100.3, False)
        btn.afc.run_script.assert_called_once_with("MY_SHORT_MACRO", gcmd="force_acquire")
        btn.afc.CHANGE_TOOL.assert_not_called()
        assert btn.afc.logger.messages == []

    def test_long_press_gcode_does_not_affect_short_press(self) -> None:
        """A configured long_press_gcode must only ever gate the long-press
        branch, proving the two overrides are checked independently rather
        than either one suppressing both press types."""
        btn = _make_button("lane1", long_press_gcode="MY_LONG_MACRO")
        btn._press_time = 100.0
        btn.afc.function.is_printing.return_value = False
        btn.afc.function.get_current_lane_obj.return_value = None
        btn.afc.CHANGE_TOOL = MagicMock()
        btn.afc.run_script = MagicMock()
        btn.button_callback(100.3, False)  # short press
        btn.afc.CHANGE_TOOL.assert_called_once_with(btn.lane_obj, gcmd="callback")
        btn.afc.run_script.assert_not_called()
        assert btn.afc.logger.messages == [
            ("info", "lane1: Short press detected."),
            ("info", "Loading tool to lane1."),
        ]

    # long press, embedded logic (no custom gcode configured)

    def test_long_press_no_active_lane_ejects(self) -> None:
        btn = _make_button("lane1", long_press_duration=1.0)
        btn._press_time = 100.0
        btn.afc.function.is_printing.return_value = False
        btn.afc.function.get_current_lane_obj.return_value = None
        btn.afc.LANE_UNLOAD = MagicMock()
        btn.button_callback(101.5, False)  # 1.5s >= long_press_duration (1.0s)
        btn.afc.LANE_UNLOAD.assert_called_once_with(btn.lane_obj, gcmd="callback")
        assert btn.afc.logger.messages == [
            ("info", "lane1: Long press detected."),
            ("info", "Ejecting lane1."),
        ]

    def test_long_press_with_this_lane_active_unloads_then_ejects(self) -> None:
        btn = _make_button("lane1", long_press_duration=1.0)
        btn._press_time = 100.0
        btn.afc.function.is_printing.return_value = False
        cur_lane = MagicMock()
        cur_lane.name = "lane1"
        btn.afc.function.get_current_lane_obj.return_value = cur_lane
        btn.afc.TOOL_UNLOAD = MagicMock(return_value=True)
        btn.afc.LANE_UNLOAD = MagicMock()
        btn.button_callback(101.5, False)
        btn.afc.TOOL_UNLOAD.assert_called_once()
        btn.afc.LANE_UNLOAD.assert_called_once_with(btn.lane_obj, gcmd="callback")
        btn.afc.afc_stats.increase_unload_error_count.assert_not_called()
        assert btn.afc.logger.messages == [
            ("info", "lane1: Long press detected."),
            ("info", "Unloading lane1 before ejecting."),
        ]

    def test_long_press_unload_failure_increases_unload_error_count_and_skips_eject(self) -> None:
        btn = _make_button("lane1", long_press_duration=1.0)
        btn._press_time = 100.0
        btn.afc.function.is_printing.return_value = False
        cur_lane = MagicMock()
        cur_lane.name = "lane1"
        btn.afc.function.get_current_lane_obj.return_value = cur_lane
        btn.afc.TOOL_UNLOAD = MagicMock(return_value=False)
        btn.afc.LANE_UNLOAD = MagicMock()
        btn.button_callback(101.5, False)
        btn.afc.afc_stats.increase_unload_error_count.assert_called_once_with(btn.afc)
        btn.afc.LANE_UNLOAD.assert_not_called()
        assert btn.afc.logger.messages == [
            ("info", "lane1: Long press detected."),
            ("info", "Unloading lane1 before ejecting."),
        ]

    def test_long_press_unload_failure_with_no_afc_stats_does_not_raise(self) -> None:
        btn = _make_button("lane1", long_press_duration=1.0)
        btn._press_time = 100.0
        btn.afc.function.is_printing.return_value = False
        cur_lane = MagicMock()
        cur_lane.name = "lane1"
        btn.afc.function.get_current_lane_obj.return_value = cur_lane
        btn.afc.TOOL_UNLOAD = MagicMock(return_value=False)
        btn.afc.LANE_UNLOAD = MagicMock()
        btn.afc.afc_stats = None
        btn.button_callback(101.5, False)
        btn.afc.LANE_UNLOAD.assert_not_called()
        assert btn.afc.logger.messages == [
            ("info", "lane1: Long press detected."),
            ("info", "Unloading lane1 before ejecting."),
        ]

    def test_long_press_with_different_lane_active_ejects_only(self) -> None:
        btn = _make_button("lane1", long_press_duration=1.0)
        btn._press_time = 100.0
        btn.afc.function.is_printing.return_value = False
        cur_lane = MagicMock()
        cur_lane.name = "lane2"
        btn.afc.function.get_current_lane_obj.return_value = cur_lane
        btn.afc.LANE_UNLOAD = MagicMock()
        btn.afc.TOOL_UNLOAD = MagicMock()
        btn.button_callback(101.5, False)
        btn.afc.LANE_UNLOAD.assert_called_once_with(btn.lane_obj, gcmd="callback")
        btn.afc.TOOL_UNLOAD.assert_not_called()
        assert btn.afc.logger.messages == [
            ("info", "lane1: Long press detected."),
            ("info", "Ejecting lane1."),
        ]

    # long press, custom gcode override

    def test_long_press_custom_gcode_overrides_embedded_logic(self) -> None:
        """Must return before the embedded logic's own logger.info calls,
        not just skip calling LANE_UNLOAD."""
        btn = _make_button("lane1", long_press_duration=1.0, long_press_gcode="MY_LONG_MACRO")
        btn._press_time = 100.0
        btn.afc.function.is_printing.return_value = False
        btn.afc.function.get_current_lane_obj.return_value = None
        btn.afc.LANE_UNLOAD = MagicMock()
        btn.afc.run_script = MagicMock()
        btn.button_callback(101.5, False)
        btn.afc.run_script.assert_called_once_with("MY_LONG_MACRO", gcmd="force_acquire")
        btn.afc.LANE_UNLOAD.assert_not_called()
        assert btn.afc.logger.messages == []

    def test_short_press_gcode_does_not_affect_long_press(self) -> None:
        btn = _make_button("lane1", long_press_duration=1.0, short_press_gcode="MY_SHORT_MACRO")
        btn._press_time = 100.0
        btn.afc.function.is_printing.return_value = False
        btn.afc.function.get_current_lane_obj.return_value = None
        btn.afc.LANE_UNLOAD = MagicMock()
        btn.afc.run_script = MagicMock()
        btn.button_callback(101.5, False)  # long press
        btn.afc.LANE_UNLOAD.assert_called_once_with(btn.lane_obj, gcmd="callback")
        btn.afc.run_script.assert_not_called()
        assert btn.afc.logger.messages == [
            ("info", "lane1: Long press detected."),
            ("info", "Ejecting lane1."),
        ]


# ── load_config_prefix() ──────────────────────────────────────────────────────

class TestLoadConfigPrefix:
    def test_returns_afc_button_instance(self) -> None:
        from tests.conftest import MockAFC, MockConfig, MockPrinter
        afc = MockAFC()
        printer = MockPrinter(afc=afc)
        printer._objects["buttons"] = MagicMock()
        printer._objects["gcode_macro"] = _FakeGCodeMacro()
        config = MockConfig(name="AFC_button lane1", printer=printer,
                            values={"pin": "PA0", "long_press_duration": 1.2})
        result = load_config_prefix(config)
        assert isinstance(result, AFCButton)
        assert result.afc is afc
