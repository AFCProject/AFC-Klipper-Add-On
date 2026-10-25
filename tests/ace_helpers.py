"""
Typed test fakes and real-construction builders for the ACE/ACE2 unit tests.

These replace broad MagicMock usage: every fake declares its real attribute
set explicitly, so a typo'd or missing attribute fails the test instead of
silently returning a truthy MagicMock. Callables under observation are
Recorder instances (plain callables that record their calls).

The builders in the "Real-construction builders" section construct every
ACE-family object through its real __init__ (AGENTS.md, Unit Test Rules) on a
typed fake printer, and record each log channel as exact (level, message)
tuples. That section's header lists the API and where each channel lands.

Self-contained on purpose: the ACE tests must not import from the OpenAMS
test helpers so the ACE work can be upstreamed on its own.
"""

from __future__ import annotations

import atexit
import configparser
import contextlib
import json
import logging
import logging.handlers
import weakref
from dataclasses import dataclass, field
from typing import (Any, Callable, Dict, Iterable, Iterator, List, Mapping, NamedTuple,
                    Optional, Tuple, Union)

import pytest
from gcode import CommandError

import extras.AFC_ACE as afc_ace_module
from extras.AFC_ACE import (MAX_PAYLOAD_SIZE, REQUEST_TIMEOUT, ACEConnection, ACESerialError,
                            ACETimeoutError, afcACE)
from extras.AFC_ACE2 import ACE2Connection, afcACE2
# Uncomment below once RFID and spoolman creation support is added
# from extras.AFC_ACE2_rfid import AFC_ACE2_RFID, Ace2Link, _Ace2RegLink
from extras.AFC_lane import AFCLane, AFCLaneState
from extras.temperature_ace import TemperatureACE


class Recorder:
    """A plain callable that records calls; optionally returns a fixed value
    or raises. Replaces MagicMock for callbacks/methods under observation."""

    def __init__(self, result=None, raises=None):
        self.calls = []
        self.result = result
        self.raises = raises

    def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        if self.raises is not None:
            raise self.raises
        return self.result

    @property
    def called(self):
        return len(self.calls) > 0

    @property
    def call_count(self):
        return len(self.calls)

    @property
    def last_args(self):
        return self.calls[-1][0]

    @property
    def last_kwargs(self):
        return self.calls[-1][1]


class FakeLogger:
    """Records log lines per level."""

    def __init__(self):
        self.lines = {"info": [], "debug": [], "warning": [], "error": []}

    # AFC's signatures, not the stdlib's. A fake that accepts printf
    # args passes calls the real logger rejects -- which is how two
    # printf-style calls in AFC_ACE2 stayed green in this suite.
    def info(self, msg, console_only=False):
        self.lines["info"].append(msg)

    def debug(self, msg, only_debug=False, traceback=None):
        self.lines["debug"].append(msg)

    def warning(self, msg):
        self.lines["warning"].append(msg)

    def error(self, msg, traceback=None, stack_name=""):
        self.lines["error"].append(msg)


class FakeReactor:
    NOW = 0.0
    NEVER = 9_999_999_999.0

    def __init__(self, monotonic_value=100.0):
        self._monotonic = monotonic_value
        self.register_callback = Recorder()
        self.register_timer = Recorder(result="timer-handle")
        self.unregister_timer = Recorder()
        self.update_timer = Recorder()

    def monotonic(self):
        return self._monotonic

    def pause(self, until):
        pass

    def completion(self):
        comp = Recorder()
        comp.complete = Recorder()
        return comp


class FakeError:
    """Stands in for afc.error: records AFC_error calls."""

    def __init__(self):
        self.AFC_error = Recorder()


class FakeFunction:
    """Stands in for afc.function with explicit printing/pause state."""

    def __init__(self, printing=False, paused=False, in_print_flag=False):
        self.printing = printing
        self.paused = paused
        self.in_print_flag = in_print_flag
        self.raise_on_is_printing = None

    def is_printing(self, check_movement=False):
        if self.raise_on_is_printing is not None:
            raise self.raise_on_is_printing
        return self.printing

    def is_paused(self):
        return self.paused

    def in_print(self):
        return self.in_print_flag


class FakeAFC:
    """Explicit-attribute afc object for unit tests."""

    def __init__(self):
        # Jimmy's led_effects work (led_effects branch) reads and clears this
        # on every lane-state change; the shared conftest MockAFC gained it in
        # the same commit, but these helpers roll their own fake.
        self.active_led_effects = []
        self.lanes = {}
        self.current = None
        self.in_toolchange = False
        self.error = FakeError()
        self.function = FakeFunction()
        self.reactor = FakeReactor()
        self.logger = FakeLogger()
        self.save_vars = Recorder()
        self.load_to_hub = False


class FakeExtruderObj:
    """AFC_extruder stand-in: section name, physical name, loaded lane."""

    def __init__(self, name="extruder", th_extruder_name=None, lane_loaded=None):
        self.name = name
        self.th_extruder_name = th_extruder_name if th_extruder_name is not None else name
        self.lane_loaded = lane_loaded


class FakeLane:
    """AFCLane stand-in with the explicit state the ACE/OpenAMS units touch."""

    def __init__(self, name, extruder_obj=None, hub_obj=None,
                 tool_loaded=False, runout_lane=None, status=None):
        self.name = name
        self.extruder_obj = extruder_obj
        self.hub_obj = hub_obj
        self.tool_loaded = tool_loaded
        self.runout_lane = runout_lane
        self.status = status
        self.prep_state = False
        self.loaded_to_hub = False
        self._load_state = False
        self._load_suppressed = False
        self.current_led_state = ""
        self._afc_prep_done = True
        self._oams_runout_detected = False
        self._oams_runout_empty = False
        self.load_to_hub = False
        self.use_feed_assist = None
        # Upstream's virtual-tools work made a lane's map a LIST of T(n)
        # commands, rendered by map_to_string(); the unit modules print it in
        # their prep line. Mirrors AFCLane._format_map: sorted and comma-joined,
        # "NONE" when nothing is mapped.
        self.map = None
        # Observable lane methods
        self.handle_load_runout = Recorder()
        self.sync_to_extruder = Recorder()
        self.unsync_to_extruder = Recorder()
        self.enable_buffer = Recorder()
        self.set_tool_unloaded = Recorder()
        self.get_toolhead_pre_sensor_state = Recorder(result=False)

    def map_to_string(self):
        if not self.map:
            return "NONE"
        if isinstance(self.map, str):
            return self.map
        return ", ".join(sorted(self.map))


class FakeHub:
    """AFC_hub stand-in; virtual=None means 'real switch hub' (no
    is_virtual_pin attribute at all)."""

    def __init__(self, virtual=True):
        self._virtual = virtual

    def is_virtual_pin(self):
        return self._virtual


class FakeToolheadPrinter:
    """printer stand-in for _active_assist_lane: lookup_object('toolhead')
    resolves to an object whose get_extruder().get_name() is the active
    extruder. Set active_extruder=None to make the lookup raise."""

    class _Extruder:
        def __init__(self, name):
            self._name = name

        def get_name(self):
            return self._name

    class _Toolhead:
        def __init__(self, name):
            self._extruder = FakeToolheadPrinter._Extruder(name)

        def get_extruder(self):
            return self._extruder

    def __init__(self, active_extruder="extruder"):
        self.active_extruder = active_extruder
        self.state_message = "Printer is ready"

    def lookup_object(self, name, default=None):
        if name == "toolhead":
            if self.active_extruder is None:
                raise RuntimeError("no toolhead")
            return self._Toolhead(self.active_extruder)
        return default


class FakeGcmd:
    """Gcode command stand-in: params dict + response capture; error() returns
    an exception instance (the code does `raise gcmd.error(...)`)."""

    def __init__(self, **params):
        self._params = params
        self.responses = []

    def get_int(self, name, default=None, minval=None, maxval=None):
        val = self._params.get(name, default)
        return int(val) if val is not None else None

    def get(self, name, default=None):
        return self._params.get(name, default)

    def respond_info(self, msg):
        self.responses.append(msg)

    def error(self, msg):
        return RuntimeError(msg)


class FakeAce:
    """ACE serial connection stand-in: records the commands the unit sends.

    Models the V1 connection. Use :class:`FakeAce2` for the ACE 2 Pro transport.
    """

    def __init__(self, connected=True):
        self.connected = connected
        self.start_feed_assist = Recorder()
        self.stop_feed_assist = Recorder()
        self.stop_feed_filament = Recorder()
        self.send_command_async = Recorder()
        # These read/write wrappers live on the base connection (V1 firmware
        # reports the ACE2-only ones as unsupported at runtime). Defaults are
        # empty; tests override .result per case.
        self.get_temp = Recorder(result={})
        self.get_material_info = Recorder(result={})
        self.set_material_name = Recorder(result={})
        self.get_sensor_state = Recorder(result={})


class FakeAce2(FakeAce):
    """ACE 2 Pro connection stand-in."""

    def __init__(self, connected=True):
        super().__init__(connected=connected)


# ══ Real-construction builders ═══════════════════════════════════════════════
# Everything below builds through the real __init__, never __new__. One
# AcePrinter is the registry a test's objects share, as Klipper's printer is:
# a unit, its lanes, the ACE2 RFID reader and a temperature_ace sensor find
# each other through it.
#
# Builders:
#   make_ace_printer()            the shared printer, reactor, AFC core, gcode, toolhead
#   make_ace_unit() / make_ace2_unit()   afcACE / afcACE2 with real AFCLane lanes
#   make_fake_ace_connection()    scripted transport under the real wrappers
#   make_ace_connection()         real ACEConnection / ACE2Connection on FakeSerial
#   (make_ace2_rfid/make_ace2_reg_link/make_ace2_link are commented out below,
#    pending RFID and spoolman creation support)
#   make_temperature_ace()        TemperatureACE linked to a unit
#   make_gcmd()                   a g-code command with Klipper's get semantics
#   reset_ace_globals() / ace_isolation   reset the ACE modules' process-wide state
#
# Log channels, each an exact list of (level, message) tuples:
#   AFC logger: unit.logger, lane.logger, rfid.logger, afc.logger and the
#     _logger of a connection built by these builders  -> printer.logger.messages
#     (printer.logger.calls adds each call's keyword arguments, e.g. traceback).
#     A link klippy:ready makes logs there too, unless start_args has log_file.
#   afc.error.AFC_error / handle_lane_failure -> Hook .calls (and the timeline)
#   gcode.respond_info / respond_raw          -> printer.gcode.messages
#   gcmd.respond_info / respond_raw           -> gcmd.messages
#   afc.afcDeltaTime.log_with_time            -> printer.afc.afcDeltaTime.messages
#   printer.send_event                        -> printer.events, (event, args)
#   printer.invoke_shutdown                   -> printer.shutdowns
#   Python loggers, through capture_log(name): "AFC_ACE_serial" (a connection
#     given no logger), "AFC_ACE2" (V2 codec helpers), "AFC_ACE_serial_file"
#     (the serial log file), "temperature_ace" (a sensor before handle_ready)
#     and the root logger (afcACE.__init__ when heaters cannot load).
# One ordered record across the fakes, for interleaving: printer.timeline
#   (connection commands and lifecycle, afc.move_e_pos, gcode scripts, events,
#   AFC_error and handle_lane_failure; see AcePrinter).
#
# Events: a builder runs klippy:connect (units) or klippy:ready (rfid bind,
# temperature_ace ready) for the object it builds and then drops that
# object's handler, as Klipper fires each once. A unit's klippy:ready handlers
# stay: printer.send_event("klippy:ready") runs afcACE._handle_ready, which
# queues _deferred_ace_connect, and reactor.run_callbacks() then connects
# through the real _make_connection, routed to a scripted transport (see
# AceConnectionRouter). Lanes log their own ready lines on that event too.
#
# Builders clear every record once wiring is done (clear_records=True), so a
# test sees only what its own calls produced. Pending reactor callbacks and
# timers are state and stay; clear_records(pending=True) drops the callbacks.
#
# Process-wide state (the "AFC_ACE_serial_file" logger, the claimed-port set,
# temperature_ace's factory flag) outlives a test; use the ace_isolation
# fixture, or reset_ace_globals(), in any test that connects or logs to file.


class _Default:
    """Marks a builder argument left at its default, distinct from None."""

    def __repr__(self) -> str:
        """
        :return str: "DEFAULT"
        """
        return "DEFAULT"


DEFAULT: Any = _Default()
_NO_DEFAULT: Any = object()
Timeline = List[Tuple[str, Any]]


class Hook(Recorder):
    """A Recorder that also runs a function and returns its result."""

    def __init__(self, func: Callable[..., Any], raises: Optional[BaseException] = None) -> None:
        """
        Record each call, then run func.

        :param func: called with the recorded arguments; its result is returned
        :param raises: exception to raise instead of running func
        """
        super().__init__(raises=raises)
        self.func = func

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        """
        Record the call, then raise or run func.

        :return Any: func's result
        """
        self.calls.append((args, kwargs))
        if self.raises is not None:
            raise self.raises
        return self.func(*args, **kwargs)


def _reset_recorders(obj: Any) -> None:
    """
    Clear the calls of every Recorder attribute on obj.

    :param obj: object whose Recorder attributes are cleared
    """
    for value in vars(obj).values():
        if isinstance(value, Recorder):
            value.calls.clear()


# ── Logs ──────────────────────────────────────────────────────────────────────

