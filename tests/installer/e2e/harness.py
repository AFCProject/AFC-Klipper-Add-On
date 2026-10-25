"""End-to-end harness: run the real ``install-afc.sh`` / ``update-afc.sh`` scripts in a sandbox.

Each run gets its own HOME, Klipper tree and copy of the checkout, with git, sudo, systemctl
and friends replaced by stubs that log every call. A result carries the exit status, the
output, the files and links left behind and the log of stubbed commands.
"""
import http.server
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

REPO_ROOT = Path(__file__).resolve().parents[3]
COPY_ITEMS = ["install-afc.sh", "update-afc.sh", "include", "config", "templates", "extras",
              "installer"]
IDLE = json.dumps({"result": {"status": {"idle_timeout": {"state": "Idle"}}}})
PRINTING = json.dumps({"result": {"status": {"idle_timeout": {"state": "Printing"}}}})

GIT_STUB = r"""#!/bin/bash
echo "git $*" >> "$AFC_CALLS_LOG"
args=" $* "
case "$args" in
  *" rev-parse --short HEAD "*) echo abc1234 ;;
  *" rev-parse --verify --quiet "*)
    missing="${AFC_STUB_NOBRANCH:-@none@}"
    [[ " $* " == *" $missing "* || " $* " == *" origin/$missing "* ]] && exit 1 ;;
  *" checkout --quiet "*) echo "${@: -1}" > "$AFC_STUB_STATE/branch" ;;
  *" rev-parse --abbrev-ref HEAD "*)
    if [ -f "$AFC_STUB_STATE/branch" ]; then cat "$AFC_STUB_STATE/branch"
    else echo "${AFC_STUB_BRANCH:-main}"; fi ;;
  *" pull "*) touch "$AFC_STUB_STATE/pulled" ;;&
  *" status --porcelain --branch "*)
    if [ -f "$AFC_STUB_STATE/pulled" ]; then echo "## main...origin/main"
    else echo "## main...origin/main${AFC_STUB_BEHIND:-}"; fi ;;
  *" diff --quiet --cached "*) exit "${AFC_STUB_STAGED:-0}" ;;
  *" diff --quiet "*) exit "${AFC_STUB_DIRTY:-0}" ;;
esac
exit 0
"""
LOGGING_STUB = '#!/bin/bash\necho "{name} $*" >> "$AFC_CALLS_LOG"\n'
SILENT_STUB = "#!/bin/bash\nexit 0\n"
_RUN_INSTALLER = (
    "import sys, time, runpy; time.sleep = lambda s: None; "
    "sys.argv = ['installer'] + sys.argv[1:]; "
    "runpy.run_module('installer', run_name='__main__')"
)
PYTHON_WRAPPER = (
    "#!/bin/sh\n"
    'if [ "$1" = "-m" ] && [ "$2" = "installer" ]; then\n'
    "  shift 2\n"
    '  exec {real} -c "' + _RUN_INSTALLER + '" "$@"\n'
    "fi\n"
    'exec {real} "$@"\n'
)
CLEAR_SEQUENCES = ("\x1b[H\x1b[2J\x1b[3J", "\x1b[H\x1b[2J", "\x1bc")
BACKUP_NAME = re.compile(r"\.(backup|bak)\.\d{14}")


@dataclass
class Sandbox:
    """One side's private world."""

    root: Path
    home: Path
    afc: Path
    bin: Path
    log: Path
    venv: Path


@dataclass
class Result:
    """What one side did."""

    code: int
    stdout: str
    stderr: str
    log: List[str]
    tree: Dict[str, str]
    sandbox: Sandbox


@dataclass
class Case:
    """One scenario: the command line, the typed answers and the sandbox setup."""

    args: List[str] = field(default_factory=list)
    answers: List[str] = field(default_factory=list)
    program: str = "install-afc.sh"
    git: bool = True
    printer_cfg: Optional[str] = "[printer]\nkinematics: corexy\n"
    moonraker: Optional[str] = "[server]\nhost: 0.0.0.0\n"
    files: Dict[str, str] = field(default_factory=dict)  # home-relative extra files
    env: Dict[str, str] = field(default_factory=dict)
    idle: bool = True
    use_server: bool = False  # point -a/-n at a local fake Moonraker
    prepare: Optional[Callable[[Sandbox], None]] = None


class FakeMoonraker:
    """A tiny HTTP server answering the idle_timeout query on a random local port."""

    def __init__(self, body: str) -> None:
        handler_body = body.encode()

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(handler_body)

            def log_message(self, *args: object) -> None:
                pass

        self.server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def port(self) -> int:
        return int(self.server.server_address[1])

    def __enter__(self) -> "FakeMoonraker":
        self.thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.server.shutdown()
        self.server.server_close()


