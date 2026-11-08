"""Standard part values and yield: the step from an optimised design to one you can order.

The fast tests run on the analytic backend. The template tests use ngspice and are
skipped without it.
"""

from __future__ import annotations

import math

import pytest

from spicemcp import buildable as B, db, engine, server, sim

needs_ngspice = pytest.mark.skipif(not sim.find_ngspice(), reason="ngspice not installed")


@sim.register_analytic("build_rc")
def _rc(p: dict) -> dict:
    return {"fc": 1 / (2 * math.pi * p["R1"] * p["C1"])}


NET = ("V1 in 0 AC 1\nR1 in out {R1}\nC1 out 0 {C1}\n.ac dec 20 10 1Meg\n"
       ".meas AC fc WHEN mag(V(out))=0.7071 FALL=1\n.end\n")


@pytest.fixture()
def project(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "PROJECT", str(tmp_path))
    monkeypatch.setattr(server, "_con", db.connect(tmp_path))
    return tmp_path


def test_the_series_are_the_iec_60063_tables():
    assert [len(B.SERIES[s]) for s in ("E6", "E12", "E24", "E48", "E96")] == \
        [6, 12, 24, 48, 96]
    for vals in B.SERIES.values():
        assert vals == sorted(vals) and vals[0] == 1.0 and vals[-1] < 10
        ratios = [b / a for a, b in zip(vals, vals[1:])]
        assert max(ratios) / min(ratios) < 1.6           # roughly geometric
    assert 4.7 in B.E6 and 5.6 in B.E12 and 9.1 in B.E24 and 4.99 in B.E96


@pytest.mark.parametrize("v,series,lo,hi,near", [
    (15915.5, "E96", 15800, 16200, 15800),
    (1.5915e-8, "E12", 1.5e-8, 1.8e-8, 1.5e-8),
    (9.9e3, "E96", 9.76e3, 1e4, 1e4),                    # across a decade
    (1e4, "E24", 1e4, 1e4, 1e4),                         # already standard
    (0.97, "E6", 0.68, 1.0, 1.0),
])
def test_bracket_and_snap(v, series, lo, hi, near):
    assert B.bracket(v, series) == pytest.approx((lo, hi))
    assert B.snap(v, series) == pytest.approx(near)


def test_values_print_as_they_are_ordered():
    assert [B.eng_value(x) for x in (15800, 1.8e-9, 33e-6, 1e6, 0.5)] == \
        ["15.8k", "1.8n", "33u", "1Meg", "500m"]


def _rc_run(rid, lo, hi):
    out = server.start_optimization(
        rid, NET, [server.Param(name="R1", min=1e3, max=1e5),
                   server.Param(name="C1", min=1e-10, max=1e-7)],
        [server.Goal(metric="param_c1", direction="min")],
        [server.Limit(metric="fc", min=lo, max=hi)],
        sim_options=server.SimOptions(backend="analytic", model="build_rc"))
    assert "error" not in out, out
    engine.evaluate(server.con(), rid, {"R1": 15915.494, "C1": 1e-8}, None, None, False,
                    "llm")


def test_finish_picks_standard_parts_that_still_meet_the_limits(project):
    _rc_run("rc", 980, 1020)                            # fc 1 kHz +-2 %
    out = server.finish_design("rc", yield_runs=40)
    assert out["meets_all_limits"], out
    parts = out["parts"]
    assert parts["R1"]["series"] == "E96" and parts["C1"]["series"] == "E12"
    for p in parts.values():
        base = B.SERIES[p["series"]]
        m = p["value"] / 10 ** math.floor(math.log10(p["value"]))
        assert any(math.isclose(m, x, rel_tol=1e-6) for x in base), p
    fc = 1 / (2 * math.pi * parts["R1"]["value"] * parts["C1"]["value"])
    assert 980 <= fc <= 1020
    y = out["yield"]
    assert y["runs"] == 40 and 0 <= y["yield_pct"] <= 100
    assert y["samples_path"].endswith("samples.json") and "samples" not in y
    assert engine.get_run(server.con(), "rc")["status"] == "stopped"
    assert "generate_design_report" in out["next_step"] or "yield" in out["next_step"]


def test_tighter_parts_raise_yield(project):
    _rc_run("loose", 980, 1020)
    loose = server.finish_design("loose", yield_runs=60)["yield"]["yield_pct"]
    _rc_run("tight", 980, 1020)
    tight = server.finish_design("tight", tolerance_pct={"R": 0.1, "C": 1},
                                 yield_runs=60)["yield"]["yield_pct"]
    assert tight > loose and tight >= 95, (loose, tight)


def test_a_finer_series_can_be_asked_for_and_none_keeps_a_value(project):
    _rc_run("rc", 900, 1100)
    out = server.finish_design("rc", resistors="none", capacitors="E24", yield_runs=0)
    assert set(out["parts"]) == {"C1"} and out["parts"]["C1"]["series"] == "E24"
    assert "R1" in out["other_values"] and out["yield"] is None


def test_finish_explains_what_to_do_when_it_cannot(project):
    out = server.finish_design("nope")
    assert "unknown run" in out["error"] and out["fix"]
    _rc_run("rc", 980, 1020)
    out = server.finish_design("rc", supply="Vbat", supply_tol_pct=5, yield_runs=3)
    assert "supply source" in out["error"] and "DC source" in out["fix"]


@needs_ngspice
def test_sallen_key_nearest_values_fail_but_finish_finds_a_passing_set(project):
    out = server.start_from_template("sk", "sallen_key_lowpass", {"fc": 10000},
                                     server.SimOptions(backend="ngspice"))
    assert out["start_result"]["feasible"]
    out = server.finish_design("sk", yield_runs=10)
    assert not out["nearest_values_meet_limits"]        # plain rounding misses
    assert out["meets_all_limits"] and out["worst_margin_pct"] > 0


def test_yield_states_a_confidence_bound_not_a_resolution():
    assert B.yield_lower_bound(30, 30) == pytest.approx(0.05 ** (1 / 30), abs=1e-4)
    assert "at least 90%" in B.yield_statement(30, 30)
    assert B.yield_lower_bound(0, 30) == 0.0
    assert B.yield_lower_bound(29, 30) < B.yield_lower_bound(30, 30)


def test_yield_seed_is_fresh_each_time_reported_and_repeatable(project):
    _rc_run("a", 990, 1010)
    y1 = server.finish_design("a", yield_runs=20)["yield"]
    _rc_run("b", 990, 1010)
    y2 = server.finish_design("b", yield_runs=20)["yield"]
    assert y1["seed"] != y2["seed"] and "confidence" in y1["statement"]
    _rc_run("c", 990, 1010)
    y3 = server.finish_design("c", yield_runs=20, seed=y1["seed"])["yield"]
    assert y3["passed"] == y1["passed"] and y3["seed"] == y1["seed"]


def test_finish_answer_is_short_and_the_rest_is_on_query_run(project):
    _rc_run("rc", 980, 1020)
    out = server.finish_design("rc", yield_runs=5)
    assert "alternatives" not in out and "optimised_metrics" not in out
    d = server.query_run("rc", "details")
    assert d["finish"]["alternatives"] and d["finish"]["yield"]["spread"] is not None