class AceLogger:
    """
    AFC logger stand-in with AFC's signatures (not the stdlib's).

    .messages holds exact (level, message) tuples, the shape conftest's
    MockLogger uses. .calls holds (level, message, keyword arguments) with
    every argument AFC's logger takes, defaults included, so a test can tell
    an error logged with its traceback from a plain one:
    ("error", msg, {"traceback": tb, "stack_name": ""}). raw lines are
    ("raw", message).
    """

    def __init__(self) -> None:
        """
        Start with no messages.
        """
        self.messages: List[Tuple[str, str]] = []
        self.calls: List[Tuple[str, str, Dict[str, Any]]] = []

    def _record(self, level: str, message: str, **kwargs: Any) -> None:
        """
        :param level: log level
        :param message: finished message
        :param kwargs: the call's other arguments
        """
        self.messages.append((level, message))
        self.calls.append((level, message, kwargs))

    def info(self, message: str, console_only: bool = False) -> None:
        """
        :param message: finished message
        :param console_only: AFC's flag, kept in .calls
        """
        self._record("info", message, console_only=console_only)

    def debug(self, message: str, only_debug: bool = False,
              traceback: Optional[str] = None) -> None:
        """
        :param message: finished message
        :param only_debug: AFC's flag, kept in .calls
        :param traceback: AFC's traceback text, kept in .calls
        """
        self._record("debug", message, only_debug=only_debug, traceback=traceback)

    def warning(self, message: str) -> None:
        """
        :param message: finished message
        """
        self._record("warning", message)

    def error(self, message: str, traceback: Optional[str] = None,
              stack_name: str = "") -> None:
        """
        :param message: finished message
        :param traceback: AFC's traceback text, kept in .calls
        :param stack_name: AFC's stack label, kept in .calls
        """
        self._record("error", message, traceback=traceback, stack_name=stack_name)

    def raw(self, message: str) -> None:
        """
        :param message: finished message
        """
        self._record("raw", message)

    def set_debug(self, debug: bool) -> None:
        """
        :param debug: ignored
        """

    def clear(self) -> None:
        """Drop every recorded line."""
        self.messages.clear()
        self.calls.clear()


class LogCapture(logging.Handler):
    """Python logging handler recording exact (level, message) tuples."""

    def __init__(self) -> None:
        """
        Capture every level.
        """
        super().__init__(logging.DEBUG)
        self.messages: List[Tuple[str, str]] = []

    def emit(self, record: logging.LogRecord) -> None:
        """
        :param record: the record to keep, as (lowercase level, message)
        """
        self.messages.append((record.levelname.lower(), record.getMessage()))


@contextlib.contextmanager
def capture_log(name: str = "") -> Iterator[LogCapture]:
    """
    Capture a Python logger's lines while the block runs.

    Attaches to the named logger itself, so loggers that do not propagate
    (AFC_ACE_serial_file) are captured too. "" is the root logger. While it
    is attached to "AFC_ACE_serial_file" that logger has a handler, so
    afcACE._create_serial_logger takes its early return and hands it out.

    :param name: logger name, e.g. "AFC_ACE2" or "temperature_ace"
    :return Iterator[LogCapture]: the handler; read its .messages
    """
    logger = logging.getLogger(name)
    handler = LogCapture()
    old_level = logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    try:
        yield handler
    finally:
        logger.removeHandler(handler)
        logger.setLevel(old_level)


# ── Time ──────────────────────────────────────────────────────────────────────

class AceTimer:
    """A registered reactor timer: callback(eventtime) -> next waketime."""

    def __init__(self, callback: Callable[[float], float], waketime: float,
                 seq: int = 0) -> None:
        """
        :param callback: timer function
        :param waketime: next time it is due
        :param seq: registration order, breaks waketime ties
        """
        self.callback = callback
        self.waketime = waketime
        self.seq = seq


class AceCompletion:
    """Klipper ReactorCompletion stand-in on the controllable clock."""

    def __init__(self, reactor: "AceReactor") -> None:
        """
        :param reactor: reactor whose clock a timed-out wait advances
        """
        self._reactor = reactor
        self.result: Any = None
        self.done = False

    def complete(self, result: Any) -> None:
        """
        :param result: value wait() returns
        """
        self.result = result
        self.done = True

    def test(self) -> bool:
        """
        :return bool: True once completed
        """
        return self.done

    def wait(self, waketime: float = 9_999_999_999.0, waketime_result: Any = None) -> Any:
        """
        Return the result, or time out at once by moving the clock to waketime.

        :param waketime: deadline
        :param waketime_result: value returned on timeout
        :return Any: the completed result or waketime_result
        """
        if self.done:
            return self.result
        if waketime < self._reactor.NEVER:
            self._reactor.now = max(self._reactor.now, waketime)
        return waketime_result


class AcePending(NamedTuple):
    """A queued register_callback entry."""
    callback: Callable[[float], Any]
    waketime: float
    completion: AceCompletion
    seq: int


class AceMutex:
    """Reactor mutex stand-in."""

    def __init__(self, is_locked: bool = False) -> None:
        """
        :param is_locked: initial state
        """
        self.is_locked = is_locked

    def test(self) -> bool:
        """
        :return bool: whether it is held
        """
        return self.is_locked

    def lock(self) -> None:
        """Take it."""
        self.is_locked = True

    def unlock(self) -> None:
        """Release it."""
        self.is_locked = False

    def __enter__(self) -> "AceMutex":
        """
        Take it for a with block.

        :return AceMutex: the mutex
        """
        self.lock()
        return self

    def __exit__(self, *exc: Any) -> None:
        """
        Release it when the with block ends.

        :param exc: exception details, ignored
        """
        self.unlock()


class AceReactor:
    """
    Controllable reactor and monotonic clock.

    monotonic() reads .now. pause(t) moves the clock to t (never back) and then
    calls on_pause(now) when set, so wait loops end on their own deadline; it
    runs no timers or callbacks. A timed-out completion wait moves the clock to
    its deadline too.

    register_callback queues an AcePending in .pending (with inline_callbacks,
    one already due runs at once instead). run_callbacks() runs the due ones
    without moving the clock. advance(dt) runs every callback and timer due by
    now + dt in time order (waketime, then registration order), each with the
    clock at its own waketime, then leaves the clock at now + dt. Both raise
    RuntimeError after MAX_EVENTS runs, which a timer returning a time <= now
    reaches.

    Recorded entry points: pause, register_callback, register_timer,
    update_timer, unregister_timer and register_fd are Hooks and
    unregister_fd a Recorder, so .calls and .call_count work as on
    FakeReactor. monotonic, completion, mutex, run_callbacks and advance are
    plain methods. Every callback and timer runs inside dispatch_context(),
    which AcePrinter points at its connection router.

    There is deliberately no register_async_callback: run_off_reactor then runs
    the autodetect inline instead of on a worker thread.
    """
    NOW = 0.0
    NEVER = 9_999_999_999.0
    MAX_EVENTS = 10_000

    def __init__(self, start: float = 100.0, inline_callbacks: bool = False) -> None:
        """
        :param start: initial monotonic time
        :param inline_callbacks: run a due register_callback callback immediately
        """
        self.now = start
        self.inline_callbacks = inline_callbacks
        self.on_pause: Optional[Callable[[float], None]] = None
        self.dispatch_context: Callable[[], Any] = contextlib.nullcontext
        self.timers: List[AceTimer] = []
        self.pending: List[AcePending] = []
        self.fds: List[Tuple[int, Callable[[float], None]]] = []
        self._seq = 0
        self.pause = Hook(self._pause)
        self.register_callback = Hook(self._register_callback)
        self.register_timer = Hook(self._register_timer)
        self.update_timer = Hook(self._update_timer)
        self.unregister_timer = Hook(self._unregister_timer)
        self.register_fd = Hook(self._register_fd)
        self.unregister_fd = Recorder()

    def monotonic(self) -> float:
        """
        :return float: the current clock
        """
        return self.now

    def _next_seq(self) -> int:
        """
        :return int: the next registration number
        """
        self._seq += 1
        return self._seq

    def _pause(self, waketime: float) -> float:
        """
        :param waketime: time to sleep until
        :return float: the clock after the pause
        """
        if waketime < self.NEVER:
            self.now = max(self.now, waketime)
        if self.on_pause is not None:
            self.on_pause(self.now)
        return self.now

    def _register_callback(self, callback: Callable[[float], Any],
                           waketime: float = 0.0) -> AceCompletion:
        """
        :param callback: called with the event time
        :param waketime: earliest time it may run
        :return AceCompletion: completed with the callback's result once it runs
        """
        entry = AcePending(callback, waketime, AceCompletion(self), self._next_seq())
        if self.inline_callbacks and waketime <= self.now:
            self._run_callback(entry)
        else:
            self.pending.append(entry)
        return entry.completion

    def _register_timer(self, callback: Callable[[float], float],
                        waketime: float = 9_999_999_999.0) -> AceTimer:
        """
        :param callback: timer function
        :param waketime: first due time
        :return AceTimer: the timer handle
        """
        timer = AceTimer(callback, waketime, self._next_seq())
        self.timers.append(timer)
        return timer

    def _update_timer(self, timer: AceTimer, waketime: float) -> None:
        """
        :param timer: handle from register_timer
        :param waketime: new due time
        """
        timer.waketime = waketime

    def _unregister_timer(self, timer: AceTimer) -> None:
        """
        :param timer: handle from register_timer
        """
        if timer in self.timers:
            self.timers.remove(timer)

    def _register_fd(self, fd: int, callback: Callable[[float], None]) -> Tuple[int, Any]:
        """
        :param fd: file descriptor
        :param callback: read handler
        :return tuple: the fd handle
        """
        self.fds.append((fd, callback))
        return (fd, callback)

    def completion(self) -> AceCompletion:
        """
        :return AceCompletion: a new completion
        """
        return AceCompletion(self)

    def mutex(self, is_locked: bool = False) -> AceMutex:
        """
        :param is_locked: initial state
        :return AceMutex: a new mutex
        """
        return AceMutex(is_locked)

    def _run_callback(self, entry: AcePending) -> None:
        """
        :param entry: callback to run now and complete
        """
        with self.dispatch_context():
            entry.completion.complete(entry.callback(self.now))

    def _next_event(self, until: float, timers: bool,
                    match: Optional[Callable[[AcePending], bool]] = None
                    ) -> Optional[Union[AcePending, AceTimer]]:
        """
        :param until: latest waketime that counts as due
        :param timers: consider timers as well as callbacks
        :param match: only callbacks it accepts
        :return Optional[Union[AcePending, AceTimer]]: the earliest due event
        """
        events: List[Union[AcePending, AceTimer]] = [
            e for e in self.pending if e.waketime <= until and (match is None or match(e))]
        if timers:
            events.extend(t for t in self.timers if t.waketime <= until)
        if not events:
            return None
        return min(events, key=lambda e: (e.waketime, e.seq))

    def _dispatch(self, event: Union[AcePending, AceTimer]) -> None:
        """
        Move the clock to the event's waketime (never back) and run it.

        :param event: a due callback or timer
        """
        self.now = max(self.now, event.waketime)
        if isinstance(event, AceTimer):
            with self.dispatch_context():
                event.waketime = event.callback(self.now)
            return
        self.pending.remove(event)
        self._run_callback(event)

    def run_callbacks(self, match: Optional[Callable[[AcePending], bool]] = None) -> int:
        """
        Run every queued callback due now, including ones they queue, in time
        order without moving the clock.

        :param match: only run the callbacks it accepts
        :return int: how many ran
        """
        for ran in range(self.MAX_EVENTS):
            event = self._next_event(self.now, timers=False, match=match)
            if event is None:
                return ran
            self._dispatch(event)
        error_str = f"run_callbacks: still running after {self.MAX_EVENTS} callbacks"
        raise RuntimeError(error_str)

    def advance(self, seconds: float) -> None:
        """
        Move the clock forward, running due callbacks and timers in time order.

        :param seconds: how far to move the clock
        """
        end = self.now + seconds
        for _ in range(self.MAX_EVENTS):
            event = self._next_event(end, timers=True)
            if event is None:
                self.now = max(self.now, end)
                return
            self._dispatch(event)
        error_str = (f"advance: over {self.MAX_EVENTS} events before {end}; a timer that "
                     f"returns a time <= now never lets the clock move")
        raise RuntimeError(error_str)


# ── G-code ────────────────────────────────────────────────────────────────────

class AceGcode:
    """Klipper gcode object stand-in. Registration is strict, as Klipper's is: a
    second registration of the same command raises. respond_info/respond_raw
    land in .messages as ("info", msg) / ("raw", msg). run_script_from_command
    and run_script are Hooks that also append ("run_script_from_command" /
    "run_script", script) to the timeline."""
    error = CommandError

    def __init__(self, timeline: Optional[Timeline] = None) -> None:
        """
        Start with no commands and no messages.

        :param timeline: shared ordered record, a new list when None
        """
        self.timeline: Timeline = timeline if timeline is not None else []
        self.commands: Dict[str, Callable[..., Any]] = {}
        self.mux_commands: Dict[Tuple[str, str, Any], Callable[..., Any]] = {}
        self.messages: List[Tuple[str, str]] = []
        self.run_script_from_command = Hook(self._run_script_from_command)
        self.run_script = Hook(self._run_script)

    def _run_script_from_command(self, script: str) -> None:
        """
        :param script: g-code text
        """
        self.timeline.append(("run_script_from_command", script))

    def _run_script(self, script: str) -> None:
        """
        :param script: g-code text
        """
        self.timeline.append(("run_script", script))

    def register_command(self, cmd: str, func: Optional[Callable[..., Any]],
                         when_not_ready: bool = False, desc: Optional[str] = None) -> None:
        """
        :param cmd: command name
        :param func: handler, or None to remove the command
        :param when_not_ready: Klipper's flag, ignored
        :param desc: help text, ignored
        """
        if func is None:
            self.commands.pop(cmd, None)
            return
        if cmd in self.commands:
            error_str = f"gcode command {cmd} already registered"
            raise configparser.Error(error_str)
        self.commands[cmd] = func

    def register_mux_command(self, cmd: str, key: str, value: Any,
                             func: Callable[..., Any], desc: Optional[str] = None) -> None:
        """
        :param cmd: command name
        :param key: mux parameter, e.g. UNIT
        :param value: mux value, e.g. the unit name
        :param func: handler
        :param desc: help text, ignored
        """
        if (cmd, key, value) in self.mux_commands:
            error_str = f"mux command {cmd} {key} {value} already registered"
            raise configparser.Error(error_str)
        self.mux_commands[(cmd, key, value)] = func

    def respond_info(self, msg: str, log: bool = True) -> None:
        """
        :param msg: console line
        :param log: Klipper's flag, ignored
        """
        self.messages.append(("info", msg))

    def respond_raw(self, msg: str) -> None:
        """
        :param msg: console line
        """
        self.messages.append(("raw", msg))


