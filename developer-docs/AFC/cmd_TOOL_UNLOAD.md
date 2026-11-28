# `cmd_TOOL_UNLOAD`

**Class:** `afc`  
**File:** `extras/AFC.py`

Gcode entry point for `TOOL_UNLOAD LANE=`. Unloads the named lane, or the currently loaded lane when none is given.

## Flow

```mermaid
flowchart TD
  S(["TOOL_UNLOAD LANE="]) --> BP{"Bypass sensor<br/>sees filament?"}
  BP -->|yes| BU["_check_bypass runs the renamed<br/>stock UNLOAD_FILAMENT, then return"]
  BP -->|no| L{"LANE given,<br/>else current lane?"}
  L -->|none| R1(["Return"])
  L -->|unknown name| R2(["Log unknown lane, return"])
  L -->|found| TU["TOOL_UNLOAD lane"]
  TU --> OK{"Success?"}
  OK -->|no| ST["Count an unload error"]
  OK -->|yes| SP
  ST --> SP["set_active_spool(None)"]
```

## Where it lives in code

| Step | Code |
| --- | --- |
| Command handler | `afc.cmd_TOOL_UNLOAD` in `extras/AFC.py` |
| Bypass handling | [`afc._check_bypass`](_check_bypass.md) with `unload=True` |
| Current lane | `afc.current` |
| Unload | [`afc.TOOL_UNLOAD`](TOOL_UNLOAD.md) |
| Stats | `afc.afc_stats.increase_unload_error_count` |
| Spool | `AFCSpool.set_active_spool` in `extras/AFC_spool.py` |

## Notes

- The active spool is cleared even when the unload failed, because the command means the user took the filament out of the toolhead.
- `force_unload` is not set here, so [`afc.TOOL_UNLOAD`](TOOL_UNLOAD.md) runs its normal checks, including the next-lane logic, with `next_lane_load` left as `None`.

## Related

- [`afc.TOOL_UNLOAD`](TOOL_UNLOAD.md)
- [`afc._check_bypass`](_check_bypass.md)
