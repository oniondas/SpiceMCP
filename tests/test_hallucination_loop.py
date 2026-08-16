"""Spec S21: reproduce the hallucination loop and prove the MCP state survives it.

The scripted score sequence is improve, improve, BEST, worse, worse, improve --
the exact shape that makes a memory-based agent panic and start "restoring" a
circuit it has misremembered. Everything asserted here is read back out of
SQLite, never carried in Python variables between phases.

Run: pytest -q
"""

from __future__ import annotations

import json

import pytest

from spicemcp import db, engine, ir, sim

# --- analytic device under test -------------------------------------------------

C_NOM = 10.0          # nF; gain peaks here
CALLS: list[dict] = []  # every actual "simulation" -- proves dedup skips runs


@sim.register_analytic("test_amp")
def _amp(params: dict) -> dict:
    """Concave gain in R1, bandwidth/power strictly monotone in R1, gain also
    penalised away from C1=10n. Deterministic, closed form, no LTspice needed."""
    CALLS.append(dict(params))
    x = params["R1"] / 100e3
    c = params["C1"] / 1e-9
    return {
        "gain_db": 18 + 42.5 * x - 25 * x * x - 2 * (c - C_NOM) ** 2,
        "bandwidth": 200e3 * (1 - 0.5 * x),
        "power": 0.001 + 0.004 * x,
    }


NETLIST = """* two-pole amp under test
V1 in 0 AC 1
R1 in mid {R1}
C1 mid 0 {C1}
RL out 0 10k
E1 out 0 mid 0 100
.ac dec 100 1 10Meg
.meas AC gain_db MAX mag(V(out))
.meas AC bandwidth WHEN mag(V(out))=0.707 FALL=1
.end
"""

# x, and the intent of that iteration
SEQUENCE = [
    (0.20, "improve"),   # 1  score 25.5
    (0.50, "improve"),   # 2  score 33.0
    (0.70, "best"),      # 3  score 35.5   <- incumbent
    (1.05, "worse"),     # 4  infeasible: bandwidth 95k < 100k
    (0.35, "worse"),     # 5  score 29.8125
    (0.60, "improve"),   # 6  score 34.5 -- better than 5, still NOT better than 3
    (0.85, "new_best"),  # 7  score 36.0625 -- legitimately takes the lead
]


@pytest.fixture()
def run(tmp_path):
    CALLS.clear()
    con = db.connect(tmp_path)
    engine.start_run(
        con, "amp_001", NETLIST,
        param_space={"R1": {"min": 10e3, "max": 110e3},
                     "C1": {"min": 5e-9, "max": 15e-9}},
        objectives=[{"metric": "gain_db", "direction": "max"},
                    {"metric": "bandwidth", "direction": "max"},
                    {"metric": "power", "direction": "min"}],
        constraints={"bandwidth": ">100000", "power": "<0.005"},
        sim_config={"backend": "analytic", "model": "test_amp"},
        seed=42,
    )
    return con, tmp_path


def _drive(con) -> list[dict]:
    out = []
    for x, _intent in SEQUENCE:
        out.append(engine.evaluate(con, "amp_001",
                                   {"R1": x * 100e3, "C1": C_NOM * 1e-9}))
    return out


# --- the required test ----------------------------------------------------------

def test_scripted_sequence_shape(run):
    """The fixture really does produce improve/improve/best/worse/worse/improve."""
    con, _ = run
    res = _drive(con)
    scores = [r["score"] for r in res]
    assert scores[1] > scores[0], "iter 2 must improve"
    assert scores[2] > scores[1], "iter 3 must improve to the best"
    assert scores[3] < scores[2], "iter 4 must be worse"
    assert scores[4] < scores[3], "iter 5 must be worse"
    assert scores[5] > scores[4], "iter 6 must improve"
    assert scores[5] < scores[2], "...but iter 6 must not beat the incumbent"
    assert scores[6] > scores[2], "iter 7 legitimately takes the lead"


