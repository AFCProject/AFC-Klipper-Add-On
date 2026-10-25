from pathlib import Path
from typing import List

import pytest

from installer.unit_registry.registry import UNIT_TYPES
from tests.installer.e2e.harness import Case, Result, preinstall, run_case

TYPES = [unit.display_name for unit in UNIT_TYPES]
CONFIG = "printer_data/config"
# A first unit that ramms with its own TurtleNeck buffer (the buffer row is number 11)
FIRST_UNIT = ["I", "1", "11", "I", "", "", "Q"]
SENSOR_UNIT = ["I", "1", "10", "I", "Q"]
MACRO_COUNT = len(list((Path(__file__).resolve().parents[3] / "config" / "macros").glob("*.cfg")))


def update(tmp_path: Path, answers: List[str], **kwargs: object) -> Result:
    install = FIRST_UNIT if kwargs.get("git", True) else ["", *FIRST_UNIT]
    case = Case(
        program="update-afc.sh", args=kwargs.pop("args", ["-t"]), answers=answers,  # type: ignore
        prepare=preinstall(install), **kwargs,  # type: ignore[arg-type]
    )
    return run_case(tmp_path, case)


class TestUpdate:
    def test_declining_the_macro_refresh(self, tmp_path: Path) -> None:
        result = update(tmp_path, ["U", "n", "Q"])
        assert result.code == 0, result.stdout
        assert "AFC Klipper Add-On updated successfully with version v" in result.stdout
        assert "Starting update process..." in result.stdout

    def test_agreeing_but_not_confirming_changes_nothing(self, tmp_path: Path) -> None:
        result = update(tmp_path, ["U", "y", "n", "Q"])
        assert "Macros" not in result.stdout.split("Starting update")[-1].split("updated")[0]

    @pytest.mark.parametrize(
        "choice,needle",
        [
            ("o", f"AFC Macros updated successfully ({MACRO_COUNT} copied, 0 skipped)."),
            ("s", f"AFC Macros update skipped (all {MACRO_COUNT} files kept as-is)."),
            ("b", f"AFC Macros updated successfully ({MACRO_COUNT} copied, 0 skipped)."),
        ],
    )
    def test_refreshing_every_macro_file(
        self, tmp_path: Path, choice: str, needle: str
    ) -> None:
        result = update(tmp_path, ["U", "y", "y", *[choice] * MACRO_COUNT, "Q"])
        assert needle in result.stdout

    def test_backing_up_keeps_the_old_file_next_to_the_new_one(self, tmp_path: Path) -> None:
        result = update(tmp_path, ["U", "y", "y", *["b"] * MACRO_COUNT, "Q"])
        assert [n for n in result.tree if ".bak.<DATE>" in n]

    def test_old_t_macros_and_velocity_settings_are_removed(self, tmp_path: Path) -> None:
        def prepare(sb) -> None:  # type: ignore[no-untyped-def]
            preinstall(FIRST_UNIT)(sb)
            cfg = sb.home / "printer_data" / "config" / "AFC"
            with open(cfg / "macros" / "AFC_macros.cfg", "a") as handle:
                handle.write("\n[gcode_macro T0]\ngcode:\n  M117 zero\n\n[gcode_macro KEEP_ME]\n"
                             "gcode:\n  M117 k\n")
            (cfg / "AFC_buffer_extra.cfg").write_text(
                "[AFC_buffer B9]\nvelocity: 1\nadvance_pin: x\n"
            )

        case = Case(
            program="update-afc.sh", args=["-t"], answers=["U", "n", "Q"], prepare=prepare,
        )
        result = run_case(tmp_path, case)
        assert "[gcode_macro T0]" not in result.tree[f"{CONFIG}/AFC/macros/AFC_macros.cfg"]
        assert "[gcode_macro KEEP_ME]" in result.tree[f"{CONFIG}/AFC/macros/AFC_macros.cfg"]
        assert "velocity" not in result.tree[f"{CONFIG}/AFC/AFC_buffer_extra.cfg"]
        assert "Removed deprecated velocity setting" in result.stdout

    def test_an_install_without_a_buffer_section_still_updates(self, tmp_path: Path) -> None:
        case = Case(
            program="update-afc.sh", args=["-t"], answers=["U", "n", "Q"],
            prepare=preinstall(SENSOR_UNIT),
        )
        result = run_case(tmp_path, case)
        assert result.code == 0
        assert "updated successfully" in result.stdout

    def test_without_test_mode_it_syncs_and_restarts(self, tmp_path: Path) -> None:
        result = update(tmp_path, ["U", "n", "Q"], args=[], use_server=True)
        assert result.code == 0
        assert any("fetch --prune --quiet" in line for line in result.log)
        assert "sudo systemctl restart klipper" in result.log

    def test_a_pending_upstream_change_is_pulled_first(self, tmp_path: Path) -> None:
        result = update(
            tmp_path, ["U", "n", "Q"], args=[], use_server=True,
            env={"AFC_STUB_BEHIND": " [behind 1]"},
        )
        assert any(line.endswith("pull --rebase --quiet") for line in result.log)

    def test_a_zip_install_warns_then_updates(self, tmp_path: Path) -> None:
        result = update(tmp_path, ["U", "", "n", "Q"], git=False)
        assert "Git installation not detected." in result.stdout
        assert "AFC Klipper Add-On update process completed." in result.stdout

    def test_invalid_keys_and_going_back_to_the_main_menu(self, tmp_path: Path) -> None:
        result = update(tmp_path, ["x", "", "M", "Q"])
        assert result.code == 0
        assert "Install Additional System" in result.stdout

    def test_end_of_input_exits_one(self, tmp_path: Path) -> None:
        assert update(tmp_path, ["U"]).code == 1

    def test_help_and_a_bad_flag(self, tmp_path: Path) -> None:
        assert run_case(tmp_path / "a", Case(program="update-afc.sh", args=["-h"])).code == 0
        bad = run_case(tmp_path / "b", Case(program="update-afc.sh", args=["-x"]))
        assert (bad.code, bad.stderr) == (1, "./update-afc.sh: illegal option -- x\n")


