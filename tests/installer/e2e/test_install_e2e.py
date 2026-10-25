from pathlib import Path
from typing import List

import pytest

from installer.unit_registry.registry import UNIT_TYPES
from tests.installer.e2e.harness import Case, Result, run_case

TYPES = [unit.display_name for unit in UNIT_TYPES]
CONFIG = "printer_data/config"
SENSOR_ROW = "10"
BUFFER_ROW = "11"


def install_keys(index: int, *presses: str, finish: bool = True) -> List[str]:
    """Main menu to the picker, the unit type at ``index`` (0 based), the presses, install, quit."""
    keys = ["I", str(index + 1), *presses]
    return keys + (["I", "Q"] if finish else [])


def install(tmp_path: Path, index: int, *presses: str, answers: List[str] = (),  # type: ignore
            **kwargs: object) -> Result:
    keys = ["I", str(index + 1), *presses, "I", *answers, "Q"]
    case = Case(args=kwargs.pop("args", ["-t"]), answers=keys, **kwargs)  # type: ignore
    return run_case(tmp_path, case)


def tree_text(result: Result, relative: str) -> str:
    return result.tree[f"{CONFIG}/{relative}"]


class TestFirstInstallPerUnitType:
    @pytest.mark.parametrize("index", range(len(TYPES)), ids=TYPES)
    def test_with_a_toolhead_sensor(self, tmp_path: Path, index: int) -> None:
        result = install(tmp_path, index, SENSOR_ROW)
        assert result.code == 0, result.stdout + result.stderr
        names = [name.split("/")[-1] for name in result.tree if name.startswith(CONFIG)]
        assert "AFC.cfg" in names and "AFC_Hardware.cfg" in names
        assert any(n.startswith("AFC_") and n not in ("AFC_Hardware.cfg",) for n in names)
        assert "[include AFC/*.cfg]" in tree_text(result, "printer.cfg")
        assert "klipper/klippy/extras/AFC.py" in result.tree
        assert result.tree["klipper/klippy/extras/AFC.py"].startswith("-> ")

    @pytest.mark.parametrize("index", range(len(TYPES)), ids=TYPES)
    def test_the_screens_lead_to_the_install_and_a_clean_exit(
        self, tmp_path: Path, index: int
    ) -> None:
        result = install(tmp_path, index, SENSOR_ROW)
        assert "Starting installation process.." in result.stdout
        assert "Test mode enabled; skipping Klipper restart." in result.stdout
        assert result.stderr == ""


