"""The matrix of unit type cases whose recorded outputs live in ``fixtures/unit_cases``.

Each case names a mode (what is being checked), a unit type and its variants. The recorded
output of every case is the expected result the Python code is replayed against by
``unit_driver.py``.
"""
from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures" / "unit_cases"

BUFFER_TYPES = ["TurtleNeck", "TurtleNeckV2", "TurtleNeck2Remastered", "FPS_PSF", "None"]

UNIT_VARIANTS: dict[str, list[dict[str, str]]] = {
    "BoxTurtle (4-Lane)": [{}],
    "BoxTurtle (8-Lane)": [{}],
    "NightOwl": [{"nightowl_board_type": b} for b in ["ERB_2.0", "OWLFC_Mini"]],
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

# The default name offered for an additional unit of each type.
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


def iter_cases():
    for itype, variants in UNIT_VARIANTS.items():
        for variant in variants:
            variant_suffix = "__".join(f"{k}-{v}" for k, v in sorted(variant.items()))
            base_name = slug(itype) + (f"__{variant_suffix}" if variant_suffix else "")

            # copy_unit_files: the default unit name and a custom one, to exercise how each
            # type renames its files.
            names = (("defaultname", "Turtle_1"), ("customname", "CustomName1"))
            for name_label, name_value in names:
                overrides = {"installation_type": itype, "boxturtle_name": name_value, **variant}
                yield f"copy_unit_files__{base_name}__{name_label}", "copy_unit_files", overrides

            # install_additional_unit: type-specific default name and a custom one.
            default_add_name = ADDITIONAL_UNIT_DEFAULT_NAME[itype]
            for name_label, name_value in (
                ("defaultname", default_add_name),
                ("customname", "CustomName2"),
            ):
                overrides = {"installation_type": itype, "boxturtle_name": name_value, **variant}
                yield (
                    f"install_additional_unit__{base_name}__{name_label}",
                    "install_additional_unit",
                    overrides,
                )

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
        yield (
            f"name_additional_unit__{slug(itype)}",
            "name_additional_unit",
            {"installation_type": itype},
        )

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
        yield (
            f"additional_default_name__BoxTurtle_4Lane__{case_label}",
            "additional_default_name",
            overrides,
        )

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
