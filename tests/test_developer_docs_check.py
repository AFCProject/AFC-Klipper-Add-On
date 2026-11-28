# Unit tests for developer-docs/tools/check_docs.py

from __future__ import annotations

import ast
import importlib.util
from pathlib import Path

import pytest

TOOL_PATH = Path(__file__).resolve().parent.parent / "developer-docs" / "tools" / "check_docs.py"
_spec = importlib.util.spec_from_file_location("check_docs", TOOL_PATH)
check_docs = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(check_docs)

VALID_PAGE = """# `prep_callback`

**Class:** `AFCLane`
**File:** `extras/AFC_lane.py`

Summary.

## Flow

```mermaid
flowchart TD
  A["start"] --> B["done"]
```

## Where it lives in code

| Step | Code |
| --- | --- |
| Method | `AFCLane.prep_callback` |
"""

VALID_SOURCE = """class Base:
    def helper(self):
        self.base_attr = 1


class AFCLane(Base):
    LIMIT = 3

    def prep_callback(self):
        self.prep_state = False
"""


def _make_repo(tmp_path, page_text=VALID_PAGE, readme=None):
    extras = tmp_path / "extras"
    extras.mkdir()
    (extras / "AFC_lane.py").write_text(VALID_SOURCE)
    docs = tmp_path / "developer-docs"
    (docs / "AFC_lane").mkdir(parents=True)
    page = docs / "AFC_lane" / "prep_callback.md"
    page.write_text(page_text)
    if readme is None:
        readme = "[prep_callback](AFC_lane/prep_callback.md)\n"
    (docs / "README.md").write_text(readme)
    return tmp_path, docs, page


def _class_node(source):
    return ast.parse(source).body[0]


class TestSelfAttributes:
    def test_collects_stores_to_self(self):
        source = "class A:\n    def f(self):\n        self.x = 1\n        self.y: int = 2\n"
        node = _class_node(source)
        assert check_docs._self_attributes(node) == {"x", "y"}

    def test_ignores_reads_of_self(self):
        node = _class_node("class A:\n    def f(self):\n        return self.x\n")
        assert check_docs._self_attributes(node) == set()

    def test_ignores_stores_to_other_names(self):
        node = _class_node("class A:\n    def f(self, other):\n        other.x = 1\n")
        assert check_docs._self_attributes(node) == set()

    def test_ignores_nested_attribute_targets(self):
        node = _class_node("class A:\n    def f(self):\n        self.a.b = 1\n")
        assert check_docs._self_attributes(node) == set()


class TestClassLevelNames:
    def test_methods_and_assignments(self):
        source = ("class A:\n    X = 1\n    Y: int = 2\n    def f(self): pass\n"
                  "    async def g(self): pass\n")
        assert check_docs._class_level_names(_class_node(source)) == {"X", "Y", "f", "g"}

    def test_multiple_assignment_targets(self):
        assert check_docs._class_level_names(_class_node("class A:\n    X = Y = 1\n")) == {"X", "Y"}

    def test_tuple_targets_and_other_statements_are_skipped(self):
        source = 'class A:\n    """doc"""\n    a, b = 1, 2\n'
        assert check_docs._class_level_names(_class_node(source)) == set()

    def test_annotated_attribute_target_is_skipped(self):
        assert check_docs._class_level_names(_class_node("class A:\n    a.b: int = 1\n")) == set()


class TestBaseNames:
    def test_plain_and_dotted_bases(self):
        node = _class_node("class A(B, mod.C):\n    pass\n")
        assert check_docs._base_names(node) == ["B", "C"]

    def test_other_expressions_are_skipped(self):
        node = _class_node("class A(Generic[T]):\n    pass\n")
        assert check_docs._base_names(node) == []


