from __future__ import annotations

# pytest plugin that collects *.test files as Klipper integration tests.
# Adapted from KalicoCrew/kalico/test/klippy/conftest.py.
#
# Each .test file describes:
#   DICTIONARY <fname>   - MCU dict file (looked up in DICTDIR / test/dict/)
#   CONFIG    <fname>    - Klipper config file (relative to the .test file)
#   SHOULD_FAIL          - mark the test as expected to fail (xfail)
#   <anything else>      - treated as a GCode line to execute
#
# Usage:
#   KLIPPER_PATH=/path/to/klipper pytest tests/klippy/
#   DICTDIR=/path/to/dicts pytest tests/klippy/
#
# Debugging options:
#   pytest tests/klippy/ --klippy-keep      Keep temp files after test completion

import os
import pathlib
import shutil
import subprocess
import sys
import tempfile

import pytest

ROOT = pathlib.Path(__file__).parent.parent.parent
KLIPPER_PATH = pathlib.Path(
    os.environ.get("KLIPPER_PATH", str(ROOT / "klipper"))
)
DICT_DIR = pathlib.Path(
    os.environ.get("DICTDIR", str(ROOT / "tests" / "dict"))
)
LOG_DIR = ROOT / "tests" / "logs"


def pytest_addoption(parser):
    parser.addoption(
        "--klippy-keep", action="store_true", default=False,
        help="Keep temporary files (logs, gcode) after test completion",
    )


def pytest_collect_file(parent, file_path):
    if file_path.suffix == ".test":
        if not KLIPPER_PATH.exists():
            return None  # Klipper not available; skip integration tests
        return KlippyTest.from_parent(parent, path=file_path)


class KlippyTest(pytest.File):
    def _resolve(self, fname, root=None):
        base = root if root else self.path.parent
        return base.joinpath(fname).resolve()

    def collect(self):
        should_fail = False
        gcode: list[str] = []
        config_file = None
        dictionaries: list[str] = []

        with self.path.open("r", encoding="utf-8") as fh:
            for line in fh:
                parts = line.strip().split()
                if not parts or line.strip().startswith("#"):
                    continue

                if parts[0] == "SHOULD_FAIL":
                    should_fail = True

                elif parts[0] == "DICTIONARY":
                    dictionaries = [str(self._resolve(parts[1], root=DICT_DIR))]
                    # Optional extra MCUs: mcu_name=dict_file
                    for extra in parts[2:]:
                        mcu, fname = extra.split("=", maxsplit=1)
                        dictionaries.append(
                            f"{mcu}={self._resolve(fname, root=DICT_DIR)}"
                        )

                elif parts[0] == "CONFIG":
                    config_file = self._resolve(parts[1])

                else:
                    gcode.append(line.strip())

        keepfiles = self.config.getoption("--klippy-keep")

        yield KlippyTestItem.from_parent(
            self,
            name=str(config_file),
            gcode=gcode,
            config_file=config_file,
            dictionaries=dictionaries,
            should_fail=should_fail,
            klipper_path=KLIPPER_PATH,
            keepfiles=keepfiles,
        )


class KlippyTestItem(pytest.Item):
    def __init__(
        self,
        *,
        config_file,
        dictionaries,
        gcode,
        should_fail=False,
        klipper_path,
        keepfiles=False,
        **kwargs,
    ):
        super().__init__(**kwargs)
        self.config_file = config_file
        self.dictionaries = dictionaries
        self.gcode = gcode
        self.klipper_path = klipper_path
        self.keepfiles = keepfiles
        if should_fail:
            self.add_marker(pytest.mark.xfail)

    def setup(self):
        # Build a descriptive directory name from the firmware, .test file, and config.
        firmware = self.klipper_path.name                     # e.g. "klipper" or "kalico"
        test_file = pathlib.Path(self.parent.path).stem       # e.g. "afc_buffer_commands"
        config_name = pathlib.Path(self.config_file).stem     # e.g. "afc_with_buffer"
        prefix = f"{firmware}_{test_file}_{config_name}_"

        LOG_DIR.mkdir(parents=True, exist_ok=True)
        self.tmp_dir = pathlib.Path(tempfile.mkdtemp(prefix=prefix, dir=LOG_DIR))
        # AFC_logger writes to AFC.log in the same directory as klippy.log.
        self.afc_log_path = self.tmp_dir / "AFC.log"

    def teardown(self):
        if self.keepfiles:
            sys.stderr.write(f"  Keeping temp files in: {self.tmp_dir}\n")
            return
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def runtest(self):
        gcode_file = self.tmp_dir / "_test_.gcode"
        output_file = self.tmp_dir / "_test_.output"
        log_file = self.tmp_dir / "klippy.log"

        gcode_file.write_text("\n".join(self.gcode) + "\n")

        # Run klippy via the launcher so that QueueListener's bg_thread is
        # daemonised, allowing klippy to exit cleanly when -l is passed.
        launcher = pathlib.Path(__file__).parent / "klippy_launcher.py"
        klippy_script = self.klipper_path / "klippy" / "klippy.py"
        args = [sys.executable, str(launcher), str(klippy_script), str(self.config_file)]
        args += ["-i", str(gcode_file)]
        args += ["-o", str(output_file)]
        args += ["-l", str(log_file)]  # enables AFC.log in the same directory
        args += ["-v"]
        for df in self.dictionaries:
            args += ["-d", df]

        # Add Klipper root to PYTHONPATH so "python -m klippy" resolves.
        env = dict(os.environ)
        existing_pp = env.get("PYTHONPATH", "")
        klipper_str = str(self.klipper_path)
        env["PYTHONPATH"] = (
            f"{klipper_str}{os.pathsep}{existing_pp}"
            if existing_pp
            else klipper_str
        )

        result = subprocess.run(
            args,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            env=env,
        )

        if result.returncode != 0:
            raise KlippyTestError(result.returncode, args, result.stdout)

    def repr_failure(self, excinfo, style=None):
        if isinstance(excinfo.value, KlippyTestError):
            err = excinfo.value
            output = err.output or ""
            tail = output[-4000:] if len(output) > 4000 else output
            lines = [
                f"Klippy test failed (exit {err.returncode}): {self.name}",
                f"  Config:       {self.config_file}",
                f"  Dictionaries: {', '.join(self.dictionaries)}",
            ]
            if tail:
                lines += ["", tail]
            # Include klippy.log — with -l, klippy writes its detailed log here
            # rather than to stdout, so stdout is mostly empty on failure.
            klippy_log_path = self.tmp_dir / "klippy.log"
            if klippy_log_path.exists():
                klippy_content = klippy_log_path.read_text(errors="replace")
                if klippy_content:
                    klippy_tail = klippy_content[-4000:] if len(klippy_content) > 4000 else klippy_content
                    lines += [
                        "",
                        "─── klippy.log ────────────────────────────────────────────",
                        klippy_tail,
                    ]
            # Include AFC.log when it was written (requires -l to have been passed).
            if self.afc_log_path.exists():
                afc_content = self.afc_log_path.read_text(errors="replace")
                if afc_content:
                    afc_tail = afc_content[-4000:] if len(afc_content) > 4000 else afc_content
                    lines += [
                        "",
                        "─── AFC.log ───────────────────────────────────────────────",
                        afc_tail,
                    ]
            return "\n".join(lines)
        return super().repr_failure(excinfo=excinfo, style=style)


class KlippyTestError(Exception):
    def __init__(self, returncode, args, output):
        super().__init__(f"klippy exited with {returncode}")
        self.returncode = returncode
        self.args_ = args
        self.output = output
