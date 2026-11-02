"""The shipped config templates' gcode macros must compile as Klipper Jinja."""
from __future__ import annotations

import configparser
import pathlib

import jinja2
import pytest

# Only the CFS templates: inside AFC's repo the folder also holds every other unit's
TEMPLATES = sorted((pathlib.Path(__file__).resolve().parent.parent / "templates")
                   .glob("AFC_CFS_*.cfg"))
if not TEMPLATES:  # tests run from inside the AFC checkout; find the repo copy via the symlink
    TEMPLATES = sorted(pathlib.Path(__file__).resolve().parent.parent.parent
                       .glob("templates/AFC_CFS_*.cfg"))


def _macros():
    for path in TEMPLATES:
        cp = configparser.RawConfigParser(strict=False, inline_comment_prefixes=None,
                                          comment_prefixes=("#", ";"))
        cp.read(path)
        for section in cp.sections():
            if section.startswith("gcode_macro ") and cp.has_option(section, "gcode"):
                yield pytest.param(cp.get(section, "gcode"), id="{}:{}".format(path.name, section))


@pytest.mark.parametrize("gcode", list(_macros()))
def test_macro_compiles(gcode):
    # Klipper's environment: "{" "}" for expressions
    env = jinja2.Environment("{%", "%}", "{", "}")
    env.parse(gcode)


def test_found_templates():
    # AFC_CFS_wipe.cfg is optional; the leave-purge macro the load needs is in AFC_CFS_1.cfg
    ids = [p.id for p in _macros()]
    assert "AFC_CFS_1.cfg:gcode_macro AFC_CFS_LEAVE_PURGE" in ids
