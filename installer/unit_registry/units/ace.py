# AFCProject Automated Filament Changer
#
# Copyright (C) 2026 AFCProject
#
# This file may be distributed under the terms of the GNU GPLv3 license.
"""ACE: Anycubic's ACE Pro (V1) and ACE 2 Pro filament changers."""
from __future__ import annotations

from typing import List, Tuple

from installer.config_editor.edits import replace_text
from installer.platform_core.console import Console, Prompter
from installer.platform_core.context import Context
from installer.platform_core.fsops import safe_copy
from installer.unit_registry.base import BufferTarget, UnitType
from installer.unit_registry.options import ACE_UNIT_TYPES, ChoiceOption, NameOption, UnitOption
from installer.unit_registry.selection import UnitSelection

_BUFFER_ORDER: Tuple[str, ...] = (
    "Internal", "TurtleNeck", "TurtleNeckV2", "TurtleNeck2Remastered", "FPS_PSF", "None"
)


class Ace(UnitType):
    """
    ACE is not an MCU: no board/motor type, no pins or steppers file.

    V1 (`AFC_ACE`) and V2_Pro (`AFC_ACE2`) are separate Klipper classes with their own
    templates and default names, toggled by `ace_unit_type`. ACE's load sequence always
    uses a toolhead sensor (ramming is not a supported strategy for it), and it buffers
    internally by default, so its buffer cycle includes `Internal` alongside the usual
    mechanical options.
    """

    display_name = "ACE"
    key = "ACE"
    additional_name_prefix = "Ace"
    additional_buffer_supported = True
    additional_buffer_options: Tuple[str, ...] = (
        "None", "Internal", "TurtleNeck", "TurtleNeckV2", "TurtleNeck2Remastered", "FPS_PSF"
    )
    additional_buffer_default = "Internal"
    ramming_supported = False
    force_additional_buffer_default = True

    def copy_files(
        self, ctx: Context, console: Console, prompter: Prompter, sel: UnitSelection
    ) -> None:
        """
        Copy the hardware file and the fixed-name unit file for the selected ACE version.

        :param ctx: installer context
        :param console: where messages go
        :param prompter: used when a destination file already exists
        :param sel: the user's unit choices
        """
        templates = ctx.afc_path / "templates"
        cfg = ctx.afc_config_dir
        safe_copy(ctx, console, prompter, templates / "AFC_Hardware-AFC.cfg",
                  cfg / "AFC_Hardware.cfg")
        template_name = "AFC_ACE2_1.cfg" if sel.ace_unit_type == "V2_Pro" else "AFC_ACE_1.cfg"
        safe_copy(ctx, console, prompter, templates / template_name, cfg / template_name)

    def _additional_file_stem(self, sel: UnitSelection, name: str) -> str:
        """
        The file name for an additional unit currently named `name`.

        Still on the untouched default (`sel.turtle_renamed` is False), the brand prefix is
        upper-cased for the file, matching the first unit's `AFC_ACE_1.cfg`; a name the user
        chose is used exactly as typed.

        :param sel: the user's unit choices
        :param name: the unit's current name
        :return str: the file name stem (without `AFC_` or `.cfg`)
        """
        prefix = self.name_prefix(sel)
        if (not sel.turtle_renamed
            and name.startswith(prefix)):
            return prefix.upper() + name[len(prefix):]
        return name

    def install_additional(
        self, ctx: Context, console: Console, prompter: Prompter, sel: UnitSelection
    ) -> None:
        """
        Copy the unit file under its new name and rename the default unit name inside it.

        The unit name inside the file (section names, `unit:` references) is always exactly
        the chosen name, e.g. `Ace2_1`; see `_additional_file_stem` for the file's own name.

        :param ctx: installer context
        :param console: where messages go
        :param prompter: used when a destination file already exists
        :param sel: the user's unit choices
        """
        cfg = ctx.afc_config_dir
        name = sel.unit_name
        unit_file = cfg / f"AFC_{self._additional_file_stem(sel, name)}.cfg"
        if sel.ace_unit_type == "V2_Pro":
            template = ctx.afc_path / "templates" / "AFC_ACE2_1.cfg"
            safe_copy(ctx, console, prompter, template, unit_file)
            replace_text(unit_file, "Ace2_1", name)
        else:
            template = ctx.afc_path / "templates" / "AFC_ACE_1.cfg"
            safe_copy(ctx, console, prompter, template, unit_file)
            replace_text(unit_file, "Ace_1", name)

    def buffer_target(self, ctx: Context, sel: UnitSelection) -> BufferTarget:
        """
        The fixed first unit (by ACE version), or the named additional unit.

        :param ctx: installer context
        :param sel: the user's unit choices
        :return BufferTarget: where the buffer goes
        """
        if sel.ace_unit_type == "V2_Pro":
            section_prefix, default_name, unit_file = "AFC_ACE2", "Ace2_1", "AFC_ACE2_1.cfg"
        else:
            section_prefix, default_name, unit_file = "AFC_ACE", "Ace_1", "AFC_ACE_1.cfg"
        if sel.is_additional_unit:
            name = sel.unit_name
            file_stem = self._additional_file_stem(sel, name)
            return BufferTarget(
                name, section_prefix, ctx.afc_config_dir / f"AFC_{file_stem}.cfg", name
            )
        return BufferTarget(
            default_name, section_prefix, ctx.afc_config_dir / unit_file, default_name
        )

    def message(self, ctx: Context, sel: UnitSelection) -> str:
        """
        Point the user at serial settings, and at the RFID section for an ACE 2 Pro.

        :param ctx: installer context
        :param sel: the user's unit choices
        :return str: the message
        """
        if sel.ace_unit_type == "V2_Pro":
            return (
                "\n- Ensure you check the serial_port and ace_index settings in the "
                f"{ctx.afc_config_dir}/AFC_ACE2_1.cfg file (leaving serial_port as auto is "
                "fine).\n\n"
                "- Review the [AFC_ACE2_rfid] section if your unit has an RFID reader.\n  "
            )
        return (
            "\n- Ensure you check the serial_port and ace_index settings in the "
            f"{ctx.afc_config_dir}/AFC_ACE_1.cfg file (leaving serial_port as auto is "
            "fine).\n  "
        )

    def install_options(self) -> List[UnitOption]:
        """
        The ACE version option of the install screen.

        :return List[UnitOption]: the options
        """
        return [ChoiceOption("ACE Unit Type", "ace_unit_type", ACE_UNIT_TYPES)]

    def additional_options(self) -> List[UnitOption]:
        """
        The name and version options of the additional-unit screen.

        :return List[UnitOption]: the options
        """
        return [
            NameOption("ACE Name", additional_prompt=True),
            ChoiceOption("ACE Unit Type", "ace_unit_type", ACE_UNIT_TYPES),
        ]

    def additional_probe_file(self, prefix: str, number: int, sel: UnitSelection) -> str:
        """
        File name whose existence means the default `<prefix>_<number>` is taken.

        Matches the all-caps file an unrenamed default gets (see `_additional_file_stem`),
        so an already-installed default isn't offered again.

        :param prefix: name prefix, e.g. `Ace2`
        :param number: candidate number
        :param sel: the user's unit choices
        :return str: file name inside the AFC config directory
        """
        return f"AFC_{prefix.upper()}_{number}.cfg"

    def name_prefix(self, sel: UnitSelection) -> str:
        """
        `Ace2` for an ACE 2 Pro, `Ace` for a V1, instead of a single static prefix.

        :param sel: the user's unit choices
        :return str: the prefix additional units are named after
        """
        return "Ace2" if sel.ace_unit_type == "V2_Pro" else "Ace"

    def on_select(self, sel: UnitSelection) -> None:
        """
        ACE always uses a toolhead sensor and buffers internally by default.

        Also fixes up the first unit's display name for the currently selected version,
        since it was just set from the static `default_name`.

        :param sel: the user's unit choices, updated in place
        """
        sel.toolhead_sensor = "Sensor"
        sel.buffer_type = "Internal"
        sel.unit_name = "Ace2_1" if sel.ace_unit_type == "V2_Pro" else "Ace_1"

    def cycle_buffer(self, sel: UnitSelection) -> None:
        """
        Step to the next buffer type: `Internal`, then the mechanical options, then `None`.

        Ramming is never active for ACE, so unlike the generic cycle, `FPS_PSF` falls
        back to `None` rather than back to `TurtleNeck`.

        :param sel: the user's unit choices, `buffer_type` is updated
        """
        if sel.buffer_type in _BUFFER_ORDER:
            position = (_BUFFER_ORDER.index(sel.buffer_type) + 1) % len(_BUFFER_ORDER)
        else:
            position = 0
        sel.buffer_type = _BUFFER_ORDER[position]
