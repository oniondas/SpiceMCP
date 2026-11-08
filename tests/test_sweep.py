"""Checks for the parametric / corner sweep.

Split deliberately: the deck-building half is pure text and runs anywhere, and only the
integration tests need LTspice. Those launch the real simulator on a real RC circuit --
mocking it would test the mock, and the whole point of the sweep is that the numbers
came from LTspice.
"""

from __future__ import annotations

import pytest

from spicemcp import sim

RC = """* rc lowpass
V1 in 0 AC 1
R1 in out 1k
C1 out 0 100n
.ac dec 50 10 1meg
.meas AC fc WHEN mag(V(out))=0.7071
.meas AC gain_lf FIND mag(V(out)) AT 10
.end
"""

needs_ltspice = pytest.mark.skipif(not sim.find_ltspice(),
                                   reason="LTspice not installed")


# --- deck building (no simulator) ----------------------------------------------

def _stepped(deck: str) -> list[str]:
    """The `.step` line's tokens, so the tests assert on the numbers rather than on how
    `ir.fmt_value` happens to spell a float."""
    line = next(ln for ln in deck.splitlines() if ln.startswith(".step"))
    return line.split()


def test_a_component_value_is_replaced_by_a_stepped_parameter():
    deck, symbol, note = sim.sweep_netlist(RC, "R1", [1000, 2000, 5000])
    assert symbol == "R1_sweep" and note["kind"] == "component"
    assert "R1 in out {R1_sweep}" in deck
    assert _stepped(deck)[:4] == [".step", "param", "R1_sweep", "list"]
    assert [float(v) for v in _stepped(deck)[4:]] == [1000, 2000, 5000]
    assert ".param R1_sweep=1000.0" in deck             # a value for a non-stepped run
    assert "R1 in out 1k" not in deck
    assert deck.count(".step") == 1 and deck.rstrip().endswith(".end")


def test_a_parameter_placeholder_is_stepped_in_place():
    nl = RC.replace("R1 in out 1k", "R1 in out {RVAL}")
    deck, symbol, note = sim.sweep_netlist(nl, "RVAL", [1e3, 1e4])
    assert symbol == "RVAL" and note["kind"] == "param"
    assert _stepped(deck)[:4] == [".step", "param", "RVAL", "list"]
    assert [float(v) for v in _stepped(deck)[4:]] == [1e3, 1e4]
    assert "{RVAL}" in deck                            # untouched
    assert "_sweep" not in deck                         # no second parameter invented


def test_temperature_is_its_own_kind_of_sweep():
    deck, symbol, note = sim.sweep_netlist(RC, "temp", [-40, 25, 85, 125])
    assert symbol == "temp" and note == {"kind": "temperature"}
    assert _stepped(deck)[:3] == [".step", "temp", "list"]
    assert [float(v) for v in _stepped(deck)[3:]] == [-40, 25, 85, 125]
    assert "R1 in out 1k" in deck                      # the circuit is not rewritten


def test_an_existing_step_is_replaced_not_nested():
    deck, _, _ = sim.sweep_netlist(RC + ".step param x 1 3 1\n", "R1", [1, 2])
    assert deck.count(".step") == 1 and "param x" not in deck


def test_the_title_line_is_never_eaten():
    """SPICE discards a deck's first line. A netlist that starts with a component would
    lose it, and LTspice runs the resulting circuit without complaining."""
    deck, _, _ = sim.sweep_netlist("V1 in 0 5\nR1 in out 1k\n.op\n", "R1", [1, 2])
    assert deck.splitlines()[0].startswith("*")
    assert "V1 in 0 5" in deck.splitlines()[1:]


@pytest.mark.parametrize("name,values,why", [
    ("", [1, 2], "empty"),
    ("R1", [1000], "at least 2"),
    ("R1", [], "at least 2"),
    ("R1", [1000, 1000], "duplicate"),
    ("R1", [1000, float("inf")], "finite"),
    ("R1", [1000, "banana"], "not a number"),
    ("R1 R2", [1, 2], "not a usable"),
    ("R9", [1, 2], "nothing to sweep"),
])
def test_a_sweep_that_cannot_be_built_fails_before_launching_ltspice(name, values, why):
    with pytest.raises(sim.SweepError, match=why):
        sim.sweep_netlist(RC, name, values)


def test_the_step_value_is_read_by_name_from_the_log_line():
    assert sim._step_value("r1_sweep=2000", "R1_sweep") == 2000
    assert sim._step_value("temp=-40°", "temp") == -40      # LTspice adds a unit
    assert sim._step_value("run=3 r1_sweep=5000", "r1_sweep") == 5000
    assert sim._step_value("run=3", "r1_sweep") is None


# --- against the real simulator ------------------------------------------------

