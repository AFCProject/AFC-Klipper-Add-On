# Minimal stub for Klipper's klippy/configfile.py -- only the members
# actually used by this project's extras/*.py, not the full real API.
from configparser import RawConfigParser
from typing import Any, Callable, Optional, Tuple, TypeVar, Union, overload

from klippy import Printer

_T = TypeVar("_T")
_V = TypeVar("_V")

class error(Exception): ...

class ConfigWrapper:
    section: str
    fileconfig: RawConfigParser
    error: type[error]

    def __init__(self, printer: Printer, fileconfig: RawConfigParser,
                 access_tracking: dict, section: str) -> None: ...
    def get_printer(self) -> Printer: ...
    def get_name(self) -> str: ...
    # _get_wrapper returns `default` unchanged (without parsing) when the
    # option is missing, so a None default (explicit or via a variable typed
    # Optional) can propagate straight through -- modeled with a generic
    # fallback overload (as typeshed does for dict.get) rather than `default:
    # None` directly, since that form makes mypy flag the pair as
    # overlapping-with-incompatible-return.
    @overload
    def get(self, option: str, *, note_valid: bool = True) -> str: ...
    @overload
    def get(self, option: str, default: str, note_valid: bool = True) -> str: ...
    @overload
    def get(self, option: str, default: _T, note_valid: bool = True) -> Union[str, _T]: ...
    @overload
    def getint(self, option: str, *, minval: Optional[int] = None,
               maxval: Optional[int] = None, note_valid: bool = True) -> int: ...
    @overload
    def getint(self, option: str, default: int, minval: Optional[int] = None,
               maxval: Optional[int] = None, note_valid: bool = True) -> int: ...
    @overload
    def getint(self, option: str, default: _T, minval: Optional[int] = None,
               maxval: Optional[int] = None, note_valid: bool = True) -> Union[int, _T]: ...
    @overload
    def getfloat(self, option: str, *, minval: Optional[float] = None,
                 maxval: Optional[float] = None, above: Optional[float] = None,
                 below: Optional[float] = None, note_valid: bool = True) -> float: ...
    @overload
    def getfloat(self, option: str, default: float, minval: Optional[float] = None,
                 maxval: Optional[float] = None, above: Optional[float] = None,
                 below: Optional[float] = None, note_valid: bool = True) -> float: ...
    @overload
    def getfloat(self, option: str, default: _T, minval: Optional[float] = None,
                 maxval: Optional[float] = None, above: Optional[float] = None,
                 below: Optional[float] = None, note_valid: bool = True) -> Union[float, _T]: ...
    @overload
    def getboolean(self, option: str, *, note_valid: bool = True) -> bool: ...
    @overload
    def getboolean(self, option: str, default: bool, note_valid: bool = True) -> bool: ...
    @overload
    def getboolean(self, option: str, default: _T, note_valid: bool = True) -> Union[bool, _T]: ...
    # getlist/getlists both funnel through getlists, which parses via
    # `lparser` (always a tuple, nested when `seps` has more than one entry)
    # when the option is present, but -- like the scalar getters above --
    # goes through _get_wrapper and returns `default` unchanged when it's
    # absent, so the same no-default/generic-default overload split applies.
    @overload
    def getlist(self, option: str, *, sep: str = ',', count: Optional[int] = None,
                note_valid: bool = True) -> Tuple[str, ...]: ...
    @overload
    def getlist(self, option: str, default: Tuple[str, ...], sep: str = ',',
                count: Optional[int] = None, note_valid: bool = True) -> Tuple[str, ...]: ...
    @overload
    def getlist(self, option: str, default: _T, sep: str = ',', count: Optional[int] = None,
                note_valid: bool = True) -> Union[Tuple[str, ...], _T]: ...
    # getfloatlist funnels through getlists the same way getlist does, just
    # with parser=float instead of parser=str.
    @overload
    def getfloatlist(self, option: str, *, sep: str = ',', count: Optional[int] = None,
                     note_valid: bool = True) -> Tuple[float, ...]: ...
    @overload
    def getfloatlist(self, option: str, default: Tuple[float, ...], sep: str = ',',
                     count: Optional[int] = None, note_valid: bool = True) -> Tuple[float, ...]: ...
    @overload
    def getfloatlist(self, option: str, default: _T, sep: str = ',', count: Optional[int] = None,
                     note_valid: bool = True) -> Union[Tuple[float, ...], _T]: ...
    @overload
    def getlists(self, option: str, *, seps: tuple = (',',), count: Optional[int] = None,
                 parser: Callable[[str], Any] = str, note_valid: bool = True) -> Tuple[Any, ...]: ...
    @overload
    def getlists(self, option: str, default: Tuple[Any, ...], seps: tuple = (',',),
                 count: Optional[int] = None, parser: Callable[[str], Any] = str,
                 note_valid: bool = True) -> Tuple[Any, ...]: ...
    @overload
    def getlists(self, option: str, default: _T, seps: tuple = (',',), count: Optional[int] = None,
                 parser: Callable[[str], Any] = str,
                 note_valid: bool = True) -> Union[Tuple[Any, ...], _T]: ...
    # getchoice always returns a value out of `choices`, since `default`
    # (when given) must itself be a valid key of `choices` -- unlike the
    # scalar getters above, there's no "unparsed default passes through
    # untouched" case to split into a separate overload.
    @overload
    def getchoice(self, option: str, choices: dict[Any, _V], *,
                  note_valid: bool = True) -> _V: ...
    @overload
    def getchoice(self, option: str, choices: dict[Any, _V], default: Any,
                  note_valid: bool = True) -> _V: ...
    def getsection(self, section: str) -> ConfigWrapper: ...
    def has_section(self, section: str) -> bool: ...
    def deprecate(self, option: str, value: Any = None) -> None: ...
