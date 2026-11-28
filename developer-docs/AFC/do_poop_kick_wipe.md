# `do_poop_kick_wipe`

**Class:** `afc`  
**File:** `extras/AFC.py`

Runs the purge, kick and wipe macros after a load. Each one only runs if the user enabled it.

## Flow

```mermaid
flowchart TD
  S(["do_poop_kick_wipe"]) --> PO{"poop enabled?"}
  PO -->|yes| P1["run poop_cmd EXTRUDER=<br/>with PURGE_LENGTH if one was given"]
  PO -->|no| KI
  P1 --> W1{"wipe enabled?"}
  W1 -->|yes| W1R["run wipe_cmd"]
  W1 -->|no| KI
  W1R --> KI{"kick enabled?"}
  KI -->|yes| K1["run kick_cmd"]
  KI -->|no| W2
  K1 --> W2{"wipe enabled?"}
  W2 -->|yes| W2R["run wipe_cmd"]
  W2 -->|no| WT["Wait for moves to finish"]
  W2R --> WT["Wait for moves to finish"]
  WT --> NP(["Clear lane.need_purge"])
```

## Where it lives in code

| Step | Code |
| --- | --- |
| Method | `afc.do_poop_kick_wipe` in `extras/AFC.py` |
| Macros | `afc.poop_cmd`, `afc.kick_cmd`, `afc.wipe_cmd`, run with `afc.run_script` |
| Built-in macros | `AFC_POOP`, `AFC_KICK`, `AFC_BRUSH` in `config/macros/`, variables in `AFC_Macro_Vars.cfg` |
| Position checks | [`afc.verify_macro_positions`](verify_macro_positions.md) |

## Notes

- With poop and wipe both on, wipe runs twice: once straight after the purge and once after the kick.
- `lane.need_purge` is always cleared at the end, even when poop is off.
- [`afc.load_sequence`](load_sequence.md) does not call this. [`afc.TOOL_LOAD`](TOOL_LOAD.md) does, so a gear-only load never purges.

## Related

- [`afc.TOOL_LOAD`](TOOL_LOAD.md)