def test_best_is_never_overwritten_by_a_worse_iteration(run):
    con, _ = run
    res = _drive(con)
    ids = [r["candidate_id"] for r in res]

    # After the two regressions, cand_0003 is still the recorded best.
    after_5 = [r for r in res[:5]]
    assert after_5[2]["improved_best"] is True
    assert after_5[3]["improved_best"] is False
    assert after_5[4]["improved_best"] is False
    assert after_5[3]["best_candidate"] == ids[2]
    assert after_5[4]["best_candidate"] == ids[2]

    # Iteration 6 improves on 5 without dethroning 3 (the anti-panic case, S18).
    assert res[5]["improved_best"] is False
    assert res[5]["best_candidate"] == ids[2]

    # ...and the best DOES move when something is genuinely better (not just frozen).
    assert res[6]["improved_best"] is True
    assert engine.get_best(con, "amp_001")["best"]["candidate_id"] == ids[6]


def test_worse_iteration_does_not_mutate_the_incumbent(run):
    con, _ = run
    res = _drive(con)
    cand3 = res[2]["candidate_id"]
    row = engine.get_candidate(con, cand3)
    assert db.jloads(row, "params")["R1"] == pytest.approx(70e3)
    assert json.loads(row["params"])["C1"] == pytest.approx(10e-9)
    # one candidate per distinct design, and none was rewritten
    assert row["netlist"] == ir.render(NETLIST, db.jloads(row, "params"))[0]
    assert len({r["candidate_id"] for r in res}) == len(SEQUENCE)


def test_candidate_three_restores_exactly(run):
    con, tmp = run
    res = _drive(con)
    cand3 = res[2]["candidate_id"]
    expected = engine.get_candidate(con, cand3)["netlist"]

    back = engine.rollback(con, "amp_001", cand3)
    assert back["current_candidate"] == cand3
    assert back["params"]["R1"] == pytest.approx(70e3)
    # byte-for-byte, from storage -- not reconstructed
    assert open(back["netlist_path"], encoding="utf-8").read() == expected
    assert back["restored_result"]["score"] == pytest.approx(res[2]["score"])
    # rollback records current state without inventing a new candidate
    assert engine.get_run(con, "amp_001")["current_candidate"] == cand3
    assert con.execute("SELECT COUNT(*) FROM designs").fetchone()[0] == len(SEQUENCE)


def test_duplicates_are_not_simulated_twice(run):
    con, _ = run
    res = _drive(con)
    calls_before = len(CALLS)

    again = engine.evaluate(con, "amp_001", {"R1": 70e3, "C1": C_NOM * 1e-9})
    assert again["cached"] is True
    assert again["candidate_id"] == res[2]["candidate_id"]
    assert again["previous_iteration"] == 3
    assert len(CALLS) == calls_before, "cached hit must not invoke the simulator"
    assert con.execute("SELECT COUNT(*) FROM experiments").fetchone()[0] == len(SEQUENCE)

    # force=true is the only way to re-run it
    forced = engine.evaluate(con, "amp_001", {"R1": 70e3, "C1": C_NOM * 1e-9},
                             force=True)
    assert forced["cached"] is False
    assert len(CALLS) == calls_before + 1

    # Same circuit, different simulation conditions = a different experiment (S13).
    other = engine.evaluate(con, "amp_001", {"R1": 70e3, "C1": C_NOM * 1e-9},
                            sim_config={"temp": 85})
    assert other["cached"] is False
    assert other["candidate_id"] == res[2]["candidate_id"], "same design, new experiment"


