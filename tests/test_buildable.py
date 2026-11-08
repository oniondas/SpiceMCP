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


def test_corners_put_a_temp_line_in_every_deck_and_find_the_template_supply():
    two = "* a\nV1 a 0 1\n.op\n.end\n* b\nV1 a 0 1\n.op\n.end\n"
    assert B.at_temperature(two, -40).count(".temp -40\n.end") == 2
    assert B.default_supply("Vcc vcc 0 5\nVee vee 0 -5\n.end") == "Vcc,Vee"
    assert B.default_supply("Vin in 0 12\n.end") == "Vin"
    assert B.default_supply("V1 in 0 AC 1\n.end") is None
    assert B._scale_supply("Vcc vcc 0 5\nVee vee 0 -5", "Vcc,Vee", 1.1).split("\n") == [
        "Vcc vcc 0 5.5", "Vee vee 0 -5.5"]
    assert not B.has_semiconductors(NET)
    assert B.has_semiconductors("X1 a b c d e SPMCP_OA\n")
    assert B.has_semiconductors("D1 a 0 DX\n")


def test_worst_case_names_the_limit_the_corner_and_the_part():
    cons = [{"metric": "vd", "op": ">=", "value": 0.6}]
    rows = []
    for i, r in enumerate((0.99, 1.01, 0.99, 1.01)):
        bad = r > 1
        rows.append({"passed": not bad, "failed": ["vd"] if bad else [],
                     "failed_at_temps": [85] if bad else [],
                     "parts": {"R1": 1000 * r}, "worst": {"vd": 0.59 if bad else 0.61},
                     "worst_temp": {"vd": 85}})
    w = B.worst_case(rows, cons, {"R1": 0.01}, {"R1": 1000})
    assert w["boards_failing"] == 2 and w["tightest_limit"] == "vd"
    assert w["limits"]["vd"]["worst"] == 0.59 and w["limits"]["vd"]["at_temp_c"] == 85
    assert w["most_failures_at_c"] == 85
    assert w["part_driving_failures"] == {"part": "R1", "direction": "high",
                                          "strength": 2.0}


DIODE = ("* diode\nV1 in 0 5\nR1 in a {R1}\nD1 a 0 DX\n.model DX D(IS=1e-14)\n"
         ".dc V1 5 5.001 0.001\n.meas DC vd FIND V(a) AT 5.0005\n.end\n")


@needs_ngspice
def test_a_design_that_passes_at_room_temperature_fails_at_85c(project):
    server.start_optimization(
        "d", DIODE, [server.Param(name="R1", min=1e3, max=1e5)],
        [server.Goal(metric="vd", direction="max")], [server.Limit(metric="vd", min=0.62)],
        sim_options=server.SimOptions(backend="ngspice"))
    engine.evaluate(server.con(), "d", {"R1": 1000}, None, None, False, "llm")
    room = server.finish_design("d", yield_runs=4, seed=1)
    assert room["yield"]["passed"] == 4 and "27" in room["yield"]["covers"]
    hot = server.finish_design("d", yield_runs=4, seed=1, temps_c=[-40, 25, 85])
    assert hot["yield"]["passed"] == 0 and "85" in hot["yield"]["covers"]
    wc = hot["yield"]["worst_case"]
    assert wc["most_failures_at_c"] == 85 and wc["tightest"]["worst"] < 0.62
    assert "85 C" in hot["next_step"]


SWITCH = ("* switch\nV1 in 0 PULSE(0 5 0 1u 1u 5u 10u)\nR1 in a 10\nL1 a b 10u\nD1 b c DX\n"
          "C1 c 0 1u\nM1 c g 0 0 NM\nVg g 0 2\n.model DX D(IS=1e-14)\n"
          ".model NM NMOS(VTO=1 KP=1e-3)\n.tran 0.1u 100u\n.end\n")


def test_stress_probes_every_top_level_part_and_skips_blocks_and_fixtures():
    from spicemcp import stress
    net = SWITCH.replace(".end", ".subckt BLK a b\nR9 a b 1k\n.ends\nLbrk c x 1k\n.end")
    names = [n for n, _, _ in stress.parts(net)]
    assert names == ["R1", "L1", "D1", "C1", "M1"]
    probed = stress.instrument(net)
    assert ".meas TRAN st_d1_vr MAX V(c,b)" in probed
    assert ".meas TRAN st_m1_pavg AVG V(c)*Id(M1)" in probed
    assert stress.instrument("* ac\nV1 a 0 AC 1\nR1 a 0 1k\n.ac dec 5 1 10\n.end") is None
    assert stress._up(4.1, stress.C_V) == 6.3 and stress._up(0.2, stress.R_W) == 0.25


def test_ngspice_measures_expressions_and_device_currents():
    ng = sim.to_ngspice("* t\nV1 a 0 1\n.tran 1u\n.meas TRAN p AVG V(a,b)*Id(M1)\n"
                        ".meas TRAN q MAX V(a)\n.meas TRAN r MAX I(L1) FROM 1u TO 2u\n.end\n")
    assert ".options savecurrents" in ng
    assert "let x_p = (v(a)-v(b))*@m1[id]" in ng and "meas tran p AVG x_p" in ng
    assert "meas tran q MAX V(a)" in ng
    assert "meas tran r MAX x_r FROM=1u TO=2u" in ng
    ng = sim.to_ngspice("* n\n.noise V(out) V1 dec 20 10 1k\n.meas NOISE vn INTEG V(onoise)\n"
                        ".meas NOISE vn_uv PARAM vn*1e6\n.end\n")
    assert "let vn = onoise_total" in ng and "let vn_uv = vn*1e6" in ng


@needs_ngspice
def test_stress_gives_a_rating_to_buy_for_each_part(tmp_path):
    from spicemcp import stress
    st = stress.check(SWITCH, {"backend": "ngspice"}, tmp_path)
    p = st["parts"]
    assert st["checked"] and set(p) == {"R1", "L1", "D1", "C1", "M1"}
    assert p["L1"]["peak_a"] == pytest.approx(0.349, rel=0.02)
    assert p["R1"]["buy"] == "0.25 W" and p["C1"]["buy"] == "10 V"
    assert p["D1"]["reverse_v"] == pytest.approx(4.9, rel=0.05)
    assert p["M1"]["dissipates_w"] == pytest.approx(1.73e-3, rel=0.05)
    assert not stress.check("* ac\nV1 a 0 AC 1\nR1 a 0 1k\n.ac dec 5 1 10\n.end\n",
                            {"backend": "ngspice"}, tmp_path)["checked"]


@needs_ngspice
def test_amplifier_and_filter_report_output_noise(tmp_path):
    from spicemcp import ir, topologies as T
    for name, spec, lo, hi in (("noninverting_amp", {"gain": 10, "bw": 1e5}, 50, 500),
                               ("sallen_key_lowpass", {"fc": 1e4}, 1, 20)):
        d = T.get(name).design(spec)
        net, _ = ir.render(d.netlist, d.start)
        r = sim.simulate(sim.decks(net)[-1], {}, {"backend": "ngspice"}, tmp_path, name)
        assert lo < r.metrics["vn_uv"] < hi, (name, r.metrics)
    d = T.get("noninverting_amp").design({"gain": 10, "bw": 1e5, "noise_uv": 100})
    assert {"metric": "vn_uv", "max": 100} in d.limits
