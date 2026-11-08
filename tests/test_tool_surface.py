"""The MCP tool list is the whole product as a model sees it.

These tests pin the rules that keep it usable by small models: few tools, short
descriptions, typed inputs, a `next_step` in every answer and a fix + example in every
error. The end-to-end test drives the server through the real MCP call path with JSON
arguments, the way a client does, on the analytic backend (no LTspice needed).
"""

from __future__ import annotations

import asyncio
import json

import pytest

from spicemcp import db, server, sim

MAX_TOOLS = 13
MAX_DESC = 200


def _tools():
    return asyncio.run(server.mcp.list_tools())


def _call(name: str, **args) -> dict:
    """Call a tool exactly as an MCP client would and decode its JSON answer."""
    return json.loads(server.answer_text(asyncio.run(server.mcp.call_tool(name, args))))


@sim.register_analytic("surface_rc")
def _rc(p: dict) -> dict:
    fc = 1 / (2 * 3.141592653589793 * p["R1"] * p["C1"])
    return {"fc": fc, "power": 1e-3 * 1e3 / p["R1"]}


NETLIST = ("V1 in 0 AC 1\nR1 in out {R1}\nC1 out 0 {C1}\n.ac dec 20 10 1Meg\n"
           ".meas AC fc WHEN mag(V(out))=0.7071 FALL=1\n.end\n")


@pytest.fixture()
def project(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "PROJECT", str(tmp_path))
    monkeypatch.setattr(server, "_con", db.connect(tmp_path))
    return tmp_path


def test_the_tool_list_is_small_and_every_description_is_short():
    tools = _tools()
    assert len(tools) <= MAX_TOOLS, [t.name for t in tools]
    for t in tools:
        assert len(t.description) <= MAX_DESC, (t.name, len(t.description))
        assert len(t.description.splitlines()) <= 2, t.name


def test_structured_inputs_have_a_schema_not_a_bare_object():
    """A bare {"type": "object"} makes a model guess the keys. Every structured input
    must name its fields (or be a typed map like params: {name: number})."""
    def walk(node, path):
        if isinstance(node, dict):
            if node.get("type") == "object" and "properties" not in node:
                extra = node.get("additionalProperties")
                assert isinstance(extra, dict) and extra, f"shapeless object at {path}"
            if node.get("type") == "array":
                assert node.get("items"), f"shapeless list at {path}"
            for k, v in node.items():
                walk(v, f"{path}.{k}")
        elif isinstance(node, list):
            for i, v in enumerate(node):
                walk(v, f"{path}[{i}]")
    for t in _tools():
        walk(server.tool_schema(t), t.name)


def test_every_example_is_a_valid_call_for_its_tool():
    tools = {t.name: server.tool_schema(t) for t in _tools()}
    assert set(server.EXAMPLES) == set(tools)
    for name, ex in server.EXAMPLES.items():
        props = tools[name].get("properties", {})
        assert set(ex) <= set(props), (name, set(ex) - set(props))
        assert set(tools[name].get("required", [])) <= set(ex), name


def test_a_full_run_through_the_mcp_path_always_says_what_to_do_next(project):
    opts = {"backend": "analytic", "model": "surface_rc"}
    out = _call("start_optimization", run_id="rc_001", netlist=NETLIST,
                params=[{"name": "R1", "min": 100, "max": 100000},
                        {"name": "C1", "values": [1e-9, 1e-8, 1e-7]}],
                goals=[{"metric": "fc", "direction": "max"}],
                limits=[{"metric": "fc", "max": 20000},
                        {"metric": "power", "max": 0.005}],
                sim_options=opts)
    assert "error" not in out, out
    assert out["constraints"] == [{"metric": "fc", "op": "<=", "value": 20000.0},
                                  {"metric": "power", "op": "<=", "value": 0.005}]
    assert "run_optimization" in out["next_step"]

    out = _call("run_optimization", run_id="rc_001", iterations=15)
    assert out["iterations_run"] >= 1 and out["best_candidate"]
    assert out["next_step"]
    best = out["best_candidate"]

    for what in ("status", "best", "pareto", "history", "candidates", "sensitivity",
                 "explored", "failed", "trace", "next"):
        r = _call("query_run", run_id="rc_001", what=what)
        assert "error" not in r, (what, r)
        assert r["next_step"], what

    r = _call("query_run", run_id="rc_001", what="netlist", candidate_id=best)
    assert "{R1}" not in r["netlist"] and "R1 in out" in r["netlist"]
    r = _call("query_run", what="runs")
    assert [x["run_id"] for x in r["runs"]] == ["rc_001"]

    cands = _call("query_run", run_id="rc_001", what="candidates")["candidates"]
    ids = [c["candidate_id"] for c in cands if c["evaluated"]][:2]
    assert "delta" in _call("query_run", run_id="rc_001", what="compare",
                             candidate_ids=ids)

    r = _call("evaluate_candidate", run_id="rc_001", params={"R1": 1000, "C1": 1e-8})
    assert r["status"] == "ok" and r["next_step"]
    r = _call("rollback_to_candidate", run_id="rc_001", candidate_id=best)
    assert r["current_candidate"] == best and r["next_step"]
    r = _call("generate_design_report", run_id="rc_001", plots=False)
    assert r["report_path"].endswith("rc_001_DESIGN_REPORT.md") and r["next_step"], r
    r = _call("finish_design", run_id="rc_001", yield_runs=5)
    assert r["parts"] and r["yield"]["runs"] == 5 and r["next_step"], r
    assert all(p["series"] in ("E96", "E12") for p in r["parts"].values())


