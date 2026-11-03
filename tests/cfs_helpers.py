"""
Test fakes and real-construction builders for the CFS unit tests.

Every afcCFS and AFCLane here is built through its real __init__ (AGENTS.md,
Unit Test Rules) on AFC's MockPrinter/MockConfig. The box itself is replaced by
a ScriptedBox standing in for CFSTransport.request: it records each request
and answers from a per-function script, so a test can assert on the exact
traffic a method sends. Log calls land in the unit's MockLogger as exact
(level, message) tuples.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional, Tuple, Union

from extras.AFC_lane import AFCLane
from tests.conftest import MockAFC, MockConfig, MockPrinter

from extras import AFC_CFS as P
from extras.AFC_CFS import CFSBus, Frame, afcCFS

try:
    from tests.conftest import CommandError, MockGCodeCommand
except ImportError:      # AFCProject main's conftest predates these; same behavior here
    from unittest.mock import MagicMock

    class CommandError(Exception):       # type: ignore[no-redef]
        pass

    class MockGCodeCommand:              # type: ignore[no-redef]
        """G-code command: get()/get_int()/get_float() with Klipper's default handling."""

        _missing = object()

        def __init__(self, params: Optional[Dict[str, Any]] = None) -> None:
            self._params = dict(params or {})
            self.error = MagicMock(side_effect=lambda *a, **kw: CommandError(*a, **kw))
            self.respond_info = MagicMock()
            self.respond_raw = MagicMock()
            self.get = MagicMock(side_effect=self._get)
            self.get_int = MagicMock(side_effect=lambda n, d=self._missing, **kw:
                                     self._get(n, d, int, **kw))
            self.get_float = MagicMock(side_effect=lambda n, d=self._missing, **kw:
                                       self._get(n, d, float, **kw))

        def _get(self, name: str, default: Any = _missing, parser: Any = str,
                 minval: Any = None, maxval: Any = None, above: Any = None,
                 below: Any = None) -> Any:
            value = self._params.get(name)
            if value is None:
                if default is self._missing:
                    raise self.error(f"missing {name}")
                return default
            return parser(value)

Reply = Union[Frame, None, Callable[[bytes], Optional[Frame]]]

SERIAL = "/dev/fake_cfs"


def frame(fn: int, data: bytes = b"", status: int = P.ST_OK, addr: int = 1) -> Frame:
    """
    A reply frame.

    :param fn: function code
    :param data: payload
    :param status: status byte
    :param addr: box address
    :return Frame: the frame
    """
    return Frame(addr=addr, status=status, fn=fn, data=bytes(data))


class Recorder:
    """A plain callable that records its calls and returns a fixed value or raises."""

    def __init__(self, result: Any = None, raises: Optional[BaseException] = None) -> None:
        self.calls: List[Tuple[tuple, dict]] = []
        self.result = result
        self.raises = raises

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        self.calls.append((args, kwargs))
        if self.raises is not None:
            raise self.raises
        return self.result

    @property
    def called(self) -> bool:
        return bool(self.calls)

    @property
    def call_count(self) -> int:
        return len(self.calls)

    @property
    def args(self) -> List[tuple]:
        return [c[0] for c in self.calls]


class Request:
    """One request the unit sent, as ScriptedBox recorded it."""

    def __init__(self, addr: int, fn: int, data: bytes, kwargs: Dict[str, Any]) -> None:
        self.addr = addr
        self.fn = fn
        self.data = bytes(data)
        self.kwargs = kwargs

    @property
    def key(self) -> Tuple[int, int, bytes]:
        """(addr, fn, data), for comparing traffic in one assert."""
        return (self.addr, self.fn, self.data)

    def __repr__(self) -> str:
        return f"Request({self.addr}, {P.fn_name(self.fn)}, {self.data.hex()})"


class ScriptedBox:
    """
    Stands in for CFSTransport.request. Replies come from a per-function
    queue (reply()) or handler (on()); anything unscripted answers OK with no
    data from the requested address, like an idle box.
    """

    def __init__(self) -> None:
        self.sent: List[Request] = []
        self._queues: Dict[int, List[Reply]] = {}
        self._handlers: Dict[int, Callable[[Request], Optional[Frame]]] = {}
        self.default_ok = True

    def reply(self, fn: int, *replies: Reply) -> "ScriptedBox":
        """Queue replies for fn, used once each in order (None means no reply)."""
        self._queues.setdefault(fn, []).extend(replies)
        return self

    def on(self, fn: int, handler: Callable[[Request], Optional[Frame]]) -> "ScriptedBox":
        """Answer every fn request through handler(request)."""
        self._handlers[fn] = handler
        return self

    def request(self, addr: int, fn: int, data: bytes = b"", **kwargs: Any) -> Optional[Frame]:
        req = Request(addr, fn, data, kwargs)
        self.sent.append(req)
        queue = self._queues.get(fn)
        if queue:
            item = queue.pop(0)
            return item(req.data) if callable(item) else item
        if fn in self._handlers:
            return self._handlers[fn](req)
        if not self.default_ok:
            return None
        return frame(fn, addr=addr)

    def fns(self) -> List[int]:
        """Function codes sent, in order."""
        return [r.fn for r in self.sent]

    def keys(self) -> List[Tuple[int, int, bytes]]:
        """(addr, fn, data) of every request, in order."""
        return [r.key for r in self.sent]

    def clear(self) -> None:
        self.sent.clear()