class TestOptions:
    def test_the_toggles_change_what_is_written(self, tmp_path: Path) -> None:
        default = install(tmp_path / "a", 0, SENSOR_ROW)
        flipped = install(tmp_path / "b", 0, *[str(n) for n in range(1, 10)], SENSOR_ROW)
        assert default.tree != flipped.tree

    def test_each_toggle_alone_changes_the_result_except_the_hub_cutter(
        self, tmp_path: Path
    ) -> None:
        default = install(tmp_path / "base", 0, SENSOR_ROW)
        changed = [
            number for number in range(1, 10)
            if install(tmp_path / f"t{number}", 0, str(number), SENSOR_ROW).tree != default.tree
        ]
        assert changed == [1, 2, 3, 5, 6, 7, 8, 9]

    def test_the_tip_cut_servo_example_is_only_in_the_hardware_file_when_asked_for(
        self, tmp_path: Path
    ) -> None:
        for index in (0, TYPES.index("HTLF"), TYPES.index("NightOwl"), TYPES.index("QuattroBox")):
            off = install(tmp_path / f"off{index}", index, SENSOR_ROW)
            on = install(tmp_path / f"on{index}", index, "9", SENSOR_ROW)
            assert "Tip Cut Servo" not in tree_text(off, "AFC/AFC_Hardware.cfg")
            assert "######### Tip Cut Servo" in tree_text(on, "AFC/AFC_Hardware.cfg")

    def test_a_sensor_pin_ends_up_in_the_config(self, tmp_path: Path) -> None:
        result = install(tmp_path, 0, SENSOR_ROW, "13", "nhk:gpio13")
        assert any("nhk:gpio13" in text for name, text in result.tree.items()
                   if name.startswith(CONFIG))

    def test_a_custom_unit_name_names_the_unit_file(self, tmp_path: Path) -> None:
        result = install(tmp_path, 0, SENSOR_ROW, "12", "MyUnit")
        assert f"{CONFIG}/AFC/AFC_MyUnit.cfg" in result.tree
        assert f"{CONFIG}/AFC/AFC_Turtle_1.cfg" not in result.tree

    def test_a_board_choice_changes_the_hardware_file(self, tmp_path: Path) -> None:
        index = TYPES.index("HTLF")
        default = install(tmp_path / "a", index, SENSOR_ROW)
        other = install(tmp_path / "b", index, SENSOR_ROW, "13")
        assert default.tree != other.tree

    @pytest.mark.parametrize(
        "presses,answers,needles",
        [
            (1, ["adv:1", "trail:2"], ["adv:1", "trail:2"]),
            (2, [], ["TurtleNeckv2"]),
            (3, [], ["TN2.cfg", "TN2R:PSF"]),
            (4, ["buf:PA1"], ["buf:PA1"]),
        ],
        ids=["turtleneck", "turtleneck-v2", "turtleneck-2-remastered", "fps-psf"],
    )
    def test_ramming_with_each_buffer_type(
        self, tmp_path: Path, presses: int, answers: List[str], needles: List[str]
    ) -> None:
        result = install(tmp_path, 0, *[BUFFER_ROW] * presses, answers=answers)
        assert result.code == 0, result.stdout
        everything = "\n".join(t for n, t in result.tree.items() if n.startswith(CONFIG))
        for needle in needles:
            assert needle in everything
        assert "[AFC_buffer" in everything

    def test_ramming_without_a_buffer_is_refused_with_nothing_written(
        self, tmp_path: Path
    ) -> None:
        case = Case(args=["-t"], answers=["I", "1", "I", "Q"])
        result = run_case(tmp_path, case)
        assert "Ramming requires a buffer type" in result.stdout
        assert not [n for n in result.tree if n.startswith(f"{CONFIG}/AFC")]


class TestEnvironment:
    def test_a_real_run_restarts_moonraker_and_klipper_when_idle(self, tmp_path: Path) -> None:
        result = install(tmp_path, 0, SENSOR_ROW, args=[], use_server=True)
        assert result.code == 0
        assert "sudo systemctl restart moonraker" in result.log
        assert "sudo systemctl restart klipper" in result.log

    def test_a_real_run_skips_the_klipper_restart_while_printing(self, tmp_path: Path) -> None:
        result = install(tmp_path, 0, SENSOR_ROW, args=[], use_server=True, idle=False)
        assert "sudo systemctl restart klipper" not in result.log
        assert "not idle/ready" in result.stdout

    def test_a_real_run_adds_the_extras_to_the_klipper_exclude_list(self, tmp_path: Path) -> None:
        result = install(tmp_path, 0, SENSOR_ROW, args=[], use_server=True)
        assert "klippy/extras/AFC.py" in result.tree["klipper/.git/info/exclude"]

    def test_quitting_without_installing_does_not_restart(self, tmp_path: Path) -> None:
        result = run_case(tmp_path, Case(answers=["Q"], use_server=True))
        assert result.code == 0
        assert "sudo systemctl restart klipper" not in result.log

    def test_a_custom_printer_config_dir_and_moonraker_config(self, tmp_path: Path) -> None:
        def prepare(sb) -> None:  # type: ignore[no-untyped-def]
            other = sb.home / "elsewhere"
            other.mkdir()
            (other / "printer.cfg").write_text("[printer]\n")
            (other / "moonraker.conf").write_text("[server]\n")

        case = Case(
            args=["-t", "-p", "{home}/elsewhere"], prepare=prepare,
            answers=["I", "1", SENSOR_ROW, "I", "Q"],
        )
        result = run_case(tmp_path, case)
        assert result.code == 0
        assert (result.sandbox.home / "elsewhere" / "AFC" / "AFC.cfg").is_file()
        printer_cfg = (result.sandbox.home / "elsewhere" / "printer.cfg").read_text()
        assert "[include AFC/*.cfg]" in printer_cfg

    def test_existing_config_files_ask_before_being_replaced(self, tmp_path: Path) -> None:
        for answer, kept in (("o", False), ("s", True)):
            result = install(
                tmp_path / answer, 0, SENSOR_ROW, answers=[answer],
                files={f"{CONFIG}/AFC/AFC.cfg": "[AFC]\nOLD_MARKER: 1\n"},
            )
            assert ("OLD_MARKER" in tree_text(result, "AFC/AFC.cfg")) is kept, answer

    def test_printer_cfg_that_already_includes_afc_is_not_changed(self, tmp_path: Path) -> None:
        text = "[include AFC/*.cfg]\n[printer]\nkinematics: corexy\n"
        result = install(tmp_path, 0, SENSOR_ROW, printer_cfg=text)
        assert tree_text(result, "printer.cfg") == text


