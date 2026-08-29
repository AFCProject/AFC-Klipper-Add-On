# Code Writing / Editing Rules

- **Lines ≤100 characters** — break them up if they exceed that.
- **Multi-condition boolean logic**: one comparison per line; continuation
   lines start with the comparator (`and`/`or`, etc.), aligned with the
   content right after the opening parenthesis.

   ```python
   if (self.enable and has_stepper
       and not getattr(self, "_correction_running", False)):
   ```
- **Match existing formatting** as closely as possible — follow the
   surrounding file's conventions rather than imposing a different style.
- **Correct typing** — including things like `Optional[float]` instead of
   `float = None`. Methods should be fully annotated (parameters and return
   type). This applies to actual code (`extras/*.py` and similar); unit test
   methods may be annotated too, but it's optional there, not required.
   Local variables and attributes only need an explicit annotation when a
   linter/type checker (e.g. mypy) can't infer the type on its own — for
   example an attribute first assigned `None` and later assigned a concrete
   type elsewhere, or an empty `{}`/`[]` whose element type isn't obvious
   from that line alone.
- **Use f-strings, not `str.format()`** — `f"Lane {self.name}"` rather than
   `"Lane {}".format(self.name)`.
- **Format error/exception strings before raising, not inline** — build the
   message into a variable first, then raise it as its own statement, rather
   than constructing the string inside the `raise` call.

   ```python
   # Bad
   raise error(f"Lane {self.name}: [filament_feed {self.feed_module}] not found")

   # Good
   error_str = f"Lane {self.name}: [filament_feed {self.feed_module}] not found"
   raise error(error_str)
   ```
- **Keep comments short and succinct** — a sentence or two at most. Comments
   still need to make sense to the developer reading them, just don't let
   them turn into paragraphs.
- **No em-dashes.** Use a period, comma, or colon instead when breaking up a
   clause, in comments, docstrings, commit messages, or any other prose. This
   applies to new code and documentation, and to existing documentation while
   it's being edited (clean up any em-dashes in a doc you're already touching,
   but don't go sweep unrelated files just for this).
- **Docstrings** — every new or changed method needs a docstring matching
   Sphinx-style convention in this shape: a short description, a blank line,
   then one `:param name:` per parameter (skip `self`) and a `:return type:`
   line if the method returns something meaningful (omit it otherwise).
   No blank line between the `:param`/`:return` block and the closing `"""`.

   ```python
   def register_lane_macros(self, lane_obj: AFCLane):
       """
       Callback function to register macros with proper lane names.

       :param lane_obj: object for lane to register
       """
   ```

   `cmd_*` gcode-command handlers follow a different, existing convention
   instead — description, then `Usage`/`Example` sections — see
   `cmd_AFC_QUIET_MODE` in `extras/AFC.py` for the pattern to match:

   ````python
   def cmd_AFC_QUIET_MODE(self, gcmd):
       """
       Set lower speed on any filament moves.

       Mainly this would be used to turn down motor noise during quiet runs.

       Usage
       -------
       `AFC_QUIET_MODE SPEED=<new quietmode speed> ENABLE=<1 or 0>`

       Example
       -------
       ```
       AFC_QUIET_MODE SPEED=75 ENABLE=1
       ```
       """
   ````

   Test methods (`tests/test_*.py`) are exempt from this rule — a docstring
   is fine if it adds real context, but it's optional, not required. Test
   method names are already descriptive enough on their own
   (`test_success_after_one_retry_uses_resolved_lane_name`), so don't add
   one just to satisfy this rule.
- **All new code needs unit tests** — any code added must come with unit
   tests that follow the Unit Test Rules below.

# Unit Test Rules

- **Verify class variable changes** — if a test exercises code that changes
   an attribute, the test must assert on that attribute's new value, not
   just that a method ran.
