from pathlib import Path
from typing import List

import pytest

from installer.platform_core.context import Context
from installer.unit_registry.base import BufferTarget
from installer.unit_registry.naming import lowest_free_name
from installer.unit_registry.registry import unit_for
from installer.unit_registry.selection import UnitSelection
from installer.unit_registry.units.cfs import (
    UID_PLACEHOLDER,
    Cfs,
    CfsBox,
    _box_addr,
    _lane_sort_key,
    _next_lane_number,
    _strip_comment,
    additional_box_config,
    find_cfs_boxes,
    normalize_uid,
)
from tests.installer.fakes import PrinterTree, RecordingConsole, ScriptedPrompter
from tests.installer.unit_rows import additional_rows, install_rows

CFS = "Creality CFS"
UID_A = "67913b482422b00236353434"
UID_B = "b0313b4e8411b00645353230"
SERIAL = "/dev/serial/by-id/usb-1a86_USB_Serial-if00-port0"


def make_ctx(tree: PrinterTree) -> Context:
    ctx = Context.create(tree.afc_path, home=tree.home)
    ctx.afc_config_dir.mkdir(parents=True, exist_ok=True)
    return ctx


def put(ctx: Context, name: str, content: str) -> Path:
    path = ctx.afc_config_dir / name
    path.write_text(content)
    return path


def files(ctx: Context) -> List[str]:
    return sorted(
        str(p.relative_to(ctx.afc_config_dir)) for p in ctx.afc_config_dir.rglob("*")
        if p.is_file()
    )


def unit() -> Cfs:
    found = unit_for(CFS)
    assert isinstance(found, Cfs)
    return found


def first_box_file(uid: str = "") -> str:
    uid_line = f"box_uid: {uid}\n" if uid else ""
    return (
        "[AFC_CFS CFS_1]\n"
        f"serial: {SERIAL}   # check with: ls /dev/serial/by-id/\n"
        f"{uid_line}"
        "hub: CFS_1\n"
        "extruder: extruder\n"
        "\n"
        "[AFC_hub CFS_1]\n"
        "switch_pin: virtual\n"
        "\n"
        "[AFC_lane lane1]\nunit: CFS_1:1\n[AFC_lane lane2]\nunit: CFS_1:2\n"
        "[AFC_lane lane3]\nunit: CFS_1:3\n[AFC_lane lane4]\nunit: CFS_1:4\n"
    )


def expected_box(name: str, addr: int, lanes: range, hub: str = "CFS_1",
                 serial: str = SERIAL, extruder: str = "extruder") -> str:
    lane_text = "".join(
        f"[AFC_lane lane{n}]\nunit: {name}:{slot}\n" for slot, n in enumerate(lanes, 1)
    )
    return (
        "# Another Creality CFS on the same chain as CFS_1: box 1 OUT -> box 2 IN.\n"
        "# Every box on a chain needs box_uid. Restart Klipper with the new box powered on:\n"
        "# AFC.log names its uid (\"isn't in any [AFC_CFS] section\"). Put it in box_uid\n"
        "# below, restart Klipper, then power-cycle the boxes once.\n"
        "\n"
        f"[AFC_CFS {name}]\n"
        f"serial: {serial}   # same port as CFS_1\n"
        f"box_addr: {addr}\n"
        "box_uid: 000000000000000000000000   # replace with this box's uid\n"
        f"hub: {hub}   # the shared 4-in-1 buffer\n"
        f"extruder: {extruder}\n"
        "\n"
        f"{lane_text}"
    )


def additional(name: str) -> UnitSelection:
    return UnitSelection(installation_type=CFS, unit_name=name, is_additional_unit=True)


class TestStripComment:
    def test_drops_a_trailing_comment(self) -> None:
        assert _strip_comment("/dev/x   # check with ls") == "/dev/x"

    def test_a_plain_value_is_kept(self) -> None:
        assert _strip_comment(" 2 ") == "2"


