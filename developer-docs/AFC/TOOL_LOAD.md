# `TOOL_LOAD`

**Class:** `afc`  
**File:** `extras/AFC.py`

Makes sure the right extruder is active and free, checks that the lane is ready, runs the load sequence, then purges.

## Flow

```mermaid
flowchart TD
  S(["TOOL_LOAD lane"]) --> CK{"Bypass clear, printer homed, macro positions valid,<br/>lane given, custom load not asked to gear-only load?"}
  CK -->|no| F1(["Return False<br/>error reported"])
  CK -->|yes| EXT{"Lane's extruder is not<br/>the active extruder?"}
  EXT -->|yes| SW["tool_swap"]
  EXT -->|no| STALE{"Another lane is loaded<br/>on this extruder?"}
  SW --> STALE
  STALE -->|yes| UN["TOOL_UNLOAD that lane first"]
  UN --> UOK{"Unload ok?"}
  UOK -->|no| UE(["error.fix, count an unload error<br/>return False"])
  UOK -->|yes| CUR{"Lane is not the<br/>current lane?"}
  STALE -->|no| CUR
  CUR -->|no| PG
  CUR -->|yes| RDY{"Load sensor triggered and<br/>hub clear, or direct hub?"}
  RDY -->|no| F2(["Lane failure, pause if printing<br/>return False"])
  RDY -->|yes| LD["state LOADING, lane status TOOL_LOADING<br/>LED: loading, remember toolhead temperature"]
  LD --> LS["load_sequence"]
  LS --> LOK{"Success?"}
  LOK -->|no| LF(["Return False<br/>state back to IDLE"])
  LOK -->|yes| FIN["LED: tool loaded<br/>Unless gear-only: espooler assist, poop, kick, wipe<br/>Enable fault detection<br/>extruder.lane_loaded = lane, set active spool, save_vars"]
  FIN --> PM["Load stats, post_load_macro"]
  PM --> PG{"Lane needs purge<br/>and not gear-only?"}
  PG -->|yes| PU["Heat, poop, kick, wipe<br/>post_load_macro"]
  PG -->|no| OK(["Return True"])
  PU --> OK
```

## Where it lives in code

| Step | Code |
| --- | --- |
| Method | `afc.TOOL_LOAD` in `extras/AFC.py` |
| Pre-checks | [`afc._check_bypass`](_check_bypass.md), `afcFunction.check_homed`, [`afc.verify_macro_positions`](verify_macro_positions.md), `afcFunction.check_absolute_mode` |
| Tool swap | `AFCLane.tool_swap` ([`AFCLane.tool_swap`](../AFC_lane/tool_swap.md)) |
| Stale lane unload | [`afc.TOOL_UNLOAD`](TOOL_UNLOAD.md) and [`afcError.fix`](../AFC_error/fix.md) |
| Readiness | `AFCLane.load_state`, `afc_hub.state`, `AFCLane.is_direct_hub` |
| Not ready | [`afcError.handle_lane_failure`](../AFC_error/handle_lane_failure.md) |
| LEDs | `afcUnit.lane_loading`, `lane_tool_loaded` in `extras/AFC_unit.py` |
| Temperature | [`afc.capture_toolhead_temp`](capture_toolhead_temp.md), [`afc.restore_toolhead_temp`](restore_toolhead_temp.md), [`afc._check_extruder_temp`](_check_extruder_temp.md) |
| Load | [`afc.load_sequence`](load_sequence.md) |
| Purge | [`afc.do_poop_kick_wipe`](do_poop_kick_wipe.md) |
| After the load | `AFCLane.enable_fault_detection`, `AFCSpool.set_active_spool`, `AFCLane.espooler.do_assist_move`, `afc.post_load_macro` |

## Notes

- The unload branch handles the restart case: AFC remembers an old lane on the extruder, so it unloads that before loading the new one.
- Gear-only loading (`load_to_gears`) stops once filament is in the extruder gears and skips the purge macros. It is rejected if the lane has a `custom_load_cmd`, because that command is opaque gcode.
- The purge step at the end also covers standalone toolheads whose lane was marked `need_purge` without a fresh load.
- A hub that is still triggered, or a load sensor that is not, stops the load before any filament moves.
- `extruder.lane_loaded` is assigned here and again inside [`AFCLane.set_tool_loaded`](../AFC_lane/set_tool_loaded.md), so the lane and extruder always agree.

## Related

- [`afc.load_sequence`](load_sequence.md)
- [`afc.CHANGE_TOOL`](CHANGE_TOOL.md)
- [`afc.cmd_TOOL_LOAD`](cmd_TOOL_LOAD.md)
- [`afc.TOOL_UNLOAD`](TOOL_UNLOAD.md)
