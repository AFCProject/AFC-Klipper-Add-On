# `eject_lane`

**Class:** `afcBoxTurtle`  
**File:** `extras/AFC_BoxTurtle.py`

Retracts a lane's filament out of the unit so the spool can be removed.

## Flow

```mermaid
flowchart TD
  S(["eject_lane"]) --> H{"Lane is loaded to the hub?"}
  H -->|yes| HM["Retract dist_hub back to the load sensor<br/>homing if enabled"]
  H -->|no| L
  HM --> L{"Load sensor still triggered?"}
  L -->|yes| MV["Retract one short move toward the lane"]
  MV --> LIM{"Tried MAX_NUM_MOVES times?"}
  LIM -->|yes| ERR["AFC_error: failed to eject<br/>stop retracting"]
  LIM -->|no| L
  L -->|no| CL
  ERR --> CL["Retract extruder_clear_dis more<br/>so the spool is free"]
  CL --> OFF(["Stepper off"])
```

## Where it lives in code

| Step | Code |
| --- | --- |
| Method | `afcBoxTurtle.eject_lane` in `extras/AFC_BoxTurtle.py` |
| Base class | `afcUnit.eject_lane` in `extras/AFC_unit.py`, a placeholder |
| Homing retract | [`AFCLane.move_to`](../AFC_lane/move_to.md) to `AFCLane.load_es` |
| Short retracts | `AFCLane.move_advanced` with `AssistActive.YES` |
| Limit | `afcBoxTurtle.MAX_NUM_MOVES` |
| Stepper | `AFCLane.do_enable(False)` in a `finally` block |
| Caller | [`afc.LANE_UNLOAD`](../AFC/LANE_UNLOAD.md) |

## Notes

- The stepper is switched off even if a move raises, so a failed eject does not leave the motor energized.

## Related

- [`afc.LANE_UNLOAD`](../AFC/LANE_UNLOAD.md)
- [`afcBoxTurtle.prep_load`](prep_load.md)
