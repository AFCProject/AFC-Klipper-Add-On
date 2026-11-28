# `prep_load`

**Class:** `afcBoxTurtle`  
**File:** `extras/AFC_BoxTurtle.py`

Feeds a freshly inserted spool until the load sensor triggers.

## Flow

```mermaid
flowchart TD
  S(["prep_load"]) --> H{"Homing enabled?"}
  H -->|yes| HM["Homing move toward the load sensor,<br/>up to short_move_dis times MAX_NUM_MOVES"]
  H -->|no| L
  HM --> L{"Load sensor not triggered,<br/>prep sensor still triggered,<br/>and lane has a load sensor?"}
  L -->|no| E(["Done"])
  L -->|yes| MV["Move short_move_dis<br/>pause 0.1 seconds"]
  MV --> LIM{"More than MAX_NUM_MOVES moves?"}
  LIM -->|yes| ERR["AFC_error: failed to load<br/>lane fault LED, status NONE"]
  LIM -->|no| L
  ERR --> E
```

## Where it lives in code

| Step | Code |
| --- | --- |
| Method | `afcBoxTurtle.prep_load` in `extras/AFC_BoxTurtle.py` |
| Base class | `afcUnit.prep_load` in `extras/AFC_unit.py`, a placeholder that logs that it is not defined |
| Homing move | [`AFCLane.move_to`](../AFC_lane/move_to.md) with `AFCLane.load_es` |
| Short moves | `AFCLane.move` |
| Sensors | `AFCLane.raw_load_state` and `prep_state` |
| Limit | `afcBoxTurtle.MAX_NUM_MOVES` |
| Error | [`afcError.AFC_error`](../AFC_error/AFC_error.md), `afcUnit.lane_fault` |
| Caller | [`AFCLane.prep_callback`](../AFC_lane/prep_callback.md) |

## Notes

- Other units, such as HTLF, ACE, Vivid and CFS, implement this in their own file with the same signature.

## Related

- [`AFCLane.prep_callback`](../AFC_lane/prep_callback.md)
- [`afcBoxTurtle.prep_post_load`](prep_post_load.md)