class TestCollectCodeIndex:
    def test_indexes_classes_with_bases_and_members(self, tmp_path):
        extras = tmp_path / "extras"
        extras.mkdir()
        (extras / "AFC_lane.py").write_text(VALID_SOURCE)
        index = check_docs.collect_code_index(extras)
        bases, members = index["AFCLane"]
        assert bases == ["Base"]
        assert members == {"LIMIT", "prep_callback", "prep_state"}

    def test_same_class_name_in_two_files_is_merged(self, tmp_path):
        extras = tmp_path / "extras"
        extras.mkdir()
        (extras / "a.py").write_text("class X(P):\n    def one(self): pass\n")
        (extras / "b.py").write_text("class X(Q):\n    def two(self): pass\n")
        bases, members = check_docs.collect_code_index(extras)["X"]
        assert sorted(bases) == ["P", "Q"]
        assert members == {"one", "two"}

    def test_non_class_nodes_are_ignored(self, tmp_path):
        extras = tmp_path / "extras"
        extras.mkdir()
        (extras / "a.py").write_text("def f():\n    pass\nVALUE = 1\n")
        assert check_docs.collect_code_index(extras) == {}


class TestClassMembers:
    def test_includes_inherited_members(self):
        index = {"A": (["B"], {"a"}), "B": ([], {"b"})}
        assert check_docs.class_members(index, "A") == {"a", "b"}

    def test_unknown_class_has_no_members(self):
        assert check_docs.class_members({}, "Nope") == set()

    def test_unknown_base_is_skipped(self):
        assert check_docs.class_members({"A": (["Missing"], {"a"})}, "A") == {"a"}

    def test_inheritance_cycle_terminates(self):
        index = {"A": (["B"], {"a"}), "B": (["A"], {"b"})}
        assert check_docs.class_members(index, "A") == {"a", "b"}

    def test_shared_base_is_visited_once(self):
        index = {"A": (["B", "C"], set()), "B": (["D"], set()), "C": (["D"], set()),
                 "D": ([], {"d"})}
        assert check_docs.class_members(index, "A") == {"d"}


class TestFindPages:
    def test_skips_special_pages_and_sorts(self, tmp_path):
        (tmp_path / "AFC").mkdir()
        for name in ("README.md", "MAINTAINING.md", "_TEMPLATE.md"):
            (tmp_path / name).write_text("x")
        (tmp_path / "AFC" / "b.md").write_text("x")
        (tmp_path / "AFC" / "a.md").write_text("x")
        (tmp_path / "AFC" / "notes.txt").write_text("x")
        assert check_docs.find_pages(tmp_path) == [tmp_path / "AFC" / "a.md",
                                                   tmp_path / "AFC" / "b.md"]


class TestCheckMermaid:
    def test_clean_block(self):
        assert check_docs.check_mermaid('flowchart TD\n  A["x"] --> B["y"]\n') == []

    def test_empty_block(self):
        assert check_docs.check_mermaid("  \n") == ["empty mermaid block"]

    def test_unbalanced_quote(self):
        problems = check_docs.check_mermaid('flowchart TD\n  A["x] --> B["y"]\n')
        assert problems == ["mermaid line 2 has an unbalanced quote"]

    def test_lowercase_end_node_id(self):
        problems = check_docs.check_mermaid('flowchart TD\n  A --> end["done"]\n')
        assert problems == ["mermaid line 2 uses lowercase end as a node id"]

    def test_uppercase_end_is_fine(self):
        assert check_docs.check_mermaid('flowchart TD\n  A --> END(["done"])\n') == []

    def test_end_inside_a_word_is_fine(self):
        assert check_docs.check_mermaid('flowchart TD\n  A["send"] --> Bend["x"]\n') == []


class TestCheckLinks:
    def test_existing_relative_link(self, tmp_path):
        (tmp_path / "other.md").write_text("x")
        assert check_docs.check_links(tmp_path / "page.md", "[o](other.md)") == []

    def test_anchor_is_ignored(self, tmp_path):
        (tmp_path / "other.md").write_text("x")
        assert check_docs.check_links(tmp_path / "page.md", "[o](other.md#top)") == []

    def test_broken_link(self, tmp_path):
        assert check_docs.check_links(tmp_path / "page.md", "[o](gone.md)") == [
            "broken link gone.md"]

    def test_external_links_are_not_checked(self, tmp_path):
        text = "[a](https://x.y/z) [b](http://x.y) [c](mailto:a@b.c)"
        assert check_docs.check_links(tmp_path / "page.md", text) == []


