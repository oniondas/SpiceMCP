"""Checks for the DC bias audit, PVT corners and Monte Carlo yield.

Three layers, and the split is deliberate:

* **Deck rewriting and netlist reading** are pure string work, so they are tested
  directly. Every bug this module has actually had lived here -- a swallowed title line,
  a case-sensitive `DC` -- and none of them needed a simulator to find.
* **The bias verdicts** are tested against a hand-built `raw.Wave`, which reaches every
  region (saturated / triode / cutoff / PMOS / NPN / PNP / saturated BJT / unknown) at
  exactly the bias each branch cares about. A fixture per region would be nine LTspice
  runs to test arithmetic.
* **The whole path** runs against the golden `op_mos.raw`, with `sim.simulate` stubbed,
  so `audit_dc_operating_points` is covered end to end with no LTspice installed. The
  few tests that genuinely need LTspice are marked and skip cleanly.
"""

from __future__ import annotations

import math
from pathlib import Path

import pytest

from spicemcp import raw, robustness as rb, sim

FX = Path(__file__).parent / "fixtures"

needs_ltspice = pytest.mark.skipif(sim.find_ltspice() is None,
                                  reason="LTspice not installed")

NMOS = ".model NM NMOS(VTO=0.7 KP=200u)"
PMOS = ".model PM PMOS(VTO=-0.7 KP=100u)"
NPN = ".model NP NPN(BF=200 IS=1e-15)"
PNP = ".model PP PNP(BF=150 IS=1e-15)"


def wave(**nodes: float) -> raw.Wave:
    """A one-point `.op` result carrying exactly the node voltages named."""
    names = [f"V({n})" for n in nodes]
    return raw.Wave(plotname="Operating Point", flags=["real"], names=names,
                    kinds=["voltage"] * len(names), x=[0.0],
                    data={f"V({n})": [v] for n, v in nodes.items()})


# ------------------------------------------------------------------ netlist inspection

def test_terminal_order_is_read_off_the_element_line():
    devs = rb.devices(f"M1 drn gat src bulk NM L=2u W=10u\n{NMOS}\n")
    assert devs == [{"name": "M1", "kind": "mos", "model": "nm",
                     "nodes": {"d": "drn", "g": "gat", "s": "src", "b": "bulk"}}]


def test_a_bjt_with_a_substrate_node_still_finds_its_model():
    """LTspice allows `Qname c b e [subs] model`, so the model is not the 5th field.

    Reading it positionally names the substrate node as the model, and the device then
    reports `unknown` -- a silently unaudited transistor.
    """
    three = rb.devices(f"Q1 col bas emi NP\n{NPN}\n")[0]
    four = rb.devices(f"Q2 col bas emi sub NP\n{NPN}\n")[0]
    assert three["model"] == "np" and three["nodes"] == {"c": "col", "b": "bas",
                                                        "e": "emi"}
    assert four["model"] == "np" and four["nodes"]["subs"] == "sub"


def test_trailing_key_value_pairs_are_not_mistaken_for_the_model():
    d = rb.devices(f"Q1 c b e NP area=2 temp=50\n{NPN}\n")[0]
    assert d["model"] == "np"


def test_continuation_lines_are_joined_before_the_terminals_are_read():
    d = rb.devices(f"M1 d g s\n+ b NM L=2u\n{NMOS}\n")[0]
    assert d["nodes"] == {"d": "d", "g": "g", "s": "s", "b": "b"}


def test_comments_and_directives_are_not_devices():
    assert rb.devices("* M1 is commented out\n.model M1 NMOS(VTO=0.7)\nR1 a b 1k\n") == []


def test_model_params_come_back_as_numbers_with_suffixes_resolved():
    m = rb.models(".model NM NMOS(VTO=0.7 KP=200u LAMBDA=0.01 LEVEL=1)\n")["nm"]
    assert m["type"] == "nmos" and m["vto"] == 0.7 and m["kp"] == 200e-6