class AceGcmd:
    """
    GCodeCommand stand-in with Klipper's get semantics: a missing parameter
    with no default raises gcmd.error, and minval/maxval/above/below are
    enforced. Values are kept as given; get() passes them through str like
    Klipper's parser. respond_info lands in .responses (plain strings, as on
    FakeGcmd) and in .messages as ("info", msg); respond_raw as ("raw", msg).
    """
    error = CommandError

    def __init__(self, params: Optional[Mapping[str, Any]] = None,
                 commandline: str = "") -> None:
        """
        :param params: command parameters by upper-case name
        :param commandline: the raw line, used in error text
        """
        self._params: Dict[str, Any] = dict(params or {})
        self._commandline = commandline
        self.responses: List[str] = []
        self.messages: List[Tuple[str, str]] = []

    def get(self, name: str, default: Any = _NO_DEFAULT, parser: Callable[[Any], Any] = str,
            minval: Optional[float] = None, maxval: Optional[float] = None,
            above: Optional[float] = None, below: Optional[float] = None) -> Any:
        """
        :param name: parameter name
        :param default: value when absent; absent with no default raises
        :param parser: converts the value
        :param minval: inclusive lower bound
        :param maxval: inclusive upper bound
        :param above: exclusive lower bound
        :param below: exclusive upper bound
        :return Any: the parsed value or the default
        """
        value = self._params.get(name)
        if value is None:
            if default is _NO_DEFAULT:
                error_str = f"Error on '{self._commandline}': missing {name}"
                raise self.error(error_str)
            return default
        try:
            value = parser(value)
        except (TypeError, ValueError):
            error_str = f"Error on '{self._commandline}': unable to parse {value}"
            raise self.error(error_str)
        for bound, failed, text in ((minval, lambda b: value < b, "must have minimum of"),
                                    (maxval, lambda b: value > b, "must have maximum of"),
                                    (above, lambda b: value <= b, "must be above"),
                                    (below, lambda b: value >= b, "must be below")):
            if bound is not None and failed(bound):
                error_str = f"Error on '{self._commandline}': {name} {text} {bound}"
                raise self.error(error_str)
        return value

    def get_int(self, name: str, default: Any = _NO_DEFAULT, minval: Optional[int] = None,
                maxval: Optional[int] = None) -> Any:
        """
        :param name: parameter name
        :param default: value when absent
        :param minval: inclusive lower bound
        :param maxval: inclusive upper bound
        :return Any: the int value or the default
        """
        return self.get(name, default, parser=int, minval=minval, maxval=maxval)

    def get_float(self, name: str, default: Any = _NO_DEFAULT,
                  minval: Optional[float] = None, maxval: Optional[float] = None,
                  above: Optional[float] = None, below: Optional[float] = None) -> Any:
        """
        :param name: parameter name
        :param default: value when absent
        :param minval: inclusive lower bound
        :param maxval: inclusive upper bound
        :param above: exclusive lower bound
        :param below: exclusive upper bound
        :return Any: the float value or the default
        """
        return self.get(name, default, parser=float, minval=minval, maxval=maxval,
                        above=above, below=below)

    def get_command_parameters(self) -> Dict[str, Any]:
        """
        :return dict: a copy of the parameters
        """
        return dict(self._params)

    def get_commandline(self) -> str:
        """
        :return str: the raw line
        """
        return self._commandline

    def respond_info(self, msg: str, log: bool = True) -> None:
        """
        :param msg: reply line
        :param log: Klipper's flag, ignored
        """
        self.responses.append(msg)
        self.messages.append(("info", msg))

    def respond_raw(self, msg: str) -> None:
        """
        :param msg: reply line
        """
        self.messages.append(("raw", msg))


def make_gcmd(commandline: str = "", **params: Any) -> AceGcmd:
    """
    Build a g-code command, e.g. make_gcmd(UNIT="Ace_1", SLOT=0).

    :param commandline: raw line for error text
    :param params: parameters by upper-case name
    :return AceGcmd: the command
    """
    return AceGcmd(params, commandline=commandline)


# ── AFC core and collaborators ────────────────────────────────────────────────

class AceFunction(FakeFunction):
    """afc.function stand-in: FakeFunction's print state plus the
    AFC_functions helpers the ACE unit, afcUnit and AFCLane call. LED helpers
    pass values through unchanged. get_extruder_pos returns .result (0.0, no
    extrusion); get_filament_status builds AFC's "<state>:<led>" text."""

    def __init__(self, printing: bool = False, paused: bool = False,
                 in_print_flag: bool = False) -> None:
        """
        :param printing: is_printing() result
        :param paused: is_paused() result
        :param in_print_flag: in_print() result
        """
        super().__init__(printing=printing, paused=paused, in_print_flag=in_print_flag)
        self.afc_led = Recorder()
        self.TcmdAssign = Recorder()
        self.ConfigRewrite = Recorder()
        self.check_for_td1_id = Recorder(result=(True, ""))
        self.register_mux_command = Recorder()
        # AFC's bowden-calibration check: (failed_check, reset_tool_start, msg).
        self._calibration_check_tool_start = Recorder(result=(False, False, ""))
        self.log_toolhead_pos = Recorder()
        self.get_extruder_pos = Recorder(result=0.0)
        self.check_macro_present = Recorder(result=False)
        self.get_filament_status = Hook(self._get_filament_status)

    def HexConvert(self, value: str) -> str:
        """
        :param value: "r,g,b,w" colour
        :return str: the value unchanged
        """
        return value

    def HexToLedString(self, value: str) -> str:
        """
        :param value: hex colour
        :return str: the value unchanged
        """
        return value

    def _get_filament_status(self, cur_lane: Any) -> str:
        """
        AFC_functions.get_filament_status: lane state and its LED colour.

        :param cur_lane: the lane
        :return str: "In Tool:", "Ready:", "Prep:" or "Not Ready:" plus the colour
        """
        if not cur_lane.prep_state:
            state, led = "Not Ready", cur_lane.led_not_ready
        elif not cur_lane.load_state:
            state, led = "Prep", cur_lane.led_prep_loaded
        elif (cur_lane.extruder_obj is not None
              and cur_lane.extruder_obj.lane_loaded == cur_lane.name):
            state, led = "In Tool", cur_lane.led_tool_loaded
        else:
            state, led = "Ready", cur_lane.led_ready
        return f"{state}:{self.HexConvert(led).split(':')[-1]}"


class AceError:
    """afc.error stand-in. AFC_error and handle_lane_failure are Hooks with
    AFC_error.py's signatures that also append ("AFC_error", msg) /
    ("handle_lane_failure", (lane name, message)) to the timeline."""

    def __init__(self, timeline: Optional[Timeline] = None) -> None:
        """
        Record every error call.

        :param timeline: shared ordered record, a new list when None
        """
        self.timeline: Timeline = timeline if timeline is not None else []
        self.AFC_error = Hook(self._afc_error)
        self.handle_lane_failure = Hook(self._handle_lane_failure)

    def _afc_error(self, msg: str, pause: bool = True,
                   stack_name: Optional[str] = None) -> None:
        """
        :param msg: error text
        :param pause: AFC's pause flag, kept in .calls
        :param stack_name: AFC's stack label, kept in .calls
        """
        self.timeline.append(("AFC_error", msg))

    def _handle_lane_failure(self, cur_lane: Any, message: str, pause: bool = True) -> None:
        """
        :param cur_lane: the failed lane
        :param message: failure text
        :param pause: AFC's pause flag, kept in .calls
        """
        self.timeline.append(("handle_lane_failure",
                              (getattr(cur_lane, "name", cur_lane), message)))


class AceSpool:
    """afc.spool stand-in."""

    def __init__(self) -> None:
        """
        Record every spool call; no staged spool id.
        """
        self.next_spool_id: Optional[int] = None
        self.set_active_spool = Recorder()
        self.clear_values = Recorder()
        self.set_spoolID = Recorder()
        self._set_values = Recorder()
        self.set_snapmaker_filament_params = Recorder()


class FakeDeltaTime:
    """afc.afcDeltaTime stand-in. Lines land in .messages without the real
    one's wall-clock suffix, as ("debug"|"info", msg)."""

    def __init__(self) -> None:
        """
        Start unstarted.
        """
        self.start_time: Optional[float] = None
        self.messages: List[Tuple[str, str]] = []

    def set_start_time(self) -> None:
        """Start the clock."""
        self.start_time = 0.0

    def log_with_time(self, msg: str, debug: bool = True) -> None:
        """
        :param msg: line
        :param debug: debug level when True, info otherwise
        """
        self.messages.append(("debug" if debug else "info", msg))

    def log_major_delta(self, msg: str, debug: bool = True) -> float:
        """
        :param msg: line
        :param debug: ignored, as in the real one
        :return float: 0.0
        """
        self.messages.append(("info", msg))
        return 0.0

    def log_total_time(self, msg: str) -> float:
        """
        :param msg: line
        :return float: 0.0
        """
        self.messages.append(("info", msg))
        return 0.0


class AceAFC:
    """
    The AFC core object, with extras/AFC.py's defaults for every attribute
    afcUnit.__init__ and AFCLane.__init__ inherit, plus what the ACE load and
    unload flows and AFCLane reach: toolhead (the printer's, as AFC.py looks
    it up), common_density_values (the lane.material setter), post_unload_macro,
    move_e_pos (a Hook with AFC.py's signature that appends ("move_e_pos",
    (e_amount, speed, log_string, wait_tool)) to the timeline) and
    do_tool_cut_tip_form (a Recorder).
    """

    def __init__(self, reactor: AceReactor, logger: AceLogger, gcode: AceGcode,
                 toolhead: Optional["AceToolhead"] = None,
                 timeline: Optional[Timeline] = None) -> None:
        """
        :param reactor: shared reactor
        :param logger: AFC logger
        :param gcode: shared gcode object
        :param toolhead: the printer's toolhead, a new one when None
        :param timeline: shared ordered record, gcode's when None
        """
        self.timeline: Timeline = timeline if timeline is not None else gcode.timeline
        self.reactor = reactor
        self.logger = logger
        self.gcode = gcode
        self.toolhead = toolhead if toolhead is not None else AceToolhead()
        self.function = AceFunction()
        self.error = AceError(self.timeline)
        self.spool = AceSpool()
        self.afcDeltaTime = FakeDeltaTime()
        self.save_vars = Recorder()
        self.move_e_pos = Hook(self._move_e_pos)
        self.do_tool_cut_tip_form = Recorder()
        self.moonraker: Optional[Any] = None
        self.spoolman: Optional[Any] = None
        self.lanes: Dict[str, Any] = {}
        self.units: Dict[str, Any] = {}
        self.hubs: Dict[str, Any] = {}
        self.tools: Dict[str, Any] = {}
        self.buffers: Dict[str, Any] = {}
        self.current: Optional[str] = None
        self.current_loading: Optional[str] = None
        self.in_toolchange = False
        self.error_state = False
        self.active_led_effects: List[str] = []
        self.td1_present = False
        self.td1_defined = False
        self.default_material_type: Optional[str] = None
        self.common_density_values: List[str] = ["PLA:1.24", "PETG:1.23", "ABS:1.04",
                                                 "ASA:1.07"]
        self.post_unload_macro: Optional[str] = None
        self.show_macros = True
        self.testing = False
        self.load_to_hub = True
        self.enable_sensors_in_gui = False
        self.debounce_delay = 0.0
        self.enable_hub_runout = True
        self.led_off = "0,0,0,0"
        self.led_fault = "1,0,0,0"
        self.led_ready = "0,0.8,0,0"
        self.led_not_ready = "1,0,0,0"
        self.led_loading = "1,1,1,0"
        self.led_unloading = "1,1,.5,0"
        self.led_tool_loaded = "0,0,1,0"
        self.led_tool_loaded_idle = "0.4,0.4,0,0"
        self.led_tool_unloaded = "1,0,0,0"
        self.led_spool_illum = "1,1,1,1"
        self.led_use_filament_color = False
        self.long_moves_speed = 100.0
        self.long_moves_accel = 400.0
        self.short_moves_speed = 25.0
        self.short_moves_accel = 400.0
        self.short_move_dis = 10.0
        self.max_move_dis = 999999.0
        self.n20_break_delay_time = 0.2
        self.load_then_home_var = True
        self.load_undershoot = 20.0
        self.tool_max_unload_attempts = 4
        self.rev_long_moves_speed_factor = 1.0
        self.enable_assist = True
        self.enable_assist_weight = 500.0
        self.assisted_unload = True
        self.unload_on_runout = False
        self.td1_when_loaded = False
        self.auto_spool_switch = False
        self.auto_spool_switch_threshold = 25.0

    def _move_e_pos(self, e_amount: float, speed: float, log_string: str = "",
                    wait_tool: bool = False) -> None:
        """
        :param e_amount: extruder move, mm (negative retracts)
        :param speed: move speed, mm/s
        :param log_string: AFC's log label
        :param wait_tool: wait for the toolhead
        """
        self.timeline.append(("move_e_pos", (e_amount, speed, log_string, wait_tool)))


class AceHub:
    """AFC_hub stand-in; switch_pin "virtual" is the pinless hub ACE units use."""

    def __init__(self, name: str, switch_pin: Optional[str] = "virtual",
                 afc_bowden_length: float = 900.0) -> None:
        """
        :param name: hub name
        :param switch_pin: "virtual", a pin name, or None
        :param afc_bowden_length: hub to toolhead sensor, mm
        """
        self.name = name
        self.switch_pin = switch_pin
        self.afc_bowden_length = afc_bowden_length
        self.td1_bowden_length = afc_bowden_length - 50
        self.afc_unload_bowden_length = afc_bowden_length
        self.lanes: Dict[str, Any] = {}
        self.state = False

    def is_virtual_pin(self) -> bool:
        """
        :return bool: True for a "virtual" switch pin, as afc_hub decides
        """
        return self.switch_pin.lower() == "virtual" if self.switch_pin else False


