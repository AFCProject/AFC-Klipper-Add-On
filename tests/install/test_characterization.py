"""Characterization tests for the AFC installer's unit-handling bash functions.

These tests do NOT assert anything about "correct" behavior. They run the
installer's per-unit-type functions (copy_unit_files, install_additional_unit,
get_unit_buffer_target, build_install_message, name_additional_unit) through
tests/install/run_case.sh for a matrix of unit types/board/motor/buffer
variants, and diff the output against committed fixtures in
tests/install/fixtures/.

If a fixture ever needs to change, that must be a deliberate, reviewed behavior
change -- not a side effect of refactoring -- regenerate it with:

    python3 tests/install/test_characterization.py --regenerate
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
RUN_CASE = Path(__file__).resolve().parent / "run_case.sh"
FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"

BUFFER_TYPES = ["TurtleNeck", "TurtleNeckV2", "FPS_PSF", "None"]

UNIT_VARIANTS: dict[str, list[dict[str, str]]] = {
    "BoxTurtle (4-Lane)": [{}],
    "BoxTurtle (8-Lane)": [{}],
    "NightOwl": [{}],
    "ViViD": [{}],
    "OpenAMS": [{}],
    "HTLF": [{"htlf_board_type": b} for b in ["ERB", "MMB_1.1", "MMB_1.0"]],
    "Claymore": [{"htlf2_board_type": "AFC_Lite"}],
    "QuattroBox": [
        {"qb_motor_type": m, "qb_board_type": b}
        for m in ["NEMA_17", "NEMA_14"]
        for b in ["MMB_1.0", "MMB_1.1", "MMB_2.0"]
    ],
    "EMU": [
        {"emu_num_lanes": str(n), "emu_board_type": bt}
        for n in (1, 3, 8)
        for bt in ("EBB", "SLB")
    ],
}

# The name_additional_unit() default per type (mirrors unit_functions.sh).
ADDITIONAL_UNIT_DEFAULT_NAME = {
    "BoxTurtle (4-Lane)": "Turtle_2",
    "BoxTurtle (8-Lane)": "Turtle_2",
    "NightOwl": "NightOwl_1",
    "ViViD": "Vivid_1",
    "QuattroBox": "QuattroBox_1",
    "OpenAMS": "AMS_1",
    "EMU": "EMU_1",
    "HTLF": "HTLF_1",
    "Claymore": "Claymore_1",
}


def slug(s: str) -> str:
    return (
        s.replace(" ", "_")
        .replace("(", "")
        .replace(")", "")
        .replace("-", "")
        .replace(".", "")
    )


def run_case(mode: str, env_overrides: dict[str, str]) -> str:
    env = os.environ.copy()
    env["MODE"] = mode
    for k, v in env_overrides.items():
        env[f"CASE_{k}"] = v
    result = subprocess.run(
        ["bash", str(RUN_CASE)],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"run_case.sh failed (mode={mode}, overrides={env_overrides}):\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
    return result.stdout


def iter_cases():
    for itype, variants in UNIT_VARIANTS.items():
        for variant in variants:
            variant_suffix = "__".join(f"{k}-{v}" for k, v in sorted(variant.items()))
            base_name = slug(itype) + (f"__{variant_suffix}" if variant_suffix else "")

            # copy_unit_files: default boxturtle_name (as constants.sh sets it)
            # and a custom one, to exercise the per-type renaming quirks.
            for name_label, name_value in (("defaultname", "Turtle_1"), ("customname", "CustomName1")):
                overrides = {"installation_type": itype, "boxturtle_name": name_value, **variant}
                yield f"copy_unit_files__{base_name}__{name_label}", "copy_unit_files", overrides

            # install_additional_unit: type-specific default name and a custom one.
            default_add_name = ADDITIONAL_UNIT_DEFAULT_NAME[itype]
            for name_label, name_value in (
                ("defaultname", default_add_name),
                ("customname", "CustomName2"),
            ):
                overrides = {"installation_type": itype, "boxturtle_name": name_value, **variant}
                yield f"install_additional_unit__{base_name}__{name_label}", "install_additional_unit", overrides

            # get_unit_buffer_target: doesn't depend on buffer_type, one case per variant.
            overrides = {"installation_type": itype, "boxturtle_name": "Turtle_1", **variant}
            yield f"buffer_target__{base_name}", "buffer_target", overrides

            # get_unit_buffer_target for an *additional* unit (is_additional_unit
            # set). NightOwl/HTLF/QuattroBox/OpenAMS used to hardcode a "_1"
            # target here that only matched the first unit -- a second unit's
            # buffer would silently target (or collide with) the first one's
            # file/section. BoxTurtle/Claymore/EMU already derived the target
            # from $boxturtle_name and are included for symmetry/regression
            # coverage.
            overrides = {
                "installation_type": itype,
                "boxturtle_name": "CustomName2",
                "is_additional_unit": "True",
                **variant,
            }
            yield f"buffer_target__{base_name}__additional", "buffer_target", overrides

            # install_menu.sh's type-specific option rows: doesn't depend on
            # turtle_renamed (that only matters for the additional-unit menu).
            overrides = {"installation_type": itype, "boxturtle_name": "Foo_1", **variant}
            yield f"install_menu_options__{base_name}", "install_menu_options", overrides

            # additional_system_menu.sh's name + option row(s): depends on
            # turtle_renamed (whether a prior rename sticks) and can mutate
            # boxturtle_name as a side effect (see e.g. NightOwl/Claymore/EMU).
            for renamed in ("True", "False"):
                overrides = {
                    "installation_type": itype,
                    "boxturtle_name": "Foo_1",
                    "turtle_renamed": renamed,
                    **variant,
                }
                yield (
                    f"additional_menu_row__{base_name}__renamed-{renamed}",
                    "additional_menu_row",
                    overrides,
                )

            # build_install_message: depends on buffer_type too.
            for buffer_type in BUFFER_TYPES:
                overrides = {
                    "installation_type": itype,
                    "boxturtle_name": "Turtle_1",
                    "buffer_type": buffer_type,
                    **variant,
                }
                yield f"message__{base_name}__buffer-{buffer_type}", "message", overrides

        # name_additional_unit: only depends on installation_type.
        yield f"name_additional_unit__{slug(itype)}", "name_additional_unit", {"installation_type": itype}

    # unit_additional_default_name: the "add additional unit" menu's default
    # name is the lowest free "<prefix>_N" for the type, based on what's
    # already in $afc_config_dir -- not a hardcoded "_2" that assumes a
    # first unit of that type already exists (the reported bug: a first
    # BoxTurtle added via this menu, e.g. after installing a Claymore first,
    # used to default to "Turtle_2" even though no BoxTurtle existed yet).
    default_name_cases = [
        ("none-existing", []),
        ("first-existing", ["AFC_Turtle_1.cfg"]),
        ("first-and-second-existing", ["AFC_Turtle_1.cfg", "AFC_Turtle_2.cfg"]),
        ("gap-only-second-existing", ["AFC_Turtle_2.cfg"]),
    ]
    for case_label, seed_files in default_name_cases:
        overrides = {
            "installation_type": "BoxTurtle (4-Lane)",
            "seed_files": ",".join(seed_files),
        }
        yield f"additional_default_name__BoxTurtle_4Lane__{case_label}", "additional_default_name", overrides

    # HTLF's filename also carries its (normalized) board type --
    # AFC_<board>_<name>.cfg, e.g. AFC_ERB_HTLF_1.cfg -- unlike every other
    # type's plain AFC_<prefix>_<n>.cfg. Probing the generic pattern here
    # would never find an existing HTLF unit and would keep offering
    # "HTLF_1" as the default even when it's already taken.
    yield (
        "additional_default_name__HTLF__board-ERB__first-existing",
        "additional_default_name",
        {
            "installation_type": "HTLF",
            "htlf_board_type": "ERB",
            "seed_files": "AFC_ERB_HTLF_1.cfg",
        },
    )
    # MMB_1.0/MMB_1.1 both normalize to the "MMB" filename segment (see
    # htlf_normalize_board_type) -- confirm the probe uses that normalized
    # form, not the raw htlf_board_type value.
    yield (
        "additional_default_name__HTLF__board-MMB_1.0__first-existing",
        "additional_default_name",
        {
            "installation_type": "HTLF",
            "htlf_board_type": "MMB_1.0",
            "seed_files": "AFC_MMB_HTLF_1.cfg",
        },
    )


def all_cases():
    return list(iter_cases())


def fixture_path(case_name: str) -> Path:
    return FIXTURES_DIR / f"{case_name}.txt"


def regenerate():
    FIXTURES_DIR.mkdir(parents=True, exist_ok=True)
    for case_name, mode, overrides in all_cases():
        output = run_case(mode, overrides)
        fixture_path(case_name).write_text(output)
        print(f"wrote {case_name}")


def _pytest_cases():
    return [(name, mode, overrides) for name, mode, overrides in all_cases()]


try:
    import pytest

    @pytest.mark.parametrize(
        "case_name,mode,overrides",
        _pytest_cases(),
        ids=[c[0] for c in _pytest_cases()],
    )
    def test_characterization(case_name, mode, overrides):
        expected_path = fixture_path(case_name)
        assert expected_path.exists(), f"missing fixture {expected_path}; run with --regenerate first"
        expected = expected_path.read_text()
        actual = run_case(mode, overrides)
        assert actual == expected

    def test_installation_options_matches_registry_order():
        # installation_options is now derived from the registry's
        # UNIT_TYPE_ORDER rather than a literal array in constants.sh --
        # pin its content and order so that derivation can't silently drift.
        actual = run_case("installation_options", {}).splitlines()
        assert actual == [
            "BoxTurtle (4-Lane)",
            "BoxTurtle (8-Lane)",
            "NightOwl",
            "HTLF",
            "Claymore",
            "QuattroBox",
            "OpenAMS",
            "ViViD",
            "EMU",
        ]

    def test_no_adapter_dispatchers_survive_under_set_e():
        # install-afc.sh runs under `set -e` and calls each unit_* dispatcher
        # (and print_unit_art) as a bare statement. A type with no registered
        # adapter must make the dispatcher return 0, the same way an
        # unmatched branch in the old if/elif chains fell through with
        # nothing to do -- otherwise the whole script dies mid-menu the
        # first time someone adds a type that's missing one adapter (see
        # registry.sh's "how to add a new unit type" steps 3-4).
        actual = run_case("no_adapter_survival", {})
        assert actual == "still alive\n"

    def test_openams_offers_additional_unit_buffer():
        # CodeRabbit flagged that OpenAMS was left out of
        # UNIT_ADDITIONAL_BUFFER_SAFE despite having a working
        # unit_buffer_target_OpenAMS and FPS_PSF pin handling in
        # apply_unit_buffer -- confirm it's supported, and that its cycle
        # is restricted to None/FPS_PSF (it has no TurtleNeck/TurtleNeckV2
        # hardware path, unlike every other supported type).
        actual = run_case("additional_buffer_options", {"installation_type": "OpenAMS"})
        assert actual == (
            "=== VARS ===\n"
            "supported=True\n"
            "options=None FPS_PSF\n"
            "default=FPS_PSF\n"
        )

    def test_generic_type_offers_full_additional_unit_buffer_cycle():
        actual = run_case(
            "additional_buffer_options", {"installation_type": "BoxTurtle (4-Lane)"}
        )
        assert actual == (
            "=== VARS ===\n"
            "supported=True\n"
            "options=None TurtleNeck TurtleNeckV2 FPS_PSF\n"
            "default=TurtleNeck\n"
        )

    def test_vivid_has_no_additional_unit_buffer():
        # ViViD has no unit_buffer_target_ViViD adapter at all.
        actual = run_case("additional_buffer_options", {"installation_type": "ViViD"})
        assert actual.splitlines()[1] == "supported=False"

    def test_additional_buffer_forced_none_does_not_survive_unsupported_type():
        # CodeRabbit: cycling "T" through an unsupported type (e.g. ViViD)
        # forces additional_buffer_type to "None". That forced "None" used
        # to be indistinguishable from an intentional shared-buffer choice,
        # so cycling on to the next *supported* type kept "None" instead of
        # resetting to that type's own-buffer default.
        actual = run_case(
            "additional_buffer_transition",
            {
                "old_installation_type": "ViViD",
                "installation_type": "HTLF",
                "current_buffer_type": "None",
            },
        )
        assert actual == "=== VARS ===\nresult=TurtleNeck\n"

    def test_additional_buffer_none_survives_between_supported_types():
        # An explicit "None" (shared buffer) chosen on a *supported* type
        # must survive switching to another supported type -- only a
        # forced "None" from an unsupported type gets overwritten.
        actual = run_case(
            "additional_buffer_transition",
            {
                "old_installation_type": "BoxTurtle (4-Lane)",
                "installation_type": "HTLF",
                "current_buffer_type": "None",
            },
        )
        assert actual == "=== VARS ===\nresult=None\n"

    def test_additional_buffer_resets_for_unsupported_type():
        actual = run_case(
            "additional_buffer_transition",
            {
                "old_installation_type": "BoxTurtle (4-Lane)",
                "installation_type": "ViViD",
                "current_buffer_type": "TurtleNeck",
            },
        )
        assert actual == "=== VARS ===\nresult=None\n"

    def test_additional_buffer_resets_when_invalid_for_new_type():
        # TurtleNeck isn't in OpenAMS's cycle (None/FPS_PSF only) -- must
        # fall back to OpenAMS's own default, not carry TurtleNeck over.
        actual = run_case(
            "additional_buffer_transition",
            {
                "old_installation_type": "BoxTurtle (4-Lane)",
                "installation_type": "OpenAMS",
                "current_buffer_type": "TurtleNeck",
            },
        )
        assert actual == "=== VARS ===\nresult=FPS_PSF\n"

except ImportError:
    pass


if __name__ == "__main__":
    if "--regenerate" in sys.argv:
        regenerate()
    else:
        print("Use --regenerate to (re)generate fixtures, or run under pytest to compare.")
