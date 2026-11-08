# AFCProject Automated Filament Changer
#
# Copyright (C) 2026 AFCProject
#
# This file may be distributed under the terms of the GNU GPLv3 license.
#
# Thanks to @Esoterical, @bolliostu, and @dormouse for contributions to the original source
# and inspiration for this script at
# https://github.com/Esoterical/voron_canbus/blob/main/troubleshooting/debugging/can_debug.sh
"""
Collects AFC diagnostic information and uploads it for AFCProject support.
"""
from __future__ import annotations

import argparse
import datetime
import glob
import ipaddress
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import urllib.request
import zipfile
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Dict, List, Optional, Pattern, Sequence, Tuple

TERMBIN_HOST = "termbin.com"
TERMBIN_PORT = 9999
SEPARATOR = "=" * 64
USB_SYS_ROOT = "/sys/bus/usb/devices"
REDACTED = "<redacted>"
PUBLIC_IP = "<public-ip>"
SECRET_LABEL = "secret value"
IP_LABEL = "public IP address"
_SECRET_KEYS = r"(?:api[_-]?key|token|secret|passw(?:or)?d|passphrase|authorization|credential)"
SECRET_KV_RE = re.compile(
    rf"(?P<key>[\w.\-]*{_SECRET_KEYS}[\w.\-]*[\"']?[ \t]*[:=][ \t]*)\S[^\r\n]*", re.I)
URL_SECRET_RE = re.compile(r"(?P<key>\burl[ \t]*[:=][ \t]*)(?!https?://)\S[^\r\n]*", re.I)
URL_CRED_RE = re.compile(r"(?P<head>\b[A-Za-z][A-Za-z0-9+.\-]*://[^\s:/@]+:)[^\s/@]+@")
BEARER_RE = re.compile(r"(?P<head>\bBearer[ \t]+)[\w.~+/=\-]{8,}")
TOKEN_RE = re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}"
                      r"|github_pat_[A-Za-z0-9_]{20,}"
                      r"|xox[abprs]-[A-Za-z0-9\-]{10,}"
                      r"|AKIA[0-9A-Z]{16}"
                      r"|eyJ[\w\-]{5,}\.[\w\-]{5,}\.[\w\-]{5,})")
IPV4_RE = re.compile(r"(?<![\w.])(?:\d{1,3}\.){3}\d{1,3}(?:/\d{1,2})?(?!\w|\.\d)")
IPV6_RE = re.compile(r"(?<![\w:.])(?=[0-9A-Fa-f:]*:[0-9A-Fa-f:]*:)[0-9A-Fa-f:]+(?:/\d{1,3})?"
                     r"(?![\w:])")
REDACT_RULES: Tuple[Tuple[Pattern[str], str, str], ...] = (
    (SECRET_KV_RE, r"\g<key>" + REDACTED, SECRET_LABEL),
    (URL_SECRET_RE, r"\g<key>" + REDACTED, SECRET_LABEL),
    (URL_CRED_RE, r"\g<head>" + REDACTED + "@", SECRET_LABEL),
    (BEARER_RE, r"\g<head>" + REDACTED, SECRET_LABEL),
    (TOKEN_RE, REDACTED, SECRET_LABEL))


class DebugError(Exception):
    """Raised for conditions that should stop the script with a message."""


@dataclass
class Config:
    """Resolved paths and settings for one run."""

    printer_log_dir: Path
    printer_config_dir: Path
    klipper_dir: Path
    afc_path: Path
    afc_config_dir: Path
    klipper_venv: Path
    moonraker_config: Path
    moonraker_address: str
    moonraker_port: int
    klipper_service: str
    assume_yes: bool
    zip_only: bool
    redact: bool


def _expand(value: str) -> Path:
    """
    Expand `~` in a user supplied path.

    :param value: path string
    :return Path: expanded path
    """
    return Path(value).expanduser()