def test_constraint_repair_moves_the_right_parameter(tmp_path):
    """S12: when nothing feasible exists yet, the next experiment must attack the
    violated constraint using measured sensitivity, in the helpful direction."""
    CALLS.clear()
    con = db.connect(tmp_path)
    engine.start_run(
        con, "tight", NETLIST,
        param_space={"R1": {"min": 10e3, "max": 110e3},
                     "C1": {"min": 5e-9, "max": 15e-9}},
        objectives=[{"metric": "gain_db", "direction": "max"}],
        # unreachable at high R1: bandwidth = 200k - R1, so this forces R1 down
        constraints={"bandwidth": ">180000"},
        sim_config={"backend": "analytic", "model": "test_amp"}, seed=3)

    for r1 in (80e3, 100e3):                    # both violate: bw 120k, 100k
        r = engine.evaluate(con, "tight", {"R1": r1, "C1": 10e-9})
        assert r["feasible"] is False
    # C1 varied once so the unvaried-parameter screen is satisfied and we reach the
    # repair branch; bandwidth doesn't depend on C1, so this stays infeasible.
    assert engine.evaluate(con, "tight", {"R1": 80e3, "C1": 8e-9})["feasible"] is False
    assert engine.get_best(con, "tight")["best_feasible"] is None

    nxt = engine.select_next(con, "tight")
    assert nxt["method"] == "constraint_repair", nxt
    assert "bandwidth" in nxt["rationale"]
    # bandwidth rises as R1 falls, so the proposal must reduce R1
    base = engine.get_best(con, "tight")["best"]["candidate_id"]
    assert nxt["params"]["R1"] < db.jloads(engine.get_candidate(con, base), "params")["R1"]

    # and following its own advice actually reaches feasibility
    out = engine.optimize(con, "tight", iterations=6)
    assert out["best_is_feasible"] is True, out


def test_unvaried_parameter_is_screened_before_exploiting(tmp_path):
    """A parameter nobody has moved has no sensitivity, so the exploit stages can
    never choose it. Without an explicit screen the search fixates on whichever
    parameter the seed happened to move and re-scans it until the budget runs out.

    C1 is decade-spaced here on purpose: that is what a real E-series part list looks
    like, and multiplicative jitter cannot cross a decade gap.
    """
    CALLS.clear()
    con = db.connect(tmp_path)
    engine.start_run(
        con, "screen", NETLIST,
        param_space={"R1": {"min": 10e3, "max": 110e3},
                     "C1": {"values": [1e-10, 1e-9, 1e-8]}},
        objectives=[{"metric": "bandwidth", "direction": "max"}],
        constraints={"bandwidth": ">100000"},
        sim_config={"backend": "analytic", "model": "test_amp"}, seed=4)

    for r1 in (40e3, 60e3):                 # only R1 moves; C1 pinned at one value
        engine.evaluate(con, "screen", {"R1": r1, "C1": 1e-10})
    assert engine.explored(con, "screen")["C1"]["count"] == 1

    nxt = engine.select_next(con, "screen")
    assert nxt["method"] == "probe_unvaried", nxt
    assert nxt["params"]["C1"] != 1e-10, "a decade-spaced list must actually change"
    assert nxt["params"]["C1"] in (1e-9, 1e-8)

    # and the screen is one-shot per parameter, not a permanent hijack of the search
    engine.evaluate(con, "screen", nxt["params"])
    assert engine.select_next(con, "screen")["method"] != "probe_unvaried"

    # end to end: every parameter gets measured, none is left silently frozen
    engine.optimize(con, "screen", iterations=6)
    ex = engine.explored(con, "screen")
    assert ex["C1"]["count"] > 1 and ex["R1"]["count"] > 1
    assert {s["parameter"] for s in engine.sensitivity(con, "screen")} == {"R1", "C1"}

    # bandwidth is maximal at R1's lower bound, so the reported optimum is the box's,
    # not the circuit's -- the summary has to say so or it reads as real convergence
    s = engine.summary(con, "screen")
    assert any("R1" in p and "at min" in p for p in s["at_param_space_bound"]), s


def test_parameter_history_is_correct(run):
    con, _ = run
    _drive(con)
    exp = engine.explored(con, "amp_001")
    assert exp["R1"]["evaluated"] == pytest.approx(
        sorted(x * 100e3 for x, _ in SEQUENCE))
    assert exp["R1"]["count"] == len(SEQUENCE)
    assert exp["C1"]["evaluated"] == pytest.approx([C_NOM * 1e-9])
    gap = exp["R1"]["largest_unexplored_gap"]
    assert gap["low"] < gap["high"] and gap["width"] > 0
    assert not (gap["low"] <= 70e3 <= gap["high"]), "tried values are not 'unexplored'"


