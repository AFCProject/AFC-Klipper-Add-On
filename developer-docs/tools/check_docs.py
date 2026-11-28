# AFCProject Automated Filament Changer
#
# Copyright (C) 2026 AFCProject
#
# This file may be distributed under the terms of the GNU GPLv3 license.

"""
Checks the developer-docs pages against the code they describe.

Run it from the repository root after changing `extras/` or a page:

    python developer-docs/tools/check_docs.py

It exits with 1 and prints one line per problem, so it can also run in CI.
"""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

SPECIAL_PAGES = {"README.md", "MAINTAINING.md", "_TEMPLATE.md"}
REQUIRED_SECTIONS = ("## Flow", "## Where it lives in code")
CODE_REF = re.compile(r"`([A-Za-z_][A-Za-z0-9_]*)\.([A-Za-z_][A-Za-z0-9_]*)`")
HEADER_CLASS = re.compile(r"^\*\*Class:\*\* `([^`]+)`", re.M)
HEADER_FILE = re.compile(r"^\*\*File:\*\* `([^`]+)`", re.M)
MD_LINK = re.compile(r"\[[^\]]*\]\(([^)#\s]+)(?:#[^)]*)?\)")
MERMAID_BLOCK = re.compile(r"```mermaid\n(.*?)```", re.S)
RESERVED_END = re.compile(r"(^|[\s;])end(\[|\(|\{|\s|$)")

# Class name to (base class names, member names)
CodeIndex = Dict[str, Tuple[List[str], Set[str]]]


def _self_attributes(node: ast.ClassDef) -> Set[str]:
    """
    Names assigned as `self.<name>` anywhere in a class body.

    :param node: class definition to scan
    :return Set[str]: attribute names set on instances
    """
    names: Set[str] = set()
    for child in ast.walk(node):
        if (isinstance(child, ast.Attribute)
            and isinstance(child.value, ast.Name)
            and child.value.id == "self"
            and isinstance(child.ctx, ast.Store)):
            names.add(child.attr)
    return names


def _class_level_names(node: ast.ClassDef) -> Set[str]:
    """
    Methods and class level assignments of a class body, enum members included.

    :param node: class definition to scan
    :return Set[str]: names defined directly in the class body
    """
    names: Set[str] = set()
    for stmt in node.body:
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
            names.add(stmt.name)
        elif isinstance(stmt, ast.Assign):
            names.update(t.id for t in stmt.targets if isinstance(t, ast.Name))
        elif (isinstance(stmt, ast.AnnAssign)
              and isinstance(stmt.target, ast.Name)):
            names.add(stmt.target.id)
    return names


def _base_names(node: ast.ClassDef) -> List[str]:
    """
    Simple names of a class's bases.

    :param node: class definition to scan
    :return List[str]: base class names, attribute bases reduced to their last part
    """
    bases: List[str] = []
    for base in node.bases:
        if isinstance(base, ast.Name):
            bases.append(base.id)
        elif isinstance(base, ast.Attribute):
            bases.append(base.attr)
    return bases


def collect_code_index(extras_dir: Path) -> CodeIndex:
    """
    Parse every module in `extras_dir` and index its classes.

    :param extras_dir: directory holding the AFC modules
    :return CodeIndex: for each class name, its bases and all member names
    """
    index: CodeIndex = {}
    for path in sorted(extras_dir.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef):
                continue
            bases, members = index.setdefault(node.name, ([], set()))
            bases.extend(_base_names(node))
            members.update(_class_level_names(node))
            members.update(_self_attributes(node))
    return index


def class_members(index: CodeIndex, name: str) -> Set[str]:
    """
    All member names of a class, including those inherited from indexed bases.

    :param index: result of `collect_code_index`
    :param name: class name to look up
    :return Set[str]: member names, empty when the class is not indexed
    """
    members: Set[str] = set()
    pending = [name]
    seen: Set[str] = set()
    while pending:
        current = pending.pop()
        if (current in seen
            or current not in index):
            continue
        seen.add(current)
        bases, own = index[current]
        members.update(own)
        pending.extend(bases)
    return members


def find_pages(docs_dir: Path) -> List[Path]:
    """
    Every function page below `docs_dir`, leaving out the index, guide and template.

    :param docs_dir: the developer-docs directory
    :return List[Path]: page paths in sorted order
    """
    return sorted(p for p in docs_dir.rglob("*.md") if p.name not in SPECIAL_PAGES)


