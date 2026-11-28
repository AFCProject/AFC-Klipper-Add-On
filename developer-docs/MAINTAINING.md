# Maintaining the developer docs

These pages describe how AFC's code behaves, one function per page. They only stay useful if they change when the code changes. This guide says how.

## What belongs here

A page explains what a function does, in what order, and where each step lives in the code. It does not explain how to use AFC or what a setting means. Those belong in the user documentation.

If a page needs a long paragraph to explain something, the explanation probably belongs in a code comment or docstring instead, and the page should point at it.

## Layout

```
developer-docs/
  README.md             index, one row per page
  MAINTAINING.md        this guide
  _TEMPLATE.md          copy this to start a page
  tools/check_docs.py   consistency checker
  AFC/                  one folder per module in extras/, named after the file
    CHANGE_TOOL.md      one file per function, named after the function
  AFC_lane/
  ...
```

- The folder is the file name in `extras/` without `.py`. `extras/AFC_lane.py` is `AFC_lane/`.
- The page is the function or method name, with its leading underscore if it has one.
- Pages today cover one class per file. If a second class in a file needs pages, the header names that class and the page keeps the method name, and the checker looks the method up on whichever class the header names.

## When you change code, update the docs

| You changed | Update |
| --- | --- |
| The order of steps, or a branch, in a function | The diagram, the "Where it lives in code" rows, and any note it affects |
| A new error path, retry loop or limit | The diagram, and a note naming the setting or constant |
| A function's name, or the file or class it lives in | Rename the page and folder, fix the header, links in other pages, and the README row |
| A function was removed | Delete the page and remove it from the README and from other pages' tables |
| A function was added to an existing flow | Copy `_TEMPLATE.md`, write the page, add it to the README, and add a row to the pages that call it |
| A new unit type overrides `prep_load`, `eject_lane` and so on | Add pages in a folder named after its file, as `AFC_BoxTurtle/` does |
| A TODO or known quirk mentioned in a note was fixed | Remove or reword the note |

To find every page that mentions something, search for it:

```
grep -rl "load_sequence" developer-docs
```

Each page's "Where it lives in code" table lists the functions it uses, and "Related" lists the neighbors, so a caller or callee of what you changed is usually one link away.

## Checking your changes

Run this from the repository root before you push:

```
python developer-docs/tools/check_docs.py
```

It checks that:

- every page has its `**Class:**` and `**File:**` header, the file exists, the page is in the right folder, and the page's function exists in that class, counting inherited methods
- every `` `Class.member` `` in backticks exists in the code, for any class defined in `extras/`, including attributes set as `self.name` in a method
- every relative link points at a file, and the README links every page
- every page has a `## Flow` and a `## Where it lives in code` section, and at least one mermaid diagram
- mermaid blocks have balanced quotes and do not use lowercase `end` as a node id
- there are no em-dashes

It does not check that a diagram is correct, or that prose is true. That is a review job. The tool has its own tests: `pytest tests/test_developer_docs_check.py`.

### Check that diagrams render

GitHub renders mermaid in the preview of a pull request or a branch. To check syntax without pushing, paste a diagram into <https://mermaid.live>. To check every diagram at once, make a scratch folder outside the repository and parse them with mermaid itself:

```
mkdir /tmp/mmcheck && cd /tmp/mmcheck
npm init -y && npm install mermaid@10.9.3 jsdom@24
```

Save this as `/tmp/mmcheck/check.mjs`:

