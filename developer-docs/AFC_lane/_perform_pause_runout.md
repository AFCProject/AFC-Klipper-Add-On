# `_perform_pause_runout`

**Class:** `AFCLane`  
**File:** `extras/AFC_lane.py`

Pauses the print when a lane runs out and no replacement lane is set up. Optionally unloads and ejects the empty lane first.

## Flow

```mermaid
flowchart TD
  S(["_perform_pause_runout"]) --> U{"unit.unload_on_runout?"}
  U -->|yes| P["Pause, save position<br/>TOOL_UNLOAD the lane"]
  P --> OK{"Unloaded and no error state?"}
  OK -->|yes| EJ["LANE_UNLOAD the lane"]
  OK -->|no| ER["Count an unload error"]
  U -->|no| M
  EJ --> M["status = NONE"]
  ER --> M
  M --> T{"Triggered by the weight threshold?"}
  T -->|yes| W1["message: minimum weight threshold reached"]
  T -->|no| W2["message: runout triggered"]
  W1 --> L
  W2 --> L["unit.lane_not_ready: set the LED<br/>AFC_error: load the next spool and resume"]
```

## Where it lives in code

| Step | Code |
| --- | --- |
| Method | `AFCLane._perform_pause_runout` in `extras/AFC_lane.py` |
| Setting | `unload_on_runout` on the unit |
| Pause | `afc.error.pause_resume.send_pause_command` and [`afc.save_pos`](../AFC/save_pos.md) |
| Unload and eject | [`afc.TOOL_UNLOAD`](../AFC/TOOL_UNLOAD.md), [`afc.LANE_UNLOAD`](../AFC/LANE_UNLOAD.md) |
| Stats | `afc.afc_stats.increase_unload_error_count` |
| Message | [`afcError.AFC_error`](../AFC_error/AFC_error.md), which pauses |
| Weight trigger | `AFCLane.auto_switch_triggered`, set by the weight timer |

## Notes

- The user loads the next spool by hand and resumes. [`afcError.cmd_AFC_RESUME`](../AFC_error/cmd_AFC_RESUME.md) restores the saved position.

## Related

- [`AFCLane._perform_infinite_runout`](_perform_infinite_runout.md)
- [`AFCLane.handle_toolhead_runout`](handle_toolhead_runout.md)
