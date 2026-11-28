# `_heat_next_extruder`

**Class:** `afc`  
**File:** `extras/AFC.py`

Starts heating the extruder of the next lane during a tool change, so it is hot by the time the load starts.

## Flow

```mermaid
flowchart TD
  S(["_heat_next_extruder"]) --> N{"next_lane_load set?"}
  N -->|no| E(["AFC_error: next lane is None<br/>return False"])
  N -->|yes| T{"next_temp given?"}
  T -->|yes| ST["set_temp = next_temp"]
  T -->|no| CT["set_temp = current target of the<br/>active toolhead extruder"]
  ST --> SET["Set the next extruder's heater to set_temp, no wait"]
  CT --> SET
  SET --> WT{"wait requested and next extruder<br/>is not the current one?"}
  WT -->|yes| W["Wait within the extruder's deadband"]
  WT -->|no| R(["Return the extruder and set_temp"])
  W --> R
```

## Where it lives in code

| Step | Code |
| --- | --- |
| Method | `afc._heat_next_extruder` in `extras/AFC.py` |
| Next extruder | `afc.lanes[next_lane_load].extruder_obj`, set by [`afc.CHANGE_TOOL`](CHANGE_TOOL.md) |
| Heater | `heaters` object `set_temperature` |
| Waiting | [`afc._wait_for_temp_within_tolerance`](_wait_for_temp_within_tolerance.md) |
| Error | [`afcError.AFC_error`](../AFC_error/AFC_error.md) |

## Notes

- [`afc.CHANGE_TOOL`](CHANGE_TOOL.md) calls it with `wait=False`, then does the unload while the next extruder heats, and waits afterwards in its own step.

## Related

- [`afc.CHANGE_TOOL`](CHANGE_TOOL.md)
- [`afc._cooldown_last_extruder`](_cooldown_last_extruder.md)