class TestFindCfsBoxes:
    def test_reads_each_section_with_its_options(self, printer_tree: PrinterTree) -> None:
        ctx = make_ctx(printer_tree)
        path1 = put(ctx, "AFC_CFS_1.cfg", first_box_file(UID_A))
        path2 = put(ctx, "AFC_CFS_2.cfg", f"[AFC_CFS CFS_2]\nbox_addr = 2\nbox_uid: {UID_B}\n")
        assert find_cfs_boxes(ctx.afc_config_dir) == [
            CfsBox("CFS_1", path1, {"serial": SERIAL, "box_uid": UID_A, "hub": "CFS_1",
                                    "extruder": "extruder"}),
            CfsBox("CFS_2", path2, {"box_addr": "2", "box_uid": UID_B}),
        ]

    def test_options_of_other_sections_are_ignored(self, printer_tree: PrinterTree) -> None:
        ctx = make_ctx(printer_tree)
        path = put(ctx, "AFC_X.cfg", "serial: /dev/a\n[AFC_CFS CFS_1]\nhub: CFS_1\n"
                                     "[AFC_hub CFS_1]\nswitch_pin: virtual\n")
        assert find_cfs_boxes(ctx.afc_config_dir) == [CfsBox("CFS_1", path, {"hub": "CFS_1"})]

    def test_commented_and_blank_lines_are_not_options(self, printer_tree: PrinterTree) -> None:
        ctx = make_ctx(printer_tree)
        path = put(ctx, "AFC_CFS_1.cfg", "[AFC_CFS CFS_1]\n# box_uid: x\n\n#box_addr: 3\n")
        assert find_cfs_boxes(ctx.afc_config_dir) == [CfsBox("CFS_1", path, {})]

    def test_unreadable_files_are_skipped(self, printer_tree: PrinterTree) -> None:
        ctx = make_ctx(printer_tree)
        (ctx.afc_config_dir / "AFC_bin.cfg").write_bytes(b"\xff\xfe[AFC_CFS X]\n")
        (ctx.afc_config_dir / "AFC_dir.cfg").mkdir()
        assert find_cfs_boxes(ctx.afc_config_dir) == []

    def test_no_files(self, printer_tree: PrinterTree) -> None:
        assert find_cfs_boxes(make_ctx(printer_tree).afc_config_dir) == []


class TestBoxAddr:
    def test_unset_is_address_1(self) -> None:
        assert _box_addr(CfsBox("CFS_1", Path("x"), {})) == 1

    def test_set(self) -> None:
        assert _box_addr(CfsBox("CFS_2", Path("x"), {"box_addr": "3"})) == 3

    def test_unreadable_is_0(self) -> None:
        assert _box_addr(CfsBox("CFS_2", Path("x"), {"box_addr": "two"})) == 0


class TestNextLaneNumber:
    def test_after_the_highest_lane(self, printer_tree: PrinterTree) -> None:
        ctx = make_ctx(printer_tree)
        put(ctx, "AFC_CFS_1.cfg", first_box_file())
        put(ctx, "AFC_Turtle_1.cfg", "[AFC_lane lane9]\n[AFC_lane spare]\n")
        assert _next_lane_number(ctx.afc_config_dir) == 10

    def test_no_numbered_lanes(self, printer_tree: PrinterTree) -> None:
        ctx = make_ctx(printer_tree)
        put(ctx, "AFC_X.cfg", "[AFC_lane left]\n")
        assert _next_lane_number(ctx.afc_config_dir) == 1


class TestNormalizeUid:
    def test_as_status_prints_it(self) -> None:
        assert normalize_uid(UID_A) == UID_A

    def test_colons_spaces_and_upper_case(self) -> None:
        assert normalize_uid("67:91:3B:48 24:22:B0:02 36:35:34:34") == UID_A

    @pytest.mark.parametrize("text", ["", "6791", UID_A + "00", "zz" + UID_A[2:]])
    def test_not_a_uid(self, text: str) -> None:
        assert normalize_uid(text) is None


class TestAdditionalBoxConfig:
    def test_shares_the_first_boxs_port_hub_and_extruder(self) -> None:
        first = CfsBox("CFS_1", Path("x"), {"serial": "/dev/b", "hub": "Buf",
                                            "extruder": "extruder1"})
        assert additional_box_config("CFS_2", first, 2, 5) == expected_box(
            "CFS_2", 2, range(5, 9), hub="Buf", serial="/dev/b", extruder="extruder1"
        )

    def test_defaults_when_the_first_box_leaves_them_unset(self) -> None:
        first = CfsBox("CFS_1", Path("x"), {})
        assert additional_box_config("Box3", first, 3, 9) == expected_box(
            "Box3", 3, range(9, 13), serial=""
        )