def check_mermaid(block: str) -> List[str]:
    """
    Cheap syntax checks on one mermaid block. It does not replace a real render.

    :param block: text inside a mermaid fence
    :return List[str]: problems found
    """
    problems: List[str] = []
    if not block.strip():
        problems.append("empty mermaid block")
    for number, line in enumerate(block.split("\n"), 1):
        if line.count('"') % 2:
            problems.append(f"mermaid line {number} has an unbalanced quote")
        if RESERVED_END.search(line):
            problems.append(f"mermaid line {number} uses lowercase end as a node id")
    return problems


def check_links(page: Path, text: str) -> List[str]:
    """
    Check that every relative markdown link in a page points at a file.

    :param page: page the text came from, used to resolve relative links
    :param text: page text
    :return List[str]: problems found
    """
    problems: List[str] = []
    for target in MD_LINK.findall(text):
        if target.startswith(("http://", "https://", "mailto:")):
            continue
        if not (page.parent / target).resolve().exists():
            problems.append(f"broken link {target}")
    return problems


def check_code_refs(text: str, index: CodeIndex) -> List[str]:
    """
    Check every `Class.member` in backticks against the code index. A first part that is
    not a class name, such as `gcode_move.cmd_G1`, is skipped.

    :param text: page text
    :param index: result of `collect_code_index`
    :return List[str]: problems found
    """
    problems: List[str] = []
    for cls, member in sorted(set(CODE_REF.findall(text))):
        if (cls in index
            and member not in class_members(index, cls)):
            problems.append(f"`{cls}.{member}` does not exist in the code")
    return problems


def check_page(page: Path, repo_root: Path, index: CodeIndex) -> List[str]:
    """
    Run every check on one page.

    :param page: page to check
    :param repo_root: repository root, used to find the source file named in the header
    :param index: result of `collect_code_index`
    :return List[str]: problems found, each starting with the page name
    """
    text = page.read_text(encoding="utf-8")
    problems: List[str] = []
    class_match = HEADER_CLASS.search(text)
    file_match = HEADER_FILE.search(text)
    if class_match is None:
        problems.append("missing **Class:** header line")
    elif class_match.group(1) not in index:
        problems.append(f"class {class_match.group(1)} not found in extras")
    elif page.stem not in class_members(index, class_match.group(1)):
        problems.append(f"{page.stem} is not a member of {class_match.group(1)}")
    if file_match is None:
        problems.append("missing **File:** header line")
    else:
        source = repo_root / file_match.group(1)
        if not source.is_file():
            problems.append(f"file {file_match.group(1)} does not exist")
        elif source.stem != page.parent.name:
            problems.append(f"page is in folder {page.parent.name}, expected {source.stem}")
    for section in REQUIRED_SECTIONS:
        if section not in text:
            problems.append(f"missing section {section}")
    blocks = MERMAID_BLOCK.findall(text)
    if not blocks:
        problems.append("no mermaid diagram")
    for block in blocks:
        problems.extend(check_mermaid(block))
    if "—" in text:
        problems.append("contains an em-dash")
    problems.extend(check_code_refs(text, index))
    problems.extend(check_links(page, text))
    return [f"{page.parent.name}/{page.name}: {problem}" for problem in problems]


def check_index(docs_dir: Path, pages: List[Path]) -> List[str]:
    """
    Check that the README links every page and only links files that exist.

    :param docs_dir: the developer-docs directory
    :param pages: result of `find_pages`
    :return List[str]: problems found
    """
    readme = docs_dir / "README.md"
    if not readme.is_file():
        return ["README.md is missing"]
    text = readme.read_text(encoding="utf-8")
    problems = [f"README.md: {p}" for p in check_links(readme, text)]
    for page in pages:
        link = f"{page.parent.name}/{page.name}"
        if link not in text:
            problems.append(f"README.md does not link {link}")
    return problems


def run_checks(docs_dir: Path, repo_root: Path) -> List[str]:
    """
    Check the whole docs tree.

    :param docs_dir: the developer-docs directory
    :param repo_root: repository root, which holds `extras/`
    :return List[str]: every problem found
    """
    index = collect_code_index(repo_root / "extras")
    pages = find_pages(docs_dir)
    problems: List[str] = []
    for page in pages:
        problems.extend(check_page(page, repo_root, index))
    problems.extend(check_index(docs_dir, pages))
    return problems


def main(argv: Optional[List[str]] = None) -> int:
    """
    Command line entry point.

    :param argv: arguments after the program name, defaults to `sys.argv`
    :return int: 0 when the docs are consistent with the code, 1 otherwise
    """
    args = sys.argv[1:] if argv is None else argv
    docs_dir = Path(args[0]).resolve() if args else Path(__file__).resolve().parent.parent
    repo_root = docs_dir.parent
    problems = run_checks(docs_dir, repo_root)
    for problem in problems:
        print(problem)
    pages = len(find_pages(docs_dir))
    print(f"checked {pages} pages, {len(problems)} problems")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
