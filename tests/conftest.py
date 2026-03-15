"""
Shared test fixtures and Klipper mock infrastructure for AFC unit tests.

All Klipper-specific modules (configfile, queuelogger, webhooks) are mocked
here at the module level so that all test files can import AFC extras modules
without a running Klipper instance.
"""

import configparser
import logging
import logging.handlers
import os
import pathlib
import queue
import sys
import types
from unittest.mock import MagicMock, patch  # noqa: F401

import pytest

# ── Path setup ────────────────────────────────────────────────────────────────
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)


# ── Klipper module mocks ──────────────────────────────────────────────────────
# These must be installed before any extras.* imports happen.

def _make_configfile_mock():
    """Mock for Klipper's configfile module (not the PyPI configfile package)."""
    mod = types.ModuleType("configfile")

    class KlipperError(Exception):
        """Klipper-style configuration error."""

    class ConfigWrapper:
        def __init__(self, printer=None, fileconfig=None, access_tracking=None, section=""):
            self._printer = printer
            self.fileconfig = fileconfig or configparser.RawConfigParser()
            self._section = section

        def get_printer(self):
            return self._printer

        def get_name(self):
            return self._section

        def get(self, option, default=None):
            return default

        def getfloat(self, option, default=0.0, **kwargs):
            return float(default)

        def getboolean(self, option, default=False, **kwargs):
            return bool(default)

        def getint(self, option, default=0, **kwargs):
            return int(default)

        def getlist(self, option, default=None, **kwargs):
            return default or []

        def error(self, msg):
            raise KlipperError(msg)

        def deprecate(self, option):
            pass

    mod.error = KlipperError
    mod.ConfigWrapper = ConfigWrapper
    return mod


def _make_queuelogger_mock():
    """Mock for Klipper's queuelogger module."""
    mod = types.ModuleType("queuelogger")

    class QueueListener:
        def __init__(self, *args, **kwargs):
            self.bg_queue = queue.Queue()

        def stop(self):
            pass

        def setFormatter(self, fmt):
            pass

        def start(self):
            pass

    class QueueHandler(logging.Handler):
        def __init__(self, q):
            super().__init__()
            self.queue = q

        def emit(self, record):
            pass

    mod.QueueListener = QueueListener
    mod.QueueHandler = QueueHandler
    return mod


def _make_webhooks_mock():
    """Mock for Klipper's webhooks module."""
    mod = types.ModuleType("webhooks")

    class GCodeHelper:
        pass

    mod.GCodeHelper = GCodeHelper
    return mod


def _make_led_mock():
    """Mock for extras.led (Klipper's shared LED helper module).

    AFC_led.py uses ``from . import led`` (relative import within the extras
    package), which resolves to ``extras.led``.
    """
    mod = types.ModuleType("extras.led")

    class LEDHelper:
        def __init__(self, config, update_func, chain_count):
            self.led_count = chain_count
            self._color_data = [(0.0, 0.0, 0.0, 0.0)] * chain_count

        def get_status(self, eventtime=None):
            return {"color_data": self._color_data}

        def _set_color(self, index, color):
            pass

        def _check_transmit(self, print_time):
            pass

        def set_color(self, index, color):
            pass

        def check_transmit(self, print_time):
            pass

    mod.LEDHelper = LEDHelper
    return mod


# ── Additional Klipper C-extension mocks ─────────────────────────────────────

def _make_chelper_mock():
    """Mock for Klipper's chelper C extension."""
    mod = types.ModuleType("chelper")

    def get_ffi():
        ffi_main = MagicMock()
        ffi_lib = MagicMock()
        ffi_main.gc = lambda obj, free_fn: obj
        return ffi_main, ffi_lib

    mod.get_ffi = get_ffi
    return mod


