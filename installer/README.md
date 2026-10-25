# AFC installer

The code behind `install-afc.sh`, `update-afc.sh` and the Snapmaker U1 firmware script. Those
three scripts only find a Python 3.8+ interpreter (`include/py_installer.sh`) and run
`python -m installer install|update|u1`. Everything else lives here.

It uses only the standard library and runs on Python 3.8 and newer. `rich` is optional: if it is
installed and the output is a terminal, menus are drawn with it, otherwise with plain ANSI.

## Layout

* `cli.py`, `app.py`, `__main__.py`: flag parsing, the install / update / U1 programs, dispatch.
* `startup.py`, `git_sync.py`: the checks before the menus, branch switching, self update.
* `actions.py`: the functions the menus call (`RealActions`), wired to the flows.
* `platform_core/`: `Context` (all settings), console and prompt I/O, command runner, file
  helpers.
* `config_editor/`: edits to Klipper style `.cfg` files (set a value, add an include, delete a
  section).
* `checks/`: preflight checks (root, Happy Hare, Python version, ZIP install, existing install).
* `unit_registry/`: the unit types and everything a unit can choose (options, buffers, naming).
* `install_flow/`, `update_flow/`: what install, uninstall, update and the Snapmaker actions do.
* `ui_menus/`: menus, the screen description they build, art and the two renderers.

A run goes: shim, `app.py`, `startup.py` checks, then the menus. Menus never do the work
themselves; they call `RealActions`, which calls the flows.

## Adding a new unit type

A unit type is one class. The menus, the type picker and the buffer prompts read everything from it.

1. **Add the config files.** Put the unit's template under `templates/` and its MCU file under
   `config/mcu/` if it has one. The class below copies them.

2. **Write the class** in `unit_registry/units/<name>.py`, subclassing `UnitType`
   (`ViViD` is the smallest example):

   ```python
   class MyUnit(UnitType):
       display_name = "MyUnit"            # shown in the type picker
       key = "MyUnit"                     # also the banner file name
       additional_name_prefix = "MyUnit"  # default names MyUnit_1, MyUnit_2...
       # default_name is "<prefix>_1" (MyUnit_1) unless you override the property

       def copy_files(self, ctx, console, prompter, sel) -> None:
           ...                            # REQUIRED: copy files for a first install
   ```

   Only `copy_files` is required. Override the rest as needed:

   * `install_additional()`: copy files when adding another unit of this type, renaming `X_1` to
     `sel.unit_name`.
   * `message()`: return the follow-up text shown after an install.
   * `buffer_target()`: say where a PSF or TurtleNeck buffer is wired in (a `BufferTarget`).
   * `install_options()` / `additional_options()`: list the choices on the options screens (see
     below).
   * `additional_buffer_supported`, `additional_buffer_options`, `additional_buffer_default`:
     control the "own buffer?" choice when adding a unit.
   * `buffer_selectable`, `ramming_hardware`: hide the buffer choice, or change the ramming
     wording.
   * `on_select()`, `after_sensor_change()`, `cycle_buffer()`: adjust the buffer when the type is
     picked or the sensor / ramming choice flips.

   Copy files with `safe_copy(ctx, console, prompter, source, destination)`. It asks before
   replacing anything. Edit config with the helpers in `config_editor/` (below), never with text
   tricks.

3. **Register it** by adding `MyUnit()` to `UNIT_TYPES` in `unit_registry/registry.py`. The order
   of that list is the number shown in the type picker.

4. **Optional banner:** add `ui_menus/art/MyUnit.txt`, named after `key`. Use `\e` for the escape
   character (for example `\e[38;5;75m`). A missing file just prints no banner.

5. **Tests:** add `tests/installer/test_unit_registry_myunit.py`. Tests that loop over
   `UNIT_TYPES` (options, picker, buffers) pick the new type up on their own.

## Adding or changing a choice

### A choice that belongs to one unit type (board, motor, lane count, name)

Add an option object to that unit's `install_options()` and / or `additional_options()`. They show
up on the numbered options screens in the order you list them.

```python
def install_options(self) -> List[UnitOption]:
    return [
        NameOption("MyUnit Name"),
        ChoiceOption("MyUnit Board Type", "myunit_board_type", MYUNIT_BOARD_TYPES),
    ]
```

* `ChoiceOption(label, attr, choices, cast=str)` steps through `choices` each time it is chosen.
  `attr` is a field on `UnitSelection`. Pass `cast=int` for numeric values.
* `NameOption(label, additional_prompt=False)` asks for the unit name. Use
  `additional_prompt=True` for the prompt that offers the lowest free name (always for
  `additional_options()`).
* For anything else (a free text value, a yes / no), subclass `UnitOption` in
  `unit_registry/options.py` and implement `value(sel)` and `activate(...)`.