class TestCheckCodeRefs:
    INDEX = {"AFCLane": ([], {"prep_callback"})}

    def test_existing_member(self):
        assert check_docs.check_code_refs("`AFCLane.prep_callback`", self.INDEX) == []

    def test_missing_member(self):
        problems = check_docs.check_code_refs("`AFCLane.gone`", self.INDEX)
        assert problems == ["`AFCLane.gone` does not exist in the code"]

    def test_unknown_first_part_is_skipped(self):
        assert check_docs.check_code_refs("`gcode_move.cmd_G1`", self.INDEX) == []

    def test_repeated_reference_is_reported_once(self):
        problems = check_docs.check_code_refs("`AFCLane.gone` and `AFCLane.gone`", self.INDEX)
        assert len(problems) == 1


class TestCheckPage:
    def _check(self, tmp_path, page_text=VALID_PAGE):
        repo, _docs, page = _make_repo(tmp_path, page_text)
        index = check_docs.collect_code_index(repo / "extras")
        return check_docs.check_page(page, repo, index)

    def test_valid_page_has_no_problems(self, tmp_path):
        assert self._check(tmp_path) == []

    def test_missing_class_header(self, tmp_path):
        text = VALID_PAGE.replace("**Class:** `AFCLane`", "Class: AFCLane")
        assert self._check(tmp_path, text) == [
            "AFC_lane/prep_callback.md: missing **Class:** header line"]

    def test_unknown_class(self, tmp_path):
        text = VALID_PAGE.replace("**Class:** `AFCLane`", "**Class:** `Nope`")
        assert self._check(tmp_path, text) == [
            "AFC_lane/prep_callback.md: class Nope not found in extras"]

    def test_function_not_in_class(self, tmp_path):
        text = VALID_PAGE.replace("# `prep_callback`", "# `gone`")
        repo, docs, _page = _make_repo(tmp_path, text)
        page = docs / "AFC_lane" / "gone.md"
        page.write_text(text)
        index = check_docs.collect_code_index(repo / "extras")
        assert check_docs.check_page(page, repo, index) == [
            "AFC_lane/gone.md: gone is not a member of AFCLane"]

    def test_inherited_function_counts_as_a_member(self, tmp_path):
        text = VALID_PAGE.replace("prep_callback", "helper")
        repo, docs, _page = _make_repo(tmp_path, text)
        page = docs / "AFC_lane" / "helper.md"
        page.write_text(text)
        index = check_docs.collect_code_index(repo / "extras")
        assert check_docs.check_page(page, repo, index) == []

    def test_missing_file_header(self, tmp_path):
        text = VALID_PAGE.replace("**File:** `extras/AFC_lane.py`\n", "")
        assert self._check(tmp_path, text) == [
            "AFC_lane/prep_callback.md: missing **File:** header line"]

    def test_file_does_not_exist(self, tmp_path):
        text = VALID_PAGE.replace("AFC_lane.py", "gone.py")
        assert self._check(tmp_path, text) == [
            "AFC_lane/prep_callback.md: file extras/gone.py does not exist"]

    def test_page_in_wrong_folder(self, tmp_path):
        repo, docs, _page = _make_repo(tmp_path)
        (docs / "AFC").mkdir()
        page = docs / "AFC" / "prep_callback.md"
        page.write_text(VALID_PAGE)
        index = check_docs.collect_code_index(repo / "extras")
        assert check_docs.check_page(page, repo, index) == [
            "AFC/prep_callback.md: page is in folder AFC, expected AFC_lane"]

    @pytest.mark.parametrize("section", ["## Flow", "## Where it lives in code"])
    def test_missing_section(self, tmp_path, section):
        problems = self._check(tmp_path, VALID_PAGE.replace(section, "## Other"))
        assert f"AFC_lane/prep_callback.md: missing section {section}" in problems

    def test_no_mermaid_diagram(self, tmp_path):
        text = VALID_PAGE.replace("```mermaid", "```text")
        assert self._check(tmp_path, text) == ["AFC_lane/prep_callback.md: no mermaid diagram"]

    def test_bad_mermaid_is_reported(self, tmp_path):
        text = VALID_PAGE.replace('A["start"]', 'A["start')
        assert self._check(tmp_path, text) == [
            "AFC_lane/prep_callback.md: mermaid line 2 has an unbalanced quote"]

    def test_em_dash(self, tmp_path):
        text = VALID_PAGE.replace("Summary.", "Summary \u2014 here.")
        assert self._check(tmp_path, text) == ["AFC_lane/prep_callback.md: contains an em-dash"]

    def test_bad_code_reference(self, tmp_path):
        text = VALID_PAGE + "| Other | `AFCLane.gone` |\n"
        assert self._check(tmp_path, text) == [
            "AFC_lane/prep_callback.md: `AFCLane.gone` does not exist in the code"]

    def test_broken_link(self, tmp_path):
        text = VALID_PAGE + "\n[x](missing.md)\n"
        assert self._check(tmp_path, text) == ["AFC_lane/prep_callback.md: broken link missing.md"]


