"""The report is a *view* of run state, so what it must never do is fill a gap.

Everything here runs on the analytic backend: no LTspice, no matplotlib. That is also the
hard case for the generator -- an analytic run has no netlist semantics and no `.raw`, so
sections 4 and 5 have nothing to say, and the assertion that matters is that they say so
out loud instead of rendering an empty table or a zero.
"""

from __future__ import annotations

import pytest

from spicemcp import db, engine, report, sim

SIMS: list[dict] = []


@sim.register_analytic("report_amp")
def _amp(params: dict) -> dict:
    SIMS.append(dict(params))
    x = params["R1"] / 100e3
    if params["R1"] == 10e3:              # the seed point "fails to converge"
        return sim.SimResult(status="failed", failure_type=sim.CONVERGENCE_FAILURE,
                             failure_message="no convergence at the lower bound")
    return {"gain_db": 18 + 42.5 * x - 25 * x * x,
            "bandwidth": 200e3 * (1 - 0.5 * x),
            "power": 0.001 + 0.004 * x}


NETLIST = """* report device under test
V1 in 0 AC 1
R1 in mid {R1}
C1 mid 0 {C1}
M1 out mid 0 0 NM
.model NM NMOS (VTO=0.5 KP=120u)
.ac dec 100 1 10Meg
.meas AC gain_db MAX mag(V(out))
.meas AC bandwidth WHEN mag(V(out))=0.707 FALL=1
.meas AC power AVG (-I(V1)*V(in))
.end
"""


@pytest.fixture()
def run(tmp_path):
    SIMS.clear()
    con = db.connect(tmp_path)
    engine.start_run(
        con, "amp_001", NETLIST,
        param_space={"R1": {"min": 10e3, "max": 110e3},
                     "C1": {"min": 5e-9, "max": 15e-9}},
        objectives=[{"metric": "gain_db", "direction": "max"},
                    {"metric": "bandwidth", "direction": "max"},
                    {"metric": "power", "direction": "min"}],
        constraints={"bandwidth": ">100000", "power": "<0.005"},
        sim_config={"backend": "analytic", "model": "report_amp"},
        seed=42, notes="two-pole amp, report smoke test")
    engine.evaluate(con, "amp_001", {"R1": 10e3, "C1": 1e-8})      # fails
    engine.evaluate(con, "amp_001", {"R1": 30e3, "C1": 1e-8})      # baseline
    engine.evaluate(con, "amp_001", {"R1": 85e3, "C1": 1e-8})      # winner
    return con, tmp_path


# --- formatting units ----------------------------------------------------------

def test_eng_covers_both_prefix_cases():
    assert report.eng(4.7e-9, "F") == "4.7 nF"
    assert report.eng(1849.6, "Ω") == "1.85 kΩ"
    assert report.eng(1e6, "Hz") == "1 MHz"          # uppercase prefix above kilo
    assert report.eng(2.2e-6, "H") == "2.2 µH"       # ASCII `u` promoted for prose
    assert report.eng(None) == "—"
    assert report.eng(float("inf")) == "inf"         # not a made-up magnitude


def test_empty_table_says_so_rather_than_rendering_a_bare_header():
    assert report._table(["a", "b"], []) == ["_(nothing to report here)_"]
    assert len(report._table(["a"], [[1], [2]])) == 4


def test_unknown_is_not_a_pass():
    assert report._mark(None) not in (report._mark(True), report._mark(False))


# --- derivations ---------------------------------------------------------------

def test_tradeoff_is_judged_against_the_objective_direction():
    """A slope table cannot say what "improves" means; only the objective can.

    `power` rising is a cost when the objective is `min`, and a direction-blind version
    prints "increase R1 improves power" about a circuit that now draws twice as much.
    """
    s = [{"parameter": "R1", "metric": "bw", "estimated_sensitivity": 3.0},
         {"parameter": "R1", "metric": "power", "estimated_sensitivity": 4e-8},
         {"parameter": "R1", "metric": "noise", "estimated_sensitivity": -1.0},
         {"parameter": "C1", "metric": "bw", "estimated_sensitivity": 1.0}]
    for e in s:
        e["direction"] = "positive" if e["estimated_sensitivity"] > 0 else "negative"
    goal = {"bw": "max", "power": "min"}              # `noise` has no objective
    assert report._tradeoffs(s, goal) == [["increase `R1`", "`bw`", "`power`"]]
    # Same slopes, both minimised: now R1 hurts both, so there is no trade-off to report.
    assert report._tradeoffs(s, {"bw": "min", "power": "min"}) == []
    assert report._tradeoffs(s, {}) == []             # nothing stated, nothing claimed