@pytest.mark.parametrize("card,vth,why", [
    (".model NM NMOS(VTO=0.7)", 0.7, "vto"),
    (".model NM NMOS(VTH=0.55)", 0.55, "vth"),
    (".model NM NMOS(LEVEL=8 VTH0=0.42)", None, "BSIM"),
    (".model NM NMOS(KP=200u)", None, "no VTO/VTH"),
])
def test_vth_is_read_or_refused_never_guessed(card, vth, why):
    """A BSIM card carries VTH0, which is not the threshold -- short-channel and body
    effects move it by hundreds of millivolts. Reporting it would be a wrong `saturated`.
    """
    got, src = rb._vth(rb.models(card)["nm"])
    assert got == vth
    assert why in src


# ---------------------------------------------------------------------- bias verdicts

@pytest.mark.parametrize("d,g,s,region,verdict", [
    (3.0, 1.5, 0.0, "saturated", "pass"),      # Vds 3 >> Vov 0.8
    (0.2, 1.5, 0.0, "triode", "fail"),         # Vds 0.2 << Vov 0.8
    (3.0, 0.3, 0.0, "cutoff", "fail"),         # Vgs below Vth
    (0.82, 1.5, 0.0, "saturated (no margin)", "fail"),   # Vds 0.82, Vov 0.8, margin 0.05
    (0.9, 1.5, 0.0, "saturated", "pass"),      # 0.1 V of margin: just enough
])
def test_every_nmos_region_is_reached_at_the_bias_that_defines_it(d, g, s, region,
                                                                 verdict):
    dev = {"name": "M1", "kind": "mos", "model": "nm",
           "nodes": {"d": "d", "g": "g", "s": "s", "b": "s"}}
    r = rb._audit_one(dev, rb.models(NMOS)["nm"], wave(d=d, g=g, s=s), 0.05)
    assert (r["region"], r["verdict"]) == (region, verdict), r["reason"]


def test_a_pmos_is_audited_with_every_sign_flipped():
    """Source at Vdd, gate pulled down: Vgs and Vds are both negative and both correct.

    Auditing a PMOS with NMOS signs reports `cutoff` for a device that is conducting
    hard, which is the failure mode this test exists for. Everything comes back as a
    magnitude -- including VTO=-0.7 as 0.7 -- so one comparison serves both polarities.
    """
    dev = {"name": "M1", "kind": "mos", "model": "pm",
           "nodes": {"d": "d", "g": "g", "s": "vdd", "b": "vdd"}}
    r = rb._audit_one(dev, rb.models(PMOS)["pm"], wave(d=2.0, g=3.0, vdd=5.0), 0.05)
    assert r["verdict"] == "pass" and r["region"] == "saturated"
    assert r["vgs"] == pytest.approx(2.0)      # -(3-5)
    assert r["vds"] == pytest.approx(3.0)      # -(2-5)
    assert r["vth"] == pytest.approx(0.7)      # magnitude, and the reply says so
    assert "magnitudes" in r["sign_convention"]


def test_an_nmos_report_carries_no_sign_convention_note_to_explain_away():
    dev = {"name": "M1", "kind": "mos", "model": "nm",
           "nodes": {"d": "d", "g": "g", "s": "0", "b": "0"}}
    assert "sign_convention" not in rb._audit_one(
        dev, rb.models(NMOS)["nm"], wave(d=3.0, g=1.5), 0.05)


@pytest.mark.parametrize("c,b,e,model,region,verdict", [
    (5.0, 0.75, 0.0, NPN, "active", "pass"),
    (0.1, 0.85, 0.0, NPN, "saturation", "fail"),           # Vbc +0.75: BC conducting
    (5.0, 0.10, 0.0, NPN, "cutoff", "fail"),
    (0.80, 0.82, 0.0, NPN, "active (no margin)", "fail"),   # Vbc +0.02: not conducting
    (0.80, 0.79, 0.0, NPN, "active (no margin)", "fail"),   # Vbc -0.01: inside margin
])
def test_every_npn_region_is_reached(c, b, e, model, region, verdict):
    dev = {"name": "Q1", "kind": "bjt", "model": "np",
           "nodes": {"c": "c", "b": "b", "e": "e"}}
    r = rb._audit_one(dev, rb.models(model)["np"], wave(c=c, b=b, e=e), 0.05)
    assert (r["region"], r["verdict"]) == (region, verdict), r["reason"]


