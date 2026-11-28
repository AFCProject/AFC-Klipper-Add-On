# `unload_sequence`

**Class:** `afc`  
**File:** `extras/AFC.py`

The physical unload: retract out of the extruder, back through the bowden to the hub, then clear the hub sensor. Units can replace all or part of it.

## Flow

```mermaid
flowchart TD
  S(["unload_sequence"]) --> LED["LED: unloading<br/>Heat the toolhead if needed"]
  LED --> WH{"How is this lane unloaded?"}
  WH -->|custom_unload_cmd| CU["Run it, then post_unload_macro<br/>set_tool_unloaded"]
  WH -->|"unit has unit_unload_lane"| UU["The unit does the whole unload"]
  WH -->|standard| Q["Quick pull of 2mm to stop ooze<br/>Buffer off, sync lane to extruder, select lane"]
  Q --> CT["Cut, park, form tip<br/>each one only if enabled"]
  CT --> BUF{"tool_start is buffer?"}
  BUF -->|yes| B1["Unsync, short retracts until the buffer<br/>is back at its trailing pin<br/>then retract tool_stn_unload"]
  BUF -->|no| SN["Retract tool_stn_unload<br/>repeat while the pre-extruder sensor<br/>or tool_end is still triggered"]
  B1 --> AF["Extra tool_sensor_after_extruder move if set"]
  SN --> AF
  AF --> HUB["Unsync, retract back to the hub<br/>afc_unload_bowden_length, homing if enabled"]
  HUB --> MK["set_tool_unloaded, save_vars"]
  MK --> CLR{"Hub sensor<br/>still triggered?"}
  CLR -->|yes| CL2["Short moves back until it clears"]
  CL2 --> CLR
  CLR -->|no| HC(["hub_clear_move_dis retract<br/>unloaded"])
```

## Where it lives in code

| Step | Code |
| --- | --- |
| Method | `afc.unload_sequence` in `extras/AFC.py` |
| LEDs | `afcUnit.lane_unloading` in `extras/AFC_unit.py` |
| Heating | [`afc._check_extruder_temp`](_check_extruder_temp.md) |
| Custom path | `AFCLane.custom_unload_cmd`, `afc.post_unload_macro`, [`AFCLane.set_tool_unloaded`](../AFC_lane/set_tool_unloaded.md) |
| Unit-owned path | `unit_unload_lane` on the unit class, when it defines one |
| Extruder moves | [`afc.move_e_pos`](move_e_pos.md) |
| Cut, park, tip | [`afc.do_tool_cut_tip_form`](do_tool_cut_tip_form.md) |
| Sync and select | `AFCLane.sync_to_extruder`, `unsync_to_extruder`, `select_lane`, `disable_buffer` |
| Buffer check | `AFCLane.get_trailing` and `move_advanced` |
| Sensor check | `AFCLane.get_toolhead_pre_sensor_state` and `AFCExtruder.tool_end_state` |
| Back to the hub | [`afcUnit.move_to_hub`](../AFC_unit/move_to_hub.md) |
| Direct hub lanes | [`AFCLane.move_to`](../AFC_lane/move_to.md) to `lane.load_es` |
| Failures | [`afcError.handle_lane_failure`](../AFC_error/handle_lane_failure.md), [`afcError.AFC_error`](../AFC_error/AFC_error.md) |

## Notes

- Retry loops use `tool_max_unload_attempts`. When a limit is hit the lane goes to the failure path instead of continuing.
- `set_tool_unloaded` runs as soon as filament is behind the extruder, before the hub is cleared. A failure after that point leaves the lane marked unloaded.
- The hub-clearing loop uses short moves, or at most two homing moves when homing is enabled.
- Standalone toolheads leave the sensor loop after one retract.
- Direct-hub lanes retract to the load sensor instead of a hub.

## Related

- [`afc.TOOL_UNLOAD`](TOOL_UNLOAD.md)
- [`afc.do_tool_cut_tip_form`](do_tool_cut_tip_form.md)
- [`afcUnit.move_to_hub`](../AFC_unit/move_to_hub.md)