def additional(tmp_path: Path, keys: List[str], **kwargs: object) -> Result:
    case = Case(
        args=["-t"], answers=["A", *keys, "Q"], prepare=preinstall(FIRST_UNIT),
        **kwargs,  # type: ignore[arg-type]
    )
    return run_case(tmp_path, case)


class TestAdditionalUnit:
    @pytest.mark.parametrize("index", range(len(TYPES)), ids=TYPES)
    def test_each_unit_type_adds_a_unit_file(self, tmp_path: Path, index: int) -> None:
        before = run_case(
            tmp_path / "before", Case(args=["-t"], answers=["Q"], prepare=preinstall(FIRST_UNIT))
        )
        result = additional(tmp_path / "after", [str(index + 1), "I", "", "", ""])
        assert result.code == 0, result.stdout
        added = sorted(set(result.tree) - set(before.tree))
        assert [n for n in added if n.startswith(f"{CONFIG}/AFC/")], added
        assert "created successfully" in result.stdout

    def test_a_custom_name_names_the_new_unit(self, tmp_path: Path) -> None:
        result = additional(tmp_path, ["1", "1", "Second_1", "I", "", ""])
        assert f"{CONFIG}/AFC/AFC_Second_1.cfg" in result.tree

    def test_a_name_already_in_use_is_rejected(self, tmp_path: Path) -> None:
        result = additional(tmp_path, ["1", "1", "Turtle_1", "I"])
        assert "Unit Turtle_1 already exists, please enter a unique unit name." in result.stdout
        assert f"{CONFIG}/AFC/AFC_Turtle_1.cfg" in result.tree

    def test_a_shared_buffer_points_the_new_unit_at_an_existing_one(self, tmp_path: Path) -> None:
        # option 2 is "own buffer?": None is four presses away from TurtleNeck
        result = additional(tmp_path, ["1", "2", "2", "2", "2", "I", "1"])
        assert "is set to share the existing buffer" in result.stdout

    def test_without_a_first_unit_it_refuses(self, tmp_path: Path) -> None:
        result = run_case(tmp_path, Case(args=["-t"], answers=["A", "", "Q"]))
        assert "No existing unit installation found" in result.stdout
        assert result.code == 0

    def test_end_of_input_at_the_refusal_exits_one(self, tmp_path: Path) -> None:
        assert run_case(tmp_path, Case(args=["-t"], answers=["A"])).code == 1