def _help_formatter(prog: str) -> argparse.HelpFormatter:
    """
    Help formatter with a wide, aligned option column.

    :param prog: program name
    :return argparse.HelpFormatter: formatter instance
    """
    return argparse.HelpFormatter(prog, max_help_position=34, width=100)


def build_parser() -> argparse.ArgumentParser:
    """
    Build the command line parser. Short flags match `install-afc.sh` where they overlap.

    :return argparse.ArgumentParser: the parser
    """
    prog = os.environ.get("AFC_PROG")
    parser = argparse.ArgumentParser(
        prog=prog, usage=f"{prog or 'afc-debug.sh'} [options]",
        description="Collect AFC diagnostic information and upload it for support.",
        formatter_class=_help_formatter)
    parser.add_argument("-a", "--moonraker-address", default="http://localhost",
                        metavar="ADDRESS",
                        help="Moonraker server address (default: http://localhost)")
    parser.add_argument("-n", "--moonraker-port", type=int, default=7125, metavar="PORT",
                        help="Moonraker server port (default: 7125)")
    parser.add_argument("-m", "--moonraker-config", default=None, metavar="FILE",
                        help="path to moonraker.conf (default: <printer config dir>/"
                             "moonraker.conf)")
    parser.add_argument("-k", "--klipper-dir", default="~/klipper", metavar="DIR",
                        help="Klipper directory (default: ~/klipper)")
    parser.add_argument("-y", "--klipper-venv", default="~/klippy-env/bin", metavar="DIR",
                        help="Klipper python venv bin directory (default: ~/klippy-env/bin)")
    parser.add_argument("-s", "--klipper-service", default="klipper", metavar="NAME",
                        help="Klipper service name (default: klipper)")
    parser.add_argument("-p", "--printer-config-dir", default="~/printer_data/config",
                        metavar="DIR",
                        help="printer config directory (default: ~/printer_data/config)")
    parser.add_argument("-l", "--log-dir", default="~/printer_data/logs", metavar="DIR",
                        help="printer log directory (default: ~/printer_data/logs)")
    parser.add_argument("-c", "--afc-config-dir", default=None, metavar="DIR",
                        help="AFC config directory (default: <printer config dir>/AFC)")
    parser.add_argument("-r", "--afc-path", default=None, metavar="DIR",
                        help="AFC-Klipper-Add-On repository (default: the repository "
                             "containing this script)")
    parser.add_argument("-f", "--yes", action="store_true",
                        help="skip the confirmation prompt")
    parser.add_argument("-z", "--zip", action="store_true",
                        help="save a zip archive instead of uploading to termbin.com")
    parser.add_argument("--no-redact", action="store_true",
                        help="do not redact secrets and public IP addresses")
    return parser


def parse_args(argv: Optional[Sequence[str]] = None) -> Config:
    """
    Parse the command line into a resolved `Config`.

    :param argv: arguments without the program name, defaults to `sys.argv`
    :return Config: resolved settings
    """
    args = build_parser().parse_args(argv)
    printer_config_dir = _expand(args.printer_config_dir)
    default_afc_path = Path(__file__).resolve().parent.parent
    return Config(
        printer_log_dir=_expand(args.log_dir),
        printer_config_dir=printer_config_dir,
        klipper_dir=_expand(args.klipper_dir),
        afc_path=_expand(args.afc_path) if args.afc_path else default_afc_path,
        afc_config_dir=(_expand(args.afc_config_dir) if args.afc_config_dir
                        else printer_config_dir / "AFC"),
        klipper_venv=_expand(args.klipper_venv),
        moonraker_config=(_expand(args.moonraker_config) if args.moonraker_config
                          else printer_config_dir / "moonraker.conf"),
        moonraker_address=args.moonraker_address,
        moonraker_port=args.moonraker_port,
        klipper_service=args.klipper_service,
        assume_yes=args.yes,
        zip_only=args.zip,
        redact=not args.no_redact)