class TestCheckIndex:
    def test_all_pages_linked(self, tmp_path):
        _repo, docs, page = _make_repo(tmp_path)
        assert check_docs.check_index(docs, [page]) == []

    def test_missing_readme(self, tmp_path):
        _repo, docs, page = _make_repo(tmp_path)
        (docs / "README.md").unlink()
        assert check_docs.check_index(docs, [page]) == ["README.md is missing"]

    def test_unlinked_page(self, tmp_path):
        _repo, docs, page = _make_repo(tmp_path, readme="nothing here\n")
        assert check_docs.check_index(docs, [page]) == [
            "README.md does not link AFC_lane/prep_callback.md"]

    def test_broken_readme_link(self, tmp_path):
        readme = "[prep_callback](AFC_lane/prep_callback.md) [x](AFC_lane/gone.md)\n"
        _repo, docs, page = _make_repo(tmp_path, readme=readme)
        assert check_docs.check_index(docs, [page]) == [
            "README.md: broken link AFC_lane/gone.md"]


class TestRunChecks:
    def test_clean_tree(self, tmp_path):
        repo, docs, _page = _make_repo(tmp_path)
        assert check_docs.run_checks(docs, repo) == []

    def test_collects_page_and_index_problems(self, tmp_path):
        text = VALID_PAGE.replace("Summary.", "Summary \u2014 here.")
        repo, docs, _page = _make_repo(tmp_path, text, readme="nothing\n")
        assert check_docs.run_checks(docs, repo) == [
            "AFC_lane/prep_callback.md: contains an em-dash",
            "README.md does not link AFC_lane/prep_callback.md"]


class TestMain:
    def test_clean_docs_return_zero(self, tmp_path, capsys):
        _repo, docs, _page = _make_repo(tmp_path)
        assert check_docs.main([str(docs)]) == 0
        assert capsys.readouterr().out == "checked 1 pages, 0 problems\n"

    def test_problems_are_printed_and_return_one(self, tmp_path, capsys):
        _repo, docs, _page = _make_repo(tmp_path, readme="nothing\n")
        assert check_docs.main([str(docs)]) == 1
        assert capsys.readouterr().out == (
            "README.md does not link AFC_lane/prep_callback.md\n"
            "checked 1 pages, 1 problems\n")

    def test_defaults_to_the_docs_next_to_the_tool(self, monkeypatch, capsys):
        seen = []
        monkeypatch.setattr(check_docs, "run_checks", lambda d, r: seen.append((d, r)) or [])
        monkeypatch.setattr(check_docs.sys, "argv", ["check_docs.py"])
        assert check_docs.main() == 0
        assert seen == [(TOOL_PATH.parent.parent, TOOL_PATH.parent.parent.parent)]
        assert "problems" in capsys.readouterr().out


class TestScriptEntryPoint:
    def test_running_the_file_exits_with_the_check_result(self, tmp_path, monkeypatch, capsys):
        import runpy
        _repo, docs, _page = _make_repo(tmp_path)
        monkeypatch.setattr("sys.argv", ["check_docs.py", str(docs)])
        with pytest.raises(SystemExit) as exit_info:
            runpy.run_path(str(TOOL_PATH), run_name="__main__")
        assert exit_info.value.code == 0
        assert capsys.readouterr().out == "checked 1 pages, 0 problems\n"