You also need to:

1. Put the list of values in `unit_registry/options.py` (next to `HTLF_BOARD_TYPES` and friends).
2. Add the field, with its default, to `UnitSelection` in `unit_registry/selection.py`.
3. Use `sel.<field>` in the unit's `copy_files()` / `install_additional()`.

### A setting that applies to every install (like the macro toggles)

* The yes / no toggles are one table, `TOGGLES` in `platform_core/toggles.py`: key, menu label,
  default, and the `AFC.cfg` option it sets. The menu, the defaults and `install_afc()` all read it.
* To add one, add a `Toggle(...)` row. Give it a `config_key` and `install_afc()` writes it with
  `set_value(ctx.afc_file, config_key, str(enabled))`. With no `config_key`, read
  `options.toggles["<key>"]` in `install_afc()` and act on it there.

### A setting shown on the main menu (paths, service name, address)

These are rows 1 to 6 on the main menu. Rows that can be edited are listed in `SETTINGS` in
`ui_menus/settings.py`: a short name and a function `set_x(ctx, text) -> message` that raises
`ValueError("reason")` for bad input. The branch row is read only because changing it needs a
restart.

## Adding a menu or a screen

* A menu is a class with `step()` that draws, reads one choice and returns the next menu's name
  (its own name to stay, `None` to stop). Look at `ui_menus/unit_type_menu.py`.
* Register it in `MENUS` in `ui_menus/menus.py`. Menus are created fresh each time you navigate to
  them, so per-visit state belongs on the object.
* A screen is described by a `MenuView` (`ui_menus/view.py`): art name, intro rows, the two boxes,
  a message and the option rows. Build rows with `row("text", ("red text", "red"))`. Colors are
  names (`red`, `green`, `bold_green`); the renderers decide how to show them.
* Box lines must be exactly `BOX_WIDTH - 2` columns wide. Use `centered("text")` and a test
  fails if any box is the wrong width.
* Menus never import the install or update flows. Anything that does real work goes through the
  `Actions` interface in `ui_menus/state.py`, implemented by `actions.py`.

## Editing config files

Use `config_editor/` instead of reading and rewriting text yourself. It keeps comments, blank
lines and line endings, and saves atomically:

* `set_value(path, key, value)`: set `key: value` wherever the key appears.
* `manage_include(path, "add" or "remove")`: add or remove the AFC include in `printer.cfg`.
* `set_key_in_section`, `replace_section_block`, `remove_section_block`: work on one `[section]`.
* `delete_section`, `delete_key`: remove a section or an option.
* `replace_text`, `replace_text_in_tree`: replace literal text in a file or a folder.
* `comment_range`, `uncomment_range`: comment out or restore a range of lines.

## Talking to the user and the system

* Print with `console.print_msg(kind, text)` (`INFO`, `WARNING`, `ERROR`, anything else is plain).
* Ask with `prompter.ask(prompt)`. It returns `None` at end of input; most callers then raise
  `InstallerExit(1)`.
* Run commands through the `CommandRunner` (`runner.run([...])`, always an argument list). Never
  build shell strings. Tests swap in `FakeCommandRunner`, so nothing real runs.
* To stop the installer, raise `InstallerExit(code)`. `app.py` turns it into the exit status.

## Testing

```
.venv/bin/python -m pytest tests/installer -q          # installer tests
.venv/bin/python -m pytest tests/installer --cov=installer --cov-branch
```

* `rich` is in `requirements.txt` so its tests run in CI. To force the plain ANSI screens even
  where `rich` is installed and the output is a terminal, set `AFC_NO_RICH=1` (testing only; the
  end to end tests set it). `0` or an empty value leaves `rich` on.
* Every new method needs a test; the project aims for 100% branch coverage of `installer/`.
* `tests/installer/fakes.py` has `RecordingConsole`, `ScriptedPrompter` (a list of answers) and
  `FakeCommandRunner`. Build objects through the real constructors.
* `tests/installer/e2e/` runs the real `install-afc.sh` and `update-afc.sh` in a sandbox with
  stubbed `git`, `sudo` and `systemctl`, feeding answers on stdin, and checks the files left
  behind. Add a scenario there for anything that changes what gets written.
* `tests/installer/fixtures/` holds recorded expected outputs (`unit_cases/` for each unit type,
  `config_editor/` for the file editors). Changing one is a deliberate change of behavior.
* The code must work on Python 3.8: no `X | Y` types, no `str.removeprefix`, and `from __future__
  import annotations` at the top of each module.
* Style rules (line length, docstrings, typing) are in `AGENTS.md`. Check with
  `ruff check --config pyproject.toml` and `mypy installer`.