- **Verify all log messages** — every log call in the method under test
   needs an assertion on its content, not just "a log fired." Assert the
   exact message list (e.g. `logger.messages == [("info", "...")]`), not
   `("info", "...") in logger.messages` — an `in` check still passes if
   other, unexpected messages also fired.
- **Full branch coverage** — every branch a method can take needs a test,
   including edge cases that only show up as partial/missed branches in
   coverage tooling (not just missed statements). Run coverage with branch
   tracking enabled (`--cov-branch` for pytest-cov), not just line
   coverage — a method can show 100% on line coverage while a real branch
   (e.g. a condition only reachable with `NaN`, or an early-return path) is
   never exercised.
- **Coverage scope** — new or changed code needs full branch coverage.
   Pre-existing untested code elsewhere in the same file is out of scope
   unless the task specifically asks for it; don't expand scope to "fix"
   unrelated untested code without being asked.
- **Inline (ternary) conditionals need both-branch coverage too, verified
   by hand** — `coverage.py`'s branch tracking does not flag an inline
   `a if cond else b` expression as partially covered the way it does a
   full `if`/`else` statement, even when only one outcome was ever
   exercised. A file can show 100% branch coverage while every ternary in
   it only ever took one side. Don't trust the coverage report for these —
   for each inline conditional in new/changed code, find the test(s) that
   exercise it and confirm by reading them that one drives the condition
   truthy and another drives it falsy, each with an assertion that would
   fail under the other outcome.
- **Multi-condition `if` statements: test each variable independently** —
   for something like `if A and B:`, there need to be tests proving A alone
   can't satisfy the condition and B alone can't either, not just one test
   where both happen to be true together.
- **One test class per method** — every method on the class under test needs
   its own dedicated test class (or clearly delimited section for
   module-level functions), named after the method
   (`_apply_staged` → `TestAFCU1LaneApplyStaged`). This makes it possible to
   scan a test file and immediately see which methods have no coverage at
   all, rather than relying on line/branch coverage tooling alone — a method
   invoked only as a side effect of testing a caller can hit 100% branch
   coverage while never being independently verified. When a method gains a
   new method (or a class gains one), add its test class in the same change.
- **No `__new__` bypass for construction** — build test objects through the
   real `__init__` (mocking dependencies like config/printer/reactor as
   needed), rather than using `SomeClass.__new__(SomeClass)` and hand-setting
   attributes to skip constructor logic entirely.
- **Assertions must actually distinguish the branch under test** — an
   assertion has to be something that would be *false* if the code had
   taken the other branch, not just something that happens to be true
   either way.

   ```python
   # Bad: passes whether or not the early return actually fired, since
   # break_espooler() was never going to be called for a positive value
   # regardless.
   espooler.assist(1.0)
   espooler.break_espooler.assert_not_called()

   # Good: proves the early return specifically fired, via state that
   # would have changed had execution continued past it.
   espooler.assist(1.0)
   assert espooler.afc_motor_fwd.last_value == original_fwd_value
   ```
- **Compute expected values independently** — when asserting a computed
   number or formatted string, re-derive the expected value separately in
   the test (re-implement the formula, or hand-build the format string)
   rather than mirroring the source's own formula/format spec. Otherwise
   the test just re-runs the same logic and can't catch a wrong formula.
- **Don't reference line numbers in test comments or docstrings** — code
   moves around as a file is edited, so a comment like "see line 668" or
   "covers the branch at line 245" goes stale the moment anything above it
   shifts. Point to the method/function name, the specific condition, or
   quote the relevant snippet instead.

These apply together — for example, a test written to satisfy the
multi-condition independence rule still has to satisfy the class-variable,
logging, and branch-coverage rules for that same test.

# Pull Request Rules

- **AI/LLMs must not write the PR description.** The user has to fill out
   `.github/pull_request_template.md` themselves, in their own words, matching
   the template's sections and checklist. Do not draft, generate, or rewrite
   a PR title/description on their behalf, even if asked to summarize the
   changes for them to use elsewhere, that summary is not a substitute for
   the user completing the template.
