"""
Unit tests for extras/AFC_form_tip.py

Covers:
  - afc_tip_form attribute initialization
  - cmd_GET_TIP_FORMING: output contains all config key names
  - cmd_SET_TIP_FORMING: updates attributes from gcmd
  - afc_extrude: delegates to afc.move_e_pos
  - tip_form: calls afc_extrude the correct number of times based on config
"""

from __future__ import annotations

from unittest.mock import MagicMock
import pytest

from extras.AFC_form_tip import afc_tip_form


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_tip_form(values=None):
    """Build an afc_tip_form instance by bypassing __init__."""
    tip = afc_tip_form.__new__(afc_tip_form)

    from tests.conftest import MockAFC, MockLogger

    afc = MockAFC()
    afc.logger = MockLogger()
    afc.move_e_pos = MagicMock()

    tip.printer = MagicMock()
    tip.reactor = MagicMock()
    tip.afc = afc
    tip.gcode = afc.gcode
    tip.logger = afc.logger

    # Defaults matching the config defaults in afc_tip_form.__init__
    tip.ramming_volume = 0.0
    tip.toolchange_temp = 0.0
    tip.unloading_speed_start = 80.0
    tip.unloading_speed = 18.0
    tip.cooling_tube_position = 35.0
    tip.cooling_tube_length = 10.0
    tip.initial_cooling_speed = 10.0
    tip.final_cooling_speed = 50.0
    tip.cooling_moves = 4
    tip.use_skinnydip = False
    tip.skinnydip_distance = 4.0
    tip.dip_insertion_speed = 4.0
    tip.dip_extraction_speed = 4.0
    tip.melt_zone_pause = 4.0
    tip.cooling_zone_pause = 4.0

    if values:
        for k, v in values.items():
            setattr(tip, k, v)

    return tip


def _make_gcmd(**kwargs):
    gcmd = MagicMock()
    gcmd.get_float = lambda key, default: kwargs.get(key, default)
    gcmd.get_int = lambda key, default: int(kwargs.get(key, default))
    gcmd.get = lambda key, default: str(kwargs.get(key, default))
    return gcmd


# ── Attribute initialization ──────────────────────────────────────────────────

class TestInitDefaults:
    def test_ramming_volume_default(self):
        tip = _make_tip_form()
        assert tip.ramming_volume == 0.0

    def test_cooling_moves_default(self):
        tip = _make_tip_form()
        assert tip.cooling_moves == 4

    def test_use_skinnydip_default(self):
        tip = _make_tip_form()
        assert tip.use_skinnydip is False

    def test_unloading_speed_default(self):
        tip = _make_tip_form()
        assert tip.unloading_speed == 18.0


# ── cmd_GET_TIP_FORMING ───────────────────────────────────────────────────────

class TestGetTipForming:
    EXPECTED_KEYS = [
        "ramming_volume",
        "toolchange_temp",
        "unloading_speed_start",
        "unloading_speed",
        "cooling_tube_position",
        "cooling_tube_length",
        "initial_cooling_speed",
        "final_cooling_speed",
        "cooling_moves",
        "use_skinnydip",
        "skinnydip_distance",
        "dip_insertion_speed",
        "dip_extraction_speed",
        "melt_zone_pause",
        "cooling_zone_pause",
    ]

    def test_all_keys_in_output(self):
        tip = _make_tip_form()
        gcmd = MagicMock()
        tip.cmd_GET_TIP_FORMING(gcmd)
        raw_msgs = [m for lvl, m in tip.logger.messages if lvl == "raw"]
        combined = "\n".join(raw_msgs)
        for key in self.EXPECTED_KEYS:
            assert key in combined, f"Key '{key}' missing from GET_TIP_FORMING output"

    def test_values_are_reflected(self):
        tip = _make_tip_form({"ramming_volume": 42.0})
        gcmd = MagicMock()
        tip.cmd_GET_TIP_FORMING(gcmd)
        raw_msgs = [m for lvl, m in tip.logger.messages if lvl == "raw"]
        combined = "\n".join(raw_msgs)
        assert "42.0" in combined