def test_a_zero_slope_is_not_a_tradeoff():
    """`engine.sensitivity` labels a slope of 0 `negative` -- it has only two labels.

    Reading the label instead of the number turns a parameter nothing responded to into a
    row claiming it trades bandwidth against power.
    """
    s = [{"parameter": "C1", "metric": "bw", "estimated_sensitivity": 0.0,
          "direction": "negative"},
         {"parameter": "C1", "metric": "power", "estimated_sensitivity": 0.0,
          "direction": "negative"}]
    assert report._tradeoffs(s, {"bw": "max", "power": "min"}) == []


def test_metric_row_verdict_comes_from_the_stored_check():
    rows = report._metric_rows(
        [{"metric": "bw", "direction": "max"}],
        [{"metric": "bw", "op": ">", "value": 10000}],
        {"bw": 20000}, {"bw": 30000},
        [{"constraint": "bw", "required": "> 10000", "actual": 30000, "passed": True}])
    assert rows[0][4] == "+50.0 %"
    assert rows[0][5] == report._mark(True)


def test_spec_intervals_come_from_the_runs_constraints():
    assert report._spec_from_constraints(
        [{"metric": "bw", "op": ">", "value": 1e4},
         {"metric": "bw", "op": "<", "value": 5e4},
         {"metric": "g", "op": "==", "value": 2}]) == {"bw": (1e4, 5e4)}


def test_baseline_is_the_first_converged_iteration_not_the_first(run):
    con, _ = run
    row = report._first_ok(con, "amp_001")
    assert row["iteration"] == 2                     # iteration 1 failed
    assert db.jloads(row, "metrics")["gain_db"] == pytest.approx(28.5)   # x=0.3


def test_bom_does_not_print_a_node_name_as_a_component_value(run):
    con, _ = run
    cand = engine.get_candidate(con, engine.summary(con, "amp_001")["best_candidate"])
    rows = report._bom(cand["netlist"], db.jloads(cand, "params"), {"r": 0.01})
    by_ref = {r[0]: r for r in rows}
    assert by_ref["`R1`"][2] == "**85 kΩ**"          # substituted, in engineering units
    assert by_ref["`R1`"][3] == "`R1`"               # traced back to its knob
    assert by_ref["`R1`"][4] == "±1 %"
    assert by_ref["`M1`"][2] == "_see §2.3_"         # NOT the third node, "0"
    # `V1 in 0 AC 1`: the third field is the word `AC`, so the whole line is shown
    # rather than a value truncated to a keyword.
    assert by_ref["`V1`"][2] == "`V1 in 0 AC 1`"


# --- the document --------------------------------------------------------------

def test_report_renders_all_six_sections_and_flags_what_it_skipped(run):
    con, tmp = run
    n_sims = len(SIMS)
    out = report.generate(con, "amp_001", dc_audit=True, pvt=True, monte_carlo=100)
    text = (tmp / ".ltspice-mcp" / "reports" / "amp_001_DESIGN_REPORT.md").read_text(
        encoding="utf-8")

    for h in ["## 1. Executive Summary", "## 2. Schematic & Component Bill of Materials",
              "## 3. Optimization Summary", "## 4. Waveform & Response Analysis",
              "## 5. Robustness, DC Bias Audit & Yield Analysis",
              "## 6. LTspice Reproduction Guide"]:
        assert h in text, f"missing section: {h}"

    # An analytic run has no waveforms and no deck to bias. Every such section must be an
    # explicit skip naming the argument -- a dropped heading reads as "checked, all fine".
    assert text.count("**Not run.**") == 4           # plots + DC + PVT + MC
    assert "backend is `analytic`" in text
    assert len(SIMS) == n_sims, "nothing should be re-simulated on a non-ltspice backend"
    assert out["analyses_run"] == ["none (stored state only)"]
    assert out["figures"] == []

    # Section 2 must not invent a picture it cannot draw.
    assert "No schematic image" in text
    assert "![Schematic]" not in text

    # Stored numbers, and the winner's actual value.
    assert out["candidate_id"] == engine.summary(con, "amp_001")["best_candidate"]
    assert "**85 kΩ**" in text
    assert "iteration 2" in text                     # baseline provenance is stated
    assert out["bytes"] == len(text.encode("utf-8"))


