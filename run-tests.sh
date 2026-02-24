#!/usr/bin/env bash
# run-tests.sh — run all AFC tests locally
#
# Usage:
#   ./run-tests.sh              # unit tests + klippy tests vs klipper + kalico
#   ./run-tests.sh --unit       # unit tests only
#   ./run-tests.sh --klippy     # klippy integration tests only (both firmwares)
#   ./run-tests.sh --klipper    # klippy integration tests vs klipper only
#   ./run-tests.sh --kalico     # klippy integration tests vs kalico only
#   ./run-tests.sh -k           # keep temporary files after klippy tests

set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
VENV="$ROOT/.venv"
PYTHON="$VENV/bin/python"
PYTEST="$VENV/bin/python -m pytest"
DICT_DIR="$ROOT/tests/dict"

# ── Colour helpers ─────────────────────────────────────────────────────────────
bold=$'\e[1m'; reset=$'\e[0m'
green=$'\e[32m'; red=$'\e[31m'; cyan=$'\e[36m'; yellow=$'\e[33m'

info()    { echo "${cyan}${bold}==>${reset} $*"; }
success() { echo "${green}${bold}PASS${reset} $*"; }
failure() { echo "${red}${bold}FAIL${reset} $*"; }
warn()    { echo "${yellow}${bold}WARN${reset} $*"; }

# ── Argument parsing ───────────────────────────────────────────────────────────
usage() {
    cat <<EOF
Usage: $(basename "$0") [OPTIONS]

Run AFC tests locally.

Options:
  --unit      Run unit tests only (fast, no Klipper required)
  --klippy    Run klippy integration tests only (both klipper and kalico)
  --klipper   Run klippy integration tests against klipper only
  --kalico    Run klippy integration tests against kalico only
  -k          Keep klippy temporary files (logs, gcode) after tests
  --help      Show this help message and exit

With no options, runs unit tests + klippy integration tests against both
klipper and kalico. Klippy tests are skipped if the firmware directory is
not found (see CONTRIBUTING.md for setup instructions).
EOF
}

RUN_UNIT=true
RUN_KLIPPER=true
RUN_KALICO=true
KLIPPY_EXTRA_ARGS=()

for arg in "$@"; do
    case "$arg" in
        --unit)    RUN_UNIT=true;  RUN_KLIPPER=false; RUN_KALICO=false ;;
        --klippy)  RUN_UNIT=false; RUN_KLIPPER=true;  RUN_KALICO=true  ;;
        --klipper) RUN_UNIT=false; RUN_KLIPPER=true;  RUN_KALICO=false ;;
        --kalico)  RUN_UNIT=false; RUN_KLIPPER=false; RUN_KALICO=true  ;;
        -k)        KLIPPY_EXTRA_ARGS+=("--klippy-keep") ;;
        --help)    usage; exit 0 ;;
        *) echo "Unknown option: $arg"; echo; usage; exit 1 ;;
    esac
done

# ── Preflight ──────────────────────────────────────────────────────────────────
if [[ ! -x "$PYTHON" ]]; then
    echo "Virtual environment not found at $VENV"
    echo "Run: python -m venv .venv && .venv/bin/pip install -r requirements.txt"
    exit 1
fi

PASS=()
FAIL=()

# ── Unit tests ─────────────────────────────────────────────────────────────────
if $RUN_UNIT; then
    info "Running unit tests..."
    if $PYTEST tests/ --ignore=tests/klippy -q; then
        success "Unit tests"
        PASS+=("unit")
    else
        failure "Unit tests"
        FAIL+=("unit")
    fi
fi

# ── Klippy integration test runner ────────────────────────────────────────────
run_klippy_tests() {
    local name="$1"          # klipper | kalico
    local fw_dir="$ROOT/$name"

    info "Running klippy integration tests against ${bold}$name${reset}..."

    if [[ ! -d "$fw_dir" ]]; then
        warn "$name not found at $fw_dir — skipping. See CONTRIBUTING.md for setup."
        return
    fi

    if [[ ! -f "$DICT_DIR/stm32h723.dict" ]]; then
        warn "tests/dict/stm32h723.dict not found — building from $name..."
        cp "$ROOT/tests/klippy/stm32h723.config" "$fw_dir/.config"
        make -C "$fw_dir" olddefconfig
        make -C "$fw_dir" || true
        if [[ ! -f "$fw_dir/out/klipper.dict" ]]; then
            failure "Dict not generated from $name — skipping klippy tests for $name."
            FAIL+=("klippy-$name")
            return
        fi
        mkdir -p "$DICT_DIR"
        cp "$fw_dir/out/klipper.dict" "$DICT_DIR/stm32h723.dict"
    fi

    if KLIPPER_PATH="$fw_dir" DICTDIR="$DICT_DIR" \
        $PYTEST tests/klippy/ -v --junitxml="klippy-test-results-$name.xml" \
        "${KLIPPY_EXTRA_ARGS[@]+"${KLIPPY_EXTRA_ARGS[@]}"}"; then
        success "Klippy tests ($name)"
        PASS+=("klippy-$name")
    else
        failure "Klippy tests ($name)"
        FAIL+=("klippy-$name")
    fi
}

$RUN_KLIPPER && run_klippy_tests klipper
$RUN_KALICO  && run_klippy_tests kalico

# ── Summary ───────────────────────────────────────────────────────────────────
echo
echo "${bold}Results${reset}"
for t in "${PASS[@]+"${PASS[@]}"}"; do echo "  ${green}✓${reset} $t"; done
for t in "${FAIL[@]+"${FAIL[@]}"}"; do echo "  ${red}✗${reset} $t"; done

[[ ${#FAIL[@]} -eq 0 ]]
