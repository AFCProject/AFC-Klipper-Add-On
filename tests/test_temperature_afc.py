"""Unit tests for extras/temperature_afc.py."""

from __future__ import annotations

import configparser
import sys
import types
from typing import Any
from unittest.mock import MagicMock

import pytest

# temperature_oams.py does `from . import bus`, which resolves to
# `extras.bus`. That module is part of Klipper core and isn't vendored in
# this repo, so it must be stubbed before the module under test is imported
# (mirrors the same stub in test_temperature_oams.py).
_bus_stub = types.ModuleType("extras.bus")
_bus_stub.MCU_I2C_from_config = MagicMock()
sys.modules.setdefault("extras.bus", _bus_stub)
try:
    import extras  # noqa: E402
    if not hasattr(extras, "bus"):
        extras.bus = _bus_stub
except ImportError:
    pass

import extras.temperature_ace as temperature_ace_module  # noqa: E402
import extras.temperature_oams as temperature_oams_module  # noqa: E402
from extras.temperature_afc import (  # noqa: E402
    TemperatureAFC,
    _register_sensor_factory,
    load_config,
    register_on_heaters,
)
from tests.ace_helpers import (  # noqa: E402
    ace_isolation, AceConfig, Hook, make_ace_printer, Recorder)


pytestmark = pytest.mark.usefixtures(ace_isolation.__name__)


class _StubSensor:
    """Records the config it was built with, standing in for a real
    TemperatureACE/TemperatureOAMS instance so dispatch tests don't need
    either module's full init machinery."""

    def __init__(self, config: Any) -> None:
        self.config = config


class TestTemperatureAFCNew:
    def test_ace_afc_type_dispatches_to_temperature_ace(self, monkeypatch):
        monkeypatch.setattr(temperature_ace_module, "TemperatureACE", _StubSensor)
        printer = make_ace_printer()
        config = AceConfig("temperature_sensor t", printer, {"afc_type": "ACE"})

        result = TemperatureAFC(config)

        assert isinstance(result, _StubSensor)
        assert result.config is config

    def test_ace2_afc_type_also_dispatches_to_temperature_ace(self, monkeypatch):
        """ACE and ACE2 units are both backed by the one TemperatureACE
        class (it already branches on the unit's own type internally)."""
        monkeypatch.setattr(temperature_ace_module, "TemperatureACE", _StubSensor)
        printer = make_ace_printer()
        config = AceConfig("temperature_sensor t", printer, {"afc_type": "ACE2"})

        result = TemperatureAFC(config)

        assert isinstance(result, _StubSensor)
        assert result.config is config

    def test_openams_afc_type_dispatches_to_temperature_oams(self, monkeypatch):
        monkeypatch.setattr(temperature_oams_module, "TemperatureOAMS", _StubSensor)
        printer = make_ace_printer()
        config = AceConfig("temperature_sensor t", printer, {"afc_type": "OpenAMS"})

        result = TemperatureAFC(config)

        assert isinstance(result, _StubSensor)
        assert result.config is config

    def test_unknown_afc_type_raises_config_error(self):
        printer = make_ace_printer()
        config = AceConfig("temperature_sensor t", printer, {"afc_type": "BoxTurtle"})

        with pytest.raises(configparser.Error, match="afc_type"):
            TemperatureAFC(config)

    def test_ace_unit_auto_detects_and_dispatches_to_temperature_ace(self, monkeypatch):
        """afc_type omitted: ace_unit alone is enough to detect ACE."""
        monkeypatch.setattr(temperature_ace_module, "TemperatureACE", _StubSensor)
        printer = make_ace_printer()
        config = AceConfig("temperature_sensor t", printer, {"ace_unit": "Ace_1"})

        result = TemperatureAFC(config)

        assert isinstance(result, _StubSensor)
        assert result.config is config

    def test_i2c_mcu_auto_detects_and_dispatches_to_temperature_oams(self, monkeypatch):
        """afc_type omitted: i2c_mcu alone is enough to detect OpenAMS."""
        monkeypatch.setattr(temperature_oams_module, "TemperatureOAMS", _StubSensor)
        printer = make_ace_printer()
        config = AceConfig("temperature_sensor t", printer, {"i2c_mcu": "oams_mcu1"})

        result = TemperatureAFC(config)

        assert isinstance(result, _StubSensor)
        assert result.config is config

    def test_explicit_afc_type_overrides_conflicting_detection_signal(self, monkeypatch):
        """afc_type must win even when a different type's detection option is
        also present, so a user can always force the choice explicitly."""
        monkeypatch.setattr(temperature_oams_module, "TemperatureOAMS", _StubSensor)
        printer = make_ace_printer()
        config = AceConfig("temperature_sensor t", printer,
                           {"afc_type": "OpenAMS", "ace_unit": "Ace_1"})

        result = TemperatureAFC(config)

        assert isinstance(result, _StubSensor)
        assert result.config is config

    def test_ambiguous_detection_without_afc_type_raises_config_error(self):
        """Both ace_unit and i2c_mcu present with no afc_type to break the tie
        must fail loudly instead of silently guessing one type."""
        printer = make_ace_printer()
        config = AceConfig("temperature_sensor t", printer,
                           {"ace_unit": "Ace_1", "i2c_mcu": "oams_mcu1"})

        with pytest.raises(configparser.Error, match="afc_type"):
            TemperatureAFC(config)

    def test_no_detection_signal_and_no_afc_type_raises_config_error(self):
        """Neither afc_type nor any detection option (ace_unit/i2c_mcu/i2c_bus)
        given: detection is inconclusive, so this must fail loudly."""
        printer = make_ace_printer()
        config = AceConfig("temperature_sensor t", printer, {})

        with pytest.raises(configparser.Error, match="afc_type"):
            TemperatureAFC(config)