def test_a_pnp_in_the_active_region_is_not_read_as_cutoff():
    dev = {"name": "Q1", "kind": "bjt", "model": "pp",
           "nodes": {"c": "c", "b": "b", "e": "vdd"}}
    r = rb._audit_one(dev, rb.models(PNP)["pp"], wave(c=1.0, b=4.25, vdd=5.0), 0.05)
    assert r["verdict"] == "pass" and r["region"] == "active"
    assert r["vbe"] == pytest.approx(0.75)     # -(4.25-5)
    assert r["vce"] == pytest.approx(4.0)      # -(1-5)


def test_a_library_device_is_unknown_and_says_why_rather_than_passing():
    dev = {"name": "M1", "kind": "mos", "model": "libfet",
           "nodes": {"d": "d", "g": "g", "s": "0", "b": "0"}}
    r = rb._audit_one(dev, {}, wave(d=3.0, g=1.5), 0.05)
    assert r["verdict"] == "unknown" and r["region"] is None
    assert "library or subcircuit" in r["reason"]


def test_a_bsim_device_is_unknown_even_though_its_bias_is_known():
    dev = {"name": "M1", "kind": "mos", "model": "bs",
           "nodes": {"d": "d", "g": "g", "s": "0", "b": "0"}}
    model = rb.models(".model BS NMOS(LEVEL=8 VTH0=0.42 TOX=4n)")["bs"]
    r = rb._audit_one(dev, model, wave(d=3.0, g=1.5), 0.05)
    assert r["verdict"] == "unknown" and "BSIM" in r["reason"]
    assert r["vds"] == pytest.approx(3.0)      # the bias is still reported


def test_a_node_missing_from_the_op_results_is_unknown_not_a_crash():
    dev = {"name": "M1", "kind": "mos", "model": "nm",
           "nodes": {"d": "nowhere", "g": "g", "s": "0", "b": "0"}}
    r = rb._audit_one(dev, rb.models(NMOS)["nm"], wave(g=1.5), 0.05)
    assert r["verdict"] == "unknown" and "not in the .op results" in r["reason"]


def test_ground_is_zero_volts_without_appearing_in_the_raw():
    """LTspice does not write V(0) -- treating its absence as a missing node would make
    every source-grounded transistor `unknown`, i.e. every common-source amplifier."""
    assert rb._node_v(wave(d=1.0), "0") == 0.0
    assert rb._node_v(wave(d=1.0), "gnd") == 0.0


def test_a_body_effect_model_passes_with_a_caveat_not_silently():
    dev = {"name": "M1", "kind": "mos", "model": "nm",
           "nodes": {"d": "d", "g": "g", "s": "s", "b": "0"}}
    model = rb.models(".model NM NMOS(VTO=0.7 KP=200u GAMMA=0.5 PHI=0.6)")["nm"]
    r = rb._audit_one(dev, model, wave(d=3.0, g=2.5, s=1.0), 0.05)
    assert r["verdict"] == "pass" and "GAMMA" in r["caveat"]


def test_the_margin_is_what_separates_saturated_from_no_margin():
    """Zero margin calls a boundary device saturated; the default 50 mV does not. A
    device on the boundary at 27 C is in triode at 125 C, which is the whole point."""
    dev = {"name": "M1", "kind": "mos", "model": "nm",
           "nodes": {"d": "d", "g": "g", "s": "0", "b": "0"}}
    w, m = wave(d=0.81, g=1.5), rb.models(NMOS)["nm"]
    assert rb._audit_one(dev, m, w, 0.0)["verdict"] == "pass"
    assert rb._audit_one(dev, m, w, 0.05)["verdict"] == "fail"


# ------------------------------------------------------------------ audit, end to end

def test_the_audit_reads_the_golden_op_raw_and_grades_both_devices(monkeypatch):
    """`op_mos.raw` is real LTspice output; its own header claims the NMOS is saturated
    and it is not (Vd = 98 mV, Vgs-Vth = 1.3 V). That mislabel is the reason this audit
    exists, so the fixture doubles as the regression."""
    nl = (FX / "op_mos.cir").read_text()
    monkeypatch.setattr(sim, "simulate", lambda *a, **k: sim.SimResult(
        status="ok", raw_path=str(FX / "op_mos.raw")))
    r = rb.audit_dc_operating_points(nl, "build/unused")
    assert r["status"] == "ok" and r["n_devices"] == 2
    by = {d["name"]: d for d in r["devices"]}
    assert by["M1"]["region"] == "triode" and by["M1"]["verdict"] == "fail"
    assert by["Q1"]["region"] == "active" and by["Q1"]["verdict"] == "pass"
    assert r["all_in_region"] is False and r["n_fail"] == 1
    assert r["violations"] == [f"M1: {by['M1']['reason']}"]