class CfsHub:
    """AFC_hub stand-in: the CFS buffer as a pinless (virtual) hub."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.switch_pin = "virtual"
        self.afc_bowden_length = 900.0
        self.td1_bowden_length = 850.0
        self.afc_unload_bowden_length = 900.0
        self.move_dis = 50.0
        self.lanes: Dict[str, Any] = {}
        self.state = False

    def is_virtual_pin(self) -> bool:
        return True


class _ToolheadExtruder:
    def __init__(self, name: str) -> None:
        self._name = name

    def get_name(self) -> str:
        return self._name


class CfsExtruder:
    """AFC_extruder stand-in with the attributes AFCLane and the CFS unit read."""

    def __init__(self, name: str = "extruder") -> None:
        self.name = name
        self.th_extruder_name = name
        self.toolhead_extruder = _ToolheadExtruder(name)
        self.lane_loaded: Optional[str] = None
        self.lanes: Dict[str, Any] = {}
        self.tool_start = "tool_start_pin"
        self.tool_start_state = False
        self.tool_end = None
        self.tool_end_state = False
        self.buffer_name: Optional[str] = None
        self.fila_tool_start: Optional[Any] = None
        self.filament_sensor_obj: Optional[Any] = None
        self.tool_stn = 60.0
        self.tool_stn_unload = 0.0
        self.tool_unload_speed = 25.0
        self.tool_load_speed = 10.0
        self.led_index: Optional[str] = None
        self.tc_lane = None
        self.check_lanes = Recorder()
        self.set_status_led = Recorder()
        self.estats = ExtruderStats()


class ExtruderStats:
    """AFC_extruder.estats stand-in: counts cuts."""

    def __init__(self) -> None:
        self.increase_cut_total = Recorder()


class FakeMutex:
    """Reactor mutex: test() says whether it is held."""

    def __init__(self) -> None:
        self.locked = False

    def test(self) -> bool:
        return self.locked

    def __enter__(self) -> "FakeMutex":
        self.locked = True
        return self

    def __exit__(self, *exc: Any) -> bool:
        self.locked = False
        return False


class Timer:
    """A registered reactor timer."""

    def __init__(self, callback: Callable[[float], float], waketime: float) -> None:
        self.callback = callback
        self.waketime = waketime


class TimedReactor:
    """
    Reactor whose clock moves forward on pause(), so loops that wait on
    monotonic() end. on_pause hooks run at each pause (a box changing state
    while the unit waits).
    """

    NOW = 0.0
    NEVER = 9_999_999_999.0

    def __init__(self, now: float = 1000.0) -> None:
        self.now = now
        self.pauses: List[float] = []
        self.on_pause: List[Callable[[float], None]] = []
        self.timers: List[Timer] = []
        self.unregistered: List[Timer] = []
        self.updated: List[Tuple[Timer, float]] = []
        self.callbacks: List[Any] = []

    def monotonic(self) -> float:
        return self.now

    def pause(self, until: float) -> float:
        self.pauses.append(until)
        self.now = max(self.now, until)
        for hook in list(self.on_pause):
            hook(self.now)
        return self.now

    def mutex(self, is_locked: bool = False) -> FakeMutex:
        return FakeMutex()

    def completion(self) -> Any:
        raise AssertionError("unit tests talk to a ScriptedBox, not a real completion")

    def register_timer(self, callback: Callable[[float], float], waketime: float = NEVER) -> Timer:
        timer = Timer(callback, waketime)
        self.timers.append(timer)
        return timer

    def update_timer(self, timer: Timer, waketime: float) -> None:
        timer.waketime = waketime
        self.updated.append((timer, waketime))

    def unregister_timer(self, timer: Timer) -> None:
        self.unregistered.append(timer)

    def register_callback(self, callback: Any, waketime: float = NOW) -> None:
        self.callbacks.append(callback)

    def register_async_callback(self, callback: Any, waketime: float = NOW) -> None:
        self.callbacks.append(callback)


class RecTrace:
    """Stands in for a unit's CFSTrace: records events instead of writing a file."""

    def __init__(self, enabled: bool = True) -> None:
        self.enabled = enabled
        self.events: List[Tuple[str, Dict[str, Any]]] = []
        self.stopped = False
        self.path = "/tmp/cfs_trace.jsonl"
        self.written = 0
        self.dropped = 0
        self.last_error: Optional[str] = None

    def event(self, ev: str, **fields: Any) -> None:
        self.events.append((ev, fields))

    def stop(self, timeout: float = 2.0) -> None:
        self.stopped = True

    def start(self) -> bool:
        self.enabled = True
        return True

    def names(self) -> List[str]:
        return [e for e, _ in self.events]