def test_pareto_frontier_is_correct(run):
    con, _ = run
    res = _drive(con)
    front = {c["id"] for c in engine.pareto(con, "amp_001")}
    ids = [r["candidate_id"] for r in res]

    assert ids[3] not in front, "infeasible candidate must be excluded by default"
    # bandwidth falls and power rises monotonically with R1, while gain still rises
    # over this range, so every feasible point trades off -- none is dominated.
    assert front == set(ids) - {ids[3]}

    # A strictly worse design (same R1, detuned C1 -> same bw/power, lower gain)
    # must be dominated and therefore excluded.
    dom = engine.evaluate(con, "amp_001", {"R1": 70e3, "C1": 12e-9})
    assert dom["metrics"]["gain_db"] < res[2]["metrics"]["gain_db"]
    assert dom["metrics"]["bandwidth"] == pytest.approx(res[2]["metrics"]["bandwidth"])
    front2 = {c["id"] for c in engine.pareto(con, "amp_001")}
    assert dom["candidate_id"] not in front2
    assert ids[2] in front2

    # Two distinct designs can measure identically (gain is symmetric about C1=10n,
    # bandwidth/power depend only on R1). Both are non-dominated; the frontier should
    # report the trade-off once, not once per design that happens to land on it.
    a = engine.evaluate(con, "amp_001", {"R1": 45e3, "C1": 9e-9})
    b = engine.evaluate(con, "amp_001", {"R1": 45e3, "C1": 11e-9})
    assert a["metrics"]["gain_db"] == pytest.approx(b["metrics"]["gain_db"])
    assert a["candidate_id"] != b["candidate_id"]
    front3 = [c for c in engine.pareto(con, "amp_001")]
    assert a["candidate_id"] in {c["id"] for c in front3}, "the point is on the frontier"
    assert b["candidate_id"] not in {c["id"] for c in front3}, "...but only once"
    pts = [(c["gain_db"], c["bandwidth"], c["power"]) for c in front3]
    assert len(pts) == len(set(pts)), "no duplicated trade-offs anywhere on the frontier"


def test_sensitivity_comes_from_measured_data(run):
    con, _ = run
    _drive(con)
    s = {(r["parameter"], r["metric"]): r for r in engine.sensitivity(con, "amp_001")}

    # bandwidth = 200k*(1 - R1/200k)  ->  d bw / d R1 = -1.0 exactly
    bw = s[("R1", "bandwidth")]
    assert bw["method"] == "finite_difference"
    assert bw["estimated_sensitivity"] == pytest.approx(-1.0)
    assert bw["direction"] == "negative"

    # power = 1m + 4m*(R1/100k)  ->  d P / d R1 = 4e-8 exactly
    pw = s[("R1", "power")]
    assert pw["estimated_sensitivity"] == pytest.approx(4e-8)
    assert pw["confidence"] > 0.5 and pw["n"] >= 2

    # C1 was never varied, so no sensitivity may be invented for it.
    assert ("C1", "gain_db") not in s


def test_failure_memory_records_why(run):
    con, _ = run
    res = _drive(con)
    fails = engine.failed_candidates(con, "amp_001")
    bad = next(f for f in fails if f["candidate_id"] == res[3]["candidate_id"])
    assert bad["failure_type"] == sim.CONSTRAINT_VIOLATION
    v = bad["detail"]["violations"][0]
    assert v["constraint"] == "bandwidth"
    assert v["required"] == ">100000"
    assert v["actual"] == pytest.approx(95e3)

    # a known-bad design is never proposed again
    nxt = engine.select_next(con, "amp_001")
    assert nxt["params"] != {"R1": 105e3, "C1": C_NOM * 1e-9}


def test_restart_loses_nothing(run):
    con, tmp = run
    res = _drive(con)
    before = engine.summary(con, "amp_001")
    front = engine.pareto(con, "amp_001")
    sens = engine.sensitivity(con, "amp_001")
    con.close()                       # simulate an MCP process restart

    con2 = db.connect(tmp)
    assert engine.summary(con2, "amp_001") == before
    assert engine.pareto(con2, "amp_001") == front
    assert engine.sensitivity(con2, "amp_001") == sens
    assert engine.get_best(con2, "amp_001")["best"]["candidate_id"] == \
        res[6]["candidate_id"]
    assert len(engine.history(con2, "amp_001", limit=99)) == len(SEQUENCE)
    # and the byte-exact netlist is still recoverable after the restart
    assert engine.rollback(con2, "amp_001", res[2]["candidate_id"])["netlist_sha"] == \
        engine.get_candidate(con2, res[2]["candidate_id"])["design_fp"]