@needs_ltspice
def test_a_resistor_sweep_returns_one_measured_row_per_value(tmp_path):
    vals = [1000.0, 2000.0, 5000.0, 10000.0]
    res = sim.simulate_sweep(RC, "R1", vals, tmp_path)
    assert res["status"] == "ok", res
    assert res["points"] == len(vals) and res["complete"]
    assert [r["value"] for r in res["metrics_table"]] == vals
    assert all(r["value_source"] == "log" for r in res["metrics_table"])
    assert "fc" in res["metrics"] and "gain_lf" in res["metrics"]

    # The physics is the real check that the value/metric pairing is right: fc = 1/2piRC,
    # so the corner has to fall as R rises, and by the ratio the values ask for.
    fcs = [r["metrics"]["fc"] for r in res["metrics_table"]]
    assert fcs == sorted(fcs, reverse=True), fcs
    assert fcs[0] == pytest.approx(1 / (2 * 3.14159265 * 1e3 * 100e-9), rel=0.02)
    assert fcs[0] / fcs[-1] == pytest.approx(10, rel=0.05)


@needs_ltspice
def test_the_raw_file_holds_one_dataset_per_point(tmp_path):
    from spicemcp import raw                           # noqa: PLC0415

    res = sim.simulate_sweep(RC, "R1", [1000.0, 4000.0], tmp_path, "raws")
    assert res["raw_path"], res
    waves = raw.read_raw(res["raw_path"])
    assert len(waves) == 2
    assert [w.step for w in waves] == [0, 1]
    assert all(w.step_params for w in waves)           # labelled from the log
    assert all(w.analysis == "ac" for w in waves)


@needs_ltspice
def test_a_temperature_corner_sweep_runs(tmp_path):
    res = sim.simulate_sweep(RC, "temp", [-40.0, 27.0, 125.0], tmp_path, "temps")
    assert res["status"] == "ok", res
    assert [r["value"] for r in res["metrics_table"]] == [-40.0, 27.0, 125.0]


@needs_ltspice
def test_a_deck_with_no_meas_still_reports_its_points_and_says_why(tmp_path):
    nl = "\n".join(ln for ln in RC.splitlines() if not ln.startswith(".meas"))
    res = sim.simulate_sweep(nl, "R1", [1000.0, 2000.0], tmp_path, "nomeas")
    assert res["status"] == "ok" and res["points"] == 2
    assert res["metrics"] == [] and not res["complete"]
    assert res["raw_path"]
    assert any(".meas" in w for w in res["warnings"]), res["warnings"]


@needs_ltspice
def test_a_waveform_only_op_sweep_reads_its_values_from_the_raw(tmp_path):
    """The case with no evidence in the log at all: a stepped `.op` with no `.meas` gets
    neither a measure table nor a `.step` echo (see tests/fixtures/temp_op.log), and
    LTspice stores it as one dataset whose sweep axis *is* the step variable. So the axis
    is the only place the values exist -- without reading it, a sweep that ran three
    corners reports as a failure."""
    op = "* divider\nV1 in 0 1\nR1 in out 1k tc=1m\nR2 out 0 1k\n.op\n.end\n"
    res = sim.simulate_sweep(op, "temp", [-40.0, 27.0, 125.0], tmp_path, "opsweep")
    assert res["status"] == "ok", res
    assert res["points"] == 3 and res["metrics"] == []
    assert [r["value"] for r in res["metrics_table"]] == [-40.0, 27.0, 125.0]
    assert [r["value_source"] for r in res["metrics_table"]] == ["raw"] * 3
    assert res["raw_path"]


@needs_ltspice
def test_a_broken_netlist_reports_a_structured_failure(tmp_path):
    bad = "* broken\nV1 in 0 AC 1\nR1 in out\n.ac dec 10 1 1k\n.end\n"
    res = sim.simulate_sweep(bad, "temp", [0.0, 25.0], tmp_path, "broken")
    assert res["status"] == "failed"
    assert res["failure_type"] and res["failure_message"]
    assert res["metrics_table"] == []


@needs_ltspice
def test_the_tool_returns_the_documented_shape(tmp_path, monkeypatch):
    from spicemcp import server                        # noqa: PLC0415
    monkeypatch.setattr(server, "PROJECT", str(tmp_path))

    res = server.simulate(RC, sweep_param="R1", sweep_values=[1000.0, 2000.0],
                          timeout_s=60.0)
    assert res["sweep_param"]["name"] == "R1"
    assert res["sweep_param"]["values"] == [1000.0, 2000.0]
    assert all(set(r) >= {"step", "value", "metrics"} for r in res["metrics_table"])
    assert res["raw_path"] and str(tmp_path) in res["raw_path"]

    bad = server.simulate(RC, sweep_param="R9", sweep_values=[1.0, 2.0])
    assert "nothing to sweep" in bad["error"] and bad["fix"]
