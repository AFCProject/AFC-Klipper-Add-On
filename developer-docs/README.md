# AFC developer docs

One page per function. Each page has a flow diagram, a table pointing at where each step lives in the code, and notes on behavior that is easy to miss.

Pages are grouped by the module the function lives in. Names are functions and methods, not line numbers, so they stay correct as files change.

To update these pages when the code changes, see [MAINTAINING.md](MAINTAINING.md). Run `python developer-docs/tools/check_docs.py` before you push, it fails when a page names code that no longer exists.

Diagrams are [Mermaid](https://mermaid.js.org/) and render on GitHub. Rounded boxes are start and end points, diamonds are decisions, rectangles are actions.

## Start here

| Action | Page |
| --- | --- |
| Selecting a new lane with `T(n)` | [`cmd_CHANGE_TOOL`](AFC/cmd_CHANGE_TOOL.md), then [`CHANGE_TOOL`](AFC/CHANGE_TOOL.md) |
| Unloading the toolhead | [`TOOL_UNLOAD`](AFC/TOOL_UNLOAD.md), then [`unload_sequence`](AFC/unload_sequence.md) |
| Loading the toolhead | [`TOOL_LOAD`](AFC/TOOL_LOAD.md), then [`load_sequence`](AFC/load_sequence.md) |
| Pushing a spool into a lane | [`prep_callback`](AFC_lane/prep_callback.md) |
| Ejecting a spool | [`LANE_UNLOAD`](AFC/LANE_UNLOAD.md) |
| Running out of filament | [`_handle_toolhead_sensor_runout`](AFC_extruder/_handle_toolhead_sensor_runout.md), [`handle_toolhead_runout`](AFC_lane/handle_toolhead_runout.md) |
| Swapping toolheads | [`tool_swap`](AFC_Toolchanger/tool_swap.md) |
| Something failing | [`AFC_error`](AFC_error/AFC_error.md), [`handle_lane_failure`](AFC_error/handle_lane_failure.md), [`cmd_AFC_RESUME`](AFC_error/cmd_AFC_RESUME.md) |

## Core workflows

`extras/AFC.py`, class `afc`

| Function | What it does |
| --- | --- |
| [`cmd_CHANGE_TOOL`](AFC/cmd_CHANGE_TOOL.md) | Gcode entry point for `T0`, `T1` and so on, and for `CHANGE_TOOL LANE=` |
| [`CHANGE_TOOL`](AFC/CHANGE_TOOL.md) | Performs a tool change: saves the position, unloads whatever is loaded, optionally heats and waits, loads the new lane, then restores the position |
| [`cmd_TOOL_UNLOAD`](AFC/cmd_TOOL_UNLOAD.md) | Gcode entry point for `TOOL_UNLOAD LANE=` |
| [`TOOL_UNLOAD`](AFC/TOOL_UNLOAD.md) | Decides whether anything has to come out of the toolhead, then runs the unload sequence for the lane in the extruder |
| [`unload_sequence`](AFC/unload_sequence.md) | The physical unload: retract out of the extruder, back through the bowden to the hub, then clear the hub sensor |
| [`do_tool_cut_tip_form`](AFC/do_tool_cut_tip_form.md) | Runs the optional cut, park and tip-forming macros during an unload |
| [`cmd_TOOL_LOAD`](AFC/cmd_TOOL_LOAD.md) | Gcode entry point for `TOOL_LOAD LANE=` |
| [`TOOL_LOAD`](AFC/TOOL_LOAD.md) | Makes sure the right extruder is active and free, checks that the lane is ready, runs the load sequence, then purges |
| [`load_sequence`](AFC/load_sequence.md) | The physical load: feed filament to the hub, through the bowden to the toolhead sensor, then into the nozzle |
| [`do_poop_kick_wipe`](AFC/do_poop_kick_wipe.md) | Runs the purge, kick and wipe macros after a load |
| [`cmd_LANE_UNLOAD`](AFC/cmd_LANE_UNLOAD.md) | Gcode entry point for `LANE_UNLOAD LANE=` |
| [`LANE_UNLOAD`](AFC/LANE_UNLOAD.md) | Ejects a lane's filament back out of the unit when the lane is not in use |
| [`save_pos`](AFC/save_pos.md) | Saves the toolhead position and gcode state before a tool change or an error pause, so it can be restored afterwards |
| [`restore_pos`](AFC/restore_pos.md) | Moves the toolhead back to the position saved by `save_pos` and restores the gcode state: lift, move over, restore offsets, lower |
| [`_check_bypass`](AFC/_check_bypass.md) | Checks whether filament is sitting in the bypass sensor |
| [`_check_extruder_temp`](AFC/_check_extruder_temp.md) | Heats or cools the toolhead to the lane's target temperature before a load or unload, and waits if it had to heat |
| [`_heat_next_extruder`](AFC/_heat_next_extruder.md) | Starts heating the extruder of the next lane during a tool change, so it is hot by the time the load starts |
| [`_cooldown_last_extruder`](AFC/_cooldown_last_extruder.md) | Lowers the temperature of the extruder that was just used, either by the configured drop or all the way to zero for an infinite runout |
| [`_wait_for_temp_within_tolerance`](AFC/_wait_for_temp_within_tolerance.md) | Blocks until a heater is within a tolerance of a target temperature, checking once a second |
| [`capture_toolhead_temp`](AFC/capture_toolhead_temp.md) | Remembers the toolhead's current target temperature before a load or unload, so it can be put back afterwards |
| [`restore_toolhead_temp`](AFC/restore_toolhead_temp.md) | Puts the toolhead target temperature back to the value saved by `capture_toolhead_temp` |
| [`verify_macro_positions`](AFC/verify_macro_positions.md) | Checks that the cut, park, purge, kick and wipe macros have had their positions set, when those macros are enabled |
| [`move_z_pos`](AFC/move_z_pos.md) | Moves Z to a height, capped just under the printer's maximum, and returns the Z it actually moved to |
| [`move_e_pos`](AFC/move_e_pos.md) | Moves the extruder by a distance, positive to extrude and negative to retract |

## Lanes: sensors, state and moves

`extras/AFC_lane.py`, class `AFCLane`

| Function | What it does |
| --- | --- |
| [`prep_callback`](AFC_lane/prep_callback.md) | Runs when the prep (filament present) sensor changes |
| [`load_callback`](AFC_lane/load_callback.md) | Runs when the load sensor changes |
| [`handle_load_runout`](AFC_lane/handle_load_runout.md) | Handles the load switch for units that only have a load sensor |
| [`set_loaded`](AFC_lane/set_loaded.md) | Marks a lane as loaded into the unit, with filament at the load sensor but not in the toolhead |
| [`set_unloaded`](AFC_lane/set_unloaded.md) | Marks a lane as empty: the spool has been removed or ejected |
| [`set_tool_loaded`](AFC_lane/set_tool_loaded.md) | Marks a lane as loaded into the toolhead and makes it the extruder's lane |
| [`set_tool_unloaded`](AFC_lane/set_tool_unloaded.md) | Marks a lane as no longer in the toolhead and clears the extruder's lane |
| [`handle_toolhead_runout`](AFC_lane/handle_toolhead_runout.md) | Decides what a toolhead sensor reporting no filament means: a jam, an infinite spool changeover, or a pause for a new spool |
| [`_perform_pause_runout`](AFC_lane/_perform_pause_runout.md) | Pauses the print when a lane runs out and no replacement lane is set up |
| [`_perform_infinite_runout`](AFC_lane/_perform_infinite_runout.md) | Swaps to the runout lane without stopping the print: unload the empty lane, load the next one, swap their T(n) mappings, and resume |
| [`tool_swap`](AFC_lane/tool_swap.md) | Thin wrapper that hands a toolhead swap to the toolchanger unit that owns the lane's extruder |
| [`move_to`](AFC_lane/move_to.md) | Moves a lane's stepper, either as a homing move that stops at a sensor or as a plain move |

## Toolhead extruder

`extras/AFC_extruder.py`, class `AFCExtruder`

| Function | What it does |
| --- | --- |
| [`_handle_toolhead_sensor_runout`](AFC_extruder/_handle_toolhead_sensor_runout.md) | Entry point for a toolhead sensor reporting no filament |

## Toolchanger unit

`extras/AFC_Toolchanger.py`, class `AfcToolchanger`

| Function | What it does |
| --- | --- |
| [`tool_swap`](AFC_Toolchanger/tool_swap.md) | Swaps to the toolhead that holds a lane |
| [`cmd_AFC_SELECT_TOOL`](AFC_Toolchanger/cmd_AFC_SELECT_TOOL.md) | Gcode command that selects a toolhead by extruder name |
| [`cmd_AFC_UNSELECT_TOOL`](AFC_Toolchanger/cmd_AFC_UNSELECT_TOOL.md) | Gcode command that parks the current tool without picking up another one |

## Shared helpers

`extras/AFC_functions.py`, class `afcFunction`

| Function | What it does |
| --- | --- |
| [`handle_activate_extruder`](AFC_functions/handle_activate_extruder.md) | Makes the lane in the active toolhead extruder the active lane, and switches the others off: motors, buffers, sync and LEDs |
| [`select_loaded_lane`](AFC_functions/select_loaded_lane.md) | Re-selects the lane that is loaded in the toolhead, for units with a selector, after other lanes have been moved |

## Unit base class

`extras/AFC_unit.py`, class `afcUnit`

| Function | What it does |
| --- | --- |
| [`move_to_hub`](AFC_unit/move_to_hub.md) | Moves a lane toward or away from the hub sensor |
| [`load_then_home`](AFC_unit/load_then_home.md) | Moves filament from the hub to the toolhead sensor, either with a long homing move or with a plain move followed by a short homing move |

## BoxTurtle unit

`extras/AFC_BoxTurtle.py`, class `afcBoxTurtle`

| Function | What it does |
| --- | --- |
| [`prep_load`](AFC_BoxTurtle/prep_load.md) | Feeds a freshly inserted spool until the load sensor triggers |
| [`prep_post_load`](AFC_BoxTurtle/prep_post_load.md) | After a spool reaches the load sensor, optionally feeds it on to the hub |
| [`eject_lane`](AFC_BoxTurtle/eject_lane.md) | Retracts a lane's filament out of the unit so the spool can be removed |

## Errors, pause and resume

`extras/AFC_error.py`, class `afcError`

| Function | What it does |
| --- | --- |
| [`AFC_error`](AFC_error/AFC_error.md) | The standard way to report a problem |
| [`handle_lane_failure`](AFC_error/handle_lane_failure.md) | Reports that a lane could not do what it was asked: stops its motor, marks it as errored and tells the user |
| [`fix`](AFC_error/fix.md) | Tries to repair a reported toolhead or lane problem on its own, and pauses for the user when it cannot |
| [`PauseUserIntervention`](AFC_error/PauseUserIntervention.md) | Logs a message and pauses the print for the user, but only when the printer is homed and not already paused |
| [`pause_print`](AFC_error/pause_print.md) | Puts AFC into the error state and runs the user's `PAUSE` macro |
| [`set_error_state`](AFC_error/set_error_state.md) | Turns the AFC error state on or off |
| [`reset_failure`](AFC_error/reset_failure.md) | Clears the error bookkeeping: error state, the pause flag, the saved-position flag and the toolchange flag |
| [`cmd_AFC_RESUME`](AFC_error/cmd_AFC_RESUME.md) | Replaces the user's `RESUME` at startup |