```js
import { JSDOM } from "jsdom";
import fs from "fs";
import path from "path";
const dom = new JSDOM("<!doctype html><html><body></body></html>");
globalThis.window = dom.window;
globalThis.document = dom.window.document;
Object.defineProperty(globalThis, "navigator", { value: dom.window.navigator, configurable: true });
const mermaid = (await import("mermaid")).default;
mermaid.initialize({ startOnLoad: false });
const files = [];
(function walk(d) {
  for (const f of fs.readdirSync(d, { withFileTypes: true })) {
    const p = path.join(d, f.name);
    if (f.isDirectory()) walk(p); else if (p.endsWith(".md")) files.push(p);
  }
})(process.argv[2]);
let bad = 0, total = 0;
for (const f of files) {
  for (const m of fs.readFileSync(f, "utf8").matchAll(/```mermaid\n([\s\S]*?)```/g)) {
    total++;
    try { await mermaid.parse(m[1]); }
    catch (e) { bad++; console.log("FAIL", f, String(e.message).split("\n")[0]); }
  }
}
console.log(`checked ${total} diagrams, ${bad} failed`);
```

Then run it with the full path to the docs:

```
node check.mjs /path/to/AFC-Klipper-Add-On/developer-docs
```

This catches syntax errors only. It does not show how a diagram looks.

## Page conventions

Copy `_TEMPLATE.md`. The parts, in order:

1. **Title and header.** The title is the function name in backticks. The header gives the class and the file, so a reader knows where to open.
2. **Summary.** One or two sentences: what the function is for, not how it works.
3. **Flow.** One mermaid diagram.
4. **Where it lives in code.** A two column table, `Step` and `Code`. Each row names the function, class or attribute behind one step.
5. **Notes.** Only behavior that is easy to miss: limits, ordering that matters, side effects, why a branch exists, known quirks. Do not repeat the diagram in words.
6. **Related.** Links to the pages a reader will want next.

### Pointing at code

- Name code as `Class.member` in backticks, for example `` `AFCLane.set_tool_loaded` ``. The checker verifies these.
- Add the file when a reference is the first for that module on the page, for example `` `afc.TOOL_LOAD` in `extras/AFC.py` ``.
- Link to another page with a relative link: `[`afc.TOOL_LOAD`](TOOL_LOAD.md)` in the same folder, `[`afcUnit.move_to_hub`](../AFC_unit/move_to_hub.md)` in another.
- Do not use line numbers. They go stale on the next edit.
- For an object that is not a class in `extras/`, such as `gcode_move.move_with_transform` or `heaters`, backticks are enough. The checker skips them.
- Class names in this code base are not uniform: `afc`, `afcUnit`, `afcFunction`, `afcError`, `AFCLane`, `AFCExtruder`, `AfcToolchanger`. Copy the name from the class line in the file.

### Diagrams

- Use `flowchart TD`. Wide diagrams are hard to read on a phone and in a pull request.
- Rounded boxes `([...])` are starts and ends. Diamonds `{...}` are decisions. Rectangles `[...]` are actions.
- Put every label in double quotes, and use `<br/>` for line breaks. Unquoted labels break on parentheses and equals signs.
- Label every decision edge, `-->|yes|` and `-->|no|`.
- Do not use lowercase `end` as a node id. Mermaid reads it as the end of a block. Use `END` or `Done`.
- Define a node where it first appears, and refer to it by id afterwards.
- Keep a diagram to about 25 nodes. If it needs more, the function is probably two steps and wants two pages, with the inner one linked from the outer.
- Name the exact thing a step does when it matters, such as a setting or limit (`tool_max_unload_attempts`, "fails after 20 tries"). Those are the details people come looking for.

### Style

- No em-dashes. Use a period, comma or colon.
- Write what the code does and why. List callers in the "Where it lives in code" table or "Related", where links keep them current. A note may name a caller when that explains why the function behaves as it does.
- Say when something is unit specific. These pages describe the shared flow, and units override parts of it.

## Index

`README.md` has one table per module. When you add a page, add a row in the same format, and the checker fails until you do. The "Start here" table at the top is hand written. Add a row there only for a flow a developer would look for by action, such as "ejecting a spool".

## What these pages describe

They describe the code on the branch they were written against, which was `DEV`. When a flow differs between branches, the page says so. If you change a flow on a branch, update the page on that branch, so the two stay together.
