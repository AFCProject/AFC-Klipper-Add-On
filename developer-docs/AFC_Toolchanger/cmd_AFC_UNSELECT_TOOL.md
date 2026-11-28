# `cmd_AFC_UNSELECT_TOOL`

**Class:** `AfcToolchanger`  
**File:** `extras/AFC_Toolchanger.py`

Gcode command that parks the current tool without picking up another one.

## Flow

```mermaid
flowchart TD
  S(["AFC_UNSELECT_TOOL"]) --> A["Count an unselect on the current extruder<br/>state TOOL_DOCK, extruder status TOOL_DOCK"]
  A --> C{"Extruder has<br/>custom_unselect?"}
  C -->|yes| CU["Run the custom macro"]
  C -->|no| UT["UNSELECT_TOOL<br/>from klipper-toolchanger"]
  CU --> H["handle_activate_extruder"]
  UT --> H
  H --> L{"A lane is current?"}
  L -->|no| SP
  L -->|yes| LS{"Lane prepped and loaded?"}
  LS -->|"yes, tool loaded"| L1["LED: tool loaded idle"]
  LS -->|"yes, tool not loaded"| L2["LED: tool unloaded"]
  LS -->|no| L3["Extruder status LED: tool unloaded"]
  L1 --> SP
  L2 --> SP
  L3 --> SP["set_active_spool empty<br/>state IDLE, extruder status IDLE"]
```

## Where it lives in code

| Step | Code |
| --- | --- |
| Command handler | `AfcToolchanger.cmd_AFC_UNSELECT_TOOL` in `extras/AFC_Toolchanger.py` |
| Current extruder and lane | `afcFunction.get_current_extruder_obj` and `get_current_lane_obj` |
| Park command | `AFCExtruder.custom_unselect`, else the `UNSELECT_TOOL` command from klipper-toolchanger |
| Lane sync | [`afcFunction.handle_activate_extruder`](../AFC_functions/handle_activate_extruder.md) |
| LEDs | `afcUnit.lane_tool_loaded_idle` and `lane_tool_unloaded`, `AFCExtruder.set_status_led` |
| Spool | `AFCSpool.set_active_spool` |

## Notes

- Nothing is loaded or unloaded. The filament stays in the parked toolhead, and only AFC's idea of the active tool is cleared.

## Related

- [`AfcToolchanger.tool_swap`](tool_swap.md)
- [`AfcToolchanger.cmd_AFC_SELECT_TOOL`](cmd_AFC_SELECT_TOOL.md)