def run_command(cmd: Sequence[str], cwd: Optional[Path] = None, timeout: Optional[float] = None,
                merge_stderr: bool = False) -> Optional[str]:
    """
    Run a command and return its output with trailing newlines removed.

    :param cmd: command and arguments
    :param cwd: working directory
    :param timeout: seconds before giving up
    :param merge_stderr: include stderr in the returned output
    :return Optional[str]: output, or None if it failed to run or exited non-zero
    """
    try:
        proc = subprocess.run(list(cmd), cwd=cwd, timeout=timeout, stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT if merge_stderr else subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout.decode("utf-8", errors="replace").rstrip("\n")


def command_output(cmd: Sequence[str], default: str = "Unknown") -> str:
    """
    Output of a command, or a default when it fails.

    :param cmd: command and arguments
    :param default: text returned on failure
    :return str: output or default
    """
    output = run_command(cmd)
    return default if output is None else output


def prepout(title: str, *lines: str) -> str:
    """
    Format a section header followed by optional lines.

    :param title: section title
    :param lines: lines placed under the header
    :return str: formatted text
    """
    text = f"\n{SEPARATOR}\n{title}\n{SEPARATOR}\n"
    for line in lines:
        text += f"{line}\n"
    return text


def file_section(section_name: str, path: Path) -> str:
    """
    Build a report section holding a file's contents.

    :param section_name: section title
    :param path: file to include
    :return str: section text
    """
    text = prepout(section_name)
    if not path.is_file():
        return text + f"File not found: {path}\n"
    data = path.read_bytes()
    if b"\0" in data:
        text += f"WARNING: Null bytes detected in {path}\n"
    return text + data.replace(b"\0", b"").decode("utf-8", errors="replace")


def _sudo(cmd: Sequence[str]) -> None:
    """
    Run a command with sudo, attached to the terminal.

    :param cmd: command and arguments without sudo
    """
    try:
        proc = subprocess.run(["sudo", *cmd])
    except OSError as exc:
        error_str = f"Unable to run sudo: {exc}"
        raise DebugError(error_str)
    if proc.returncode != 0:
        error_str = f"Command failed: sudo {' '.join(cmd)}"
        raise DebugError(error_str)


def is_klipper_running_systemd(service: str) -> bool:
    """
    Check the service state through systemd.

    :param service: Klipper service name
    :return bool: True if the service exists and is active
    """
    units = run_command(["systemctl", "list-units", "--type=service"])
    if units is None or service not in units:
        return False
    return run_command(["systemctl", "is-active", "--quiet", service]) is not None


def is_klipper_running_service(service: str) -> bool:
    """
    Check the service state through the `service` command.

    :param service: Klipper service name
    :return bool: True if the service reports running
    """
    return run_command(["service", service, "status"]) is not None


def stop_klipper(service: str) -> None:
    """
    Stop Klipper using systemd or `service`.

    :param service: Klipper service name
    """
    if shutil.which("systemctl"):
        if is_klipper_running_systemd(service):
            print("Stopping Klipper using systemd...")
            _sudo(["systemctl", "stop", service])
        else:
            print("Klipper is already stopped (systemd).")
    elif shutil.which("service"):
        if is_klipper_running_service(service):
            print("Stopping Klipper using service...")
            _sudo(["service", service, "stop"])
        else:
            print("Klipper is already stopped (service).")
    else:
        raise DebugError("Unable to detect systemd or service management for Klipper.")


def start_klipper(service: str) -> None:
    """
    Start Klipper using systemd or `service`.

    :param service: Klipper service name
    """
    if shutil.which("systemctl"):
        print("Starting Klipper using systemd...")
        _sudo(["systemctl", "start", service])
    elif shutil.which("service"):
        print("Starting Klipper using service...")
        _sudo(["service", service, "start"])
    else:
        raise DebugError("Unable to detect systemd or service management for Klipper.")


def klipper_python(klipper_venv: Path) -> str:
    """
    Python used to run Klipper scripts.

    :param klipper_venv: Klipper venv bin directory
    :return str: venv python if present, otherwise the current interpreter
    """
    venv_python = klipper_venv / "python"
    return str(venv_python) if venv_python.exists() else sys.executable


def collect_can_ids(klipper_dir: Path, klipper_venv: Path) -> List[str]:
    """
    Query every network interface for CAN devices.

    :param klipper_dir: Klipper directory
    :param klipper_venv: Klipper venv bin directory
    :return List[str]: report lines for interfaces that answered
    """
    links = run_command(["ip", "-br", "link", "show"])
    interfaces = [line.split()[0] for line in (links or "").splitlines() if line.split()]
    if not interfaces:
        print("No CAN interfaces found.")
        return []
    print("Querying CAN devices...")
    python = klipper_python(klipper_venv)
    script = str(klipper_dir / "scripts" / "canbus_query.py")
    lines: List[str] = []
    for iface in interfaces:
        output = run_command([python, script, iface], timeout=30, merge_stderr=True)
        if output is None:
            continue
        lines.append(f"Interface: {iface}")
        lines.extend(output.splitlines())
        lines.append("")
    return lines


def get_afc_version(afc_path: Path) -> str:
    """
    AFC version as `<commit count>-<short hash>`.

    :param afc_path: AFC repository
    :return str: version or `Unknown`
    """
    git_hash = run_command(["git", "-C", str(afc_path), "rev-parse", "--short", "HEAD"])
    count = run_command(["git", "-C", str(afc_path), "rev-list", "HEAD", "--count"])
    if git_hash is None or count is None:
        return "Unknown"
    return f"{count}-{git_hash}"


def get_klipper_version(klipper_dir: Path) -> str:
    """
    Klipper version from `git describe`.

    :param klipper_dir: Klipper repository
    :return str: version or `Unknown`
    """
    return command_output(["git", "-C", str(klipper_dir), "describe", "--tags"])


def read_distro(etc_dir: str = "/etc") -> str:
    """
    Contents of the `*-release` files.

    :param etc_dir: directory holding the release files
    :return str: joined contents or `Unknown`
    """
    texts: List[str] = []
    for path in sorted(glob.glob(os.path.join(etc_dir, "*-release"))):
        try:
            texts.append(Path(path).read_text(errors="replace").rstrip("\n"))
        except OSError:
            continue
    return "\n".join(texts) if texts else "Unknown"


def summarize_lsusb(text: str, active: str, path: str) -> List[str]:
    """
    Reduce `lsusb -v` output for one hub to its product, protocols and active alt setting.

    :param text: `lsusb -v` output
    :param active: active alternate setting
    :param path: sysfs path of the hub
    :return List[str]: summary lines
    """
    product = ""
    proto = ""
    alt = ""
    iprotos: List[str] = []
    for line in text.splitlines():
        if "iProduct" in line:
            product = line
        if "bDeviceProtocol" in line:
            proto = line
        if "bAlternateSetting" in line:
            alt = line.split()[-1] if line.split() else ""
        if "bInterfaceProtocol" in line:
            iprotos.append(line + (" (active)" if alt == active else ""))
    return [f"Path:              {path}", product, proto, "\n".join(iprotos), "---"]


def usb_hub_details(sys_root: str = USB_SYS_ROOT) -> List[str]:
    """
    Detailed information for every USB hub (device class 09).

    :param sys_root: sysfs USB devices directory
    :return List[str]: report lines
    """
    lines: List[str] = []
    for dev in sorted(glob.glob(os.path.join(sys_root, "*", ""))):
        try:
            device_class = Path(dev, "bDeviceClass").read_text().strip()
            bus = Path(dev, "busnum").read_text().strip()
            devnum = Path(dev, "devnum").read_text().strip()
        except OSError:
            continue
        if device_class != "09":
            continue
        alt_text = ""
        for alt_file in sorted(glob.glob(os.path.join(dev, "*", "bAlternateSetting"))):
            try:
                alt_text += Path(alt_file).read_text()
            except OSError:
                continue
        active = (alt_text.splitlines() or [""])[0].replace(" ", "")
        verbose = run_command(["lsusb", "-v", "-s", f"{bus}:{devnum}"])
        if verbose is None:
            continue
        lines.extend(summarize_lsusb(verbose, active, dev))
    return lines


def collect_usb_topology() -> str:
    """
    USB tree followed by hub details.

    :return str: report text
    """
    tree = command_output(["lsusb", "-tv"], default="lsusb not available")
    return "\n".join([tree, "==="] + usb_hub_details()).replace("\0", "")


def format_stat_value(value: Any) -> str:
    """
    Convert a JSON scalar to text, rounding numeric values to 2 decimals (half away from zero).

    :param value: JSON scalar
    :return str: display text
    """
    if isinstance(value, bool):
        text = "true" if value else "false"
    elif value is None:
        text = "null"
    else:
        text = str(value)
    if not re.fullmatch(r"-?[0-9.]+", text):
        return text
    try:
        rounded = Decimal(text).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    except InvalidOperation:
        return text
    if rounded == rounded.to_integral_value():
        return str(int(rounded))
    return format(rounded.normalize(), "f")


def flatten_scalars(data: Any, prefix: str = "") -> List[Tuple[str, Any]]:
    """
    Flatten nested JSON into dotted key and scalar value pairs.

    :param data: parsed JSON
    :param prefix: key prefix for nested values
    :return List[Tuple[str, Any]]: flattened pairs
    """
    if isinstance(data, dict):
        items = list(data.items())
    elif isinstance(data, list):
        items = list(enumerate(data))
    else:
        return [(prefix, data)]
    pairs: List[Tuple[str, Any]] = []
    for key, value in items:
        pairs.extend(flatten_scalars(value, f"{prefix}.{key}" if prefix else str(key)))
    return pairs


def fetch_stats(address: str, port: int, timeout: float = 10) -> Optional[Dict[str, Any]]:
    """
    Fetch the `afc_stats` namespace from the Moonraker database.

    :param address: Moonraker address, scheme optional
    :param port: Moonraker port
    :param timeout: request timeout in seconds
    :return Optional[Dict[str, Any]]: parsed response or None on failure
    """
    base = address.rstrip("/")
    if "://" not in base:
        base = f"http://{base}"
    url = f"{base}:{port}/server/database/item?namespace=afc_stats"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8", errors="replace"))
    except (OSError, ValueError):
        return None


def stats_report(address: str, port: int, now: datetime.datetime) -> str:
    """
    Build the AFC statistics section.

    :param address: Moonraker address
    :param port: Moonraker port
    :param now: timestamp for the header
    :return str: report text
    """
    text = (f"{SEPARATOR}\nAFC STATS REPORT - {now.strftime('%Y-%m-%d %H:%M:%S')}\n"
            f"{SEPARATOR}\n")
    data = fetch_stats(address, port)
    value = data.get("result", {}).get("value") if isinstance(data, dict) else None
    if value is None:
        return text + "Unable to retrieve AFC stats from Moonraker.\n\n"
    rows = [(key, format_stat_value(val)) for key, val in flatten_scalars(value)]
    width = max((len(key) for key, _ in rows), default=0)
    for key, val in rows:
        text += f"{key.ljust(width)}  {val}\n"
    return text + "\n"


def termbin_reachable(host: str = TERMBIN_HOST, port: int = TERMBIN_PORT,
                      timeout: float = 3) -> bool:
    """
    Check that termbin accepts connections.

    :param host: termbin host
    :param port: termbin port
    :param timeout: connect timeout in seconds
    :return bool: True if a connection was made
    """
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def upload_to_termbin(data: bytes, host: str = TERMBIN_HOST, port: int = TERMBIN_PORT,
                      timeout: float = 10) -> str:
    """
    Send data to termbin and return the resulting URL.

    :param data: bytes to upload, null bytes are removed
    :param host: termbin host
    :param port: termbin port
    :param timeout: socket timeout in seconds
    :return str: URL, or an empty string on failure
    """
    chunks: List[bytes] = []
    try:
        with socket.create_connection((host, port), timeout=timeout) as sock:
            sock.sendall(data.replace(b"\0", b""))
            sock.shutdown(socket.SHUT_WR)
            while True:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                chunks.append(chunk)
    except OSError:
        return ""
    return b"".join(chunks).replace(b"\0", b"").decode("utf-8", errors="replace").strip()


def upload_file(path: Path) -> str:
    """
    Upload a file to termbin.

    :param path: file to upload
    :return str: `<name>: <url>` or `<name>: Upload failed`
    """
    url = upload_to_termbin(path.read_bytes())
    return f"{path.name}: {url}" if url else f"{path.name}: Upload failed"


def should_upload(name: str) -> bool:
    """
    Whether an extracted log is one of the files sent to support.

    :param name: file name
    :return bool: True for config, shutdown, AFC and klippy.log files
    """
    return ("config" in name
            or "shutdown" in name
            or "AFC" in name
            or name.endswith("klippy.log"))


def _is_public_ip(candidate: str) -> bool:
    """
    Check whether text is a globally routable IP address or network.

    :param candidate: IPv4 or IPv6 text, optionally with a `/prefix`
    :return bool: True for public addresses, False for private, reserved or invalid text
    """
    try:
        address = ipaddress.ip_address(candidate.split("/")[0])
    except ValueError:
        return False
    return address.is_global


def _redact_ips(text: str) -> Tuple[str, int]:
    """
    Replace public IP addresses. Private, loopback, link local and reserved ones are kept.

    :param text: text to scan
    :return Tuple[str, int]: redacted text and the number of addresses replaced
    """
    count = 0

    def replace(match: "re.Match[str]") -> str:
        nonlocal count
        if _is_public_ip(match.group(0)):
            count += 1
            return PUBLIC_IP
        return match.group(0)

    for pattern in (IPV4_RE, IPV6_RE):
        text = pattern.sub(replace, text)
    return text, count


def redact_text(text: str) -> Tuple[str, Dict[str, int]]:
    """
    Redact secrets and public IP addresses. This is best effort pattern matching.

    :param text: text to scan
    :return Tuple[str, Dict[str, int]]: redacted text and the replacement count per label
    """
    counts: Dict[str, int] = {}
    for pattern, replacement, label in REDACT_RULES:
        text, found = pattern.subn(replacement, text)
        if found:
            counts[label] = counts.get(label, 0) + found
    text, found = _redact_ips(text)
    if found:
        counts[IP_LABEL] = found
    return text, counts


def describe_counts(counts: Dict[str, int]) -> str:
    """
    Format replacement counts for display.

    :param counts: replacement count per label
    :return str: text such as `2 secret values, 1 public IP address`
    """
    parts: List[str] = []
    for label, found in counts.items():
        suffix = "" if found == 1 else ("es" if label.endswith("s") else "s")
        parts.append(f"{found} {label}{suffix}")
    return ", ".join(parts)


def redact_file(path: Path) -> Dict[str, int]:
    """
    Redact a file in place. The file is only rewritten when something was replaced.

    :param path: file to scan
    :return Dict[str, int]: replacement count per label
    """
    text, counts = redact_text(path.read_bytes().decode("utf-8", errors="replace"))
    if counts:
        path.write_text(text, encoding="utf-8")
    return counts


def redact_collected(cfg: Config, report: str, temp_dir: Path) -> str:
    """
    Redact the report and the extracted logs before they leave the machine.

    :param cfg: run settings
    :param report: report text
    :param temp_dir: directory with extracted logs
    :return str: report with a redaction summary appended when anything was replaced
    """
    if not cfg.redact:
        print("Redaction disabled. Secrets and public IP addresses will be included.")
        return report
    print("Redacting secrets and public IP addresses (best effort)...")
    report, counts = redact_text(report)
    summary = {"afc_debug.log": counts} if counts else {}
    for path in sorted(temp_dir.iterdir()):
        file_counts = redact_file(path)
        if file_counts:
            summary[path.name] = file_counts
    if not summary:
        print("Nothing to redact.")
        return report
    lines = [f"{name}: {describe_counts(found)}" for name, found in summary.items()]
    for line in lines:
        print(f"Redacted {line}")
    return report + prepout("Redaction", *lines)


def extract_klipper_logs(cfg: Config, temp_dir: Path) -> None:
    """
    Copy Klipper and AFC logs into a temp dir and run Klipper's `logextract.py`.

    :param cfg: run settings
    :param temp_dir: destination directory
    """
    print("Extracting Klipper logs...")
    klippy_log = cfg.printer_log_dir / "klippy.log"
    if not klippy_log.is_file():
        print(f"File not found: {klippy_log}")
        return
    shutil.copy(klippy_log, temp_dir)
    for afc_log in sorted(cfg.printer_log_dir.glob("AFC.log*")):
        shutil.copy(afc_log, temp_dir)
    script = str(cfg.klipper_dir / "scripts" / "logextract.py")
    python = klipper_python(cfg.klipper_venv)
    if run_command([python, script, "./klippy.log"], cwd=temp_dir, merge_stderr=True) is None:
        print("Klipper log extraction failed.")


def system_sections(cfg: Config) -> str:
    """
    System, version, USB, serial and CAN sections of the report.

    :param cfg: run settings
    :return str: report text
    """
    can_ids = collect_can_ids(cfg.klipper_dir, cfg.klipper_venv)
    text = prepout("System Information",
                   f"Distro: {read_distro()}",
                   f"Kernel: {command_output(['uname', '-a'])}",
                   f"Uptime: {command_output(['uptime'])}")
    text += prepout("Versions")
    text += f"AFC Version: {get_afc_version(cfg.afc_path)}\n"
    text += f"Klipper Version: {get_klipper_version(cfg.klipper_dir)}\n"
    text += prepout("USB Topology")
    text += collect_usb_topology() + "\n"
    serial = command_output(["ls", "-l", "/dev/serial/by-id/"],
                            default="No serial devices found.")
    text += prepout("Serial IDs")
    text += serial + "\n"
    text += prepout("CAN Bus IDs")
    text += ("\n".join(can_ids) if can_ids else "No CAN devices found.") + "\n"
    return text


def config_sections(cfg: Config) -> str:
    """
    Sections for every AFC config file and the Moonraker config.

    :param cfg: run settings
    :return str: report text
    """
    text = ""
    if cfg.afc_config_dir.is_dir():
        for path in sorted(p for p in cfg.afc_config_dir.rglob("*") if p.is_file()):
            text += file_section(path.name, path)
    else:
        text += f"File not found: {cfg.afc_config_dir}\n"
    if cfg.moonraker_config.is_file():
        text += file_section(str(cfg.moonraker_config), cfg.moonraker_config)
    else:
        text += f"File not found: {cfg.moonraker_config}\n"
    return text


def write_zip(report: str, temp_dir: Path, now: datetime.datetime) -> Path:
    """
    Save the report and extracted logs to a zip in the home directory.

    :param report: report text
    :param temp_dir: directory with extracted logs
    :param now: timestamp for the file name
    :return Path: zip file path
    """
    zip_path = Path.home() / f"afc_debug_logs_{now.strftime('%Y%m%d_%H%M%S')}.zip"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("afc_debug.log", report.replace("\0", ""))
        for path in sorted(temp_dir.iterdir()):
            archive.write(path, path.name)
    return zip_path


def confirm(assume_yes: bool) -> bool:
    """
    Ask the user to continue.

    :param assume_yes: skip the prompt
    :return bool: True to continue, False if the user declined
    """
    if assume_yes:
        return True
    try:
        answer = input("Do you wish to continue? [y/n]: ")
    except EOFError:
        answer = ""
    letter = answer[:1].lower()
    if letter == "y":
        return True
    if letter == "n":
        return False
    raise DebugError("Please answer y or n.")


def save_zip(report: str, temp_dir: Path, now: datetime.datetime, reason: str) -> None:
    """
    Save the report and logs to a zip and tell the user where it is.

    :param report: report text
    :param temp_dir: directory with extracted logs
    :param now: timestamp for the zip name
    :param reason: why the zip is being created, shown to the user
    """
    print(f"{reason}. Creating a zip archive of logs instead.")
    zip_path = write_zip(report, temp_dir, now)
    print(f"Logs have been saved to {zip_path}")
    print("Please share this file with the AFCProject support team.")


def deliver(cfg: Config, report: str, temp_dir: Path, now: datetime.datetime) -> None:
    """
    Upload the report, or save it to a zip when zip mode is set or the upload cannot happen.

    :param cfg: run settings
    :param report: report text
    :param temp_dir: directory with extracted logs
    :param now: timestamp for the zip name
    """
    if cfg.zip_only:
        save_zip(report, temp_dir, now, "Zip mode selected")
        return
    if not termbin_reachable():
        save_zip(report, temp_dir, now, f"Failed to connect to {TERMBIN_HOST}")
        return
    uploaded: List[str] = []
    for path in sorted(temp_dir.iterdir()):
        if should_upload(path.name):
            print(f'Uploading "{path.name}" to termbin...')
            uploaded.append(upload_file(path))
    full_report = report
    if uploaded:
        full_report += prepout("Uploaded Extracted Klippy Configuration and Shutdown Logs",
                               *uploaded)
    print(f"Uploading logs to {TERMBIN_HOST}...")
    url = upload_to_termbin(full_report.encode("utf-8", errors="replace"))
    if not url:
        save_zip(report, temp_dir, now, f"Failed to upload logs to {TERMBIN_HOST}")
        return
    print(f"Logs are available at {url}")
    print("Please share this URL with the AFCProject support team.")


def run(cfg: Config) -> int:
    """
    Collect diagnostics and deliver them.

    :param cfg: run settings
    :return int: process exit code
    """
    print("This script will collect diagnostic information and upload it to AFCProject "
          "support.")
    print("Please review the script if you have concerns about its contents.")
    print("Klipper will be stopped during this process. Do not run this while printing.")
    print("")
    if not confirm(cfg.assume_yes):
        return 0
    stop_klipper(cfg.klipper_service)
    try:
        with tempfile.TemporaryDirectory() as temp_name:
            temp_dir = Path(temp_name)
            print(f"Temporary directory created at {temp_dir}")
            extract_klipper_logs(cfg, temp_dir)
            print("Gathering system information...")
            report = system_sections(cfg)
            print("Gathering AFC statistics")
            now = datetime.datetime.now()
            report += stats_report(cfg.moonraker_address, cfg.moonraker_port, now)
            report += config_sections(cfg)
            report = redact_collected(cfg, report, temp_dir)
            deliver(cfg, report, temp_dir, now)
    finally:
        start_klipper(cfg.klipper_service)
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    """
    Entry point.

    :param argv: arguments without the program name
    :return int: process exit code
    """
    cfg = parse_args(argv)
    try:
        return run(cfg)
    except DebugError as exc:
        print(str(exc))
        return 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
