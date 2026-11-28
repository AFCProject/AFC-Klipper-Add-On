# `prep_post_load`

**Class:** `afcBoxTurtle`  
**File:** `extras/AFC_BoxTurtle.py`

After a spool reaches the load sensor, optionally feeds it on to the hub.

## Flow

```mermaid
flowchart TD
  S(["prep_post_load"]) --> C{"Not a direct hub, load_to_hub on,<br/>not already loaded to hub,<br/>and load and prep sensors both triggered?"}
  C -->|no| E(["Done"])
  C -->|yes| MV["lane.move dist_hub at dist_hub_move_speed<br/>fast mode when dist_hub is over 200mm"]
  MV --> F["loaded_to_hub = true"]
```

## Where it lives in code

| Step | Code |
| --- | --- |
| Method | `afcBoxTurtle.prep_post_load` in `extras/AFC_BoxTurtle.py` |
| Base class | `afcUnit.prep_post_load` in `extras/AFC_unit.py`, a placeholder |
| Move | `AFCLane.move` |
| Settings | `load_to_hub`, `dist_hub`, `dist_hub_move_speed`, `dist_hub_move_accel` |
| Caller | [`AFCLane.prep_callback`](../AFC_lane/prep_callback.md) |

## Notes

- Moving to the hub here means [`afc.load_sequence`](../AFC/load_sequence.md) later only has to add the short moves to the hub sensor, so the first toolhead load is quicker.

## Related

- [`afcBoxTurtle.prep_load`](prep_load.md)
- [`AFCLane.prep_callback`](../AFC_lane/prep_callback.md)
