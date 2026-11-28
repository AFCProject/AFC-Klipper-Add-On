# `handle_load_runout`

**Class:** `AFCLane`  
**File:** `extras/AFC_lane.py`

Handles the load switch for units that only have a load sensor. A rising edge is a spool being inserted, a falling edge is a removal or a runout.

## Flow

```mermaid
flowchart TD
  S(["Load switch changes"]) --> DB["Register the state with the<br/>debounced filament switch"]
  DB --> G{"Printer ready, unit is an<br/>ONLY_LOAD type, PREP done?"}
  G -->|no| X(["Do nothing"])
  G -->|yes| ST{"Filament present?"}
  ST -->|yes| SL["Stash the staged spool id<br/>set_loaded<br/>unit.on_filament_insert, unless suppressed"]
  SL --> TD{"TD-1 configured and<br/>not tool loaded?"}
  TD -->|yes| TDR["Read TD-1 data"]
  TD -->|no| DL
  TDR --> DL{"Hub is direct_load?"}
  DL -->|yes| MV{"Printer moving?"}
  MV -->|yes| E1(["AFC_error: cannot load while moving"])
  MV -->|no| TL["TOOL_LOAD the lane"]
  DL -->|no| MAC
  TL --> MAC["Post-prep user macro"]
  ST -->|no| RM["unit.on_filament_remove"]
  RM --> DIS{"Sensor disabled in the GUI<br/>and printing?"}
  DIS -->|yes| W(["Warning only"])
  DIS -->|no| CR{"unit.check_runout?"}
  CR -->|yes| HR["unit.handle_runout, if it has one"]
  HR --> H{"Handled by the unit?"}
  H -->|no| RL{"runout_lane set?"}
  RL -->|yes| IR["Infinite runout"]
  RL -->|no| PR["Pause runout"]
  CR -->|no| UL{"Not calibrating?"}
  UL -->|yes| SU["set_unloaded"]
```

## Where it lives in code

| Step | Code |
| --- | --- |
| Callback | `AFCLane.handle_load_runout` in `extras/AFC_lane.py` |
| Insert | [`AFCLane.set_loaded`](set_loaded.md), `afcUnit.on_filament_insert` in `extras/AFC_unit.py` |
| Direct load | [`afc.TOOL_LOAD`](../AFC/TOOL_LOAD.md) |
| Remove | `afcUnit.on_filament_remove` |
| Unit runout rules | `afcUnit.check_runout` and the unit's optional `handle_runout` |
| Runout | [`AFCLane._perform_infinite_runout`](_perform_infinite_runout.md), [`AFCLane._perform_pause_runout`](_perform_pause_runout.md) |
| Removal | [`AFCLane.set_unloaded`](set_unloaded.md) |

## Notes

- `_load_suppressed` marks a state change AFC caused itself, such as an eject, so that it does not look like a user inserting a spool.
- This method exists separately from [`AFCLane.load_callback`](load_callback.md) because it is called by the filament switch's runout helper, and the helper can delay it.

## Related

- [`AFCLane.load_callback`](load_callback.md)
- [`AFCLane._perform_infinite_runout`](_perform_infinite_runout.md)