def complete_afc(afc: Any) -> Any:
    """
    Give an older AFC test mock (AFCProject main's conftest) the settings a real
    AFCLane reads that only newer mocks carry. Values match AFC DEV's MockAFC.

    :param afc: MockAFC
    :return Any: the same mock
    """
    from unittest.mock import MagicMock
    defaults = {
        "load_to_hub": True, "active_led_effects": [], "testing": False,
        "afc_stats": MagicMock(), "TOOL_LOAD": MagicMock(return_value=True),
        "TOOL_UNLOAD": MagicMock(return_value=True),
        "_check_extruder_temp": MagicMock(return_value=False), "enable_multiple_mapping": False,
    }
    for name, value in defaults.items():
        if not hasattr(afc, name):
            setattr(afc, name, value)
    return afc


def reset_buses() -> None:
    """Forget every serial chain, so units in different tests never share one."""
    CFSBus._buses.clear()


def make_printer() -> MockPrinter:
    """A printer with the CFS extruder registered and a TimedReactor."""
    printer = MockPrinter(afc=MockAFC())
    reactor = TimedReactor()
    printer._reactor = reactor
    printer.reactor = reactor
    afc = complete_afc(printer._afc)
    afc.reactor = reactor
    # AFC.cfg options the CFS reads, neutral values; tests set what they need
    afc.tool_cut = False
    afc.tool_cut_cmd = "AFC_CUT"
    afc.park = False
    afc.park_cmd = None
    afc.form_tip = False
    afc.form_tip_cmd = None
    afc.move_e_pos = Recorder()
    printer._objects["AFC_extruder extruder"] = CfsExtruder()
    return printer


def make_unit(name: str = "CFS_1", values: Optional[Dict[str, Any]] = None,
              printer: Optional[MockPrinter] = None,
              box: Optional[ScriptedBox] = None) -> afcCFS:
    """
    Build an afcCFS through its real __init__ and attach a ScriptedBox.

    :param name: unit name
    :param values: [AFC_CFS] options over serial, hub and extruder defaults
    :param printer: shared printer (several units on one chain), built when None
    :param box: the scripted box; a new one when None (unit.box)
    :return afcCFS: the unit
    """
    printer = printer or make_printer()
    printer._objects.setdefault(f"AFC_hub {name}", CfsHub(name))
    opts: Dict[str, Any] = {"serial": SERIAL, "hub": name, "extruder": "extruder"}
    opts.update(values or {})
    unit = afcCFS(MockConfig(name=f"AFC_CFS {name}", printer=printer, values=opts))
    unit.box = box or ScriptedBox()
    unit.transport.request = unit.box.request
    printer._objects[f"AFC_CFS {name}"] = unit
    return unit


def make_lane(unit: afcCFS, index: int, name: Optional[str] = None,
              values: Optional[Dict[str, Any]] = None) -> AFCLane:
    """
    A real [AFC_lane] section for slot index of unit, registered on the printer.
    Call connect(unit) to attach it the way klippy:connect does.

    :param unit: its unit
    :param index: slot 1-4
    :param name: lane name, default lane<index>
    :param values: extra lane options
    :return AFCLane: the lane
    """
    name = name or f"lane{index}"
    opts: Dict[str, Any] = {"unit": f"{unit.name}:{index}"}
    opts.update(values or {})
    lane = AFCLane(MockConfig(name=f"AFC_lane {name}", printer=unit.printer, values=opts))
    unit.printer._objects[f"AFC_lane {name}"] = lane
    return lane


def connect(unit: afcCFS) -> afcCFS:
    """Run the unit's klippy:connect (AFC attaches its lanes, then the CFS setup)."""
    unit.handle_connect()
    unit.afc.logger.messages.clear()
    unit.box.clear()
    return unit


def gcmd(**params: Any) -> Any:
    """A G-code command with these parameters."""
    return MockGCodeCommand(params=params)