def _make_kinematics_mocks():
    """Mock for Klipper's kinematics package."""
    kin_mod = types.ModuleType("kinematics")
    ext_mod = types.ModuleType("kinematics.extruder")

    class FakeExtruderStepper:
        def __init__(self, config):
            self.stepper = MagicMock()

        def sync_to_extruder(self, name):
            pass

    ext_mod.ExtruderStepper = FakeExtruderStepper
    kin_mod.extruder = ext_mod
    return kin_mod, ext_mod


def _make_force_move_mock():
    """Mock for extras.force_move (used by AFC_stepper)."""
    mod = types.ModuleType("extras.force_move")

    def calc_move_time(dist, speed, accel):
        # Returns axis_r, accel_t, cruise_t, cruise_v
        return 1.0, 0.05, 0.1, speed

    mod.calc_move_time = calc_move_time
    return mod

def _make_klippy_ready_mock():
    mod = types.ModuleType("klippy")
    mod.message_ready = "Printer is ready"
    return mod


# Install mocks before any extras imports happen
sys.modules.setdefault("configfile", _make_configfile_mock())
sys.modules.setdefault("queuelogger", _make_queuelogger_mock())
sys.modules.setdefault("webhooks", _make_webhooks_mock())

# C-extension / Klipper internals mocks
sys.modules.setdefault("chelper", _make_chelper_mock())
_kin_mod, _kin_ext_mod = _make_kinematics_mocks()
sys.modules.setdefault("kinematics", _kin_mod)
sys.modules.setdefault("kinematics.extruder", _kin_ext_mod)
sys.modules.setdefault("extras.force_move", _make_force_move_mock())
sys.modules.setdefault("klippy", _make_klippy_ready_mock())

_led_mock = _make_led_mock()
sys.modules.setdefault("extras.led", _led_mock)

# Attach the led mock as an attribute on the extras package after it's loaded
try:
    import extras  # noqa: E402 (intentionally after sys.modules manipulation)
    if not hasattr(extras, "led"):
        extras.led = _led_mock
except ImportError:
    pass


# ── Reusable helper ───────────────────────────────────────────────────────────

def _make_fileconfig(*sections):
    """Return a RawConfigParser pre-populated with the given sections."""
    raw = configparser.RawConfigParser()
    for section in sections:
        raw.add_section(section)
    return raw


# ── Mock building blocks ──────────────────────────────────────────────────────

class MockReactor:
    NEVER = 9_999_999_999.0
    NOW = 0.0

    def __init__(self, monotonic_value=100.0):
        self._monotonic = monotonic_value

    def monotonic(self):
        return self._monotonic
    
    def pause(self, until):
        pass

    def mutex(self, is_locked=False):
        return MagicMock()

    def register_timer(self, callback, waketime=None):
        return MagicMock()

    def update_timer(self, timer, waketime):
        pass

    def register_callback(self, callback, waketime=None):
        pass

    def unregister_timer(self, timer):
        pass


class MockGcode:
    def __init__(self):
        self.output_callbacks = []
        self._commands = {}
        # Use MagicMock so tests can assert call counts / args
        self.run_script_from_command = MagicMock()

    def register_command(self, name, func, desc=None):
        self._commands[name] = func

    def register_mux_command(self, cmd, key, value, func, desc=None):
        pass

    def respond_info(self, msg):
        pass

    def respond_raw(self, msg):
        pass


class MockLogger:
    """Lightweight stand-in for AFC_logger.AFC_logger."""

    def __init__(self):
        self.messages: list = []

    def info(self, msg, **kwargs):
        self.messages.append(("info", msg))

    def warning(self, msg, **kwargs):
        self.messages.append(("warning", msg))

    def debug(self, msg, **kwargs):
        self.messages.append(("debug", msg))

    def error(self, msg=None, **kwargs):
        # AFC_logger.error() can be called as error(msg) or error(message=msg, ...)
        message = msg if msg is not None else kwargs.get("message", "")
        self.messages.append(("error", message))

    def raw(self, msg, **kwargs):
        self.messages.append(("raw", msg))

    def set_debug(self, val):
        pass