def test_summary_size_does_not_grow_with_history(run):
    """S14: the LLM must not need the whole iteration history to decide, and the
    context cost of asking must not scale with how long the run has been going."""
    con, _ = run
    _drive(con)
    small = json.dumps(engine.summary(con, "amp_001"))

    engine.optimize(con, "amp_001", iterations=25)
    s = engine.summary(con, "amp_001")
    big = json.dumps(s)

    assert s["iterations_evaluated"] > len(SEQUENCE), "more experiments really ran"
    assert len(big) < len(small) + 120, "summary must not grow with history"
    assert len(big) < 900, f"summary is {len(big)} chars; too fat to be cheap"
    assert not any(isinstance(v, list) and len(v) > 5 for v in s.values())
    assert s["best_candidate"] and s["promising_unexplored_region"]
    # full detail stays reachable, just not by default
    assert len(engine.trace(con, "amp_001").splitlines()) > 5 * s["iterations_evaluated"]


def test_engine_loop_is_deterministic_and_dedups(tmp_path):
    """S12/S19: many local experiments, no LLM in the loop, reproducible with a seed."""
    outs = []
    for i in range(2):
        CALLS.clear()
        con = db.connect(tmp_path / f"proj{i}")
        engine.start_run(
            con, "amp_001", NETLIST,
            param_space={"R1": {"min": 10e3, "max": 110e3},
                         "C1": {"min": 5e-9, "max": 15e-9}},
            objectives=[{"metric": "gain_db", "direction": "max"}],
            constraints={"bandwidth": ">100000"},
            sim_config={"backend": "analytic", "model": "test_amp"}, seed=7)
        engine.evaluate(con, "amp_001", {"R1": 40e3, "C1": 10e-9})
        engine.evaluate(con, "amp_001", {"R1": 60e3, "C1": 10e-9})
        out = engine.optimize(con, "amp_001", iterations=8)
        outs.append((out, engine.explored(con, "amp_001")["R1"]["evaluated"]))
        con.close()

    a, b = outs
    assert a[1] == b[1], "same seed must explore the same points"
    assert a[0]["best_score"] == pytest.approx(b[0]["best_score"])
    assert a[0]["cached_hits"] == 0, "engine must never re-propose an evaluated design"
    assert a[0]["unique_candidates"] == a[0]["iterations_evaluated"]
    assert a[0]["iterations_evaluated"] >= 8


# --- .meas parsing, against real captured LTspice 26.0.2 output -----------------

REAL_LOG = """LTspice 26.0.2 for Windows
Circuit: C:\\tmp\\amp.cir
tnom = 27
temp = 27
method = trap
Total elapsed time: 0.137 seconds.

gdb: MAX(db(V(out)))=(31.6904484673dB,-159.417738334\u00b0) FROM 1 TO 10000000
gmag: MAX(mag(V(out)))=(-1.71452601918e-10dB,0\u00b0) FROM 1 TO 10000000
bw: mag(V(out))=0.7071  AT 159158.003411
gdb_at: db(V(out)) =(-25.2601879309dB,-90.1799982235\u00b0) at 1000
vstep: V(out) =0.140915020014 at 6.666666667e-05
power: AVG(-I(V1)*V(vcc))=-5.33214235897e-19 FROM 0 TO 0.005
big: AVG(V(out)*1e7)=50000000 FROM 0 TO 0.005
plain: MAX(V(out))=5 FROM 0 TO 0.005
Measurement "risetime" FAIL'ed
WARNING: Less than two connections to node N007.
"""