class TestZipAndGit:
    def test_a_zip_install_asks_for_enter_and_skips_the_exclude_list(self, tmp_path: Path) -> None:
        case = Case(
            args=["-t"], git=False, answers=["", "I", "1", SENSOR_ROW, "I", "Q"],
        )
        result = run_case(tmp_path, case)
        assert result.code == 0
        assert "installed via ZIP file" in result.stdout
        assert result.tree["klipper/.git/info/exclude"] == ""

    def test_a_dirty_checkout_stops_the_install(self, tmp_path: Path) -> None:
        result = run_case(tmp_path, Case(answers=["Q"], env={"AFC_STUB_DIRTY": "1"}))
        assert result.code == 1
        assert "You have uncommitted changes" in result.stdout

    def test_a_pending_upstream_change_is_pulled_and_the_script_restarts(
        self, tmp_path: Path
    ) -> None:
        env = {"AFC_STUB_BEHIND": " [behind 1]"}
        result = run_case(tmp_path, Case(answers=["Q"], env=env, use_server=True))
        assert result.code == 0
        assert any(line.endswith("pull --rebase --quiet") for line in result.log)
        assert result.stdout.count("Ensuring no conflicting software is present...") == 2

    def test_switching_branches_with_b_restarts_the_script(self, tmp_path: Path) -> None:
        result = run_case(tmp_path, Case(args=["-b", "dev"], answers=["Q"], use_server=True))
        assert result.code == 0
        assert any(line.endswith("checkout --quiet dev") for line in result.log)
        assert "Branch switched. Restarting script" in result.stdout

    def test_an_unknown_branch_exits_one(self, tmp_path: Path) -> None:
        case = Case(args=["-b", "nope"], answers=["Q"], env={"AFC_STUB_NOBRANCH": "nope"})
        result = run_case(tmp_path, case)
        assert result.code == 1
        assert "does not exist locally or on the remote" in result.stdout


class TestFlagsAndInput:
    def test_help_lists_the_options(self, tmp_path: Path) -> None:
        result = run_case(tmp_path, Case(args=["-h"]))
        assert result.code == 0
        assert result.stdout.startswith("Usage: install-afc.sh [options]\n")

    def test_an_unknown_flag_exits_one(self, tmp_path: Path) -> None:
        result = run_case(tmp_path, Case(args=["-x"]))
        assert result.code == 1
        assert "illegal option -- x" in result.stderr

    def test_a_flag_missing_its_value_exits_one(self, tmp_path: Path) -> None:
        result = run_case(tmp_path, Case(args=["-t", "-a"]))
        assert result.code == 1
        assert "option requires an argument -- a" in result.stderr

    def test_end_of_input_at_the_main_menu_exits_one(self, tmp_path: Path) -> None:
        assert run_case(tmp_path, Case(args=["-t"], answers=[])).code == 1

    def test_end_of_input_inside_the_options_exits_one(self, tmp_path: Path) -> None:
        assert run_case(tmp_path, Case(args=["-t"], answers=["I", "1", "10"])).code == 1

    def test_invalid_keys_are_ignored(self, tmp_path: Path) -> None:
        result = run_case(tmp_path, Case(args=["-t"], answers=["x", "", "5", "I", "z", "m", "Q"]))
        assert result.code == 0