class _AceToolheadExtruder:
    """A Klipper extruder object, what toolhead.get_extruder() returns."""

    def __init__(self, name: str) -> None:
        """
        :param name: physical extruder name
        """
        self._name = name

    def get_name(self) -> str:
        """
        :return str: the extruder name
        """
        return self._name


class AceToolhead:
    """
    Klipper toolhead stand-in. It keeps one extruder object per name
    (extruder(name)), so get_extruder() returns the same object on every call,
    as Klipper's does, and AFCLane.activate_toolhead_extruder's identity check
    works. set_extruder (a Hook) makes the given extruder active;
    flush_step_generation is a Recorder. active_extruder=None makes
    get_extruder raise.
    """

    def __init__(self, active_extruder: Optional[str] = "extruder") -> None:
        """
        :param active_extruder: name get_extruder().get_name() returns
        """
        self.active_extruder = active_extruder
        self.extruders: Dict[str, _AceToolheadExtruder] = {}
        self.flush_step_generation = Recorder()
        self.set_extruder = Hook(self._set_extruder)

    def extruder(self, name: str) -> _AceToolheadExtruder:
        """
        :param name: physical extruder name
        :return _AceToolheadExtruder: that extruder's one object
        """
        found = self.extruders.get(name)
        if found is None:
            found = self.extruders[name] = _AceToolheadExtruder(name)
        return found

    def get_extruder(self) -> _AceToolheadExtruder:
        """
        :return _AceToolheadExtruder: the active extruder
        """
        if self.active_extruder is None:
            error_str = "no active extruder"
            raise RuntimeError(error_str)
        return self.extruder(self.active_extruder)

    def _set_extruder(self, extruder: _AceToolheadExtruder, extrude_pos: float) -> None:
        """
        :param extruder: extruder object to make active
        :param extrude_pos: Klipper's start position, ignored
        """
        self.active_extruder = extruder.get_name()


class AceExtruder:
    """AFC_extruder stand-in with the attributes ACE and AFCLane read.
    toolhead_extruder is the Klipper extruder object of th_extruder_name: the
    toolhead's own when one is given (AcePrinter.add_extruder does)."""

    def __init__(self, name: str = "extruder", th_extruder_name: Optional[str] = None,
                 tool_start: Optional[str] = "tool_start_pin",
                 toolhead: Optional[AceToolhead] = None,
                 reactor: Any = None) -> None:
        """
        :param name: section name
        :param th_extruder_name: physical extruder name, defaults to name
        :param tool_start: pin_tool_start value, or None
        :param toolhead: toolhead whose extruder object to use
        :param reactor: shared reactor, used by clear_stale_sensor_state's pause
        """
        self.name = name
        self.th_extruder_name = th_extruder_name if th_extruder_name is not None else name
        self.toolhead_extruder = (toolhead.extruder(self.th_extruder_name)
                                  if toolhead is not None
                                  else _AceToolheadExtruder(self.th_extruder_name))
        self.lane_loaded: Optional[str] = None
        self.lanes: Dict[str, Any] = {}
        self.tool_start = tool_start
        self.tool_start_state = False
        self.buffer_name: Optional[str] = None
        self.fila_tool_start: Optional[Any] = None
        self.filament_sensor_obj: Optional[Any] = None
        self.tool_stn = 72.0
        self.tool_stn_unload = 100.0
        self.tool_unload_speed = 25.0
        self.tool_load_speed = 25.0
        self.led_index: Optional[str] = None
        self.check_lanes = Recorder()
        self.set_status_led = Recorder()
        self.reactor = reactor

    def clear_stale_sensor_state(self, lane: Any) -> None:
        """
        AFC_extruder.clear_stale_sensor_state stand-in: same real logic,
        ported here so ACE tests exercise the production method.

        :param lane: AFCLane whose buffer latch state to clear.
        """
        if self.fila_tool_start is not None:
            self.fila_tool_start.runout_helper.filament_present = False
        self.tool_start_state = False
        buffer_obj = getattr(lane, 'buffer_obj', None)
        if (buffer_obj is not None
            and hasattr(buffer_obj, 'clear_advance_latch')):
            buffer_obj.clear_advance_latch()
        self.reactor.pause(self.reactor.monotonic() + 0.5)

    def prep_on_shuttle_check(self, lane: Any) -> str:
        """
        AFC_extruder.prep_on_shuttle_check stand-in: same real logic, ported
        here so ACE tests exercise the production method. tool_obj isn't
        modeled by this stand-in, so it's read defensively.

        :param lane: AFCLane for current extruder to easily set leds though its unit object
        :return str: Message string if lane is loaded to toolhead or in toolhead and on shuttle
        """
        msg = ""
        on_shuttle = ""
        if (getattr(self, "tool_obj", None)
            and getattr(self, "tc_unit_name", None)):
            lane.unit_obj.lane_tool_loaded_idle(lane)
            if self.on_shuttle():
                on_shuttle = " and toolhead on shuttle"
                lane.unit_obj.lane_tool_loaded(lane)
        msg += f"<span class=primary--text> in ToolHead{on_shuttle}</span>"
        return msg


class FakeHeaters:
    """heaters stand-in recording sensor factories by type name."""

    def __init__(self) -> None:
        """
        Start with no factories.
        """
        self.factories: Dict[str, Any] = {}

    def add_sensor_factory(self, sensor_type: str, factory: Any) -> None:
        """
        :param sensor_type: sensor_type name
        :param factory: sensor class
        """
        self.factories[sensor_type] = factory


class FakeMcu:
    """mcu stand-in; print time equals the reactor time."""

    def estimated_print_time(self, eventtime: float) -> float:
        """
        :param eventtime: reactor time
        :return float: the same time
        """
        return eventtime


class FakePins:
    """pins stand-in (AFCLane looks it up unconditionally)."""

    def __init__(self) -> None:
        """
        Record every call.
        """
        self.allow_multi_use_pin = Recorder()
        self.parse_pin = Recorder()
        self.setup_pin = Recorder()


class FakeButtons:
    """buttons stand-in."""

    def __init__(self) -> None:
        """
        Record every call.
        """
        self.register_buttons = Recorder()


class FakeQueryEndstops:
    """query_endstops stand-in."""

    def __init__(self) -> None:
        """
        Record every call.
        """
        self.register_endstop = Recorder()


# ── Config and printer ────────────────────────────────────────────────────────

class AceFileConfig:
    """config.fileconfig stand-in listing the printer's sections."""

    def __init__(self, printer: "AcePrinter") -> None:
        """
        :param printer: printer whose sections are listed
        """
        self._printer = printer

    def sections(self) -> List[str]:
        """
        :return list: section names in registration order
        """
        return list(self._printer.config_sections)

    def has_section(self, section: str) -> bool:
        """
        :param section: section name
        :return bool: whether it is registered
        """
        return section in self._printer.config_sections

    def options(self, section: str) -> List[str]:
        """
        :param section: section name
        :return list: its option names
        """
        return list(self._printer.section_values.get(section, {}))


class AceConfig:
    """
    Klipper ConfigWrapper stand-in with Klipper's semantics: an option with no
    default that is not set raises config.error (configparser.Error, as
    ConfigWrapper.error is), minval/maxval/above/below are enforced on set
    values, getlist returns a tuple and getchoice validates. An option whose
    value is None counts as not set.
    """
    error = configparser.Error

    def __init__(self, name: str, printer: "AcePrinter",
                 values: Optional[Mapping[str, Any]] = None) -> None:
        """
        :param name: full section name, e.g. "AFC_ACE Ace_1"
        :param printer: the printer get_printer() returns
        :param values: option values as a user would write them
        """
        self._name = name
        self._printer = printer
        self._values: Dict[str, Any] = {k: v for k, v in (values or {}).items()
                                        if v is not None}
        self.fileconfig = AceFileConfig(printer)

    def get_printer(self) -> "AcePrinter":
        """
        :return AcePrinter: the printer
        """
        return self._printer

    def get_name(self) -> str:
        """
        :return str: the section name
        """
        return self._name

    def _get(self, option: str, default: Any, parser: Callable[[Any], Any],
             minval: Optional[float] = None, maxval: Optional[float] = None,
             above: Optional[float] = None, below: Optional[float] = None) -> Any:
        """
        :param option: option name
        :param default: value when unset; unset with no default raises
        :param parser: converts a set value
        :param minval: inclusive lower bound
        :param maxval: inclusive upper bound
        :param above: exclusive lower bound
        :param below: exclusive upper bound
        :return Any: the parsed value or the default
        """
        if option not in self._values:
            if default is _NO_DEFAULT:
                error_str = f"Option '{option}' in section '{self._name}' must be specified"
                raise self.error(error_str)
            return default
        try:
            value = parser(self._values[option])
        except (TypeError, ValueError):
            error_str = f"Unable to parse option '{option}' in section '{self._name}'"
            raise self.error(error_str)
        for bound, failed, text in ((minval, lambda b: value < b, "must have minimum of"),
                                    (maxval, lambda b: value > b, "must have maximum of"),
                                    (above, lambda b: value <= b, "must be above"),
                                    (below, lambda b: value >= b, "must be below")):
            if bound is not None and failed(bound):
                error_str = f"Option '{option}' in section '{self._name}' {text} {bound}"
                raise self.error(error_str)
        return value

    def get(self, option: str, default: Any = _NO_DEFAULT, note_valid: bool = True) -> Any:
        """
        :param option: option name
        :param default: value when unset
        :param note_valid: Klipper's flag, ignored
        :return Any: the value as given, or the default
        """
        return self._get(option, default, lambda v: v)

    def getint(self, option: str, default: Any = _NO_DEFAULT, minval: Optional[int] = None,
               maxval: Optional[int] = None, note_valid: bool = True) -> Any:
        """
        :param option: option name
        :param default: value when unset
        :param minval: inclusive lower bound
        :param maxval: inclusive upper bound
        :param note_valid: Klipper's flag, ignored
        :return Any: the int value or the default
        """
        return self._get(option, default, int, minval=minval, maxval=maxval)

    def getfloat(self, option: str, default: Any = _NO_DEFAULT,
                 minval: Optional[float] = None, maxval: Optional[float] = None,
                 above: Optional[float] = None, below: Optional[float] = None,
                 note_valid: bool = True) -> Any:
        """
        :param option: option name
        :param default: value when unset
        :param minval: inclusive lower bound
        :param maxval: inclusive upper bound
        :param above: exclusive lower bound
        :param below: exclusive upper bound
        :param note_valid: Klipper's flag, ignored
        :return Any: the float value or the default
        """
        return self._get(option, default, float, minval=minval, maxval=maxval,
                         above=above, below=below)

    @staticmethod
    def _parse_bool(value: Any) -> bool:
        """
        :param value: bool or configparser boolean text
        :return bool: the parsed value
        """
        if isinstance(value, bool):
            return value
        text = str(value).strip().lower()
        if text in ("1", "yes", "true", "on"):
            return True
        if text in ("0", "no", "false", "off"):
            return False
        error_str = f"Not a boolean: {value}"
        raise ValueError(error_str)

    def getboolean(self, option: str, default: Any = _NO_DEFAULT,
                   note_valid: bool = True) -> Any:
        """
        :param option: option name
        :param default: value when unset
        :param note_valid: Klipper's flag, ignored
        :return Any: the bool value or the default
        """
        return self._get(option, default, self._parse_bool)

    def getlist(self, option: str, default: Any = _NO_DEFAULT, sep: str = ",",
                count: Optional[int] = None, note_valid: bool = True) -> Any:
        """
        :param option: option name
        :param default: value when unset
        :param sep: item separator for text values
        :param count: required item count
        :param note_valid: Klipper's flag, ignored
        :return Any: a tuple of stripped items, or the default
        """
        def parse(value: Any) -> Tuple[Any, ...]:
            """
            :param value: text split on sep, or an iterable of items
            :return tuple: the items
            """
            if isinstance(value, str):
                items = tuple(p.strip() for p in value.split(sep)) if value.strip() else ()
            else:
                items = tuple(value)
            if count is not None and len(items) != count:
                error_str = f"Option '{option}' in section '{self._name}' must have {count} items"
                raise ValueError(error_str)
            return items
        return self._get(option, default, parse)

    def getchoice(self, option: str, choices: Union[Mapping[Any, Any], List[Any]],
                  default: Any = _NO_DEFAULT, note_valid: bool = True) -> Any:
        """
        :param option: option name
        :param choices: valid values (a list, or a mapping to results)
        :param default: value when unset
        :param note_valid: Klipper's flag, ignored
        :return Any: the mapped choice
        """
        if isinstance(choices, list):
            choices = {c: c for c in choices}
        value = self.get(option, default)
        if value not in choices:
            error_str = (f"Choice '{value}' for option '{option}' in section "
                         f"'{self._name}' is not a valid choice")
            raise self.error(error_str)
        return choices[value]

    def get_prefix_options(self, prefix: str) -> List[str]:
        """
        :param prefix: option name prefix
        :return list: matching option names
        """
        return [o for o in self._values if o.startswith(prefix)]

    def getsection(self, section: str) -> "AceConfig":
        """
        :param section: another section's name
        :return AceConfig: that section, with its registered values
        """
        return AceConfig(section, self._printer, self._printer.section_values.get(section, {}))

    def has_section(self, section: str) -> bool:
        """
        :param section: section name
        :return bool: whether it is registered
        """
        return section in self._printer.config_sections

    def deprecate(self, option: str, value: Any = None) -> None:
        """
        :param option: ignored
        :param value: ignored
        """


# Printers the builders made, so reset_ace_globals can stop their units'
# serial-log listeners.
_BUILT_PRINTERS: "weakref.WeakSet[AcePrinter]" = weakref.WeakSet()