@pytest.mark.parametrize("name,args,needle", [
    ("query_run", {"run_id": "nope"}, "unknown run"),
    ("query_run", {"run_id": "rc_x", "what": "netlist"}, "needs candidate_id"),
    ("query_run", {"what": "best"}, "needs run_id"),
    ("run_optimization", {"run_id": "nope"}, "unknown run"),
    ("simulate", {"netlist": "x", "sweep_param": "R1"}, "sweep needs both"),
])
def test_every_error_names_the_fix_and_shows_a_correct_call(project, name, args, needle):
    r = _call(name, **args)
    assert needle in r["error"], r
    assert r["fix"] and isinstance(r["example"], dict), r


def test_loose_or_misspelled_inputs_are_rejected_by_the_schema(project):
    """Strict models: the old string shorthand and typo'd keys fail loudly up front
    instead of being guessed at."""
    try:
        from mcp.server.fastmcp.exceptions import ToolError
    except ImportError:                                  # mcp 2.x
        from mcp.server.mcpserver.exceptions import ToolError
    base = dict(run_id="r", netlist=NETLIST, goals=[{"metric": "fc"}],
                params=[{"name": "R1", "min": 1, "max": 2}])
    for bad in ({"limits": [{"metric": "fc", "max": "<0.005"}]},
                {"limits": [{"metric": "fc"}]},
                {"params": [{"name": "R1", "min": 5, "max": 2}]},
                {"params": [{"name": "R1", "minimum": 1, "max": 2}]},
                {"goals": [{"metric": "fc", "direction": "up"}]}):
        with pytest.raises(ToolError):
            asyncio.run(server.mcp.call_tool("start_optimization", {**base, **bad}))


def test_the_style_moved_to_a_resource():
    res = asyncio.run(server.mcp.list_resources())
    assert "spicemcp://style" in [str(r.uri) for r in res]


# --- token budget ----------------------------------------------------------------
# The tool list is re-sent on every model turn. 9,000 characters of minified JSON is
# about 2,300 tokens (measured, about 3.9 characters per token);
# v0.13.0 was 13,490 characters / 4,270 tokens.
MAX_TOOL_LIST_CHARS = 9000


def test_the_tool_list_fits_its_token_budget():
    tools = [t.model_dump(exclude_none=True, by_alias=True) for t in _tools()]
    text = json.dumps(tools, separators=(",", ":"))
    assert len(text) <= MAX_TOOL_LIST_CHARS, len(text)


def test_the_tool_list_has_no_auto_titles_and_no_advanced_inputs():
    text = json.dumps([server.tool_schema(t) for t in _tools()])
    assert '"title"' not in text
    for hidden in server.HIDDEN_INPUTS:
        assert f'"{hidden}"' not in text, hidden


def test_answers_are_one_line_json_with_short_numbers(project):
    text = server.answer_text(asyncio.run(server.mcp.call_tool("list_templates", {})))
    assert "\n" not in text and text.startswith("{\"")
    assert server.compact({"x": 3.3329999999999997}) == '{"x":3.333}'
