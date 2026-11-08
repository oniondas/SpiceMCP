"""The version lives in pyproject.toml only; the package reads it from there."""
import re
from pathlib import Path

import spicemcp


def test_package_version_is_the_pyproject_version():
    text = (Path(__file__).parents[1] / "pyproject.toml").read_text()
    want = re.search(r'^version\s*=\s*"([^"]+)"', text, re.M).group(1)
    assert spicemcp.__version__ == want


def test_changelog_has_an_entry_for_this_version():
    text = (Path(__file__).parents[1] / "CHANGELOG.md").read_text()
    assert f"## {spicemcp.__version__}" in text