class AcePrinter:
    """
    Klipper printer stand-in and the registry a test's objects share.

    lookup_object/load_object are strict, as Klipper's are: an unregistered
    name with no default raises config_error. Pre-registered objects: "AFC"
    (.afc), "AFC_functions" (.afc.function), "gcode" (.gcode), "toolhead"
    (.toolhead, also .afc.toolhead), "heaters" (.heaters), "mcu" (.mcu),
    "pins", "buttons", "query_endstops". .logger is the AFC logger, .reactor
    the shared clock, .connections the AceConnectionRouter.

    send_event records (event, args) in .events and runs the handlers inside
    connections.routed(). .timeline is one ordered list of (kind, detail)
    across the fakes:
      ("send_command" | "send_command_async", (method, params))  a scripted link
      ("connect" | "disconnect" | "reconnect", serial port)      a scripted link
      ("move_e_pos", (e_amount, speed, log_string, wait_tool))  afc.move_e_pos
      ("run_script_from_command" | "run_script", script)        gcode
      ("send_event", (event, args))                              send_event
      ("AFC_error", msg) / ("handle_lane_failure", (lane, msg))  afc.error
    """
    command_error = CommandError
    config_error = configparser.Error

    def __init__(self, monotonic: float = 100.0, active_extruder: Optional[str] = "extruder",
                 inline_callbacks: bool = False, route_connections: bool = True) -> None:
        """
        :param monotonic: initial reactor time
        :param active_extruder: toolhead's active extruder name, None to raise
        :param inline_callbacks: run due reactor callbacks at registration
        :param route_connections: route afcACE._make_connection to scripted links
        """
        self.timeline: Timeline = []
        self.reactor = AceReactor(monotonic, inline_callbacks=inline_callbacks)
        self.logger = AceLogger()
        self.gcode = AceGcode(self.timeline)
        self.toolhead = AceToolhead(active_extruder)
        self.afc = AceAFC(self.reactor, self.logger, self.gcode, self.toolhead, self.timeline)
        self.heaters = FakeHeaters()
        self.mcu = FakeMcu()
        self.objects: Dict[str, Any] = {
            "AFC": self.afc, "AFC_functions": self.afc.function, "gcode": self.gcode,
            "toolhead": self.toolhead, "heaters": self.heaters, "mcu": self.mcu,
            "pins": FakePins(), "buttons": FakeButtons(), "query_endstops": FakeQueryEndstops(),
        }
        self.config_sections: List[str] = []
        self.section_values: Dict[str, Dict[str, Any]] = {}
        self.event_handlers: Dict[str, List[Callable[..., Any]]] = {}
        self.events: List[Tuple[str, Tuple[Any, ...]]] = []
        self.shutdowns: List[str] = []
        self.start_args: Dict[str, Any] = {}
        self.state_message = "Printer is ready"
        self.connections = AceConnectionRouter(self)
        self.connections.enabled = route_connections
        self.reactor.dispatch_context = self.connections.routed
        _BUILT_PRINTERS.add(self)

    def get_reactor(self) -> AceReactor:
        """
        :return AceReactor: the shared reactor
        """
        return self.reactor

    def get_start_args(self) -> Dict[str, Any]:
        """
        :return dict: start arguments (log_file, debugoutput)
        """
        return self.start_args

    def lookup_object(self, name: str, default: Any = _NO_DEFAULT) -> Any:
        """
        :param name: object name
        :param default: value when unregistered; none given raises
        :return Any: the object or the default
        """
        if name in self.objects:
            return self.objects[name]
        if default is _NO_DEFAULT:
            error_str = f"Unknown config object '{name}'"
            raise self.config_error(error_str)
        return default

    def lookup_objects(self, module: Optional[str] = None) -> List[Tuple[str, Any]]:
        """
        :param module: name or prefix to filter on
        :return list: (name, object) pairs, as Klipper orders them
        """
        if module is None:
            return list(self.objects.items())
        prefix = module + " "
        found = [(n, o) for n, o in self.objects.items() if n.startswith(prefix)]
        if module in self.objects:
            return [(module, self.objects[module])] + found
        return found

    def load_object(self, config: Any, section: str, default: Any = _NO_DEFAULT) -> Any:
        """
        :param config: the asking section's config, unused
        :param section: object name
        :param default: value when unregistered; none given raises
        :return Any: the object or the default
        """
        if section in self.objects:
            return self.objects[section]
        if default is _NO_DEFAULT:
            error_str = f"Unable to load module '{section}'"
            raise self.config_error(error_str)
        return default

    def add_object(self, name: str, obj: Any) -> None:
        """
        :param name: object name
        :param obj: the object
        """
        if name in self.objects:
            error_str = f"Printer object '{name}' already created"
            raise self.config_error(error_str)
        self.objects[name] = obj

    def add_section(self, name: str, obj: Any,
                    values: Optional[Mapping[str, Any]] = None) -> None:
        """
        Register a config section's object, as Klipper does when it loads one.

        :param name: full section name
        :param obj: the section's object
        :param values: the section's option values
        """
        self.add_object(name, obj)
        self.config_sections.append(name)
        self.section_values[name] = dict(values or {})

    def add_extruder(self, name: str = "extruder", **options: Any) -> AceExtruder:
        """
        Register [AFC_extruder <name>] whose toolhead_extruder is this toolhead's.

        :param name: section name
        :param options: AceExtruder keywords (th_extruder_name, tool_start)
        :return AceExtruder: the extruder
        """
        extruder = AceExtruder(name, toolhead=self.toolhead, reactor=self.reactor, **options)
        self.add_section(f"AFC_extruder {name}", extruder)
        return extruder

    def add_hub(self, name: str, **options: Any) -> AceHub:
        """
        Register [AFC_hub <name>].

        :param name: hub name
        :param options: AceHub keywords (switch_pin, afc_bowden_length)
        :return AceHub: the hub
        """
        hub = AceHub(name, **options)
        self.add_section(f"AFC_hub {name}", hub)
        return hub

    def register_event_handler(self, event: str, callback: Callable[..., Any]) -> None:
        """
        :param event: event name
        :param callback: handler
        """
        self.event_handlers.setdefault(event, []).append(callback)

    def remove_event_handler(self, event: str, callback: Callable[..., Any]) -> None:
        """
        Drop a handler, e.g. after a builder has run it the one time Klipper would.

        :param event: event name
        :param callback: handler to drop (bound methods compare equal)
        """
        handlers = self.event_handlers.get(event, [])
        handlers[:] = [h for h in handlers if h != callback]

    def send_event(self, event: str, *params: Any) -> List[Any]:
        """
        :param event: event name
        :param params: handler arguments
        :return list: each handler's result
        """
        self.events.append((event, params))
        self.timeline.append(("send_event", (event, params)))
        with self.connections.routed():
            return [cb(*params) for cb in list(self.event_handlers.get(event, []))]

    def invoke_shutdown(self, msg: str) -> None:
        """
        :param msg: shutdown reason
        """
        self.shutdowns.append(msg)

    def clear_records(self, pending: bool = False) -> None:
        """
        Clear every record: logs, messages, events, shutdowns, the timeline,
        Recorder calls, the router's .created and each unit's connection
        records. State is kept, reactor callbacks too unless pending.

        :param pending: also drop the queued reactor callbacks
        """
        self.events.clear()
        self.shutdowns.clear()
        self.timeline.clear()
        self.logger.clear()
        self.gcode.messages.clear()
        self.afc.afcDeltaTime.messages.clear()
        self.connections.created.clear()
        if pending:
            self.reactor.pending.clear()
        for obj in (self, self.reactor, self.gcode, self.afc, self.afc.function,
                    self.afc.error, self.afc.spool, *self.objects.values()):
            if hasattr(obj, "__dict__"):
                _reset_recorders(obj)
            conn = getattr(obj, "_ace", None)
            if isinstance(conn, ScriptedTransport):
                conn.clear()
            elif isinstance(getattr(conn, "_serial", None), FakeSerial):
                conn._serial.frames.clear()


def make_ace_printer(monotonic: float = 100.0, active_extruder: Optional[str] = "extruder",
                     inline_callbacks: bool = False, log_file: Optional[str] = None,
                     route_connections: bool = True) -> AcePrinter:
    """
    Build the printer a test's ACE objects share.

    :param monotonic: initial reactor time
    :param active_extruder: toolhead's active extruder, None makes the lookup raise
    :param inline_callbacks: run due reactor callbacks at registration
    :param log_file: klippy log path; the serial log file goes beside it
    :param route_connections: route afcACE._make_connection to scripted links
        (AceConnectionRouter); False leaves the real connection classes
    :return AcePrinter: the printer
    """
    printer = AcePrinter(monotonic, active_extruder=active_extruder,
                         inline_callbacks=inline_callbacks,
                         route_connections=route_connections)
    if log_file is not None:
        printer.start_args["log_file"] = log_file
    return printer


# ── Connections ───────────────────────────────────────────────────────────────

def ace_status(*slots: Union[str, Mapping[str, Any]], status: str = "ready",
               **fields: Any) -> Dict[str, Any]:
    """
    Build a get_status result, e.g. ace_status("ready", "empty", temp=25).

    :param slots: per slot, its status text or its full dict
    :param status: unit status
    :param fields: other top-level fields
    :return dict: {"status": ..., "slots": [{"index": i, ...}], **fields}
    """
    built = []
    for index, slot in enumerate(slots):
        entry: Dict[str, Any] = {"index": index}
        entry.update({"status": slot} if isinstance(slot, str) else dict(slot))
        built.append(entry)
    result: Dict[str, Any] = {"status": status, "slots": built}
    result.update(fields)
    return result


def default_device_info(ace2: bool = False, ace_index: int = 1) -> Dict[str, Any]:
    """
    The get_info reply a stock unit gives, the scripted links' default.

    :param ace2: ACE 2 Pro (stock host-decode firmware) when True
    :param ace_index: V1 firmware id (cable order)
    :return dict: model and firmware fields
    """
    if ace2:
        return {"model": "ACE 2 Pro", "firmware": "V1.1.31"}
    return {"id": ace_index, "slots": 4, "model": "Anycubic Color Engine Pro",
            "firmware": "V1.3.856"}


class FakeSerial:
    """pyserial stand-in for a real connection: records written frames, calls
    on_write(frame) after each write, and serves .rx bytes to read()."""

    def __init__(self, write_error: Optional[BaseException] = None) -> None:
        """
        :param write_error: raised by write() when set
        """
        self.frames: List[bytes] = []
        self.rx = bytearray()
        self.write_error = write_error
        self.on_write: Optional[Callable[[bytes], None]] = None
        self.closed = False

    def write(self, frame: bytes) -> None:
        """
        :param frame: bytes the connection sends
        """
        if self.write_error is not None:
            raise self.write_error
        self.frames.append(bytes(frame))
        if self.on_write is not None:
            self.on_write(bytes(frame))

    def flush(self) -> None:
        """Nothing buffered."""

    def read(self, size: int = 1) -> bytes:
        """
        :param size: most bytes to return
        :return bytes: queued bytes from .rx
        """
        data = bytes(self.rx[:size])
        del self.rx[:size]
        return data

    def close(self) -> None:
        """Mark closed."""
        self.closed = True

    def fileno(self) -> int:
        """
        :return int: a fixed descriptor
        """
        return 3

    def reset_input_buffer(self) -> None:
        """Drop unread bytes."""
        self.rx.clear()

    def reset_output_buffer(self) -> None:
        """Nothing buffered."""


class _Timeout:
    """Scripted reply marker: the request times out."""

    def __repr__(self) -> str:
        """
        :return str: "TIMEOUT"
        """
        return "TIMEOUT"


#: Scripted reply: the request times out as the real transport's does (the
#: clock moves to the deadline, the timeout is tracked, ACETimeoutError has
#: the real "(id=N) timed out after Ts" text).
TIMEOUT: Any = _Timeout()


@dataclass
class AceResponse:
    """
    Scripted full response, checked as the real send_command checks one: a
    non-zero code (or, on V1, msg "FORBIDDEN") raises the real "command 'm'
    failed: code=..., msg=..." ACESerialError (the "ACE2 command" text on V2);
    otherwise its result is returned (the whole response when result is None).
    """
    code: int = 0
    msg: str = ""
    result: Any = None


def ace_error(code: int, msg: str = "") -> AceResponse:
    """
    A scripted error reply, e.g. set_reply("feed_filament", ace_error(1, "FORBIDDEN")).

    :param code: firmware error code
    :param msg: firmware message, "" for the transport's default text
    :return AceResponse: the reply
    """
    return AceResponse(code=code, msg=msg)


