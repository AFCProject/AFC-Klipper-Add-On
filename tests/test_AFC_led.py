"""
Unit tests for extras/AFC_led.py

Covers:
  - AFCled.led_change: single index, range string, list
  - AFCled.set_color_fn: respects keep_leds_off
  - AFCled.turn_off_leds: sets keep_leds_off, updates all LEDs to black
  - AFCled.turn_on_leds: clears keep_leds_off, restores last colours
  - AFCled.update_color_data: maps LED state through color_map
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch, call
import pytest

from extras.AFC_led import AFCled


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_led(chain_count=4, color_order="RGB"):
    """Build an AFCled instance by bypassing __init__ and setting attrs."""
    led = AFCled.__new__(AFCled)

    from tests.conftest import MockAFC, MockPrinter, MockReactor

    afc = MockAFC()
    printer = MockPrinter(afc=afc)

    led.printer = printer
    led.afc = afc
    led.fullname = f"AFC_led test_led"
    led.name = "test_led"
    led.mutex = MagicMock()
    led.mutex.__enter__ = MagicMock(return_value=None)
    led.mutex.__exit__ = MagicMock(return_value=False)

    led.last_led_color = {}
    led.keep_leds_off = False
    led.chain_count = chain_count

    # Build a color_map for RGB chain
    # color_map: list of (cdidx, (lidx, cidx))
    # For RGB, each LED has 3 entries
    led.color_map = []
    for lidx in range(chain_count):
        for cidx, c in enumerate("RGB"):
            cdidx = lidx * 3 + cidx
            led.color_map.append((cdidx, (lidx, cidx)))

    led.color_data = bytearray(len(led.color_map))
    led.old_color_data = bytearray(len(led.color_map))

    # LED helper mock
    led.led_helper = MagicMock()
    led.led_helper.led_count = chain_count
    led.led_helper.get_status.return_value = {
        "color_data": [(0.0, 0.0, 0.0)] * chain_count
    }

    led.ledHelper_set_color_fn = MagicMock()
    led.check_transmit_fn = MagicMock()

    # MCU mock
    led.mcu = MagicMock()
    led.oid = 1
    led.pin = "PA0"
    led.neopixel_update_cmd = MagicMock()
    led.neopixel_send_cmd = MagicMock()

    return led


# ── set_color_fn ──────────────────────────────────────────────────────────────

class TestSetColorFn:
    def test_updates_last_led_color_when_update_last_true(self):
        led = _make_led()
        color = [1.0, 0.0, 0.0]
        led.set_color_fn(1, color, update_last=True)
        assert led.last_led_color["1"] == color

    def test_does_not_update_last_led_color_when_false(self):
        led = _make_led()
        color = [1.0, 0.0, 0.0]
        led.set_color_fn(1, color, update_last=False)
        assert "1" not in led.last_led_color

    def test_skips_set_color_when_keep_leds_off(self):
        led = _make_led()
        led.keep_leds_off = True
        led.set_color_fn(1, [1.0, 0.0, 0.0])
        led.ledHelper_set_color_fn.assert_not_called()

    def test_calls_led_helper_when_keep_off_false(self):
        led = _make_led()
        led.keep_leds_off = False
        color = [0.0, 1.0, 0.0]
        led.set_color_fn(2, color)
        led.ledHelper_set_color_fn.assert_called_once_with(2, color)


# ── led_change ────────────────────────────────────────────────────────────────

class TestLedChange:
    def test_single_integer_index(self):
        led = _make_led()
        led.set_color_fn = MagicMock()
        # Toolhead mock needed for lookahead callback
        led.printer._objects["toolhead"] = MagicMock()
        led.led_change(1, [1.0, 0.0, 0.0])
        led.set_color_fn.assert_called_once_with(1, [1.0, 0.0, 0.0], True)

    def test_string_color_parsed_to_list(self):
        led = _make_led()
        led.set_color_fn = MagicMock()
        led.printer._objects["toolhead"] = MagicMock()
        led.led_change(0, "0.5,0.5,0.5,0.0")
        args = led.set_color_fn.call_args[0]
        assert args[1] == [0.5, 0.5, 0.5, 0.0]

    def test_range_index_calls_set_color_for_each(self):
        led = _make_led(chain_count=6)
        led.set_color_fn = MagicMock()
        led.printer._objects["toolhead"] = MagicMock()
        # Range "1-3" → LED indices 1, 2, 3
        led.led_change("1-3", [0.0, 0.0, 1.0])
        assert led.set_color_fn.call_count == 3
        indices = [c[0][0] for c in led.set_color_fn.call_args_list]
        assert sorted(indices) == [1, 2, 3]

    def test_list_index_calls_set_color_for_each(self):
        led = _make_led()
        led.set_color_fn = MagicMock()
        led.printer._objects["toolhead"] = MagicMock()
        led.led_change([0, 2], [1.0, 1.0, 0.0])
        assert led.set_color_fn.call_count == 2

    def test_keep_leds_off_skips_lookahead(self):
        """When keep_leds_off is True, led_change should return early."""
        led = _make_led()
        led.keep_leds_off = True
        toolhead = MagicMock()
        led.printer._objects["toolhead"] = toolhead
        led.led_change(0, [1.0, 0.0, 0.0])
        toolhead.register_lookahead_callback.assert_not_called()


# ── turn_off_leds / turn_on_leds ─────────────────────────────────────────────

class TestLedPower:
    def test_turn_off_sets_keep_leds_off_true(self):
        led = _make_led(chain_count=2)
        led.printer._objects["toolhead"] = MagicMock()
        led.turn_off_leds()
        assert led.keep_leds_off is True

    def test_turn_off_calls_led_change_for_each_led(self):
        led = _make_led(chain_count=3)
        led.led_change = MagicMock()
        led.turn_off_leds()
        # 3 LEDs → 3 calls; each call passes "0,0,0,0"
        assert led.led_change.call_count == 3
        for c in led.led_change.call_args_list:
            assert c[0][1] == "0,0,0,0"

    def test_turn_on_clears_keep_leds_off(self):
        led = _make_led()
        led.keep_leds_off = True
        led.turn_on_leds()
        assert led.keep_leds_off is False

    def test_turn_on_restores_last_colors(self):
        led = _make_led()
        led.keep_leds_off = True
        led.last_led_color = {"0": [1.0, 0.0, 0.0], "1": [0.0, 1.0, 0.0]}
        led.led_change = MagicMock()
        led.turn_on_leds()
        assert led.led_change.call_count == 2

    def test_turn_on_with_no_saved_colors_does_nothing(self):
        led = _make_led()
        led.keep_leds_off = True
        led.last_led_color = {}
        led.led_change = MagicMock()
        led.turn_on_leds()
        led.led_change.assert_not_called()


# ── update_color_data ─────────────────────────────────────────────────────────

class TestUpdateColorData:
    def test_full_brightness_maps_to_255(self):
        led = _make_led(chain_count=1)
        # For a 1-LED RGB chain: color_data has 3 bytes (R, G, B)
        # Set LED 0 to full red: (1.0, 0.0, 0.0)
        led_state = [(1.0, 0.0, 0.0)]
        led.update_color_data(led_state)
        assert led.color_data[0] == 255  # R=1.0 → 255

    def test_zero_brightness_maps_to_zero(self):
        led = _make_led(chain_count=1)
        led_state = [(0.0, 0.0, 0.0)]
        led.update_color_data(led_state)
        for b in led.color_data:
            assert b == 0

    def test_half_brightness_maps_to_128(self):
        led = _make_led(chain_count=1)
        led_state = [(0.5, 0.0, 0.0)]
        led.update_color_data(led_state)
        # 0.5 * 255 + 0.5 = 128.0 → int 128
        assert led.color_data[0] == 128
