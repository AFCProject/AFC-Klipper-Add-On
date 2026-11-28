# `do_tool_cut_tip_form`

**Class:** `afc`  
**File:** `extras/AFC.py`

Runs the optional cut, park and tip-forming macros during an unload.

## Flow

```mermaid
flowchart TD
  S(["do_tool_cut_tip_form"]) --> TC{"tool_cut enabled?"}
  TC -->|yes| C1["Count a cut<br/>run tool_cut_cmd EXTRUDER="]
  C1 --> P1{"park enabled?"}
  P1 -->|yes| C2["run park_cmd EXTRUDER="]
  P1 -->|no| FT
  C2 --> FT
  TC -->|no| FT{"form_tip enabled?"}
  FT -->|no| E(["Done"])
  FT -->|yes| P2{"park enabled?"}
  P2 -->|yes| C3["run park_cmd EXTRUDER="]
  P2 -->|no| WHO
  C3 --> WHO{"form_tip_cmd is AFC?"}
  WHO -->|yes| T1["AFC_form_tip object: tip_form"]
  WHO -->|no| T2["run the custom form_tip_cmd"]
  T1 --> E
  T2 --> E
```

## Where it lives in code

| Step | Code |
| --- | --- |
| Method | `afc.do_tool_cut_tip_form` in `extras/AFC.py` |
| Cut count | `AFCExtruderStats.increase_cut_total` in `extras/AFC_extruder.py` |
| Macros | `afc.tool_cut_cmd`, `afc.park_cmd`, `afc.form_tip_cmd`, run with `afc.run_script` |
| Built-in tip forming | `AFC_form_tip` object in `extras/AFC_form_tip.py` |
| Position checks | [`afc.verify_macro_positions`](verify_macro_positions.md) |

## Notes

- Each step is only a macro call, so users can replace any of them in `AFC.cfg`.
- Park runs after the cut and again before tip forming, so the toolhead is at the park position for both.

## Related

- [`afc.unload_sequence`](unload_sequence.md)
