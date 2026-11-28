# `cmd_CHANGE_TOOL`

**Class:** `afc`  
**File:** `extras/AFC.py`

Gcode entry point for `T0`, `T1` and so on, and for `CHANGE_TOOL LANE=`. It turns the command into a lane and hands it to [`afc.CHANGE_TOOL`](CHANGE_TOOL.md).

## Flow

```mermaid
flowchart TD
  S(["T(n) or CHANGE_TOOL LANE="]) --> A{"A= parameter given<br/>and Snapmaker printer?"}
  A -->|yes| SM["Run the renamed Snapmaker T(n) handler<br/>with the same A value"]
  SM --> R1(["Return"])
  A -->|no| BP{"Bypass sensor<br/>sees filament?"}
  BP -->|yes| R2(["Return"])
  BP -->|no| HM{"Printer homed?"}
  HM -->|no| R3(["Return False"])
  HM -->|yes| PR["Parse PURGE_LENGTH and NEW_EXTRUDER_TEMP<br/>strip the leading = Klipper leaves on T(n) parameters"]
  PR --> NUM{"Both numeric?"}
  NUM -->|no| E1(["AFC_error, no pause"])
  NUM -->|yes| W{"Called as CHANGE_TOOL?"}
  W -->|yes| F["Find the T(n) command whose lane matches LANE="]
  W -->|no| F2["The T(n) command is the command name"]
  F --> FOUND{"T(n) command found?"}
  F2 --> FOUND
  FOUND -->|no| E2(["AFC_error: did not understand the change"])
  FOUND -->|yes| LK{"Lane exists in lanes?"}
  LK -->|no| E3(["AFC_error: cannot look up lane"])
  LK -->|yes| GO["lane.current_map = T(n)<br/>CHANGE_TOOL lane"]
```

## Where it lives in code

| Step | Code |
| --- | --- |
| Command handler | `afc.cmd_CHANGE_TOOL` in `extras/AFC.py` |
| Bypass check | [`afc._check_bypass`](_check_bypass.md) |
| Homed check | `afcFunction.check_homed` in `extras/AFC_functions.py` |
| T(n) to lane lookup | `afc.tool_cmds` (T(n) to lane name) and `afc.lanes` |
| Hand off | [`afc.CHANGE_TOOL`](CHANGE_TOOL.md) |

## Notes

- Snapmaker's own `T(n)` macro is renamed with a leading underscore at startup. When a `T(n)` arrives with an `A=` value, AFC passes it straight through, because that form activates the extruder without a tool change.
- `lane.current_map` is set to the command that was used, so the lane knows which `T(n)` selected it.

## Related

- [`afc.CHANGE_TOOL`](CHANGE_TOOL.md)
- [`afc._check_bypass`](_check_bypass.md)