class MockMoonraker:
    """Minimal AFC_moonraker stand-in for unit tests."""

    def __init__(self):
        self.logger = MockLogger()
        self._stats: dict = {}
        self.afc_stats_key = "afc_stats"

    def get_afc_stats(self):
        return self._stats or None

    def update_afc_stats(self, key, value):
        self._stats[key] = value

    def remove_database_entry(self, namespace, key):
        pass


class MockAFC:
    """Mock for the main afc object that most extras modules depend on."""

    def __init__(self):
        self.logger = MockLogger()
        self.reactor = MockReactor()
        self.gcode = MockGcode()
        self.error_state = False
        self.current_state = "Idle"
        self.hubs: dict = {}
        self.lanes: dict = {}
        self.tools: dict = {}
        self.units: dict = {}
        self.buffers: dict = {}
        self.led_obj: dict = {}
        self.current = None
        self.enable_sensors_in_gui = False
        self.debounce_delay = 0.1
        self.enable_hub_runout = False
        self.show_macros = True
        self.message_queue: list = []
        self.log_frame_data = True
        self.position_saved = False
        self.in_toolchange = False
        self.error_timeout = 600
        self.td1_defined = False
        self.td1_present = False
        self.moonraker = MockMoonraker()
        self.function = MagicMock()
        self.error = MagicMock()
        self.spool = MagicMock()
        self.short_moves_speed = 50.0
        self.short_moves_accel = 400.0
        self.long_moves_speed = 100.0
        self.long_moves_accel = 400.0
        self.z_hop = 0.5
        self.last_gcode_position = [0.0, 0.0, 0.0, 0.0]
        self.gcode_move = MagicMock()
        self.prep_done = False
        self.spoolman = None
        self.disable_weight_check = False
        self.ignore_spoolman_material_temps = False
        self.default_material_type = "PLA"
        self.bypass = MagicMock()
        self.save_vars = MagicMock()
        self.tool_cmds: dict = {}
        self.VarFile = "/tmp/afc_test_vars"
        # LED colour defaults
        self.led_fault = "1,0,0,0"
        self.led_ready = "0,1,0,0"
        self.led_not_ready = "0,0,0,0.25"
        self.led_loading = "0,0,1,0"
        self.led_unloading = "0,0,1,0"
        self.led_tool_loaded = "0,1,0,0"
        self.led_spool_illum = "1,1,1,0"
        self.led_off = "0,0,0,0"
        # function mock helpers
        self.function.HexConvert = lambda x: x


class MockPrinter:
    """Mock for Klipper's printer object."""
    command_error = Exception
    def __init__(self, afc=None):
        self._afc = afc or MockAFC()
        self._reactor = MockReactor()
        self.reactor = self._reactor  # AFC_logger accesses printer.reactor directly
        self._gcode = MockGcode()
        self._objects: dict = {}
        self.state_message = "Printer is ready"
        self.start_args: dict = {}
        self.objects: dict = {}
        self._event_handlers: dict = {}

    # ------------------------------------------------------------------
    def lookup_object(self, name, default=None):
        mapping = {
            "AFC": self._afc,
            "gcode": self._gcode,
        }
        if name in mapping:
            return mapping[name]
        if name in self._objects:
            return self._objects[name]
        if name == "webhooks":
            return MagicMock()
        if name == "pause_resume":
            obj = MagicMock()
            obj.send_pause_command = MagicMock()
            return obj
        if name == "idle_timeout":
            obj = MagicMock()
            obj.idle_timeout = 600
            return obj
        if name == "toolhead":
            return MagicMock()
        if name == "heaters":
            return MagicMock()
        if name == "pins":
            return MagicMock()
        if name == "buttons":
            return MagicMock()
        return default

    def load_object(self, config, name):
        result = self.lookup_object(name)
        if result is None:
            result = MagicMock()
        return result

    def get_reactor(self):
        return self._reactor

    def register_event_handler(self, event, callback):
        self._event_handlers.setdefault(event, []).append(callback)

    def send_event(self, event, *args):
        for handler in self._event_handlers.get(event, []):
            handler(*args)

    def get_start_args(self):
        return self.start_args