def build_sandbox(root: Path, case: Case) -> Sandbox:
    """Create HOME, the Klipper tree, the stub commands and a copy of the checkout."""
    home = root / "home"
    cfg = home / "printer_data" / "config"
    cfg.mkdir(parents=True)
    if case.printer_cfg is not None:
        (cfg / "printer.cfg").write_text(case.printer_cfg)
    if case.moonraker is not None:
        (cfg / "moonraker.conf").write_text(case.moonraker)
    extras = home / "klipper" / "klippy" / "extras"
    extras.mkdir(parents=True)
    (extras / "__init__.py").write_text("stock\n")
    exclude = home / "klipper" / ".git" / "info" / "exclude"
    exclude.parent.mkdir(parents=True)
    exclude.write_text("")
    afc = home / "AFC-Klipper-Add-On"
    afc.mkdir()
    for item in COPY_ITEMS:
        source = REPO_ROOT / item
        if source.is_dir():
            shutil.copytree(
                source, afc / item, ignore=shutil.ignore_patterns("__pycache__", "*.pyc")
            )
        else:
            shutil.copy2(source, afc / item)
    if case.git:
        (afc / ".git").mkdir()
    for relative, text in case.files.items():
        path = home / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    bin_dir = root / "bin"
    bin_dir.mkdir()
    stubs = {
        "git": GIT_STUB,
        "sudo": LOGGING_STUB.format(name="sudo"),
        "systemctl": LOGGING_STUB.format(name="systemctl"),
        "service": LOGGING_STUB.format(name="service"),
        "su": LOGGING_STUB.format(name="su"),
        "chown": LOGGING_STUB.format(name="chown"),
        "sleep": SILENT_STUB,
    }
    for name, body in stubs.items():
        script = bin_dir / name
        script.write_text(body)
        script.chmod(script.stat().st_mode | stat.S_IXUSR)
    venv = home / "klippy-env" / "bin"
    venv.mkdir(parents=True)
    wrapper = venv / "python"
    wrapper.write_text(PYTHON_WRAPPER.format(real=sys.executable))
    wrapper.chmod(wrapper.stat().st_mode | stat.S_IXUSR)
    sandbox = Sandbox(root, home, afc, bin_dir, root / "calls.log", venv)
    if case.prepare is not None:
        case.prepare(sandbox)
    return sandbox


EXCLUDE_FILE = "klipper/.git/info/exclude"


def preinstall(
    answers: List[str], args: Tuple[str, ...] = ("-t",)
) -> Callable[[Sandbox], None]:
    """
    Build a ``Case.prepare`` hook that runs the installer once first.

    The install's command log is cleared afterwards so a test only sees its own commands.
    """
    def prepare(sb: Sandbox) -> None:
        env = {
            "HOME": str(sb.home), "PATH": f"{sb.bin}:{os.environ['PATH']}", "TERM": "xterm",
            "AFC_CALLS_LOG": str(sb.log), "AFC_STUB_STATE": str(sb.root),
        }
        done = subprocess.run(
            ["./install-afc.sh", *args], cwd=sb.afc, env=env, capture_output=True, text=True,
            input="".join(f"{answer}\n" for answer in answers), timeout=120,
        )
        assert done.returncode == 0, done.stdout + done.stderr
        if sb.log.exists():
            sb.log.unlink()
    return prepare


def snapshot(sb: Sandbox) -> Dict[str, str]:
    """Files and links the installer can change, with the sandbox path and dates masked."""
    home = str(sb.home)
    seen: Dict[str, str] = {}

    def mask(text: str) -> str:
        return BACKUP_NAME.sub(r".\1.<DATE>", text.replace(home, "<HOME>"))

    for base in (sb.home / "printer_data", sb.home / "klipper"):
        for path in sorted(base.rglob("*")):
            relative = mask(str(path.relative_to(sb.home)))
            if path.is_symlink():
                seen[relative] = f"-> {mask(os.readlink(path))}"
            elif path.is_file():
                text = mask(path.read_bytes().decode("utf-8", errors="replace"))
                if relative == EXCLUDE_FILE:
                    # entry order is not meaningful
                    text = "".join(f"{line}\n" for line in sorted(text.splitlines()))
                seen[relative] = text
    return seen


def normalize(text: str, sb: Sandbox) -> str:
    """Make output comparable: mask the sandbox path and backup dates, drop clear sequences."""
    text = text.replace(str(sb.home), "<HOME>")
    for sequence in CLEAR_SEQUENCES:
        text = text.replace(sequence, "")
    return BACKUP_NAME.sub(r".\1.<DATE>", text)


def run_case(tmp_path: Path, case: Case) -> Result:
    """Run the script named by the case in a fresh sandbox, feeding the answers on stdin."""
    server = FakeMoonraker(IDLE if case.idle else PRINTING) if case.use_server else None
    if server is not None:
        server.__enter__()
    try:
        sb = build_sandbox(tmp_path, case)
        args = [arg.replace("{home}", str(sb.home)) for arg in case.args]
        if server is not None:
            args = ["-a", "http://127.0.0.1", "-n", str(server.port), *args]
        env = {
            "HOME": str(sb.home),
            "PATH": f"{sb.bin}:{os.environ['PATH']}",
            "TERM": "xterm",
            "AFC_CALLS_LOG": str(sb.log),
            "AFC_STUB_STATE": str(sb.root),
            "AFC_NO_RICH": "1",
            **case.env,
        }
        completed = subprocess.run(
            [f"./{case.program}", *args], cwd=sb.afc, env=env, capture_output=True, text=True,
            input="".join(f"{answer}\n" for answer in case.answers), timeout=120,
        )
        log = sb.log.read_text().splitlines() if sb.log.exists() else []
        home = str(sb.home)
        return Result(
            completed.returncode,
            normalize(completed.stdout, sb),
            normalize(completed.stderr, sb),
            [BACKUP_NAME.sub(r".\1.<DATE>", line.replace(home, "<HOME>")) for line in log],
            snapshot(sb),
            sb,
        )
    finally:
        if server is not None:
            server.__exit__()