class ScriptedTransport:
    """
    Serial transport replaced by scripted replies. Mixed in ahead of the real
    ACEConnection/ACE2Connection, so their command wrappers (get_status,
    feed_filament, enable_rfid, ...), _handle_response, _track_timeout and
    _poll_extras run unchanged and only the wire is fake.

    send_command works as the real one does up to the wire: it refuses a
    down link, takes the next request id (16-bit on V2), encodes the request
    (V1: JSON with the payload size check; V2: method_to_v2 and encode_frame
    from the base class's module, raising the real "ACE2 encode failed"
    error), records it, then resolves the next scripted reply (set_reply):
    a value is the result, an exception is raised, TIMEOUT times out,
    an AceResponse (ace_error) is checked, a callable gets the params dict
    and returns any of those. send_command_async takes an id into _async_ids
    and encodes as the real one does (a V2 encode failure is a debug line).

    Records: .commands (method, params) for every request sent, sync and
    async; .async_commands the async ones; .requests (id, method, params,
    timeout) with timeout None for async; .lifecycle "connect" /
    "disconnect" / "reconnect"; and the same sends and lifecycle steps in the
    shared .timeline. The TX/RX debug lines the real transport writes to its
    serial log are not produced.

    connect() touches no port, sends nothing and starts no heartbeat. A
    connected link returns at once, as the real one does. On a down link it
    runs on_connect(self) (every attempt, before the outcome, so it sees the
    caller's state mid-connect and may raise to fail the attempt), then
    raises the next connect_raises failure (an exception every time; a list
    in order, None succeeding) or marks the link up. disconnect() marks it
    down; reconnect() is only recorded. push_status / push_unsolicited
    deliver a reply through the real _handle_response.
    """
    _not_connected = "ACE not connected"
    _v2 = False

    def __init__(self, reactor: Any, serial_port: str, logger: Optional[Any] = None,
                 baud_rate: int = 115200, ace_index: int = 1,
                 ace_uid: Optional[Tuple[int, ...]] = None, *, connected: bool = True,
                 status: Optional[Dict[str, Any]] = None, info: Any = DEFAULT,
                 timeline: Optional[Timeline] = None) -> None:
        """
        :param reactor: reactor the real connection keeps
        :param serial_port: port name
        :param logger: connection logger
        :param baud_rate: baud rate
        :param ace_index: autodetect index
        :param ace_uid: ACE2 UID pin
        :param connected: initial link state
        :param status: get_status reply, defaults to four empty slots
        :param info: get_info reply and device_info; DEFAULT is
            default_device_info(), None is {}
        :param timeline: shared ordered record, a new list when None
        """
        super().__init__(reactor=reactor, serial_port=serial_port,  # type: ignore[call-arg]
                         logger=logger, baud_rate=baud_rate, ace_index=ace_index,
                         ace_uid=ace_uid)
        self._connected = connected
        self.commands: List[Tuple[str, Dict[str, Any]]] = []
        self.async_commands: List[Tuple[str, Dict[str, Any]]] = []
        self.requests: List[Tuple[int, str, Dict[str, Any], Optional[float]]] = []
        self.lifecycle: List[str] = []
        self.timeline: Timeline = timeline if timeline is not None else []
        self.connect_raises: Union[None, BaseException, List[Optional[BaseException]]] = None
        self.on_connect: Optional[Callable[[Any], None]] = None
        if info is DEFAULT:
            info = default_device_info(self._v2, ace_index)
        default_status = ace_status("empty", "empty", "empty", "empty")
        self.replies: Dict[str, List[Any]] = {
            "get_status": [status if status is not None else default_status],
            "get_info": [dict(info or {})],
        }
        self.device_info: Dict[str, Any] = dict(info or {})

    @property
    def connected(self) -> bool:
        """
        :return bool: the link state
        """
        return self._connected

    @connected.setter
    def connected(self, value: bool) -> None:
        """
        :param value: new link state
        """
        self._connected = bool(value)

    def set_reply(self, method: str, *replies: Any) -> None:
        """
        Script a method's replies, used in order with the last one repeating.
        A reply may be a value (the result), an exception instance (raised),
        TIMEOUT, an AceResponse (see ace_error) or a callable taking the
        params dict and returning one of those.

        :param method: V1 method name, e.g. "get_status"
        :param replies: one or more replies
        """
        if not replies:
            error_str = "set_reply needs at least one reply"
            raise ValueError(error_str)
        self.replies[method] = list(replies)

    def _next_reply(self, method: str) -> Any:
        """
        :param method: method name
        :return Any: the next scripted reply, {} when none is scripted
        """
        queue = self.replies.get(method)
        if not queue:
            return {}
        return queue.pop(0) if len(queue) > 1 else queue[0]

    def _take_request_id(self) -> int:
        """
        :return int: the next request id, 16-bit on V2 as the real one masks it
        """
        request_id = self._next_request_id & 0xFFFF if self._v2 else self._next_request_id
        self._next_request_id += 1
        return request_id

    def _wire_globals(self) -> Dict[str, Any]:
        """
        :return dict: the module globals of the real class's send_command
        """
        for klass in type(self).__mro__:
            if issubclass(klass, ScriptedTransport):
                continue
            func = vars(klass).get("send_command")
            if func is not None:
                return func.__globals__
        return {}

    def _v2_encode_error(self, request_id: int, method: str,
                         params: Optional[Dict[str, Any]]) -> Optional[Exception]:
        """
        :param request_id: request id
        :param method: method name
        :param params: params dict
        :return Optional[Exception]: what encoding the V2 frame raised, if anything
        """
        wire = self._wire_globals()
        try:
            cmd, payload = wire["method_to_v2"](method, params or {})
            wire["encode_frame"](request_id, cmd, payload)
        except Exception as e:
            return e
        return None

    @staticmethod
    def _v1_payload(request_id: int, method: str, params: Optional[Dict[str, Any]]) -> bytes:
        """
        :param request_id: request id
        :param method: method name
        :param params: params dict
        :return bytes: the JSON payload the real V1 transport frames
        """
        request: Dict[str, Any] = {"id": request_id, "method": method}
        if params is not None:
            request["params"] = params
        return json.dumps(request, separators=(",", ":")).encode("utf-8")

    def _record(self, kind: str, request_id: int, method: str,
                params: Optional[Dict[str, Any]], timeout: Optional[float]) -> Dict[str, Any]:
        """
        :param kind: "send_command" or "send_command_async"
        :param request_id: request id
        :param method: method name
        :param params: params dict
        :param timeout: sync timeout, None for async
        :return dict: the recorded params copy
        """
        sent = dict(params or {})
        self.commands.append((method, sent))
        if timeout is None:
            self.async_commands.append((method, sent))
        self.requests.append((request_id, method, sent, timeout))
        self.timeline.append((kind, (method, sent)))
        return sent

    def _time_out(self, method: str, request_id: int, timeout: float) -> None:
        """
        Wait out the deadline on the reactor and raise the real timeout error.

        :param method: method name
        :param request_id: request id
        :param timeout: seconds
        """
        self._reactor.completion().wait(self._reactor.monotonic() + timeout)
        self._track_timeout()
        prefix = "ACE2" if self._v2 else "ACE"
        error_str = f"{prefix} command '{method}' (id={request_id}) timed out after {timeout}s"
        raise ACETimeoutError(error_str)

    def _check_response(self, method: str, request_id: int, reply: AceResponse) -> Any:
        """
        :param method: method name
        :param request_id: request id
        :param reply: scripted full response
        :return Any: its result, or the whole response when it has none
        """
        response: Dict[str, Any] = {"id": request_id, "code": reply.code, "msg": reply.msg}
        if reply.result is not None:
            response["result"] = reply.result
        code, msg = reply.code, reply.msg
        if self._v2:
            if code != 0:
                error_str = f"ACE2 command '{method}' failed: code={code}, msg={msg or 'error'}"
                raise ACESerialError(error_str)
        elif code != 0 or (msg and msg.upper() == "FORBIDDEN"):
            error_str = (f"ACE command '{method}' failed: code={code}, "
                         f"msg={msg or 'unknown error'}")
            raise ACESerialError(error_str)
        return response.get("result", response)

    def _resolve(self, reply: Any, method: str, params: Dict[str, Any], request_id: int,
                 timeout: float) -> Any:
        """
        :param reply: scripted reply
        :param method: method name
        :param params: sent params
        :param request_id: request id
        :param timeout: seconds before TIMEOUT times out
        :return Any: the result
        """
        if callable(reply):
            reply = reply(params)
        if reply is TIMEOUT:
            self._time_out(method, request_id, timeout)
        if isinstance(reply, BaseException):
            raise reply
        if isinstance(reply, AceResponse):
            return self._check_response(method, request_id, reply)
        return reply

    def send_command(self, method: str, params: Optional[Dict[str, Any]] = None,
                     timeout: float = REQUEST_TIMEOUT) -> Any:
        """
        :param method: method name
        :param params: params dict
        :param timeout: seconds a TIMEOUT reply waits; recorded in .requests
        :return Any: the scripted reply's result
        """
        if not self._connected:
            raise ACESerialError(self._not_connected)
        request_id = self._take_request_id()
        if self._v2:
            error = self._v2_encode_error(request_id, method, params)
            if error is not None:
                error_str = f"ACE2 encode failed for '{method}': {error}"
                raise ACESerialError(error_str)
        else:
            payload = self._v1_payload(request_id, method, params)
            if len(payload) > MAX_PAYLOAD_SIZE:
                error_str = f"ACE payload too large ({len(payload)} > {MAX_PAYLOAD_SIZE})"
                raise ACESerialError(error_str)
        sent = self._record("send_command", request_id, method, params, timeout)
        return self._resolve(self._next_reply(method), method, sent, request_id, timeout)

    def send_command_async(self, method: str, params: Optional[Dict[str, Any]] = None) -> None:
        """
        :param method: method name
        :param params: params dict
        """
        if not self._connected:
            return
        request_id = self._take_request_id()
        self._async_ids.append(request_id)
        if self._v2:
            error = self._v2_encode_error(request_id, method, params)
            if error is not None:
                self._logger.debug(f"ACE2 async encode failed: {error}")
                return
        else:
            self._v1_payload(request_id, method, params)
        self._record("send_command_async", request_id, method, params, None)

    def connect(self) -> None:
        """
        Bring the link up: run on_connect, then raise the next connect_raises
        failure or mark the link up.
        """
        self.lifecycle.append("connect")
        self.timeline.append(("connect", self._serial_port))
        if self._connected:
            return
        if self.on_connect is not None:
            self.on_connect(self)
        failure = self.connect_raises
        if isinstance(failure, list):
            failure = failure.pop(0) if failure else None
        if failure is not None:
            raise failure
        self._connected = True

    def disconnect(self) -> None:
        """Take the link down."""
        self.lifecycle.append("disconnect")
        self.timeline.append(("disconnect", self._serial_port))
        self._connected = False

    def reconnect(self) -> None:
        """Record a reconnect request."""
        self.lifecycle.append("reconnect")
        self.timeline.append(("reconnect", self._serial_port))

    def push_status(self, result: Dict[str, Any]) -> None:
        """
        Deliver a heartbeat reply through the real _handle_response: a fresh
        request id is tracked as async (the heartbeat's own get_status is not
        recorded) and its reply routed, so a failing status_callback is
        logged as the real transport logs it rather than raised.

        :param result: the get_status (or get_temp) result
        """
        request_id = self._take_request_id()
        self._async_ids.append(request_id)
        self._handle_response({"id": request_id, "code": 0, "result": result})

    def push_unsolicited(self, result: Dict[str, Any]) -> None:
        """
        Deliver an unsolicited notification (id None) through _handle_response.

        :param result: the notification's result
        """
        self._handle_response({"id": None, "code": 0, "result": result})

    def clear(self) -> None:
        """Clear the recorded commands, requests and lifecycle."""
        self.commands.clear()
        self.async_commands.clear()
        self.requests.clear()
        self.lifecycle.clear()


class FakeAceConnection(ScriptedTransport, ACEConnection):
    """ACE PRO (V1) connection with a scripted transport."""


class FakeAce2Connection(ScriptedTransport, ACE2Connection):
    """ACE 2 Pro connection with a scripted transport."""
    _not_connected = "ACE2 not connected"
    _v2 = True


_SCRIPTED_CLASSES: Dict[type, type] = {ACEConnection: FakeAceConnection,
                                       ACE2Connection: FakeAce2Connection}


def scripted_connection_class(base: type) -> type:
    """
    The scripted class for a connection class: FakeAceConnection or
    FakeAce2Connection for the package classes, ScriptedTransport mixed into
    any other ACEConnection class (e.g. a module copy's ACE2Connection,
    treated as V2 when an ACE2Connection is in its MRO).

    :param base: a connection class, or a ScriptedTransport class (returned)
    :return type: the scripted class
    """
    if issubclass(base, ScriptedTransport):
        return base
    scripted = _SCRIPTED_CLASSES.get(base)
    if scripted is None:
        v2 = any(klass.__name__ == "ACE2Connection" for klass in base.__mro__)
        scripted = type(f"Fake{base.__name__}", (ScriptedTransport, base), {
            "__doc__": f"{base.__name__} with a scripted transport.",
            "_not_connected": "ACE2 not connected" if v2 else "ACE not connected",
            "_v2": v2})
        _SCRIPTED_CLASSES[base] = scripted
    return scripted


def make_fake_ace_connection(*, ace2: bool = False, printer: Optional[AcePrinter] = None,
                             logger: Optional[Any] = None, connected: bool = True,
                             status: Optional[Dict[str, Any]] = None, info: Any = DEFAULT,
                             serial_port: str = "/dev/ttyACM0", ace_index: int = 1,
                             ace_uid: Optional[Tuple[int, ...]] = None,
                             cls: Optional[type] = None) -> ScriptedTransport:
    """
    Build a scripted connection through the real ACEConnection.__init__.

    :param ace2: ACE 2 Pro transport when True
    :param printer: supplies the reactor, logger and timeline, built when None
    :param logger: connection logger, defaults to printer.logger
    :param connected: initial link state
    :param status: get_status reply, defaults to four empty slots
    :param info: get_info reply and device_info; DEFAULT is default_device_info()
    :param serial_port: port name
    :param ace_index: autodetect index
    :param ace_uid: ACE2 UID pin
    :param cls: connection class to script instead of the ace2 choice, e.g. a
        module copy's ACE2Connection (see scripted_connection_class)
    :return ScriptedTransport: a FakeAceConnection (FakeAce2Connection for ace2)
    """
    printer = printer if printer is not None else AcePrinter()
    scripted = scripted_connection_class(
        cls if cls is not None else (ACE2Connection if ace2 else ACEConnection))
    return scripted(printer.reactor, serial_port,
                    logger if logger is not None else printer.logger,
                    230400 if scripted._v2 else 115200, ace_index, ace_uid,
                    connected=connected, status=status, info=info, timeline=printer.timeline)


def make_ace_connection(*, ace2: bool = False, printer: Optional[AcePrinter] = None,
                        logger: Optional[Any] = None, connected: bool = True,
                        next_id: int = 0, serial: Optional[FakeSerial] = None,
                        serial_port: str = "/dev/ttyACM0", ace_index: int = 1,
                        ace_uid: Optional[Tuple[int, ...]] = None,
                        reconnect_enabled: bool = False,
                        cls: Optional[type] = None) -> ACEConnection:
    """
    Build a real ACEConnection/ACE2Connection through its __init__. When
    connected, a FakeSerial is attached and the link marked up, the state
    connect() leaves (no heartbeat timer is started). Drive replies by calling
    conn._handle_response(...) from serial.on_write. reconnect() is a no-op
    unless reconnect_enabled, so a failed write never reaches for a real port.

    :param ace2: ACE2Connection when True
    :param printer: supplies the reactor and logger, built when None
    :param logger: connection logger, defaults to printer.logger
    :param connected: attach a FakeSerial and mark the link up
    :param next_id: next request id
    :param serial: the FakeSerial to attach
    :param serial_port: port name
    :param ace_index: autodetect index
    :param ace_uid: ACE2 UID pin
    :param reconnect_enabled: let reconnect() disconnect and schedule its timer
    :param cls: connection class instead of the ace2 choice, e.g. a module copy's
    :return ACEConnection: the connection
    """
    printer = printer if printer is not None else AcePrinter()
    if cls is None:
        cls = ACE2Connection if ace2 else ACEConnection
    v2 = any(klass.__name__ == "ACE2Connection" for klass in cls.__mro__)
    conn = cls(reactor=printer.reactor, serial_port=serial_port,
               logger=logger if logger is not None else printer.logger,
               baud_rate=230400 if v2 else 115200, ace_index=ace_index, ace_uid=ace_uid)
    conn._next_request_id = next_id
    conn._reconnect_enabled = reconnect_enabled
    if connected:
        conn._serial = serial if serial is not None else FakeSerial()
        conn._connected = True
    return conn


