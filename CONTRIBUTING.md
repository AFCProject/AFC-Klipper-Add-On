
# Contributing to This Project

Thanks for your interest in contributing! Whether it's a bug report, new feature, or documentation fix, your help is welcome and appreciated.

## Getting Started

1. **Fork the repository** and create your branch from `main` or the default branch.
2. Make your changes, following the guidelines below.
3. Submit a [pull request](https://github.com/ArmoredTurtle/AFC-Klipper-Add-On/pulls).

**NOTE** All pull requests should be up to date with the latest changes from the `DEV` branch and PRs should be opened against the `DEV` branch.

## Guidelines

### Code Style

- Python code should follow [PEP 8](https://peps.python.org/pep-0008/) as closely as possible.
- Use meaningful commit messages.
- Keep changes focused—avoid mixing unrelated fixes or features.

### Scripts

- Bash scripts should be POSIX-compliant where practical.
- Include a `#!/bin/bash` or `#!/usr/bin/env bash` shebang as appropriate.
- Add inline comments to explain non-obvious logic.

### Testing

- Add or update tests in `tests/` when changing logic in `extras/`.
- All tests must pass before a PR will be merged — CI runs them automatically.
- See [Running Tests](#running-tests) below for setup and usage instructions.

### Pull Requests

- Clearly describe what your PR does and why it’s useful.
- Link to any relevant issues.
- Smaller PRs are easier to review and merge—break large changes into smaller steps if you can.

## Bug Reports & Feature Requests

- Use GitHub [Issues](https://github.com/ArmoredTurtle/AFC-Klipper-Add-On/issues) to report bugs or request features.
- When reporting a bug, please include:
  - A description of the issue
  - Steps to reproduce
  - Any relevant logs or configuration
  
## Virtual environment

It is recommended to set up a python virtual environment to keep installed dependencies isolated from your system dependencies.

You can do this by running the following:

```shell
# Install virtualenv globally
pip install virtualenv

# Create local virtual environment (will create directory named ".venv" in your working directory)
python3 -m venv .venv

# Activate the virtual environment in your current terminal/shell
source .venv/bin/activate
```

## Install dependencies

```shell
pip install -r requirements.txt
```

> [!NOTE]
> Klipper aims to be dependency free. We are ONLY using dependencies here for the development environment.
> We should not use any of these dependencies in any of the code that we intend to run in klipper.

### Klipper sources

This is not strictly required, but it can be helpful for your IDE (specifically tested with VSCode) to include the klipper
sources in your `PYTHONPATH`. To do this, at this time you'll need to manually clone klipper into this project like this:

```shell
git clone --depth=1 https://github.com/Klipper3d/klipper
```

Then you'll need to ensure the `klipper/klippy` is added in the `PYTHONPATH` environment variable. The project `.vscode/settings.json`
should configure that for VSCode, but you may need to refer to your own IDE for help ensuring the extra path is added.

## Running Tests

All tests live under the `tests/` directory. There are two kinds:

| Kind | Location | Speed | Requires Klipper? |
|------|----------|-------|-------------------|
| **Unit tests** | `tests/test_AFC_*.py` | Fast (~1 s) | No |
| **Klippy integration tests** | `tests/klippy/*.test` | Slow (~5–30 s) | Yes |

### Unit tests

Unit tests exercise individual AFC Python modules using lightweight mocks — no
Klipper process, no hardware. The shared mock infrastructure lives in
`tests/conftest.py`.

After [installing dependencies](#install-dependencies), run:

```shell
python -m pytest tests/
```

To get a coverage report as well:

```shell
python -m pytest tests/ --cov=extras --cov-report=term-missing
```

> [!TIP]
> Pass `-v` for verbose output or `-k <pattern>` to run only tests whose name
> matches a pattern, e.g. `pytest -k AFC_lane`.

### Klippy integration tests

Integration tests load AFC modules inside a real Klipper process using a
simulated STM32H723 MCU (no physical hardware required). They are slower and
need a one-time setup.

#### One-time setup

**1. Clone Klipper** into the repository root (already ignored by `.gitignore`):

```shell
git clone --depth 1 https://github.com/Klipper3d/klipper.git klipper
```

**2. Install Klipper's Python dependencies** into your virtual environment:

```shell
pip install -r klipper/scripts/klippy-requirements.txt
pip install -r klipper/scripts/tests-requirements.txt
```

**3. Install the ARM cross-compiler** needed to compile the STM32H723 firmware
and generate the MCU dictionary file:

```shell
# Ubuntu / Debian
sudo apt-get install -y gcc-arm-none-eabi libnewlib-arm-none-eabi

# Arch Linux
sudo pacman -S arm-none-eabi-gcc arm-none-eabi-newlib
```

**4. Build the STM32H723 MCU dictionary:**

```shell
cp tests/klippy/stm32h723.config klipper/.config
make -C klipper olddefconfig
make -C klipper || true   # link may fail on newer GCC; the dict is generated earlier
mkdir -p tests/dict
cp klipper/out/klipper.dict tests/dict/stm32h723.dict
```

> [!NOTE]
> The link step may fail with GCC 14+ due to an LTO mismatch — this is expected.
> The `klipper.dict` file is produced before linking, so the `|| true` keeps the
> build going. The final `test` command in CI verifies the dict was produced.

#### Running all tests with the helper script

`run-tests.sh` runs unit tests and klippy integration tests against both
Klipper and Kalico in one command:

```shell
./run-tests.sh
```

Flags to run a subset:

| Flag | What runs |
|------|-----------|
| `--unit` | Unit tests only |
| `--klippy` | Klippy integration tests against both firmwares |
| `--klipper` | Klippy integration tests against Klipper only |
| `--kalico` | Klippy integration tests against Kalico only |

The script auto-builds `tests/dict/stm32h723.dict` from whichever firmware is
being tested if the dict file doesn't exist yet.

#### Running against Klipper manually

```shell
KLIPPER_PATH=$(pwd)/klipper \
DICTDIR=$(pwd)/tests/dict \
python -m pytest tests/klippy/ -v
```

#### Running against Kalico

Kalico (a Klipper fork by KalicoCrew) can be tested the same way. Clone it
alongside the existing `klipper/` directory, then point `KLIPPER_PATH` at it.

**1. Clone Kalico:**

```shell
git clone --depth 1 https://github.com/KalicoCrew/kalico.git kalico
```

**2. Install Kalico's Python dependencies:**

```shell
pip install -r kalico/scripts/klippy-requirements.txt
```

**3. Build chelper** (Kalico does not ship `build_chelper.py`; import it directly):

```shell
python kalico/klippy/chelper/__init__.py
```

**4. Build the STM32H723 MCU dictionary from Kalico:**

```shell
cp tests/klippy/stm32h723.config kalico/.config
make -C kalico olddefconfig
make -C kalico || true
mkdir -p tests/dict
cp kalico/out/klipper.dict tests/dict/stm32h723.dict
```

**5. Run the tests:**

```shell
KLIPPER_PATH=$(pwd)/kalico \
DICTDIR=$(pwd)/tests/dict \
python -m pytest tests/klippy/ -v
```

#### Running both unit and integration tests together

```shell
KLIPPER_PATH=$(pwd)/klipper \
DICTDIR=$(pwd)/tests/dict \
python -m pytest tests/ -v
```

#### How the integration tests work

Each `tests/klippy/*.test` file describes one test scenario:

```
DICTIONARY stm32h723.dict   # MCU dict to simulate against
CONFIG     afc_base.cfg     # Klipper config file (relative to the .test file)
AFC_STATUS                  # GCode lines to execute
```

The `conftest.py` in `tests/` automatically symlinks AFC's `extras/` modules
into the cloned Klipper tree at the start of the session and removes them when
the session finishes. The Klipper configs live alongside the `.test` files in
`tests/klippy/`.

## Linting

```shell
ruff check .
```

> [!TIP]
> Some lint errors may be automatically fixable. Run `ruff check --fix` to fix them.

## Updating `requirements.txt`

If you need to add additional development tools (e.g. `pip install <some-dependency>`) that are to be used
by CI/CD workflows, ensure they are added to the `requirements.txt` by running the following:

```shell
pip freeze > requirements.txt
```

> [!WARNING]
> Ensure this is only done from a virtual environment, otherwise it will add all of your globally installed
> dependencies.