def test_a_netlist_with_nothing_active_is_not_run_at_all(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("launched LTspice to audit a resistor divider")

    monkeypatch.setattr(sim, "simulate", boom)
    r = rb.audit_dc_operating_points("V1 in 0 5\nR1 in out 1k\nR2 out 0 1k\n", "build/x")
    assert r["status"] == "ok" and r["all_in_region"] and r["devices"] == []


def test_a_failed_audit_reports_the_log_line_that_names_the_fix(tmp_path):
    log = tmp_path / "x.log"
    log.write_text("Circuit: * t\nM1 d g 0 0 NM\n"
                   "x.cir(4): Node or model name expected.\n           ^^^^^\n")
    assert "Node or model name expected." in " ".join(rb._log_excerpt(str(log)))
    assert rb._log_excerpt(None) == [] and rb._log_excerpt(str(tmp_path / "no")) == []


# -------------------------------------------------------------------- deck rewriting

@pytest.mark.parametrize("rewrite", [
    lambda n: rb._with_op(n),
    lambda n: rb._with_step(n, 5),
    lambda n: rb._corner_netlist(n, 85.0, None, 1.0, None)[0],
])
def test_no_rewriter_lets_the_first_line_be_eaten_as_the_title(rewrite):
    """SPICE ignores line 1 of a deck. Every rewriter here drops directives, so without
    a synthesised title the new first line vanishes: this exact netlist lost its `.model`
    card to the title slot and LTspice answered `Node or model name expected`.
    """
    nl = ".tran 0 1m\n.model NM NMOS(VTO=0.7)\nM1 d g 0 0 NM\nV1 d 0 5\n"
    out = rewrite(nl)
    assert out.splitlines()[0].startswith("*")
    for kept in (".model NM NMOS(VTO=0.7)", "M1 d g 0 0 NM", "V1 d 0 5"):
        assert kept in out
    assert out.count(".end") == 1


def test_an_existing_comment_line_is_left_as_the_title():
    out = rb._with_op("* my circuit\nV1 a 0 5\nR1 a 0 1k\n.tran 1m\n")
    assert out.splitlines()[0] == "* my circuit"
    assert out.splitlines()[1] == "V1 a 0 5"


def test_with_op_replaces_the_analysis_instead_of_running_it():
    out = rb._with_op("* c\nV1 a 0 5\n.tran 0 1m\n.ac dec 10 1 1k\n.step param x 1 3 1\n")
    assert ".op" in out and ".tran" not in out and ".ac" not in out
    assert ".step" not in out


def test_with_step_replaces_an_existing_step_rather_than_adding_a_second():
    out = rb._with_step("* c\nV1 a 0 5\n.step param run 1 99 1\n", 7)
    assert out.count(".step") == 1 and ".step param run 1 7 1" in out


@pytest.mark.parametrize("line,expect,changed", [
    ("Vdd vdd 0 DC 5", "Vdd vdd 0 DC 4.5", True),         # uppercase DC: the real bug
    ("Vdd vdd 0 dc 5", "Vdd vdd 0 dc 4.5", True),
    ("Vdd vdd 0 5", "Vdd vdd 0 4.5", True),
    ("vdd vdd 0 5V", "vdd vdd 0 4.5", True),              # a suffix is still a number
    ("Vdd vdd 0 5 ; the rail", "Vdd vdd 0 4.5 ; the rail", True),
    ("Vdd vdd 0 PULSE(0 5 0 1n)", "Vdd vdd 0 PULSE(0 5 0 1n)", False),
    ("Vother o 0 DC 5", "Vother o 0 DC 5", False),        # a different source
    ("Rdd vdd 0 5", "Rdd vdd 0 5", False),                # not a source at all
])
def test_only_the_named_supplys_dc_value_is_scaled(line, expect, changed):
    """The uppercase case is not hypothetical: `re` without `re.I` failed the optional
    `dc` group, so `DC` landed in the value slot, nothing was scaled, and the corner
    report still said `supply_applied: True`."""
    assert rb._scale_source(line, "Vdd", 0.9) == (expect, changed)


def test_a_supply_that_is_not_in_the_netlist_reads_as_not_applied():
    _, scaled = rb._corner_netlist("* c\nV1 a 0 5\n", 27.0, "Vbogus", 0.9, None)
    assert scaled is False
    notes = rb._corner_notes(False, "Vbogus", "* c\nV1 a 0 5\n", ["SS", "FF"])
    assert any("Vbogus" in n and "NOT scaled" in n for n in notes)


def test_a_unity_scale_counts_as_applied_because_nothing_needed_changing():
    _, scaled = rb._corner_netlist("* c\nV1 a 0 5\n", 27.0, "V1", 1.0, None)
    assert scaled is True


def test_an_existing_temp_directive_is_replaced_not_duplicated():
    out, _ = rb._corner_netlist("* c\nV1 a 0 5\n.temp 27\n", 125.0, None, 1.0, None)
    assert out.count(".temp") == 1 and ".temp 125" in out


def test_a_netlist_with_no_temp_directive_gets_one():
    out, _ = rb._corner_netlist("* c\nV1 a 0 5\n.end\n", -40.0, None, 1.0, None)
    assert ".temp -40" in out and out.count(".end") == 1


def test_a_corner_library_line_is_pulled_in():
    out, _ = rb._corner_netlist("* c\nV1 a 0 5\n", 27.0, None, 1.0, ".lib ss.lib")
    assert ".lib ss.lib" in out


# --------------------------------------------------------------------- corner rollup

def _rows(**per_corner: float) -> list[dict]:
    return [{"corner": c, "status": "ok", "metrics": {"fc": v}}
            for c, v in per_corner.items()]


def test_the_worst_corner_is_the_furthest_from_nominal_in_either_direction():
    w = rb._worst_case(_rows(TT=10000.0, SS=9000.0, FF=10500.0))["fc"]
    assert (w["min"], w["min_corner"]) == (9000.0, "SS")
    assert (w["max"], w["max_corner"]) == (10500.0, "FF")
    assert w["worst_corner"] == "SS"                       # -10% beats +5%
    assert w["worst_deviation_pct"] == pytest.approx(-10.0)
    assert w["spread_pct"] == pytest.approx(15.0)


def test_a_tie_reports_the_corner_that_ran_first_not_the_alphabetical_one():
    """SS and SF are the same simulation when no process models are supplied. Bare
    `min()` on a `(value, corner)` tuple broke the tie on the *name* and credited SF."""
    w = rb._worst_case(_rows(TT=10000.0, SS=9500.0, SF=9500.0))["fc"]
    assert w["min_corner"] == "SS"


def test_the_rollup_falls_back_to_the_first_corner_when_tt_did_not_run():
    rows = _rows(SS=9000.0, FF=11000.0)
    w = rb._worst_case(rows)["fc"]
    assert w["nominal"] == 9000.0 and w["max_corner"] == "FF"


def test_a_metric_measured_in_no_corner_is_absent_rather_than_zero():
    assert rb._worst_case(_rows(TT=1.0), metrics=["ghost"]) == {}


def test_a_spec_is_checked_against_every_corner_not_the_average():
    """A design that holds at TT and fails at SS is a failing design. Checking the mean
    passes it."""
    rows = _rows(TT=10000.0, SS=8000.0, FF=10100.0)
    worst = rb._worst_case(rows)
    c = rb._spec_checks({"fc": (9000, 11000)}, worst, rows)[0]
    assert c["passed"] is False and c["failing_corners"] == ["SS"]


def test_a_one_sided_spec_is_an_upper_limit():
    rows = _rows(TT=1.0, SS=3.0)
    checks = rb._spec_checks({"fc": 2.0}, rb._worst_case(rows), rows)
    assert checks[0]["limits"] == [None, 2.0] and checks[0]["failing_corners"] == ["SS"]


def test_a_spec_on_a_metric_nobody_measured_fails_loudly():
    rows = _rows(TT=1.0)
    c = rb._spec_checks({"ghost": (0, 1)}, rb._worst_case(rows), rows)[0]
    assert c["passed"] is False and "not measured" in c["reason"]


def test_the_notes_say_which_axes_did_not_run():
    notes = " ".join(rb._corner_notes(False, None, "V1 a 0 5\nR1 a 0 1k\n"))
    assert "process corners NOT applied" in notes
    assert "supply axis NOT applied" in notes
    assert "no active devices" in notes
    with_lib = " ".join(rb._corner_notes(True, "V1", f"M1 d g 0 0 NM\n{NMOS}\n"))
    assert with_lib == ""


# --------------------------------------------------------------- tolerance / statistics

def test_every_passive_gets_its_own_mc_call_at_the_right_tolerance():
    nl, comps = rb.tolerance_netlist(
        "* c\nR1 a b 10k\nC1 b 0 100n\nL1 b c 1m\nV1 a 0 5\n", 0.01, 0.05)
    assert "{mc(10000.0,0.01)}" in nl and "{mc(1e-07,0.05)}" in nl
    assert "{mc(0.001,0.01)}" in nl            # L follows r_tol unless told otherwise
    assert "V1 a 0 5" in nl                    # a source is not a tolerance
    assert [c["name"] for c in comps] == ["R1", "C1", "L1"]


def test_a_component_whose_value_is_an_expression_is_reported_not_toleranced():
    nl, comps = rb.tolerance_netlist("* c\nR1 a b {RV}\nR2 b 0 1k\n", 0.01, 0.05)
    assert "{RV}" in nl and "mc(" in nl
    skipped = [c for c in comps if c["tol"] is None]
    assert [c["name"] for c in skipped] == ["R1"]
    assert "expression" in skipped[0]["reason"]


def test_a_monte_carlo_with_nothing_random_is_refused_not_reported_as_a_pass():
    with pytest.raises(rb.RobustnessError, match="nothing to randomise"):
        rb.evaluate_monte_carlo("* c\nR1 a b {RV}\nV1 a 0 5\n", "build/x", n=10)


def test_one_run_is_not_a_monte_carlo():
    with pytest.raises(rb.RobustnessError, match=">=2 runs"):
        rb.evaluate_monte_carlo("* c\nR1 a b 1k\n", "build/x", n=1)


def test_yield_counts_the_spec_and_cpk_says_whether_it_was_comfortable():
    """30/30 inside spec is the same yield whether the margin is 10 sigma or 0.1. Cpk is
    what separates them, which is why a bare pass count is not the whole answer."""
    vals = [10.0 + 0.1 * i for i in range(-5, 6)]          # 9.5 .. 10.5, mean 10
    tight = rb._stats(vals, (9.9, 10.1))
    wide = rb._stats(vals, (5.0, 15.0))
    assert tight["yield_pct"] == pytest.approx(3 / 11 * 100)
    assert wide["yield_pct"] == 100.0
    assert wide["cpk"] > tight["cpk"] and tight["cpk"] < 1.0


def test_the_reported_sample_size_stops_thirty_of_thirty_reading_as_a_hundred_percent():
    s = rb._stats([1.0, 1.0, 1.0], (0.0, 2.0))
    assert s["yield_pct"] == 100.0 and "3/3 pass" in s["yield_ci_note"]
    assert s["yield_at_least_pct"] == pytest.approx(36.8, abs=0.1)   # 0.05 ** (1/3)


def test_statistics_are_the_closed_form_not_a_reimplementation():
    vals = [1.0, 2.0, 3.0, 4.0]
    s = rb._stats(vals)
    assert s["mean"] == pytest.approx(2.5)
    assert s["std"] == pytest.approx(math.sqrt(5 / 3))     # sample sd, n-1
    assert (s["min"], s["max"]) == (1.0, 4.0)
    assert s["sigma3_hi"] == pytest.approx(2.5 + 3 * math.sqrt(5 / 3))
    assert "yield_pct" not in s                            # no spec, no yield claim


def test_a_zero_spread_sample_does_not_divide_by_zero_computing_cpk():
    s = rb._stats([5.0] * 4, (4.0, 6.0))
    assert s["std"] == 0.0 and s["yield_pct"] == 100.0 and "cpk" not in s


@pytest.mark.parametrize("spec,expect", [
    (None, None), ((1, 2), (1, 2)), ([1, 2], (1, 2)), (2.0, (None, 2.0)),
])
def test_a_bare_number_spec_means_an_upper_bound(spec, expect):
    assert rb._bounds(spec) == expect


def test_the_text_histogram_puts_every_sample_in_exactly_one_bin():
    vals = [1.0, 1.5, 2.0, 2.5, 3.0]
    h = rb._hist(vals, bins=4)
    assert sum(c for _, c in h) == len(vals)
    assert rb._hist([7.0] * 3) == [(7.0, 3)]               # no width, no ZeroDivisionError


# ------------------------------------------------------------------- needs a simulator

@needs_ltspice
def test_a_real_op_run_grades_a_common_source_amp_the_way_the_physics_does(tmp_path):
    """Vgs = 1.5 V on a KP=200u W/L=25 device wants 1.6 mA, which 10k cannot pass from a
    5 V rail -- so the drain collapses and M1 is in triode. Closed form, not a guess."""
    nl = ("* cs\nV1 vdd 0 5\nV2 g 0 1.5\nRd vdd d 10k\nM1 d g 0 0 NM L=2u W=50u\n"
          f"{NMOS}\n.op\n.end\n")
    r = rb.audit_dc_operating_points(nl, tmp_path)
    assert r["status"] == "ok"
    m1 = r["devices"][0]
    assert m1["region"] == "triode" and m1["verdict"] == "fail"
    assert m1["vgs"] == pytest.approx(1.5, abs=1e-3)
    assert m1["vds"] < m1["v_overdrive"]


@needs_ltspice
def test_a_real_corner_sweep_moves_a_metric_with_temperature(tmp_path):
    """R1 carries tc=500u, so fc must fall as temperature rises -- and the corner rollup
    must name SS (125 C) as the low one."""
    nl = ("* rc\nV1 in 0 AC 1\nR1 in out 10k tc=500u\nC1 out 0 1.59n\n"
          ".ac dec 100 100 1meg\n.meas AC fc WHEN mag(V(out))=0.7071\n.temp 27\n.end\n")
    r = rb.evaluate_pvt_corners(nl, tmp_path, metrics=["fc"], spec={"fc": (9000, 11000)})
    assert r["corners_ok"] == 5 and not r["failed_corners"]
    w = r["worst_case"]["fc"]
    assert w["min_corner"] == "SS" and w["max_corner"] == "FF"
    assert w["min"] < w["nominal"] < w["max"]
    assert r["pass"] is True
    assert r["process_applied"] is False        # no corner_lib was given, and it says so


@needs_ltspice
def test_a_real_monte_carlo_spread_matches_the_tolerances_it_was_given(tmp_path):
    """One LTspice launch, 60 draws. `mc()` is uniform over +-tol, so the cv of fc is
    sqrt(r_tol^2 + c_tol^2)/sqrt(3) -- checked loosely, because 60 samples of a 3%
    spread resolve the sd to about 10%."""
    nl = ("* rc\nV1 in 0 AC 1\nR1 in out 10k\nC1 out 0 1.59n\n"
          ".ac dec 100 1k 1meg\n.meas AC fc WHEN mag(V(out))=0.7071\n.end\n")
    r = rb.evaluate_monte_carlo(nl, tmp_path, n=60, r_tol=0.01, c_tol=0.05,
                                spec={"fc": (9000, 11000)}, plot=False)
    assert r["status"] == "ok" and r["runs_measured"] == 60
    s = r["statistics"]["fc"]
    expect = math.hypot(0.01, 0.05) / math.sqrt(3) * 100
    assert s["cv_pct"] == pytest.approx(expect, rel=0.35)
    assert s["min"] < s["mean"] < s["max"]
    assert 0 < s["yield_pct"] <= 100
    assert [c["name"] for c in r["components"]] == ["R1", "C1"]


@needs_ltspice
def test_a_stepped_run_with_no_meas_says_so_instead_of_returning_no_samples(tmp_path):
    nl = ("* rc\nV1 in 0 AC 1\nR1 in out 10k\nC1 out 0 1.59n\n"
          ".ac dec 100 1k 1meg\n.end\n")
    r = rb.evaluate_monte_carlo(nl, tmp_path, n=4, plot=False)
    assert r["status"] == "failed" and ".meas" in r["failure_message"]