class TestDetectAfcType:
    """Covers TemperatureAFC._detect_afc_type directly, independent of the
    dispatch tests above that only exercise it as a side effect of __new__."""

    def test_ace_unit_present_detects_ace(self):
        printer = make_ace_printer()
        config = AceConfig("temperature_sensor t", printer, {"ace_unit": "Ace_1"})

        assert TemperatureAFC._detect_afc_type(config) == "ACE"

    def test_i2c_mcu_present_detects_openams(self):
        printer = make_ace_printer()
        config = AceConfig("temperature_sensor t", printer, {"i2c_mcu": "oams_mcu1"})

        assert TemperatureAFC._detect_afc_type(config) == "OpenAMS"

    def test_i2c_bus_present_detects_openams(self):
        """Covers the i2c_bus half of OpenAMS' i2c_mcu-or-i2c_bus check
        independently of i2c_mcu."""
        printer = make_ace_printer()
        config = AceConfig("temperature_sensor t", printer, {"i2c_bus": "i2c0"})

        assert TemperatureAFC._detect_afc_type(config) == "OpenAMS"

    def test_no_signal_present_returns_none(self):
        printer = make_ace_printer()
        config = AceConfig("temperature_sensor t", printer, {})

        assert TemperatureAFC._detect_afc_type(config) is None

    def test_conflicting_signals_returns_none(self):
        """Both ace_unit and i2c_mcu present at once is ambiguous, not a
        preference for one type over the other."""
        printer = make_ace_printer()
        config = AceConfig("temperature_sensor t", printer,
                           {"ace_unit": "Ace_1", "i2c_mcu": "oams_mcu1"})

        assert TemperatureAFC._detect_afc_type(config) is None


class TestLoadConfig:
    def test_registers_aht2x_factory(self):
        printer = make_ace_printer()
        add = printer.heaters.add_sensor_factory = Hook(printer.heaters.add_sensor_factory)

        load_config(AceConfig("temperature_afc", printer))

        assert add.calls == [(("aht2x", TemperatureAFC), {})]
        assert printer.heaters.factories == {"aht2x": TemperatureAFC}
        assert printer.heaters._temperature_afc_registered is True


class TestRegisterSensorFactory:
    def test_registers_on_loaded_heaters(self):
        printer = make_ace_printer()
        add = printer.heaters.add_sensor_factory = Hook(printer.heaters.add_sensor_factory)

        _register_sensor_factory(AceConfig("temperature_afc", printer))

        assert add.calls == [(("aht2x", TemperatureAFC), {})]
        assert printer.heaters.factories == {"aht2x": TemperatureAFC}
        assert printer.heaters._temperature_afc_registered is True

    def test_load_object_fallback_when_lookup_fails(self):
        printer = make_ace_printer()
        heaters = printer.objects.pop("heaters")  # not loaded yet, so lookup_object raises
        add = heaters.add_sensor_factory = Hook(heaters.add_sensor_factory)
        printer.load_object = Recorder(result=heaters)
        config = AceConfig("temperature_afc", printer)

        _register_sensor_factory(config)

        # Klipper's load_object takes the config being loaded.
        assert printer.load_object.calls == [((config, "heaters"), {})]
        assert add.calls == [(("aht2x", TemperatureAFC), {})]
        assert heaters.factories == {"aht2x": TemperatureAFC}
        assert heaters._temperature_afc_registered is True


class TestRegisterOnHeaters:
    def test_registers_the_factory(self):
        printer = make_ace_printer()
        add = printer.heaters.add_sensor_factory = Hook(printer.heaters.add_sensor_factory)

        register_on_heaters(printer.heaters)

        assert add.calls == [(("aht2x", TemperatureAFC), {})]
        assert printer.heaters.factories == {"aht2x": TemperatureAFC}
        assert printer.heaters._temperature_afc_registered is True

    def test_already_registered_does_not_register_again(self):
        """Tracked on the heaters object (not a module flag) so a fresh
        heaters object after a Klipper RESTART is never mistaken for an
        already-registered one; see register_on_heaters' docstring."""
        printer = make_ace_printer()
        add = printer.heaters.add_sensor_factory = Hook(printer.heaters.add_sensor_factory)
        printer.heaters._temperature_afc_registered = True

        register_on_heaters(printer.heaters)

        assert add.calls == []
        assert printer.heaters.factories == {}
        assert printer.heaters._temperature_afc_registered is True

    def test_second_call_is_a_noop(self):
        printer = make_ace_printer()
        add = printer.heaters.add_sensor_factory = Hook(printer.heaters.add_sensor_factory)

        register_on_heaters(printer.heaters)
        register_on_heaters(printer.heaters)

        assert len(add.calls) == 1

    def test_a_different_heaters_object_is_unaffected(self):
        """Covers the real bug this per-object tracking fixes: registering
        on one heaters object must not mark a different (e.g. freshly
        created post-RESTART) heaters object as already done."""
        printer1 = make_ace_printer()
        printer2 = make_ace_printer()

        register_on_heaters(printer1.heaters)

        assert printer1.heaters._temperature_afc_registered is True
        assert not getattr(printer2.heaters, "_temperature_afc_registered", False)
