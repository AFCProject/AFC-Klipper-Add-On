from installer.platform_core.toggles import TOGGLES, default_toggles

EXPECTED = [
    ("afc_includes", "Add AFC includes?", True, None),
    ("tip_forming", "Enable tip-forming?", False, "form_tip"),
    ("toolhead_cutter", "Enable toolhead cutter?", True, "tool_cut"),
    ("hub_cutter", "Enable hub cutter?", False, "hub_cut"),
    ("kick_macro", "Enable kick macro?", True, "kick"),
    ("park_macro", "Enable park macro?", True, "park"),
    ("poop_macro", "Enable poop macro?", True, "poop"),
    ("wipe_macro", "Enable wipe macro?", True, "wipe"),
    ("tip_cut_servo", "Include the tip cut servo example?", False, None),
]


class TestToggles:
    def test_the_table_lists_the_toggles_in_menu_order(self) -> None:
        actual = [(t.key, t.label, t.default, t.config_key) for t in TOGGLES]
        assert actual == EXPECTED

    def test_keys_are_unique(self) -> None:
        keys = [t.key for t in TOGGLES]
        assert len(set(keys)) == len(keys)


class TestDefaultToggles:
    def test_defaults_come_from_the_table(self) -> None:
        assert default_toggles() == {key: default for key, _, default, _ in EXPECTED}

    def test_every_call_returns_a_fresh_dict(self) -> None:
        first = default_toggles()
        first["tip_forming"] = True
        assert default_toggles()["tip_forming"] is False