class AceConnectionRouter:
    """
    printer.connections: routes the real afcACE._make_connection to scripted
    links, so _deferred_ace_connect (queued by klippy:ready) runs unchanged on
    a fake wire.

    While routed() is active, the ACEConnection / ACE2Connection global that
    each registered unit's _make_connection reads (in that function's own
    module, so subclasses and module copies are covered) is replaced by a
    builder. It makes a scripted connection of the same class through the
    real __init__, down as a new connection is, appends it to .created and
    hands it out. Its replies and device_info are copied from the last
    scripted link for its serial_port and ace_index (the unit builder's, then
    each routed one), and its connect() runs on_connect(conn) and takes
    connect_raises (one list shared by every routed link, so it spans
    _deferred_ace_connect's retries; see ScriptedTransport).

    AcePrinter.send_event and every reactor callback and timer run inside
    routed() while .enabled (make_ace_printer(route_connections=False) keeps
    the real classes). Call a connect path directly inside
    ``with printer.connections.routed():``.
    """
    _NAMES = ("ACEConnection", "ACE2Connection")

    def __init__(self, printer: AcePrinter) -> None:
        """
        :param printer: printer whose units are routed
        """
        self._printer = printer
        self.enabled = True
        self.created: List[ScriptedTransport] = []
        self.connect_raises: Union[None, BaseException, List[Optional[BaseException]]] = []
        self.on_connect: Optional[Callable[[ScriptedTransport], None]] = None
        self.scripts: Dict[Tuple[str, int], ScriptedTransport] = {}
        self._depth = 0
        self._saved: List[Tuple[Dict[str, Any], str, Any]] = []

    def remember(self, conn: ScriptedTransport) -> None:
        """
        Make conn the script that routed links for its port copy.

        :param conn: a scripted connection
        """
        self.scripts[(conn._serial_port, conn._ace_index)] = conn

    @contextlib.contextmanager
    def routed(self) -> Iterator[None]:
        """
        Route connection construction while the block runs (re-entrant).

        :return Iterator[None]: the routed block
        """
        if not self.enabled:
            yield
            return
        if self._depth == 0:
            try:
                self._install()
            except BaseException:
                self._uninstall()
                raise
        self._depth += 1
        try:
            yield
        finally:
            self._depth -= 1
            if self._depth == 0:
                self._uninstall()

    def _install(self) -> None:
        """Swap the connection classes the units' _make_connection reads."""
        for obj in list(self._printer.objects.values()):
            func = getattr(type(obj), "_make_connection", None)
            code = getattr(func, "__code__", None)
            if code is None:
                continue
            namespace = func.__globals__
            for name in self._NAMES:
                if (name not in code.co_names or name not in namespace
                        or any(ns is namespace and n == name for ns, n, _ in self._saved)):
                    continue
                self._saved.append((namespace, name, namespace[name]))
                namespace[name] = self._builder(namespace[name])

    def _uninstall(self) -> None:
        """Put the real connection classes back."""
        while self._saved:
            namespace, name, original = self._saved.pop()
            namespace[name] = original

    def _builder(self, base: type) -> Callable[..., ScriptedTransport]:
        """
        :param base: the real connection class being replaced
        :return Callable[..., ScriptedTransport]: stands in for the class
        """
        scripted = scripted_connection_class(base)

        def build(reactor: Any, serial_port: str, logger: Optional[Any] = None,
                  baud_rate: int = 115200, ace_index: int = 1,
                  ace_uid: Optional[Tuple[int, ...]] = None) -> ScriptedTransport:
            """
            Construct a routed scripted connection with the real signature.

            :return ScriptedTransport: the new, unconnected link
            """
            conn = scripted(reactor, serial_port, logger, baud_rate, ace_index, ace_uid,
                            connected=False, timeline=self._printer.timeline)
            self._adopt(conn)
            return conn

        return build

    def _adopt(self, conn: ScriptedTransport) -> None:
        """
        :param conn: a newly routed connection to script and record
        """
        script = self.scripts.get((conn._serial_port, conn._ace_index))
        if script is not None:
            conn.replies = {method: list(queue) for method, queue in script.replies.items()}
            conn.device_info = dict(script.device_info)
        conn.connect_raises = self.connect_raises
        conn.on_connect = self.on_connect
        self.created.append(conn)
        self.remember(conn)


# ── Lanes and units ───────────────────────────────────────────────────────────

@dataclass
class LaneSpec:
    """
    A lane for make_ace_unit. slot is 0-based (None: its position in the list).
    prep is filament in the slot, load is staged at the hub (loaded_to_hub),
    tool_loaded is loaded into the toolhead. values are extra [AFC_lane]
    options, e.g. {"dist_hub": 100}; "unit" and "map" default to
    "<unit>:<slot + 1>" and "T<slot>". An "extruder" or "hub" named here that
    the printer lacks is registered (AcePrinter.add_extruder / add_hub).
    """
    name: str
    slot: Optional[int] = None
    prep: bool = False
    load: bool = False
    tool_loaded: bool = False
    status: Optional[AFCLaneState] = None
    values: Dict[str, Any] = field(default_factory=dict)


def set_lane_state(lane: AFCLane, *, prep: Optional[bool] = None, load: Optional[bool] = None,
                   tool_loaded: Optional[bool] = None,
                   status: Optional[AFCLaneState] = None) -> None:
    """
    Put a lane in a prep/load/tool state the way the ACE unit leaves it.

    On a virtual hub the live hub signal (_load_state) follows tool_loaded, as
    afcACE._set_hub_state drives it; on a real hub switch it follows load.
    tool_loaded also names the lane on its extruder. status defaults to TOOLED,
    LOADED (prepped) or NONE.

    :param lane: real AFCLane
    :param prep: filament in the slot
    :param load: staged at the hub
    :param tool_loaded: loaded into the toolhead
    :param status: explicit lane status
    """
    if prep is not None:
        lane.prep_state = prep
    if load is not None:
        lane.loaded_to_hub = load
    extruder = getattr(lane, "extruder_obj", None)
    if tool_loaded is not None:
        lane.tool_loaded = tool_loaded
        if extruder is not None and tool_loaded:
            extruder.lane_loaded = lane.name
        elif extruder is not None and extruder.lane_loaded == lane.name:
            extruder.lane_loaded = None
    hub = lane.hub_obj
    virtual = hub is not None and hub.is_virtual_pin()
    lane._load_state = lane.tool_loaded if virtual else lane.loaded_to_hub
    if status is None:
        if lane.tool_loaded:
            status = AFCLaneState.TOOLED
        elif lane.prep_state:
            status = AFCLaneState.LOADED
        else:
            status = AFCLaneState.NONE
    lane.status = status


def _lane_spec(spec: Union[str, LaneSpec], index: int) -> LaneSpec:
    """
    :param spec: lane name or LaneSpec
    :param index: position in the lanes list
    :return LaneSpec: the spec with its slot filled in
    """
    if isinstance(spec, str):
        return LaneSpec(spec, slot=index)
    if spec.slot is None:
        return LaneSpec(spec.name, index, spec.prep, spec.load, spec.tool_loaded,
                        spec.status, dict(spec.values))
    return spec


def _make_lane(printer: AcePrinter, unit_name: str, spec: LaneSpec) -> AFCLane:
    """
    Construct a real AFCLane for an ACE slot and register its section.

    :param printer: shared printer
    :param unit_name: owning unit's name
    :param spec: the lane, slot filled in
    :return AFCLane: the lane, not yet connected to its unit
    """
    section = f"AFC_lane {spec.name}"
    values: Dict[str, Any] = {"unit": f"{unit_name}:{spec.slot + 1}", "map": f"T{spec.slot}"}
    values.update(spec.values)
    lane = AFCLane(AceConfig(section, printer, values))
    printer.add_section(section, lane, values)
    return lane


def _ensure_collaborators(printer: AcePrinter, *option_sets: Mapping[str, Any]) -> None:
    """
    Register the hub and extruder each option set names, when missing.

    :param printer: shared printer
    :param option_sets: unit and lane options
    """
    for options in option_sets:
        hub = options.get("hub")
        if hub and f"AFC_hub {hub}" not in printer.objects:
            printer.add_hub(hub)
        extruder = options.get("extruder")
        if extruder and f"AFC_extruder {extruder}" not in printer.objects:
            printer.add_extruder(extruder)


def _connection_class_of(cls: type, ace2: bool) -> type:
    """
    :param cls: the unit class
    :param ace2: fallback to ACE2Connection when True
    :return type: the connection class cls._make_connection constructs
    """
    func = getattr(cls, "_make_connection", None)
    code = getattr(func, "__code__", None)
    if code is not None:
        for name in reversed(AceConnectionRouter._NAMES):
            found = func.__globals__.get(name) if name in code.co_names else None
            if isinstance(found, type):
                return found
    return ACE2Connection if ace2 else ACEConnection


def _build_unit(cls: type, prefix: str, name: str, ace2: bool, *,
                lanes: Iterable[Union[str, LaneSpec]], values: Optional[Mapping[str, Any]],
                printer: Optional[AcePrinter], connection: Any, info: Any,
                hw_status: Optional[Dict[str, Any]], temp_info: Optional[Dict[str, Any]],
                inventory: Optional[Mapping[int, Mapping[str, Any]]],
                prev_slot_states: Optional[Mapping[str, bool]],
                operation_active: bool, feed_assist_active: Iterable[int],
                printing: Optional[bool], paused: Optional[bool], in_print: Optional[bool],
                current: Any, active_extruder: Any, prep_done: bool, connect: bool,
                clear_records: bool) -> Any:
    """
    Shared body of make_ace_unit and make_ace2_unit; see make_ace_unit.

    :return Any: the unit
    """
    printer = printer if printer is not None else AcePrinter()
    units_before = sum(1 for obj in printer.objects.values() if isinstance(obj, afcACE))
    cfg: Dict[str, Any] = {"serial_port": f"/dev/ttyACM{units_before}",
                           "hub": f"{name}_hub", "extruder": "extruder"}
    cfg.update(values or {})
    specs = [_lane_spec(spec, index) for index, spec in enumerate(lanes)]
    _ensure_collaborators(printer, cfg, *(spec.values for spec in specs))
    section = f"{prefix} {name}"
    unit = cls(AceConfig(section, printer, cfg))
    printer.add_section(section, unit, cfg)

    lane_objs = [_make_lane(printer, name, spec) for spec in specs]
    if connect:
        unit.handle_connect()
        printer.remove_event_handler("klippy:connect", unit.handle_connect)
    for spec, lane in zip(specs, lane_objs):
        set_lane_state(lane, prep=spec.prep, load=spec.load, tool_loaded=spec.tool_loaded,
                       status=spec.status)
        if prep_done:
            lane.set_afc_prep_done()
    if current is DEFAULT:
        loaded = [spec.name for spec in specs if spec.tool_loaded]
        if loaded:
            printer.afc.current = loaded[0]
    else:
        printer.afc.current = current

    if connection is DEFAULT:
        slot_states = ["empty"] * cls.SLOTS_PER_UNIT
        for spec in specs:
            if spec.prep and 0 <= spec.slot < len(slot_states):
                slot_states[spec.slot] = "ready"
        connection = make_fake_ace_connection(
            printer=printer, status=ace_status(*slot_states), info=info,
            serial_port=unit.serial_port, ace_index=unit.ace_index,
            ace_uid=getattr(unit, "ace_uid", None), cls=_connection_class_of(cls, ace2))
    if connection is not None:
        if getattr(connection, "_reactor", printer.reactor) is not printer.reactor:
            error_str = "connection must be built on the unit's printer (printer=...)"
            raise ValueError(error_str)
        unit._ace = connection
        connection.status_callback = unit._on_hw_status_callback
        connection.reconnect_callback = unit._on_ace_reconnect
        if isinstance(connection, ScriptedTransport):
            printer.connections.remember(connection)

    if hw_status is not None:
        unit._cached_hw_status = hw_status
        unit._hw_status_time = printer.reactor.monotonic()
    if temp_info is not None:
        unit._cached_temp_info = temp_info
    for slot, slot_info in (inventory or {}).items():
        unit._slot_inventory[slot].update(slot_info)
    if prev_slot_states is not None:
        unit._prev_slot_states = dict(prev_slot_states)
    unit._operation_active = operation_active
    unit._feed_assist_active.update(feed_assist_active)
    function = printer.afc.function
    if printing is not None:
        function.printing = printing
    if paused is not None:
        function.paused = paused
    if in_print is not None:
        function.in_print_flag = in_print
    if active_extruder is not DEFAULT:
        printer.toolhead.active_extruder = active_extruder
    if clear_records:
        printer.clear_records()
    return unit


