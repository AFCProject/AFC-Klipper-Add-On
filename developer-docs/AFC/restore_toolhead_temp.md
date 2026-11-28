# `restore_toolhead_temp`

**Class:** `afc`  
**File:** `extras/AFC.py`

Puts the toolhead target temperature back to the value saved by [`afc.capture_toolhead_temp`](capture_toolhead_temp.md).

## Flow

```mermaid
flowchart TD
  S(["restore_toolhead_temp"]) --> E{"restore_extruder_temp_on_load_or_unload<br/>enabled?"}
  E -->|no| N1(["Return"])
  E -->|yes| P{"Printing and not<br/>an async restore?"}
  P -->|yes| N2(["Return"])
  P -->|no| SET["Set the saved extruder's heater<br/>to the saved target, no wait<br/>log it"]
  SET --> ERR{"Any exception?"}
  ERR -->|yes| D(["Log at debug level and carry on"])
  ERR -->|no| R(["Done"])
```

## Where it lives in code

| Step | Code |
| --- | --- |
| Method | `afc.restore_toolhead_temp` in `extras/AFC.py` |
| Heater | `heaters` object `set_temperature` |
| Used by | [`afc.TOOL_LOAD`](TOOL_LOAD.md) and [`afc.TOOL_UNLOAD`](TOOL_UNLOAD.md), inside `finally` blocks |

## Notes

- `async_restore=True` allows a restore during a print. Toolchangers use it to bring other toolheads back to temperature.

## Related

- [`afc.capture_toolhead_temp`](capture_toolhead_temp.md)