class TestCfsCopyFiles:
    def test_copies_the_hardware_and_unit_files(self, printer_tree: PrinterTree) -> None:
        ctx = make_ctx(printer_tree)
        unit().copy_files(ctx, RecordingConsole(), ScriptedPrompter([]),
                          UnitSelection(installation_type=CFS))
        assert files(ctx) == ["AFC_CFS_1.cfg", "AFC_Hardware.cfg"]
        assert (ctx.afc_config_dir / "AFC_CFS_1.cfg").read_bytes() == (
            printer_tree.afc_path / "templates" / "AFC_CFS_1.cfg").read_bytes()
        assert (ctx.afc_config_dir / "AFC_Hardware.cfg").read_bytes() == (
            printer_tree.afc_path / "templates" / "AFC_Hardware-AFC.cfg").read_bytes()


class TestCfsInstallAdditional:
    def test_second_box_joins_the_chain(self, printer_tree: PrinterTree) -> None:
        ctx = make_ctx(printer_tree)
        put(ctx, "AFC_CFS_1.cfg", first_box_file(UID_A))
        console, prompter = RecordingConsole(), ScriptedPrompter([])
        unit().install_additional(ctx, console, prompter, additional("CFS_2"))
        assert (ctx.afc_config_dir / "AFC_CFS_2.cfg").read_text() == expected_box(
            "CFS_2", 2, range(5, 9))
        assert prompter.prompts == []
        assert console.messages == []

    def test_third_box_takes_the_next_free_address(self, printer_tree: PrinterTree) -> None:
        ctx = make_ctx(printer_tree)
        put(ctx, "AFC_CFS_1.cfg", first_box_file(UID_A))
        put(ctx, "AFC_CFS_2.cfg", expected_box("CFS_2", 2, range(5, 9)))
        unit().install_additional(ctx, RecordingConsole(), ScriptedPrompter([]),
                                  additional("CFS_3"))
        assert (ctx.afc_config_dir / "AFC_CFS_3.cfg").read_text() == expected_box(
            "CFS_3", 3, range(9, 13))

    def test_a_full_chain_adds_nothing(self, printer_tree: PrinterTree) -> None:
        ctx = make_ctx(printer_tree)
        put(ctx, "AFC_CFS_1.cfg", first_box_file(UID_A))
        for n in (2, 3, 4):
            put(ctx, f"AFC_CFS_{n}.cfg", f"[AFC_CFS CFS_{n}]\nbox_addr: {n}\n")
        console = RecordingConsole()
        unit().install_additional(ctx, console, ScriptedPrompter([]), additional("CFS_5"))
        assert not (ctx.afc_config_dir / "AFC_CFS_5.cfg").exists()
        assert console.messages == [
            ("WARNING", "A CFS chain takes at most 4 boxes; CFS_5 was not added.")]

    def test_first_box_without_a_uid_is_asked_for_it(self, printer_tree: PrinterTree) -> None:
        ctx = make_ctx(printer_tree)
        first = put(ctx, "AFC_CFS_1.cfg", first_box_file())
        prompter = ScriptedPrompter(["67:91:3B:48 24:22:B0:02 36:35:34:34"])
        unit().install_additional(ctx, RecordingConsole(), prompter, additional("CFS_2"))
        assert prompter.prompts == ["Box uid for CFS_1 (press enter to set it later): "]
        assert find_cfs_boxes(ctx.afc_config_dir)[0].options["box_uid"] == UID_A
        assert first.read_text().count("box_uid:") == 1
        assert (ctx.afc_config_dir / "AFC_CFS_2.cfg").exists()

    def test_first_box_with_a_uid_is_not_asked(self, printer_tree: PrinterTree) -> None:
        ctx = make_ctx(printer_tree)
        put(ctx, "AFC_CFS_1.cfg", first_box_file(UID_A))
        prompter = ScriptedPrompter(["ignored"])
        unit().install_additional(ctx, RecordingConsole(), prompter, additional("CFS_2"))
        assert prompter.prompts == []

    def test_no_cfs_yet_copies_the_template_renamed(self, printer_tree: PrinterTree) -> None:
        ctx = make_ctx(printer_tree)
        unit().install_additional(ctx, RecordingConsole(), ScriptedPrompter([]),
                                  additional("Box_A"))
        template = (printer_tree.afc_path / "templates" / "AFC_CFS_1.cfg").read_text()
        assert files(ctx) == ["AFC_Box_A.cfg"]
        assert text_of(ctx, "AFC_Box_A.cfg") == template.replace("CFS_1", "Box_A")

    def test_an_existing_file_is_kept_when_skipped(self, printer_tree: PrinterTree) -> None:
        ctx = make_ctx(printer_tree)
        put(ctx, "AFC_CFS_1.cfg", first_box_file(UID_A))
        put(ctx, "AFC_CFS_2.cfg", "mine\n")
        console = RecordingConsole()
        unit().install_additional(ctx, console, ScriptedPrompter(["s"]), additional("CFS_2"))
        assert text_of(ctx, "AFC_CFS_2.cfg") == "mine\n"
        path = ctx.afc_config_dir / "AFC_CFS_2.cfg"
        assert console.messages == [("WARNING", f"Destination already exists: {path}"),
                                    ("INFO", f"Skipped: {path}")]

    def test_an_existing_file_is_replaced_when_overwritten(
        self, printer_tree: PrinterTree
    ) -> None:
        ctx = make_ctx(printer_tree)
        put(ctx, "AFC_CFS_1.cfg", first_box_file(UID_A))
        put(ctx, "AFC_CFS_2.cfg", "[AFC_CFS CFS_2]\nbox_addr: 2\n")
        unit().install_additional(ctx, RecordingConsole(), ScriptedPrompter(["o"]),
                                  additional("CFS_2"))
        # its own old section still counts, so the rewrite takes the next address
        assert text_of(ctx, "AFC_CFS_2.cfg") == expected_box("CFS_2", 3, range(5, 9))