def make_ace_unit(name: str = "Ace_1", *, lanes: Iterable[Union[str, LaneSpec]] = (),
                  values: Optional[Mapping[str, Any]] = None,
                  printer: Optional[AcePrinter] = None, connection: Any = DEFAULT,
                  info: Any = DEFAULT, hw_status: Optional[Dict[str, Any]] = None,
                  temp_info: Optional[Dict[str, Any]] = None,
                  inventory: Optional[Mapping[int, Mapping[str, Any]]] = None,
                  prev_slot_states: Optional[Mapping[str, bool]] = None,
                  operation_active: bool = False, feed_assist_active: Iterable[int] = (),
                  printing: Optional[bool] = None, paused: Optional[bool] = None,
                  in_print: Optional[bool] = None, current: Any = DEFAULT,
                  active_extruder: Any = DEFAULT, prep_done: bool = True,
                  connect: bool = True, clear_records: bool = True,
                  cls: Optional[type] = None) -> afcACE:
    """
    Build an afcACE through its real __init__ and klippy:connect wiring.

    Registers [AFC_ACE <name>] on the printer, plus [AFC_hub <name>_hub]
    (virtual switch pin) and [AFC_extruder extruder] fakes, and any hub or
    extruder a LaneSpec names, when missing. serial_port defaults to
    /dev/ttyACM<n>, n the ACE units already on the printer. Each lane is a
    real AFCLane built through its __init__ as [AFC_lane <lane>], connected by
    the unit's own handle_connect (which sends AFC_unit_<name>:connect, so the
    lanes register themselves and _slot_map is built from their unit index),
    then put in its LaneSpec state. The unit's klippy:connect handler is then
    dropped, so a later send_event("klippy:connect") does not connect it twice.

    Defaults to a connected scripted connection (FakeAceConnection, or the
    unit class's own connection class mixed with ScriptedTransport) whose
    get_status reports prepped lanes' slots "ready", others "empty";
    status_callback and reconnect_callback are wired as _deferred_ace_connect
    leaves them, and printer.connections copies its script into the link a
    later klippy:ready connect makes.

    :param name: unit name
    :param lanes: lane names (slot = position) or LaneSpecs
    :param values: [AFC_ACE] options over serial_port, hub and extruder defaults
    :param printer: shared printer, built when None
    :param connection: the unit's _ace; None for no link, DEFAULT for a fake. A
        connection of your own must share the printer, e.g.
        make_fake_ace_connection(printer=printer)
    :param info: the default connection's get_info reply and device_info;
        DEFAULT is default_device_info()
    :param hw_status: _cached_hw_status, stamped as just received
    :param temp_info: _cached_temp_info
    :param inventory: per-slot fields merged into _slot_inventory
    :param prev_slot_states: _prev_slot_states, lane name to last "ready", as
        a seen heartbeat leaves it
    :param operation_active: _operation_active
    :param feed_assist_active: slots in _feed_assist_active
    :param printing: afc.function.is_printing() result, when given
    :param paused: afc.function.is_paused() result, when given
    :param in_print: afc.function.in_print() result, when given
    :param current: afc.current; DEFAULT takes the first tool-loaded lane
    :param active_extruder: toolhead's active extruder, when given
    :param prep_done: mark lanes past AFC PREP (set_afc_prep_done)
    :param connect: run handle_connect (False leaves lanes unconnected and the
        klippy:connect handler registered)
    :param clear_records: clear every record once wiring is done
    :param cls: afcACE subclass to build, e.g. a module copy's
    :return afcACE: the unit
    """
    return _build_unit(
        cls or afcACE, "AFC_ACE", name, False, lanes=lanes, values=values,
        printer=printer, connection=connection, info=info, hw_status=hw_status,
        temp_info=temp_info, inventory=inventory, prev_slot_states=prev_slot_states,
        operation_active=operation_active,
        feed_assist_active=feed_assist_active, printing=printing, paused=paused,
        in_print=in_print, current=current, active_extruder=active_extruder,
        prep_done=prep_done, connect=connect, clear_records=clear_records)


def make_ace2_unit(name: str = "Ace2_1", *, lanes: Iterable[Union[str, LaneSpec]] = (),
                   values: Optional[Mapping[str, Any]] = None,
                   printer: Optional[AcePrinter] = None, connection: Any = DEFAULT,
                   info: Any = DEFAULT, hw_status: Optional[Dict[str, Any]] = None,
                   temp_info: Optional[Dict[str, Any]] = None,
                   inventory: Optional[Mapping[int, Mapping[str, Any]]] = None,
                   prev_slot_states: Optional[Mapping[str, bool]] = None,
                   operation_active: bool = False, feed_assist_active: Iterable[int] = (),
                   printing: Optional[bool] = None, paused: Optional[bool] = None,
                   in_print: Optional[bool] = None, current: Any = DEFAULT,
                   active_extruder: Any = DEFAULT, prep_done: bool = True,
                   connect: bool = True, clear_records: bool = True,
                   cls: Optional[type] = None) -> afcACE2:
    """
    Build an afcACE2 as make_ace_unit does, as [AFC_ACE2 <name>], with a
    FakeAce2Connection by default whose device_info reports stock firmware
    (default_device_info(ace2=True)). Parameters are make_ace_unit's.

    :return afcACE2: the unit
    """
    return _build_unit(
        cls or afcACE2, "AFC_ACE2", name, True, lanes=lanes, values=values,
        printer=printer, connection=connection, info=info, hw_status=hw_status,
        temp_info=temp_info, inventory=inventory, prev_slot_states=prev_slot_states,
        operation_active=operation_active,
        feed_assist_active=feed_assist_active, printing=printing, paused=paused,
        in_print=in_print, current=current, active_extruder=active_extruder,
        prep_done=prep_done, connect=connect, clear_records=clear_records)


# Uncomment below once RFID and spoolman creation support is added
# # ── ACE2 RFID ─────────────────────────────────────────────────────────────────

# def make_ace2_rfid(values: Optional[Mapping[str, Any]] = None, *, ace2: Any = DEFAULT,
                   # printer: Optional[AcePrinter] = None, bind: bool = True,
                   # settle: bool = True, clear_records: bool = True,
                   # cls: Optional[type] = None) -> AFC_ACE2_RFID:
    # """
    # Build AFC_ACE2_RFID through its real __init__ as [AFC_ACE2_rfid].

    # bind runs its klippy:ready handler (_on_ready) once and drops it: that
    # resolves AFC, finds the afcACE2 by its "AFC_ACE2 <name>" section,
    # registers the tag writers and, with skip_factory_autostage, queues the
    # identify-disable retry (_retry_disable_identify) on the reactor.

    # settle then runs that retry while it is due, without moving the clock. On
    # a connected unit whose link reported its firmware (the default
    # connection's device_info does) it finishes at once: identify is turned
    # off (stock firmware) or kept on (firmware decode). On a unit with no
    # link, a down link or no firmware it re-queues itself for later and stays
    # in reactor.pending; settle=False leaves the first try queued too.

    # :param values: [AFC_ACE2_rfid] options
    # :param ace2: the afcACE2 to bind; DEFAULT builds one, None binds nothing
    # :param printer: shared printer, defaults to ace2's or a new one
    # :param bind: run _on_ready
    # :param settle: run the bind-time identify retry while it is due
    # :param clear_records: clear every record once wiring is done
    # :param cls: AFC_ACE2_RFID class to build, e.g. a test's module copy
    # :return AFC_ACE2_RFID: the reader object
    # """
    # if ace2 is not DEFAULT and ace2 is not None and printer is None:
        # printer = ace2.printer
    # printer = printer if printer is not None else AcePrinter()
    # if ace2 is DEFAULT:
        # ace2 = make_ace2_unit(printer=printer, clear_records=False)
    # if ace2 is not None and ace2.printer is not printer:
        # error_str = "ace2 must be built on the same printer"
        # raise ValueError(error_str)
    # config = AceConfig("AFC_ACE2_rfid", printer, values)
    # obj = (cls or AFC_ACE2_RFID)(config)
    # printer.add_section("AFC_ACE2_rfid", obj, values)
    # if bind:
        # obj._on_ready()
        # printer.remove_event_handler("klippy:ready", obj._on_ready)
        # if settle:
            # retry = getattr(obj, "_retry_disable_identify", None)
            # printer.reactor.run_callbacks(match=lambda entry: entry.callback == retry)
    # if clear_records:
        # printer.clear_records()
    # return obj


# def make_ace2_reg_link(slot: int = 0, *, ace2: Any = DEFAULT, cls: Optional[type] = None,
                       # **options: Any) -> _Ace2RegLink:
    # """
    # Build an _Ace2RegLink over an afcACE2, so its commands land in the unit's
    # connection (ace2._ace.commands).

    # :param slot: reader chip-select index
    # :param ace2: the afcACE2; DEFAULT builds one
    # :param cls: link class to build, e.g. a module copy's _Ace2RegLink
    # :param options: _Ace2RegLink keywords (power_index, reg_timeout, ...)
    # :return _Ace2RegLink: the link
    # """
    # if ace2 is DEFAULT:
        # ace2 = make_ace2_unit()
    # return (cls or _Ace2RegLink)(ace2, slot, **options)


# class FakeTransport:
    # """Ace2Link transport: records each frame, answers with .replies in order
    # (last repeats), b"" when none is set."""

    # def __init__(self, *replies: bytes) -> None:
        # """
        # :param replies: response frames
        # """
        # self.frames: List[bytes] = []
        # self.replies: List[bytes] = list(replies)

    # def __call__(self, frame: bytes) -> bytes:
        # """
        # :param frame: request frame
        # :return bytes: the next reply
        # """
        # self.frames.append(bytes(frame))
        # if not self.replies:
            # return b""
        # return self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]


# def make_ace2_link(slot: int = 0, *, transport: Optional[Callable[[bytes], bytes]] = None,
                   # ftype: int = 0x0000, cls: Optional[type] = None) -> Ace2Link:
    # """
    # Build an Ace2Link over a recording transport.

    # :param slot: reader slot
    # :param transport: frame transport, defaults to a FakeTransport
    # :param ftype: frame type word
    # :param cls: link class to build, e.g. a module copy's Ace2Link
    # :return Ace2Link: the link; its transport is link._tx
    # """
    # return (cls or Ace2Link)(transport if transport is not None else FakeTransport(),
                             # slot=slot, ftype=ftype)


# ── temperature_ace ───────────────────────────────────────────────────────────

def make_temperature_ace(sensor_name: str = "ace_temp", *, unit: Optional[afcACE] = None,
                         values: Optional[Mapping[str, Any]] = None,
                         printer: Optional[AcePrinter] = None, min_temp: float = 0.0,
                         max_temp: float = 70.0, callback: Any = DEFAULT,
                         debug_output: bool = False, ready: bool = True,
                         clear_records: bool = True,
                         cls: Optional[type] = None) -> TemperatureACE:
    """
    Build TemperatureACE through its real __init__ as [temperature_sensor
    <sensor_name>], then do what Klipper's sensor wrapper does: setup_minmax and
    setup_callback. It registers itself as "aht10 <sensor_name>" (or
    "temperature_ace <sensor_name>").

    Before handle_ready its lines go to the "temperature_ace" Python logger
    (capture_log); after it, to the AFC logger. ready runs its klippy:ready
    handler (handle_ready) once and drops it; that arms the sample timer at
    NOW, so the next advance() takes a sample. Shutdowns land in
    printer.shutdowns; with the default callback, samples land in
    sensor._callback.calls as ((print_time, temp), {}).

    :param sensor_name: sensor name
    :param unit: ACE unit to read; sets ace_unit and the printer
    :param values: section options (ace_unit, channel, ...)
    :param printer: shared printer, defaults to unit's or a new one
    :param min_temp: setup_minmax lower limit
    :param max_temp: setup_minmax upper limit
    :param callback: heaters callback; DEFAULT is a Recorder, None sets none
    :param debug_output: start in Klipper's debug-output mode (no timer)
    :param ready: run handle_ready
    :param clear_records: clear every record once wiring is done
    :param cls: sensor class to build, e.g. a module copy's TemperatureACE
    :return TemperatureACE: the sensor
    """
    if printer is None:
        printer = unit.printer if unit is not None else AcePrinter()
    cfg: Dict[str, Any] = {"ace_unit": unit.name} if unit is not None else {}
    cfg.update(values or {})
    if debug_output:
        printer.start_args["debugoutput"] = "/dev/null"
    sensor = (cls or TemperatureACE)(
        AceConfig(f"temperature_sensor {sensor_name}", printer, cfg))
    sensor.setup_minmax(min_temp, max_temp)
    if callback is DEFAULT:
        callback = Recorder()
    if callback is not None:
        sensor.setup_callback(callback)
    if ready:
        sensor.handle_ready()
        printer.remove_event_handler("klippy:ready", sensor.handle_ready)
    if clear_records:
        printer.clear_records()
    return sensor


# ── Process-wide state ────────────────────────────────────────────────────────

_SERIAL_FILE_LOGGER = "AFC_ACE_serial_file"


def reset_ace_globals() -> None:
    """
    Reset the state the ACE modules keep for the whole process, which a test
    otherwise leaks to the next: the "AFC_ACE_serial_file" logger (handlers
    removed and closed, propagate and level restored; with a handler left,
    every later _deferred_ace_connect logs to the earlier test's file), the
    serial-log queue listener of every unit on a printer these builders made
    (stopped and taken off atexit), and AFC_ACE._ACE_CLAIMED_PORTS (cleared in
    place, so AFC_ACE2's reference sees it). temperature_ace's sensor-factory
    registration is tracked per-heaters-object now, not a module flag, so it
    needs no reset here -- each test's own fresh heaters starts unmarked.
    """
    for printer in list(_BUILT_PRINTERS):
        for obj in list(printer.objects.values()):
            listener = vars(obj).get("_serial_ql") if hasattr(obj, "__dict__") else None
            if isinstance(listener, logging.handlers.QueueListener):
                atexit.unregister(listener.stop)
                if getattr(listener, "_thread", None) is not None:
                    listener.stop()
                for handler in listener.handlers:
                    handler.close()
    _BUILT_PRINTERS.clear()
    logger = logging.getLogger(_SERIAL_FILE_LOGGER)
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()
    logger.propagate = True
    logger.setLevel(logging.NOTSET)
    afc_ace_module._ACE_CLAIMED_PORTS.clear()


@pytest.fixture
def ace_isolation() -> Iterator[None]:
    """
    Run reset_ace_globals before and after a test. Import it into a test
    module and apply it with ``pytestmark = pytest.mark.usefixtures(
    "ace_isolation")`` (or request it per test).

    :return Iterator[None]: the test's run
    """
    reset_ace_globals()
    yield
    reset_ace_globals()