def test_parse_real_ltspice_log():
    m, warnings = sim.parse_meas(REAL_LOG)

    # complex AC result -> magnitude in dB, plus phase exposed as <name>_deg
    assert m["gdb"] == pytest.approx(31.6904484673)
    assert m["gdb_deg"] == pytest.approx(-159.417738334)

    # a WHEN/crossing measure yields the crossing point, not the 0.7071 trigger level
    assert m["bw"] == pytest.approx(159158.003411)

    # FIND..AT 1k prints BOTH a complex value and "at 1000"; the 1000 is the
    # frequency we asked for, so the complex form must win over the AT rule
    assert m["gdb_at"] == pytest.approx(-25.2601879309)

    # A TRAN `FIND V(out) AT t` has no parens to disambiguate it, and the identical
    # shape to `bw` above with the opposite answer: here the number after `=` is the
    # result and the one after "at" is the time we asked for. LTspice separates the
    # two cases by case alone -- uppercase AT found it, lowercase at was specified.
    # Getting this backwards returns the sample time as the voltage, which plots as a
    # perfectly plausible straight line.
    assert m["vstep"] == pytest.approx(0.140915020014)

    # plain TRAN scalars, incl. tiny and large magnitudes (no engineering suffixes)
    assert m["power"] == pytest.approx(-5.33214235897e-19)
    assert m["big"] == pytest.approx(5e7)
    assert m["plain"] == pytest.approx(5.0)

    # log settings are not metrics
    for k in ("tnom", "temp", "method", "total", "circuit"):
        assert k not in m

    assert any("risetime" in w for w in warnings)
    assert any("N007" in w for w in warnings)


def test_the_options_echo_is_not_a_metric():
    """LTspice echoes `Options:plotwinsize=0` into the log, which parsed as a metric
    named `options` worth 0.0. That line appears in every netlist written for waveform
    measurement, so the phantom rode along on most candidates -- and a phantom metric is
    scorable and constrainable, so it can decide a run."""
    m, _ = sim.parse_meas("LTspice 26.0.2 for Windows\nOptions:plotwinsize=0\n"
                          "Files loaded:\nreal: MAX(V(out))=5 FROM 0 TO 1\n")
    assert m == {"real": 5.0}, f"phantom metrics: {set(m) - {'real'}}"


STEPPED_LOG = """LTspice 26.0.2 for Windows
Circuit: C:\\tmp\\mc.cir
Measurement: fc
  step\tmag(V(out))=0.7071
     1\t1603.35725121
     2\t1580.96287678
     3\t1576.74732269

Measurement: gdc
  step\tmag(V(out)) \tat
     1\t(-0.00016895847895dB,0\u00b0)\t10
     2\t(-0.000173754333842dB,0\u00b0)\t10
     3\t(-0.000174679696595dB,0\u00b0)\t10

Total elapsed time: 0.4 seconds.
"""


def test_a_stepped_log_yields_one_value_per_run():
    """Captured from a real 4-run Monte Carlo. A `.step`ped log reports each measure as a
    table and carries no `name: value` line at all, so `parse_meas` sees nothing -- which
    is what Monte Carlo and corner sweeps need to read."""
    steps = sim.parse_meas_steps(STEPPED_LOG)
    assert steps["fc"] == pytest.approx([1603.35725121, 1580.96287678, 1576.74732269])
    # the complex column still yields magnitude, and the trailing "at 10" is not a result
    assert steps["gdc"] == pytest.approx([-0.00016895847895, -0.000173754333842,
                                          -0.000174679696595])
    assert "total" not in steps


def test_a_stepped_log_warns_instead_of_silently_scoring_nothing():
    """A scored candidate needs one value. Returning `{}` with no warning reads as
    "simulation produced no measurements", pointing at the netlist instead of the .step."""
    m, warnings = sim.parse_meas(STEPPED_LOG)
    assert m == {}
    assert any("stepped" in w and "3 runs" in w for w in warnings), warnings


def _narrow_run(con):
    """bandwidth = 200k - R1 exactly, so the window below is R1 in 49k..51k -- a 2%
    slice of the 100k range, narrower than one 25% step. gain_db peaks at R1=85k, so
    the objective pulls away from it."""
    engine.start_run(
        con, "narrow", NETLIST,
        param_space={"R1": {"min": 10e3, "max": 110e3},
                     "C1": {"min": 5e-9, "max": 15e-9}},
        objectives=[{"metric": "gain_db", "direction": "max"}],
        constraints=[{"metric": "bandwidth", "op": ">", "value": 149e3},
                     {"metric": "bandwidth", "op": "<", "value": 151e3},
                     {"metric": "power", "op": "<", "value": 0.005}],
        sim_config={"backend": "analytic", "model": "test_amp"}, seed=7)