def text_of(ctx: Context, name: str) -> str:
    return (ctx.afc_config_dir / name).read_text()


class TestCfsAskFirstUid:
    def _first(self, ctx: Context) -> CfsBox:
        put(ctx, "AFC_CFS_1.cfg", first_box_file())
        return find_cfs_boxes(ctx.afc_config_dir)[0]

    def test_writes_the_uid(self, printer_tree: PrinterTree) -> None:
        ctx = make_ctx(printer_tree)
        first = self._first(ctx)
        console = RecordingConsole()
        unit()._ask_first_uid(console, ScriptedPrompter([UID_A]), first)
        assert find_cfs_boxes(ctx.afc_config_dir)[0].options["box_uid"] == UID_A
        assert console.messages == [(
            "INFO",
            "Every box on a CFS chain needs box_uid, and CFS_1 has none yet. Run "
            "AFC_CFS_STATUS UNIT=CFS_1 in the console and copy its box uid.",
        )]

    @pytest.mark.parametrize("answers", [[""], ["not a uid"], []],
                             ids=["enter", "invalid", "no-input"])
    def test_without_a_uid_says_to_set_it(self, printer_tree: PrinterTree,
                                          answers: List[str]) -> None:
        ctx = make_ctx(printer_tree)
        first = self._first(ctx)
        before = first.path.read_text()
        console = RecordingConsole()
        unit()._ask_first_uid(console, ScriptedPrompter(answers), first)
        assert first.path.read_text() == before
        assert console.messages[1:] == [(
            "WARNING",
            f"Set box_uid in [AFC_CFS CFS_1] in {first.path} before restarting Klipper; it "
            "refuses the config while a box on the chain has none.",
        )]


class TestCfsBufferTarget:
    def test_first_unit(self, printer_tree: PrinterTree) -> None:
        ctx = make_ctx(printer_tree)
        target = unit().buffer_target(ctx, UnitSelection(installation_type=CFS))
        assert target == BufferTarget("CFS_1", "AFC_CFS", ctx.afc_config_dir / "AFC_CFS_1.cfg",
                                      "CFS_1")

    def test_additional_unit(self, printer_tree: PrinterTree) -> None:
        ctx = make_ctx(printer_tree)
        target = unit().buffer_target(ctx, additional("CFS_2"))
        assert target == BufferTarget("CFS_2", "AFC_CFS", ctx.afc_config_dir / "AFC_CFS_2.cfg",
                                      "CFS_2")


class TestCfsMessage:
    def test_points_at_serial_cutter_and_tool_settings(self, printer_tree: PrinterTree) -> None:
        ctx = make_ctx(printer_tree)
        cfg = ctx.afc_config_dir
        assert unit().message(ctx, UnitSelection(installation_type=CFS)) == (
            f"\n- Set serial in the {cfg}/AFC_CFS_1.cfg file: run ls /dev/serial/by-id/ and use "
            "the 1a86 device.\n\n"
            "- Set cutter_sensor_pin there if the CFS kit's cutter switch is on a Klipper pin.\n\n"
            f"- In {cfg}/AFC_Hardware.cfg, set tool_stn to the toolhead sensor-to-nozzle "
            "distance and tool_stn_unload to 0.\n  "
        )


