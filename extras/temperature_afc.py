# AFCProject Automated Filament Changer
#
# Copyright (C) 2026 AFCProject
#
# This file may be distributed under the terms of the GNU GPLv3 license.
#
# Shared "aht2x" Fluidd/Mainsail sensor_type alias for AFC temperature sensors.
# Only this module ever registers "aht2x", so multiple AFC unit types don't
# clobber each other's factory for it. Backing class is auto-detected from
# ace_unit or i2c_mcu/i2c_bus; afc_type is only needed if that's ambiguous.

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Optional

if TYPE_CHECKING:
    from configfile import ConfigWrapper
    from extras.heaters import PrinterHeaters

# afc_type values mapped to the (module, class) backing the sensor. Looked
# up lazily inside __new__ to avoid importing AFC_ACE/AFC_OpenAMS's deps.
AFC_TYPE_FACTORIES = {
    "ACE": ("extras.temperature_ace", "TemperatureACE"),
    "ACE2": ("extras.temperature_ace", "TemperatureACE"),
    "OpenAMS": ("extras.temperature_oams", "TemperatureOAMS"),
}

# Config options used to auto-detect afc_type when it's omitted.
DETECTION_OPTIONS = {
    "ACE": ("ace_unit",),
    "OpenAMS": ("i2c_mcu", "i2c_bus"),
}


class TemperatureAFC:
    """
    Dispatches the shared "aht2x" sensor_type alias to the real AFC
    temperature sensor class for whichever unit type backs the section, so
    only one class ever registers that alias with heaters.
    """

    def __new__(cls, config: "ConfigWrapper") -> Any:
        """
        Build and return the real sensor object for this section's AFC unit
        type, instead of a TemperatureAFC instance. __init__ never runs on
        the result since it isn't an instance of this class.

        :param config: ConfigWrapper for the [temperature_sensor <name>] section.
        :return Any: a TemperatureACE or TemperatureOAMS instance.
        """
        afc_type = config.get("afc_type", None)
        if afc_type is not None:
            factory = AFC_TYPE_FACTORIES.get(afc_type)
            if factory is None:
                valid = ", ".join(sorted(set(AFC_TYPE_FACTORIES)))
                error_str = (f"[{config.get_name()}] sensor_type: aht2x afc_type must be "
                            f"one of: {valid} (got '{afc_type}')")
                raise config.error(error_str)
        else:
            detected = cls._detect_afc_type(config)
            factory = AFC_TYPE_FACTORIES.get(detected)
            if factory is None:
                valid = ", ".join(sorted(set(AFC_TYPE_FACTORIES)))
                error_str = (f"[{config.get_name()}] sensor_type: aht2x could not determine "
                            f"which AFC unit type this sensor belongs to; add ace_unit or "
                            f"i2c_mcu/i2c_bus, or set afc_type to one of: {valid}")
                raise config.error(error_str)
        module_name, class_name = factory
        module = __import__(module_name, fromlist=[class_name])
        sensor_class = getattr(module, class_name)
        return sensor_class(config)

    @staticmethod
    def _detect_afc_type(config: "ConfigWrapper") -> Optional[str]:
        """
        Infer afc_type from whichever backing class' distinguishing config
        option (see DETECTION_OPTIONS) is present in this section.

        :param config: ConfigWrapper for the [temperature_sensor <name>] section.
        :return Optional[str]: The detected afc_type when exactly one type's
            options are present, else None when detection is ambiguous (more
            than one type matches) or inconclusive (none match).
        """
        section = config.get_name()
        matches = [afc_type for afc_type, options in DETECTION_OPTIONS.items()
                  if any(config.fileconfig.has_option(section, option) for option in options)]
        return matches[0] if len(matches) == 1 else None


def load_config(config: "ConfigWrapper") -> None:
    """
    Register the shared aht2x sensor factory with Klipper (config hook).

    :param config: Klipper config wrapper (used only to reach the printer).
    """
    _register_sensor_factory(config)


def _register_sensor_factory(config: "ConfigWrapper") -> None:
    """
    Resolve heaters from config and idempotently register the aht2x factory.

    :param config: the config being loaded; used to reach/load heaters.
    """
    printer = config.get_printer()
    heaters: "PrinterHeaters"
    try:
        heaters = printer.lookup_object("heaters")
    except Exception:
        heaters = printer.load_object(config, "heaters")
    register_on_heaters(heaters)


def register_on_heaters(heaters: "PrinterHeaters") -> None:
    """
    Idempotently register the aht2x sensor factory on a resolved heaters
    object. Tracked on heaters itself, not a module flag, so it's correctly
    re-registered on a fresh heaters object after a Klipper RESTART.

    :param heaters: the printer's heaters object.
    """
    if getattr(heaters, "_temperature_afc_registered", False):
        return
    heaters.add_sensor_factory("aht2x", TemperatureAFC)
    heaters._temperature_afc_registered = True
