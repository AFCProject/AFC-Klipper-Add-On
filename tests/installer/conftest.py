from pathlib import Path

import pytest

from tests.installer.fakes import (
    FakeCommandRunner,
    PrinterTree,
    RecordingConsole,
    ScriptedPrompter,
    build_printer_tree,
)

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def printer_tree(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> PrinterTree:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    return build_printer_tree(tmp_path, REPO_ROOT)


@pytest.fixture
def console() -> RecordingConsole:
    return RecordingConsole()


@pytest.fixture
def runner() -> FakeCommandRunner:
    return FakeCommandRunner()


@pytest.fixture
def prompter() -> ScriptedPrompter:
    return ScriptedPrompter([])