def test_step_shrinks_to_enter_a_tolerance_window(tmp_path):
    """A tolerance window narrower than one step is unreachable at fixed granularity:
    the search brackets it forever and reports a confident near-miss."""
    con = db.connect(tmp_path / "anneal")
    _narrow_run(con)
    out = engine.optimize(con, "narrow", iterations=60)
    assert out["best_is_feasible"], out["constraints"]
    assert out["step_frac_final"] < engine.STEP_FRAC, "step never refined"
    bw = out["best_metrics"]["bandwidth"]
    assert 149e3 < bw < 151e3

    # same budget, same seed, step pinned at the default: brackets but can't enter
    con2 = db.connect(tmp_path / "fixed")
    _narrow_run(con2)
    for _ in range(60):
        nxt = engine.select_next(con2, "narrow", engine.STEP_FRAC)
        engine.evaluate(con2, "narrow", nxt["params"], nxt["parent_id"])
    assert engine.get_best(con2, "narrow")["best_feasible"] is None


def test_equally_strong_parameters_both_get_moved(tmp_path):
    """Greedy coordinate descent starves a tied coordinate: a continuous parameter
    always has another untried value, so the runner-up is never reached. Two resistors
    with identical physics rank identically -- in a real filter run R2 sat at its seed
    value for all 61 iterations while R1 did all the moving."""
    @sim.register_analytic("twin")
    def _twin(p):                                  # symmetric in R1/R2, so tied ranks
        return {"fsum": 1e6 / (p["R1"] + p["R2"])}

    con = db.connect(tmp_path)
    engine.start_run(
        con, "twin", "R1 a b {R1}\nR2 b c {R2}\n",
        param_space={"R1": {"min": 100.0, "max": 10e3},
                     "R2": {"min": 100.0, "max": 10e3}},
        objectives=[{"metric": "fsum", "direction": "max"}],
        sim_config={"backend": "analytic", "model": "twin"}, seed=1)
    engine.evaluate(con, "twin", {"R1": 8e3, "R2": 8e3})

    for _ in range(12):
        nxt = engine.select_next(con, "twin")
        engine.evaluate(con, "twin", nxt["params"], nxt["parent_id"])

    seen = [json.loads(r["params"]) for r in con.execute(
        "SELECT params FROM designs WHERE run_id=?", ("twin",)).fetchall()]
    assert len({p["R1"] for p in seen}) > 1, "R1 never moved"
    assert len({p["R2"] for p in seen}) > 2, "R2 starved by its identical twin"


def test_leverage_ranking_is_unit_free():
    """Ranking on raw d(metric)/d(param) compares farads against ohms and always picks
    the capacitor, whatever the physics. Scale by the range instead."""
    cap = {"estimated_sensitivity": -2.4e11, "confidence": 1.0}   # per farad
    res = {"estimated_sensitivity": -0.045, "confidence": 1.0}    # per ohm
    cap_spec, res_spec = {"values": [1e-9, 10e-9]}, {"min": 500.0, "max": 20e3}
    assert abs(cap["estimated_sensitivity"]) > abs(res["estimated_sensitivity"]) * 1e8
    # ...yet a full-range move of either is worth a comparable amount of the metric
    assert 0.3 < engine._leverage(cap, cap_spec) / engine._leverage(res, res_spec) < 3