class MockConfig:
    """Mock for Klipper's ConfigWrapper, used to construct extras objects."""

    def __init__(self, name="test_section", printer=None, values=None):
        self._name = name
        self._printer = printer or MockPrinter()
        self._values: dict = values or {}
        self.fileconfig = _make_fileconfig()

    def get_printer(self):
        return self._printer

    def get_name(self):
        return self._name

    def get(self, option, default=None):
        return self._values.get(option, default)

    def getfloat(self, option, default=0.0, **kwargs):
        val = self._values.get(option, default)
        if val is None:
            return None
        return float(val)

    def getboolean(self, option, default=False, **kwargs):
        val = self._values.get(option, default)
        if isinstance(val, bool):
            return val
        if isinstance(val, str):
            return val.lower() in ("true", "1", "yes")
        return bool(val)

    def getint(self, option, default=0, **kwargs):
        val = self._values.get(option, default)
        return int(val)

    def getlist(self, option, default=None, **kwargs):
        val = self._values.get(option, default)
        return val if val is not None else []

    def error(self, msg):
        from configfile import error as KlipperError
        raise KlipperError(msg)

    def deprecate(self, option):
        pass


# ── pytest fixtures ───────────────────────────────────────────────────────────

@pytest.fixture
def mock_reactor():
    return MockReactor()


@pytest.fixture
def mock_gcode():
    return MockGcode()


@pytest.fixture
def mock_logger():
    return MockLogger()


@pytest.fixture
def mock_moonraker():
    return MockMoonraker()


@pytest.fixture
def mock_afc():
    return MockAFC()


@pytest.fixture
def mock_printer(mock_afc):
    return MockPrinter(afc=mock_afc)


@pytest.fixture
def mock_config(mock_printer):
    return MockConfig(printer=mock_printer)


# ── Klippy integration-test session hooks ─────────────────────────────────────
# Sets up the Klipper environment by symlinking AFC extras and the testing
# plugin into the cloned Klipper tree before any .test files are collected.

_ROOT = pathlib.Path(__file__).parent.parent
_KLIPPER_PATH = pathlib.Path(os.environ.get("KLIPPER_PATH", str(_ROOT / "klipper")))
_AFC_EXTRAS = _ROOT / "extras"
_TESTING_PLUGIN_SRC = _ROOT / "tests" / "klippy_testing_plugin.py"


def pytest_sessionstart(session):
    """Link AFC extras and the testing plugin into Klipper for integration tests."""
    if not _KLIPPER_PATH.exists():
        return  # Klipper not cloned yet — klippy tests will be skipped

    klippy_dir = _KLIPPER_PATH / "klippy"
    if not klippy_dir.exists():
        return

    klippy_extras = klippy_dir / "extras"
    linked: list[pathlib.Path] = []

    # Symlink each AFC_*.py module into Klipper's extras directory so that
    # "from extras.AFC_xxx import ..." resolves correctly inside klippy.
    if klippy_extras.exists():
        for src in sorted(_AFC_EXTRAS.glob("AFC*.py")):
            dst = klippy_extras / src.name
            if not dst.exists():
                os.symlink(src.resolve(), dst)
                linked.append(dst)

    # Symlink the testing plugin (provides the ASSERT gcode command) into
    # Klipper's extras directory so it loads when [testing] appears in a config.
    plugin_link = klippy_extras / "testing.py"
    if not plugin_link.exists() and _TESTING_PLUGIN_SRC.exists():
        os.symlink(_TESTING_PLUGIN_SRC.resolve(), plugin_link)
        linked.append(plugin_link)

    session._afc_klippy_links = linked


def pytest_sessionfinish(session, exitstatus):
    """Remove symlinks created during the test session."""
    for link in getattr(session, "_afc_klippy_links", []):
        try:
            link.unlink()
        except FileNotFoundError:
            pass