class TestCfsAdditionalMessage:
    def test_says_how_the_box_joined_the_chain(self, printer_tree: PrinterTree) -> None:
        ctx = make_ctx(printer_tree)
        put(ctx, "AFC_CFS_1.cfg", first_box_file(UID_A))
        put(ctx, "AFC_CFS_2.cfg", expected_box("CFS_2", 2, range(9, 13)))
        path = ctx.afc_config_dir / "AFC_CFS_2.cfg"
        assert unit().additional_message(ctx, additional("CFS_2")) == (
            "CFS_2 created: it joins the CFS chain as box_addr 2 with lanes lane9 to lane12, on "
            "the same serial port and hub as the first box.\n"
            "Restart Klipper with the new box powered on. AFC.log names its uid (\"isn't in any "
            f"[AFC_CFS] section\"): put it in box_uid in {path}, restart Klipper, then "
            "power-cycle the boxes once."
        )

    def test_a_box_without_lanes(self, printer_tree: PrinterTree) -> None:
        ctx = make_ctx(printer_tree)
        put(ctx, "AFC_CFS_2.cfg", "[AFC_CFS CFS_2]\nbox_addr: 3\n")
        message = unit().additional_message(ctx, additional("CFS_2"))
        assert message is not None
        assert message.startswith(
            "CFS_2 created: it joins the CFS chain as box_addr 3, on the same serial port")

    def test_not_added(self, printer_tree: PrinterTree) -> None:
        ctx = make_ctx(printer_tree)
        assert unit().additional_message(ctx, additional("CFS_5")) == (
            "CFS_5 was not added: a CFS chain takes at most 4 boxes.")

    def test_a_renamed_template_copy_keeps_the_generic_note(
        self, printer_tree: PrinterTree
    ) -> None:
        ctx = make_ctx(printer_tree)
        put(ctx, "AFC_Box_A.cfg", "[AFC_CFS Box_A]\nhub: Box_A\n")
        assert unit().additional_message(ctx, additional("Box_A")) is None

    def test_a_file_without_the_units_section_keeps_the_generic_note(
        self, printer_tree: PrinterTree
    ) -> None:
        ctx = make_ctx(printer_tree)
        put(ctx, "AFC_CFS_1.cfg", first_box_file(UID_A))
        put(ctx, "AFC_CFS_2.cfg", "# kept by the user\n[AFC_CFS Other]\nbox_addr: 2\n")
        assert unit().additional_message(ctx, additional("CFS_2")) is None


class TestLaneSortKey:
    def test_numbers_sort_numerically_and_others_last(self) -> None:
        names = ["spare", "lane10", "lane9", "lane12"]
        assert sorted(names, key=_lane_sort_key) == ["lane9", "lane10", "lane12", "spare"]


class TestCfsAdditionalOptions:
    def test_only_the_name(self, printer_tree: PrinterTree) -> None:
        ctx = make_ctx(printer_tree)
        put(ctx, "AFC_CFS_1.cfg", first_box_file())
        sel = UnitSelection(installation_type=CFS, unit_name="CFS_1")
        assert additional_rows(unit(), ctx, sel) == ["CFS Name : CFS_2"]
        assert install_rows(unit(), sel) == []


class TestCfsOnSelect:
    def test_toolhead_sensor_and_no_buffer(self) -> None:
        sel = UnitSelection(installation_type=CFS, toolhead_sensor="Ramming",
                            buffer_type="TurtleNeck")
        unit().on_select(sel)
        assert (sel.toolhead_sensor, sel.buffer_type) == ("Sensor", "None")


class TestCfsDefaults:
    def test_type_settings(self) -> None:
        cfs = unit()
        assert (cfs.display_name, cfs.key, cfs.default_name) == (CFS, "CFS", "CFS_1")
        assert cfs.ramming_supported is False
        assert cfs.buffer_selectable is False
        assert cfs.additional_buffer_supported is False

    def test_next_default_name_after_the_first_box(self, printer_tree: PrinterTree) -> None:
        ctx = make_ctx(printer_tree)
        put(ctx, "AFC_CFS_1.cfg", first_box_file())
        assert lowest_free_name(ctx, UnitSelection(installation_type=CFS), unit()) == "CFS_2"

    def test_the_placeholder_is_a_valid_but_unused_uid(self) -> None:
        assert normalize_uid(UID_PLACEHOLDER) == UID_PLACEHOLDER