def test_collision_retries_the_direction_instead_of_random_walking(tmp_path):
    """The best-ranked move being already-evaluated must not discard the direction.

    Because the base is usually unchanged next iteration, falling through to jitter
    re-proposes the same collision forever: a real 61-iteration filter run spent 55 of
    them jittering, and neither resistor left 7k..11k of a 500..20k range while the
    answer sat at 1.1k."""
    con = db.connect(tmp_path)
    _narrow_run(con)
    methods = []
    for _ in range(25):
        nxt = engine.select_next(con, "narrow", engine.STEP_FRAC)
        methods.append(nxt["method"])
        engine.evaluate(con, "narrow", nxt["params"], nxt["parent_id"])

    directed = sum(m in ("constraint_repair", "objective_push") for m in methods)
    assert directed > len(methods) * 0.6, f"mostly undirected: {methods}"

    # and the walk must actually be able to leave the seed's neighbourhood
    r1 = [json.loads(r["params"])["R1"] for r in con.execute(
        "SELECT params FROM designs WHERE run_id=?", ("narrow",)).fetchall()]
    assert max(r1) - min(r1) > 40e3, f"never explored: {min(r1):.0f}..{max(r1):.0f}"


def test_directed_step_on_a_discrete_list_actually_moves():
    """A decade-spaced list is the real-world case (E12 parts) and the one where a
    span-fraction step rounds back to where it started, making the move a silent
    no-op. Verified against the C2 list that stalled a real Sallen-Key run."""
    e12 = {"values": [1e-9, 2.2e-9, 4.7e-9, 10e-9]}

    # slope>0, want_up=False -> decrease. 0.25 * span(9n) = 2.25n; 10n-2.25n = 7.75n,
    # whose nearest allowed value is 10n again. Must land on 4.7n, the adjacent entry.
    assert engine._move(e12, 10e-9, 0.25, 1.0, False) == pytest.approx(4.7e-9)
    assert engine._move(e12, 4.7e-9, 0.25, 1.0, True) == pytest.approx(10e-9)
    # a negative slope inverts which direction helps
    assert engine._move(e12, 4.7e-9, 0.25, -1.0, True) == pytest.approx(2.2e-9)
    # at the end of the list there is nowhere to go; _can_move is what avoids this
    assert engine._move(e12, 10e-9, 0.25, 1.0, True) == pytest.approx(10e-9)
    # continuous parameters keep the fractional step
    cont = {"min": 500.0, "max": 20e3}
    assert engine._move(cont, 10e3, 0.25, 1.0, True) == pytest.approx(14875.0)
    assert engine._move(cont, 10e3, 1.0, 1.0, False) == pytest.approx(500.0)  # clamped


def test_lint_catches_silent_db_conversion():
    """Both AC forms that return a log instead of a number, verified on LTspice 26.0.2.
    Neither ever errors, so only the lint stands between them and a corrupt metric."""
    assert sim.lint_netlist(".ac dec 10 1 1k\n.meas AC g MAX db(V(out))\n")
    assert sim.lint_netlist(".meas ac ferr param abs(fc-15915)\n")
    # the correct idioms must stay silent
    assert sim.lint_netlist(".meas AC g MAX mag(V(out))\n") == []
    assert sim.lint_netlist(".meas TRAN e PARAM abs(vout-5)\n") == []
    # db() under TRAN is legitimate -- TRAN results are not pre-converted
    assert sim.lint_netlist(".meas TRAN g MAX db(V(out))\n") == []


@pytest.mark.parametrize("log,kind", [
    ("Analysis: Fatal error - iteration limit reached", sim.CONVERGENCE_FAILURE),
    ("Fewer than two connections to node N004.", sim.STRUCTURAL_ERROR),
    ("Matrix is singular at node V(mid)", sim.NUMERICAL_INSTABILITY),
    ("Total elapsed time: 0.2 seconds", None),
])
def test_failure_classification(log, kind):
    got, msg, detail = sim.classify_log(log)
    assert got == kind
    if kind is sim.STRUCTURAL_ERROR:
        assert detail["suspected_nodes"] == ["N004"]


def test_topology_fp_ignores_values_design_fp_does_not():
    a, _ = ir.render(NETLIST, {"R1": 10e3, "C1": 1e-9})
    b, _ = ir.render(NETLIST, {"R1": 20e3, "C1": 1e-9})
    assert ir.topology_fp(a) == ir.topology_fp(b)
    assert ir.design_fp(a, {"R1": 10e3}) != ir.design_fp(b, {"R1": 20e3})
    # comments and whitespace are not identity
    assert ir.design_fp("* hi\nR1 a b 1k\n", {}) == ir.design_fp("r1  a b 1k", {})