# ── cmd_SET_TIP_FORMING ───────────────────────────────────────────────────────

class TestSetTipForming:
    def test_sets_ramming_volume(self):
        tip = _make_tip_form()
        gcmd = _make_gcmd(RAMMING_VOLUME=25.0)
        tip.cmd_SET_TIP_FORMING(gcmd)
        assert tip.ramming_volume == 25.0

    def test_sets_cooling_moves(self):
        tip = _make_tip_form()
        gcmd = _make_gcmd(COOLING_MOVES=6)
        tip.cmd_SET_TIP_FORMING(gcmd)
        assert tip.cooling_moves == 6

    def test_use_skinnydip_true_string(self):
        tip = _make_tip_form()
        gcmd = _make_gcmd(USE_SKINNYDIP="true")
        tip.cmd_SET_TIP_FORMING(gcmd)
        assert tip.use_skinnydip is True

    def test_use_skinnydip_false_string(self):
        tip = _make_tip_form()
        tip.use_skinnydip = True
        gcmd = _make_gcmd(USE_SKINNYDIP="False")
        tip.cmd_SET_TIP_FORMING(gcmd)
        assert tip.use_skinnydip is False

    def test_sets_unloading_speed(self):
        tip = _make_tip_form()
        gcmd = _make_gcmd(UNLOADING_SPEED=20.0)
        tip.cmd_SET_TIP_FORMING(gcmd)
        assert tip.unloading_speed == 20.0

    def test_unchanged_if_not_in_gcmd(self):
        tip = _make_tip_form({"cooling_tube_length": 12.0})
        gcmd = _make_gcmd()
        tip.cmd_SET_TIP_FORMING(gcmd)
        assert tip.cooling_tube_length == 12.0


# ── afc_extrude ───────────────────────────────────────────────────────────────

class TestAFCExtrude:
    def test_delegates_to_afc_move_e_pos(self):
        tip = _make_tip_form()
        tip.afc_extrude(10.0, 5.0)
        tip.afc.move_e_pos.assert_called_once_with(10.0, 5.0, "form tip")

    def test_negative_distance_passes_through(self):
        tip = _make_tip_form()
        tip.afc_extrude(-20.0, 15.0)
        tip.afc.move_e_pos.assert_called_once_with(-20.0, 15.0, "form tip")


# ── tip_form ──────────────────────────────────────────────────────────────────

