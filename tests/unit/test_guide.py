"""The operator's guide (tasking/guide.md) ships in the package, and every TOML example in it is
a configuration the task system's reader (tasking/check.py) accepts as it is."""

import re
import tomllib
from pathlib import Path

import tasking
from tasking.check import ENTRIES, SECTIONS, check
from tasking.config import Config

GUIDE = Path(tasking.__file__).parent / "guide.md"
BLOCK = re.compile(r"^```toml\n(.*?)^```", re.DOTALL | re.MULTILINE)


def blocks() -> list[str]:
    return BLOCK.findall(GUIDE.read_text(encoding="utf-8"))


def test_the_guide_is_in_the_package():
    assert GUIDE.is_file() and len(blocks()) >= 10


def test_every_toml_example_reads_cleanly():
    for text in blocks():
        raw = tomllib.loads(text)
        assert check(raw) == [], text
        Config.from_raw(raw)


def test_the_guide_names_every_setting_and_its_check():
    text = GUIDE.read_text(encoding="utf-8")
    for keys in (*SECTIONS.values(), *ENTRIES.values()):
        for k in keys:
            assert re.search(rf"\b{k}\b", text), k
    assert "tasking.check.check" in text
