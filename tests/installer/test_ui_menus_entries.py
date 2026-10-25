import pytest

from installer.ui_menus.entries import Entry, buffer_label, require_unit
from installer.ui_menus.view import row
from installer.unit_registry.registry import UNIT_TYPES


class TestBufferLabel:
    def test_psf_has_a_friendly_label(self) -> None:
        assert buffer_label("FPS_PSF") == "Proportional Sync-Feedback(PSF)"

    def test_remastered_has_a_friendly_label(self) -> None:
        assert buffer_label("TurtleNeck2Remastered") == "TurtleNeck 2 Remastered"

    def test_other_tokens_are_shown_as_they_are(self) -> None:
        assert buffer_label("TurtleNeck") == "TurtleNeck"


class TestRequireUnit:
    @pytest.mark.parametrize("unit", UNIT_TYPES, ids=lambda u: u.display_name)
    def test_a_registered_type_is_returned(self, unit: object) -> None:
        assert require_unit(unit.display_name) is unit  # type: ignore[attr-defined]

    def test_an_unknown_type_raises(self) -> None:
        with pytest.raises(ValueError, match="Unknown installation type 'Nope'"):
            require_unit("Nope")


class TestEntry:
    def test_holds_its_row_and_action(self) -> None:
        entry = Entry(lambda n: row(f"{n}. x"), lambda: "done")
        assert entry.row(3) == row("3. x")
        assert entry.activate() == "done"