def test_schematic_and_netlist_paths_are_relative(run):
    con, tmp = run
    png = tmp / "pics" / "amp.png"
    png.parent.mkdir()
    png.write_bytes(b"\x89PNG\r\n\x1a\n")
    out = report.generate(con, "amp_001", schematic=str(png))
    text = report.Path(out["report_path"]).read_text(encoding="utf-8")
    assert "![Schematic](../../pics/amp.png)" in text
    assert "(../candidates/cand_0003.cir)" in text
    assert str(tmp) not in text.replace("\\", "/"), "no absolute paths in the document"


def test_a_run_with_nothing_to_report_refuses_instead_of_rendering_empty(tmp_path):
    con = db.connect(tmp_path)
    engine.start_run(con, "empty", NETLIST,
                     param_space={"R1": {"min": 10e3, "max": 110e3},
                                  "C1": {"min": 5e-9, "max": 15e-9}},
                     objectives=[{"metric": "gain_db", "direction": "max"}],
                     sim_config={"backend": "analytic", "model": "report_amp"})
    engine.evaluate(con, "empty", {"R1": 10e3, "C1": 1e-8})        # the only sim, failed
    with pytest.raises(report.ReportError, match="no successful candidate"):
        report.generate(con, "empty")


def test_infeasible_winner_is_labelled_as_one(tmp_path):
    """A best-*scoring* design that violates a constraint must not read as compliant."""
    con = db.connect(tmp_path)
    engine.start_run(con, "tight", NETLIST,
                     param_space={"R1": {"min": 10e3, "max": 110e3},
                                  "C1": {"min": 5e-9, "max": 15e-9}},
                     objectives=[{"metric": "gain_db", "direction": "max"}],
                     constraints={"bandwidth": ">1e9"},   # unreachable by any candidate
                     sim_config={"backend": "analytic", "model": "report_amp"})
    engine.evaluate(con, "tight", {"R1": 85e3, "C1": 1e-8})
    out = report.generate(con, "tight")
    text = report.Path(out["report_path"]).read_text(encoding="utf-8")
    assert "No feasible candidate exists in this run" in text
    assert "❌ FAIL" in text


# --- the real thing ------------------------------------------------------------

RC = """* one-pole RC, real deck
V1 in 0 AC 1 PULSE(0 1 0 1n 1n 1m 2m)
R1 in out {R1}
C1 out 0 {C1}
M1 out out 0 0 NM L=1u W=10u
.model NM NMOS (VTO=0.5 KP=120u)
.ac dec 200 10 1Meg
.meas AC gain_db MAX mag(V(out))
.meas AC fc WHEN mag(V(out))=0.7071 FALL=1
.end
"""


@pytest.mark.skipif(sim.find_ltspice() is None, reason="LTspice not installed")
def test_every_section_renders_against_real_ltspice(tmp_path):
    """The only test that proves sections 4 and 5 work: they need a `.raw`.

    One LTspice run per opt-in flag, on a deck small enough to stay fast, because the
    alternative is a generator whose waveform and robustness paths are only ever
    exercised by hand.
    """
    con = db.connect(tmp_path)
    engine.start_run(con, "rc", RC,
                     param_space={"R1": {"min": 1e3, "max": 100e3},
                                  "C1": {"values": [1e-9, 4.7e-9, 1e-8]}},
                     objectives=[{"metric": "fc", "direction": "max"}],
                     constraints={"fc": ">10000", "gain_db": ">-6"})
    engine.evaluate(con, "rc", {"R1": 10e3, "C1": 1e-9})
    engine.evaluate(con, "rc", {"R1": 4.7e3, "C1": 1e-9})
    out = report.generate(con, "rc", dc_audit=True, pvt=True, monte_carlo=20, supply="V1")
    text = report.Path(out["report_path"]).read_text(encoding="utf-8")

    assert "**Not run.**" not in text, "every section was asked for, so none may be skipped"
    assert out["notes"] == []
    assert "Reproduction check" in text          # stored vs fresh metrics
    assert "### Frequency Response (Bode)" in text
    assert "−3 dB bandwidth" in text
    assert out["figures"] and all(report.Path(p).exists() for p in out["figures"])
    assert "![Frequency Response (Bode)](" in text

    # 5.1: the deck has one MOSFET, so a real region verdict must appear.
    assert "`M1`" in text and any(r in text for r in ("Saturation", "Linear", "Cutoff"))
    # 5.2: five corners, each named.
    for corner in ("TT", "SS", "FF", "SF", "FS"):
        assert corner in text
    # 5.3: `fc` has a `>` constraint, so a yield really is computable here.
    assert "Yield" in text and "%" in text
