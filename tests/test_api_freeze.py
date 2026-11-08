"""The 1.x promise: what a client or a prompt relies on does not break.

Within 1.x a tool, a template, a parameter or an accepted value may be added, and a new
parameter must be optional. Nothing listed in tests/fixtures/api_v1.json may be removed
or renamed, become required, or lose an accepted value; that takes 2.0.0. If this test
fails, the change is breaking: undo it, or make it additive."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

from spicemcp import server, topologies as T

FROZEN = json.loads((Path(__file__).parent / "fixtures" / "api_v1.json").read_text())


def _tools() -> dict:
    tl = asyncio.run(server.mcp.list_tools())
    return {t.name: getattr(t, "inputSchema", None) or getattr(t, "input_schema")
            for t in tl}          # mcp 1.x and 2.x spell it differently


def test_every_frozen_tool_keeps_its_name_parameters_and_values():
    now = _tools()
    for name, was in FROZEN["tools"].items():
        assert name in now, f"tool {name} removed or renamed"
        props = now[name].get("properties", {})
        required = set(now[name].get("required") or [])
        for p in was["params"]:
            assert p in props, f"{name}: parameter {p} removed or renamed"
        new_required = required - set(was["required"])
        assert not new_required, f"{name}: {sorted(new_required)} became required"
        for p, values in was["enums"].items():
            lost = set(values) - set(props[p].get("enum", values))
            assert not lost, f"{name}.{p}: accepted values {sorted(lost)} removed"


def test_every_frozen_template_keeps_its_name_and_spec_keys():
    for name, was in FROZEN["templates"].items():
        assert name in T.TEMPLATES, f"template {name} removed or renamed"
        spec = {f.name: f for f in T.TEMPLATES[name].spec}
        for key in was["spec"]:
            assert key in spec, f"{name}: spec key {key} removed or renamed"
        newly = {k for k, f in spec.items() if f.default is None} - set(was["required"])
        assert not newly, f"{name}: spec keys {sorted(newly)} became required"


def test_the_snapshot_covers_every_tool_today():
    """A new tool is fine, but add it to the snapshot so it is frozen too."""
    assert set(_tools()) == set(FROZEN["tools"])