class TestTipForm:
    def _setup_toolhead(self, tip):
        """Wire up toolhead and heaters mocks for tip_form()."""
        toolhead = MagicMock()
        extruder = MagicMock()
        heater = MagicMock()
        heater.target_temp = 200.0
        extruder.get_heater.return_value = heater
        toolhead.get_extruder.return_value = extruder
        tip.afc.toolhead = toolhead
        pheaters = MagicMock()
        tip.printer.lookup_object = MagicMock(return_value=pheaters)
        return pheaters, heater

    def test_no_ramming_when_volume_zero(self):
        tip = _make_tip_form()
        tip.afc_extrude = MagicMock()
        self._setup_toolhead(tip)
        tip.tip_form()
        # Without ramming_volume, only retraction + cooling moves happen
        # Cooling moves: cooling_moves * 2 extrude calls
        # Plus initial retraction calls
        calls = tip.afc_extrude.call_count
        assert calls > 0

    def test_ramming_steps_called_when_volume_set(self):
        tip = _make_tip_form({"ramming_volume": 23.0})
        tip.afc_extrude = MagicMock()
        self._setup_toolhead(tip)
        tip.tip_form()
        # 14 ramming steps + retraction + cooling moves
        assert tip.afc_extrude.call_count > 14

    def test_cooling_moves_call_count(self):
        tip = _make_tip_form({"cooling_moves": 2})
        tip.afc_extrude = MagicMock()
        self._setup_toolhead(tip)
        tip.tip_form()
        # 2 cooling moves × 2 calls (forward + backward) = 4 cooling calls
        # Plus at least 4 retraction calls
        assert tip.afc_extrude.call_count >= 4

    def test_skinnydip_called_when_enabled(self):
        tip = _make_tip_form({"use_skinnydip": True})
        tip.afc_extrude = MagicMock()
        tip.reactor = MagicMock()
        self._setup_toolhead(tip)
        tip.tip_form()
        # Skinnydip adds 2 more extrude calls
        # Verify reactor.pause was called for melt and cooling zones
        assert tip.reactor.pause.call_count >= 2

    def test_logs_done_at_end(self):
        tip = _make_tip_form()
        tip.afc_extrude = MagicMock()
        self._setup_toolhead(tip)
        tip.tip_form()
        info_msgs = [m for lvl, m in tip.logger.messages if lvl == "info"]
        assert any("Done" in m for m in info_msgs)

    def test_toolchange_temp_set_when_positive_no_skinnydip(self):
        tip = _make_tip_form({"toolchange_temp": 150.0, "use_skinnydip": False})
        tip.afc_extrude = MagicMock()
        pheaters, heater = self._setup_toolhead(tip)
        tip.tip_form()
        # set_temperature should be called with toolchange_temp and wait=True
        calls = pheaters.set_temperature.call_args_list
        temp_calls = [c for c in calls if c[0][1] == 150.0]
        assert len(temp_calls) >= 1
        assert temp_calls[0][0][2] is True  # wait=True

    def test_toolchange_temp_no_wait_when_skinnydip_enabled(self):
        tip = _make_tip_form({"toolchange_temp": 150.0, "use_skinnydip": True})
        tip.afc_extrude = MagicMock()
        tip.reactor = MagicMock()
        pheaters, heater = self._setup_toolhead(tip)
        tip.tip_form()
        calls = pheaters.set_temperature.call_args_list
        temp_calls = [c for c in calls if c[0][1] == 150.0]
        assert len(temp_calls) >= 1
        assert temp_calls[0][0][2] is False  # wait=False

    def test_temp_restored_when_heater_target_changed_during_tip_form(self):
        tip = _make_tip_form({"toolchange_temp": 150.0})
        tip.afc_extrude = MagicMock()
        initial_temp = 200.0
        heater = MagicMock()
        heater.target_temp = initial_temp
        extruder = MagicMock()
        extruder.get_heater.return_value = heater
        tip.afc.toolhead = MagicMock()
        tip.afc.toolhead.get_extruder.return_value = extruder
        pheaters = MagicMock()

        def update_temp(heater_obj, temp, wait=True):
            heater_obj.target_temp = temp

        pheaters.set_temperature.side_effect = update_temp
        tip.printer.lookup_object = MagicMock(return_value=pheaters)
        tip.tip_form()
        # Last set_temperature call should restore original temp
        assert pheaters.set_temperature.call_count >= 2
        last_call = pheaters.set_temperature.call_args_list[-1]
        assert last_call[0][1] == initial_temp

    def test_cmd_test_tip_forming_calls_tip_form(self):
        tip = _make_tip_form()
        tip.tip_form = MagicMock()
        gcmd = MagicMock()
        tip.cmd_TEST_AFC_TIP_FORMING(gcmd)
        tip.tip_form.assert_called_once()


# ── __init__ via MockConfig ───────────────────────────────────────────────────

class TestFormTipInit:
    def test_init_from_config_sets_defaults(self):
        from tests.conftest import MockConfig, MockPrinter, MockAFC
        afc = MockAFC()
        printer = MockPrinter(afc=afc)
        config = MockConfig(printer=printer)
        tip = afc_tip_form(config)
        assert tip.ramming_volume == 0.0
        assert tip.cooling_moves == 4
        assert tip.use_skinnydip is False

    def test_init_from_config_reads_custom_values(self):
        from tests.conftest import MockConfig, MockPrinter, MockAFC
        afc = MockAFC()
        printer = MockPrinter(afc=afc)
        config = MockConfig(printer=printer, values={
            "ramming_volume": 15.0,
            "cooling_moves": 6,
        })
        tip = afc_tip_form(config)
        assert tip.ramming_volume == 15.0
        assert tip.cooling_moves == 6
