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
   `float = None`.
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
- **Docstring every method** — a short summary line, then a `:param name:`
   line for each parameter and a `:return type:` (or `:return:`) line for any
   non-`None` return value, matching the Sphinx-style convention already used
   throughout `extras/` (e.g. `AFC_lane.py`, `AFC_assist.py`). Skip the
   `:return:` line when the method returns `None`. A method that raises uses
   a plain `raises error if ...` line rather than a `:raises:` tag, matching
   existing convention.

   ```python
   def _feed_channel_present(self):
       """
       Checks whether the feeder currently reports filament present in this
       lane's channel.

       :return bool: True when the feeder reports filament present, False if
                     absent, not found, or the feeder can't be queried
       """
   ```
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
   attributes to skip constructor logic entirely. This applies to new tests
   even in a file where older tests already use `__new__` — don't migrate
   the untouched older tests to match unless asked.
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