class TestUtilities:
    def with_var_files(self, sb) -> None:  # type: ignore[no-untyped-def]
        preinstall(SENSOR_UNIT)(sb)
        cfg = sb.home / "printer_data" / "config" / "AFC"
        (cfg / "AFC.var.unit").write_text("{}\n")
        (cfg / "AFC.var.tool").write_text("{}\n")

    @pytest.mark.parametrize(
        "confirm,removed", [("y", True), ("Y", True), ("n", False), ("", False)]
    )
    def test_removing_the_var_file(self, tmp_path: Path, confirm: str, removed: bool) -> None:
        case = Case(args=["-t"], answers=["C", "1", confirm, "Q"], prepare=self.with_var_files)
        result = run_case(tmp_path, case)
        assert (f"{CONFIG}/AFC/AFC.var.unit" not in result.tree) is removed

    def test_exiting_removes_the_tool_var_file(self, tmp_path: Path) -> None:
        case = Case(args=["-t"], answers=["Q"], prepare=self.with_var_files)
        result = run_case(tmp_path, case)
        assert f"{CONFIG}/AFC/AFC.var.tool" not in result.tree

    def test_renaming_a_unit_renames_it_in_the_files(self, tmp_path: Path) -> None:
        case = Case(
            args=["-t"], answers=["C", "R", "Turtle_1", "Renamed_1", "Q"],
            prepare=preinstall(SENSOR_UNIT),
        )
        result = run_case(tmp_path, case)
        text = "\n".join(t for n, t in result.tree.items() if n.startswith(CONFIG))
        assert "Renamed_1" in text and "Turtle_1" not in text
        assert "Renaming unit Turtle_1 to Renamed_1" in result.stdout

    def test_renaming_an_unknown_unit_says_so(self, tmp_path: Path) -> None:
        case = Case(
            args=["-t"], answers=["C", "R", "Nope", "Whatever", "Q"],
            prepare=preinstall(SENSOR_UNIT),
        )
        result = run_case(tmp_path, case)
        assert "Unit Nope not found, please enter a valid unit name." in result.stdout

    def test_end_of_input_exits_one(self, tmp_path: Path) -> None:
        assert run_case(tmp_path, Case(args=["-t"], answers=["C"])).code == 1


class TestUninstall:
    def test_removing_an_installed_system_backs_up_the_config(self, tmp_path: Path) -> None:
        case = Case(args=["-t"], answers=["R", "Q"], prepare=preinstall(SENSOR_UNIT))
        result = run_case(tmp_path, case)
        assert result.code == 0
        assert not [n for n in result.tree if n.startswith(f"{CONFIG}/AFC/")]
        assert [n for n in result.tree if n.startswith(f"{CONFIG}/AFC.backup.<DATE>/")]
        assert "[include AFC/*.cfg]" not in result.tree[f"{CONFIG}/printer.cfg"]
        assert not [n for n in result.tree if n.startswith("klipper/klippy/extras/AFC")]

    def test_removing_restarts_klipper_when_idle(self, tmp_path: Path) -> None:
        case = Case(
            answers=["R", "Q"], prepare=preinstall(SENSOR_UNIT), use_server=True,
        )
        assert "sudo systemctl restart klipper" in run_case(tmp_path, case).log

    def test_removing_does_not_restart_while_printing(self, tmp_path: Path) -> None:
        case = Case(
            answers=["R", "Q"], prepare=preinstall(SENSOR_UNIT), use_server=True, idle=False,
        )
        assert "sudo systemctl restart klipper" not in run_case(tmp_path, case).log

    def test_removing_when_nothing_is_installed_still_exits_cleanly(self, tmp_path: Path) -> None:
        assert run_case(tmp_path, Case(args=["-t"], answers=["R", "Q"])).code == 0
