"""Checks for the pre-flight feasibility engine.

Every test here is either a bound that must fire or a bound that must *not* fire. The
second half matters more: a checker that blocks a legal design is worse than no checker,
because the answer to a false `infeasible` is to stop trusting the tool.

The filter-order arithmetic is pinned against the textbook 1 dB / 40 dB / 2:1 mask
(Butterworth 8, Chebyshev 5, elliptic 4), so a regression in the elliptic integral shows
up as a wrong number rather than as a slightly different verdict.
"""

from __future__ import annotations

import asyncio
import json
import math

import pytest

from spicemcp import db, engine, feasibility as fz

RC = """* one-pole RC
V1 in 0 AC 1
R1 in out {R1}
C1 out 0 {C1}
.ac dec 100 1 1Meg
.meas AC gain_db MAX mag(V(out))
.meas AC bandwidth WHEN mag(V(out))=0.707 FALL=1
.end
"""

SPACE = {"R1": {"min": 1e3, "max": 1e5}, "C1": {"min": 1e-9, "max": 1e-7}}
GAIN = [{"metric": "gain_db", "direction": "max"}]
# Maximising gain on an all-passive deck earns a warning of its own (the optimum is unity),
# so tests that need a spotless spec maximise the thing an RC can actually trade.
BW = [{"metric": "bandwidth", "direction": "max"}]


def codes(v, severity=fz.ERROR) -> set[str]:
    return {i.code for i in v.violations if i.severity == severity}


# --------------------------------------------------------------- layer 1: static/logical

def test_contradictory_bounds_have_no_solution():
    v = fz.check(GAIN, [{"metric": "fc", "op": ">", "value": 10e3},
                        {"metric": "fc", "op": "<", "value": 9e3}], mode="concept")
    assert v.status == "infeasible" and v.passed is False
    assert "empty_window" in codes(v)
    assert v.suggestions, "an impossible window must come with a way out"


def test_an_open_interval_of_zero_width_is_still_empty():
    """`>5` and `<5` is empty; `>=5` and `<=5` is one point, which is only a warning."""
    strict = fz.check(GAIN, [{"metric": "fc", "op": ">", "value": 5.0},
                             {"metric": "fc", "op": "<", "value": 5.0}], mode="concept")
    closed = fz.check(GAIN, [{"metric": "fc", "op": ">=", "value": 5.0},
                             {"metric": "fc", "op": "<=", "value": 5.0}], mode="concept")
    assert "empty_window" in codes(strict)
    assert not codes(closed)
    assert "single_point_window" in codes(closed, fz.WARN)


def test_tightest_bound_wins_when_several_are_given():
    """Three lower bounds and one upper: only the tightest lower bound decides."""
    v = fz.check(GAIN, [{"metric": "bw", "op": ">", "value": 1e3},
                        {"metric": "bw", "op": ">", "value": 5e3},
                        {"metric": "bw", "op": ">", "value": 20e3},
                        {"metric": "bw", "op": "<", "value": 10e3}], mode="concept")
    assert "empty_window" in codes(v)
    assert "2e+04" in "".join(i.message for i in v.errors)


def test_over_unity_efficiency_is_rejected_on_both_scales():
    pct = fz.check([{"metric": "eff", "direction": "max"}], {"eff": ">120"},
                   mode="concept")
    frac = fz.check([{"metric": "eff", "direction": "max"}], {"eff": ">1.2"},
                    mode="concept")
    exact = fz.check([{"metric": "eff", "direction": "max"}], {"eff": ">=100"},
                     mode="concept")
    assert "over_unity_efficiency" in codes(pct)
    assert "over_unity_efficiency" in codes(frac), "1.2 on a fraction scale is 120%"
    assert "lossless_required" in codes(exact)
    # 95% is hard, not impossible.
    ok = fz.check([{"metric": "eff", "direction": "max"}], {"eff": ">0.93"},
                  mode="concept")
    assert ok.passed and not codes(ok)


def test_power_cannot_be_below_the_load_it_feeds():
    v = fz.check([{"metric": "power", "direction": "min"}], {"power": "<0.001"},
                 mode="concept", technology_params={"vdd": 3.3, "iload": 0.1})
    assert "power_below_load" in codes(v)
    assert v.details["theoretical_min_power"] == pytest.approx(0.33)


def test_param_space_mistakes_are_reported_not_raised():
    v = fz.check(GAIN, param_space={"R1": {"min": 1e5, "max": 1e3},
                                    "C1": {"values": []},
                                    "R2": {"min": 0.0, "max": 1e3},
                                    "R3": {"min": 1e3, "max": 1e3}},
                 mode="concept")
    assert "inverted_bounds" in codes(v)
    assert "empty_values" in codes(v)
    assert "nonpositive_component_bound" in codes(v), "the search evaluates its bounds"
    assert "frozen_param" in codes(v, fz.WARN)


def test_unparseable_inputs_become_violations_not_exceptions():
    v = fz.check(GAIN, {"power": "definitely small"}, mode="concept")
    assert "unparseable_constraint" in codes(v)
    v2 = fz.check([], mode="concept")
    assert "no_objective" in codes(v2)


# ----------------------------------------------------------------- layer 2: the template

def test_a_metric_with_no_meas_directive_is_an_error():
    """The expensive mistake: every candidate scores None and every constraint fails,
    and nothing in the optimization loop says why."""
    v = fz.check(GAIN + [{"metric": "power", "direction": "min"}],
                 {"power": "<0.005"}, SPACE, RC)
    assert "metric_not_measured" in codes(v)
    issue = next(i for i in v.violations if i.code == "metric_not_measured")
    assert issue.details["missing"] == ["power"]
    assert "gain_db" in issue.details["measured"]


def test_ac_phase_needs_no_directive_of_its_own():
    """`gain_db_deg` comes free with `.meas AC gain_db`, so demanding one is wrong."""
    v = fz.check(GAIN, {"gain_db_deg": ">-120"}, SPACE, RC)
    assert "metric_not_measured" not in codes(v)


def test_placeholders_and_param_space_must_agree_both_ways():
    v = fz.check(GAIN, param_space={"R1": {"min": 1e3, "max": 1e5},
                                    "R9": {"min": 1.0, "max": 2.0}},
                 netlist_template=RC)
    assert "placeholder_without_param" in codes(v), "{C1} has no param"
    assert "param_without_placeholder" in codes(v), "R9 is wired to nothing"


def test_a_param_declared_by_dot_param_is_not_missing():
    tmpl = RC.replace(".ac dec", ".param C1=1n\n.ac dec")
    v = fz.check(GAIN, param_space={"R1": {"min": 1e3, "max": 1e5}},
                 netlist_template=tmpl)
    assert "placeholder_without_param" not in codes(v)


def test_gain_above_unity_needs_something_active():
    rc_gain = fz.check([{"metric": "av", "direction": "max"}], {"av": ">10"}, SPACE,
                       RC.replace(".meas AC gain_db MAX mag(V(out))",
                                  ".meas AC av MAX mag(V(out))"))
    assert "gain_without_active" in codes(rc_gain)

    with_vcvs = fz.check([{"metric": "av", "direction": "max"}], {"av": ">10"}, SPACE,
                         RC.replace(".meas AC gain_db MAX mag(V(out))",
                                    "E1 amp 0 out 0 100\n.meas AC av MAX mag(V(amp))"))
    assert "gain_without_active" not in codes(with_vcvs)


def test_an_lc_network_may_exceed_unity_gain():
    """A resonant tank steps voltage up with no active device, so a hard error here
    would be false. It is a warning, and only because the step-up is narrowband."""
    lc = ("V1 in 0 AC 1\nL1 in out 1m\nC1 out 0 1n\nR1 out 0 1k\n"
          ".ac dec 100 1 1Meg\n.meas AC av MAX mag(V(out))\n")
    v = fz.check([{"metric": "av", "direction": "max"}], {"av": ">5"},
                 netlist_template=lc)
    assert "gain_without_active" not in codes(v)
    assert "passive_step_up_only" in codes(v, fz.WARN)


def test_a_well_formed_rc_spec_passes_cleanly():
    v = fz.check(BW, {"gain_db": ">-3"}, SPACE, RC)
    assert v.passed and v.status == "feasible", v.to_dict()
    assert not v.violations
    assert v.details["measured_metrics"] == ["bandwidth", "gain_db"]
    assert v.details["reactive_elements"] == 1


# ------------------------------------------------------------------- layer 3: the physics

def test_filter_order_matches_the_textbook_masks():
    exact = {k: fz.min_filter_order(k, 1.0, 40.0, 1e3, 2e3)
             for k in ("butterworth", "chebyshev", "elliptic")}
    assert math.ceil(exact["butterworth"]) == 8
    assert math.ceil(exact["chebyshev"]) == 5
    assert math.ceil(exact["elliptic"]) == 4
    # D = (10^4 - 1)/(10^0.1 - 1) = 38620.7, Ws = 2:
    #   log10(D)/(2 log10 2) = 7.6185, acosh(sqrt D)/acosh(2) = 4.5361
    assert exact["butterworth"] == pytest.approx(7.6185, abs=1e-3)
    assert exact["chebyshev"] == pytest.approx(4.5361, abs=1e-3)
    assert exact["elliptic"] == pytest.approx(3.3178, abs=1e-3)
    # Each family needs strictly fewer poles than the one before it, for any mask.
    assert exact["butterworth"] > exact["chebyshev"] > exact["elliptic"]


def test_a_two_element_deck_cannot_meet_an_order_eight_mask():
    v = fz.check(GAIN, {"bandwidth": ">1e3"}, SPACE, RC, circuit_type="filter",
                 technology_params={"f_pass": 1e3, "f_stop": 2e3, "ripple_db": 1.0,
                                    "atten_db": 40.0})
    assert "order_below_requirement" in codes(v)
    assert v.details["min_filter_order_needed"] == 8
    assert v.details["max_order_from_topology"] == 1
    assert v.details["min_order_by_family"]["elliptic"] == 4


def test_a_wide_transition_band_needs_no_extra_order():
    """Same ripple and attenuation, a decade of transition band: order 2 is enough,
    and the one-pole RC deck must not be blamed for anything else."""
    v = fz.check(GAIN, {"bandwidth": ">1e3"}, SPACE, RC, circuit_type="filter",
                 technology_params={"f_pass": 1e3, "f_stop": 1e4, "ripple_db": 1.0,
                                    "atten_db": 20.0})
    assert v.details["min_filter_order_needed"] == 2
    assert "order_below_requirement" in codes(v)      # deck has one capacitor


def test_inverted_transition_band_is_caught_before_the_order_math():
    v = fz.check(GAIN, mode="concept", circuit_type="filter",
                 technology_params={"f_pass": 2e3, "f_stop": 1e3, "ripple_db": 1.0,
                                    "atten_db": 40.0})
    assert "transition_band_inverted" in codes(v)
    assert "min_filter_order_needed" not in v.details


def test_missing_filter_inputs_are_skipped_not_passed():
    v = fz.check(GAIN, {"bandwidth": ">1e3"}, SPACE, RC, circuit_type="filter")
    assert v.status == "unverified", "silence about unchecked physics is the bug"
    assert any("filter_order" in s for s in v.checks_skipped)


def test_slew_rate_is_bounded_by_the_current_budget():
    v = fz.check([{"metric": "slew_rate", "direction": "max"}],
                 {"slew_rate": ">1e8"}, circuit_type="opamp", mode="concept",
                 technology_params={"vdd": 1.8, "cload": 10e-12, "itotal": 200e-6})
    assert "slew_rate_above_current_limit" in codes(v)
    assert v.details["slew_current_needed_a"] == pytest.approx(1e-3)
    assert v.details["theoretical_max_slew_rate"] == pytest.approx(2e7)


def test_power_floor_from_slew_rate_blocks_the_budget():
    """100 V/us into 10 pF is 1 mA; at 1.8 V that is 1.8 mW before any bias current.

    The message and suggestion are pinned verbatim because README quotes them; a
    worked example that has drifted from the code is worse than no example.
    """
    v = fz.check([{"metric": "slew_rate", "direction": "max"}],
                 {"slew_rate": ">1e8", "power": "<0.0005"}, circuit_type="opamp",
                 mode="concept", technology_params={"vdd": 1.8, "cload": 10e-12})
    assert "power_below_dynamic_floor" in codes(v)
    assert v.details["theoretical_min_power"] == pytest.approx(1.8e-3)
    assert v.details["power_floor_from"] == "slew"
    issue = next(i for i in v.violations if i.code == "power_below_dynamic_floor")
    assert issue.message == (
        "power < 0.0005 W, but the slew requirement alone draws 0.001 A from 1.8 V = "
        "0.0018 W, before bias, output stage or reference current")
    assert issue.suggestion == (
        "raise the power limit above 0.0018 W, drop C_load, or relax the slew requirement")


def test_gbw_is_bounded_by_transconductance_efficiency():
    """gm/Id <= 1/Vt is the weak-inversion ceiling no MOSFET beats. 100 MHz into 10 pF
    needs gm = 6.3 mS, so >= 162 uA at 300 K -- 10 uA cannot do it."""
    v = fz.check([{"metric": "gbw", "direction": "max"}], {"gbw": ">1e8"},
                 circuit_type="opamp", mode="concept",
                 technology_params={"vdd": 1.8, "cload": 10e-12, "itotal": 10e-6})
    assert "gbw_above_transconductance_limit" in codes(v)
    assert v.details["gm_needed_s"] == pytest.approx(2 * math.pi * 1e8 * 10e-12)
    assert v.details["min_bias_current_a"] == pytest.approx(1.626e-4, rel=1e-2)

    ok = fz.check([{"metric": "gbw", "direction": "max"}], {"gbw": ">1e8"},
                  circuit_type="opamp", mode="theoretical",
                  technology_params={"vdd": 1.8, "cload": 10e-12, "itotal": 1e-3})
    assert ok.passed, ok.to_dict()


def test_gbw_cannot_exceed_the_device_ft():
    v = fz.check([{"metric": "gbw", "direction": "max"}], {"gbw": ">2e10"},
                 circuit_type="opamp", mode="concept",
                 technology_params={"cload": 1e-12, "ft": 1e10, "itotal": 1.0})
    assert "gbw_above_device_ft" in codes(v)


def test_noise_below_the_thermal_floor_of_its_own_resistor():
    """A 10 kohm source is 12.8 nV/sqrt(Hz) at 27 C. Asking for 5 nV/sqrt(Hz) with it
    in the signal path is not an amplifier problem."""
    v = fz.check([{"metric": "vn", "direction": "min"}], {"vn": "<5e-9"},
                 circuit_type="opamp", mode="concept",
                 technology_params={"rmin": 10e3, "temp_c": 27.0})
    assert "noise_below_thermal_floor" in codes(v)
    assert v.details["thermal_noise_floor_v_per_rthz"] == pytest.approx(12.8e-9, rel=0.02)

    ok = fz.check([{"metric": "vn", "direction": "min"}], {"vn": "<5e-9"},
                  circuit_type="opamp", mode="theoretical",
                  technology_params={"rmin": 100.0})
    assert ok.passed


def test_linear_regulator_efficiency_ceiling_is_vout_over_vin():
    v = fz.check([{"metric": "eff", "direction": "max"}], {"eff": ">90"},
                 circuit_type="ldo", mode="concept",
                 technology_params={"vin": 5.0, "vout": 1.8})
    assert "efficiency_above_linear_ceiling" in codes(v)
    assert v.details["max_linear_efficiency"] == pytest.approx(0.36)


def test_ldo_dissipation_and_dropout_floors():
    v = fz.check([{"metric": "power", "direction": "min"}],
                 {"power": "<0.1", "dropout": "<0.05"}, circuit_type="ldo",
                 mode="concept", technology_params={"vin": 5.0, "vout": 1.8,
                                                    "iload": 0.5, "rds_on": 0.2})
    assert "power_below_pass_dissipation" in codes(v)      # 3.2 V * 0.5 A = 1.6 W
    assert "dropout_below_rdson_limit" in codes(v)         # 0.5 A * 0.2 ohm = 0.1 V
    assert v.details["min_dropout_v"] == pytest.approx(0.1)


def test_ldo_cannot_step_up():
    v = fz.check(GAIN, mode="concept", circuit_type="ldo",
                 technology_params={"vin": 1.8, "vout": 3.3})
    assert "linear_cannot_step_up" in codes(v)


def test_a_boost_needs_energy_storage_in_the_deck():
    rc_only = ("V1 in 0 5\nR1 in out 1\nC1 out 0 10u\n.tran 1m\n"
               ".meas TRAN vout AVG V(out)\n")
    v = fz.check([{"metric": "vout", "direction": "max"}], param_space=None,
                 netlist_template=rc_only, circuit_type="dcdc",
                 technology_params={"vin": 5.0, "vout": 12.0})
    assert "step_up_without_storage" in codes(v)
    assert v.details["topology_required"] == "boost"
    assert v.details["ideal_duty_cycle"] == pytest.approx(1 - 5 / 12, abs=1e-4)


def test_dcdc_duty_cycle_against_a_stated_limit():
    v = fz.check([{"metric": "eff", "direction": "max"}], {"duty": "<0.5"},
                 circuit_type="dcdc", mode="concept",
                 technology_params={"vin": 5.0, "vout": 48.0})
    assert "duty_above_limit" in codes(v)


# ------------------------------------------------------------------------ modes and gates

def test_theoretical_mode_drops_the_engineering_guidelines():
    tp = {"vdd": 1.8, "cload": 10e-12, "rmin": 1e9}
    practical = fz.check([{"metric": "pm", "direction": "max"}], {"pm": ">20"},
                         circuit_type="opamp", mode="practical",
                         param_space={"R1": {"min": 1e3, "max": 1e9}},
                         technology_params=tp)
    theoretical = fz.check([{"metric": "pm", "direction": "max"}], {"pm": ">20"},
                           circuit_type="opamp", mode="theoretical",
                           param_space={"R1": {"min": 1e3, "max": 1e9}},
                           technology_params=tp)
    assert "low_phase_margin" in codes(practical, fz.WARN)
    assert "component_value_high" in codes(practical, fz.WARN)
    assert not theoretical.violations, theoretical.to_dict()
    assert any("practical" in s for s in theoretical.checks_skipped)


def test_concept_mode_needs_no_netlist_and_ignores_one():
    v = fz.check(GAIN, {"bandwidth": ">1e4"}, SPACE, RC, mode="concept")
    assert "metric_not_measured" not in codes(v)
    assert any("concept" in s for s in v.checks_skipped)
    assert "measured_metrics" not in v.details


def test_negative_phase_margin_is_a_hard_error_in_any_mode():
    v = fz.check([{"metric": "pm", "direction": "max"}], {"pm": "<0"}, mode="practical")
    assert "negative_phase_margin_required" in codes(v)


def test_slew_rate_unit_confusion_is_flagged():
    """100 V/us written as `slew_rate > 100` passes every SI check trivially, which is
    exactly why it needs saying."""
    v = fz.check([{"metric": "slew_rate", "direction": "max"}], {"slew_rate": ">100"},
                 circuit_type="opamp", mode="practical",
                 technology_params={"vdd": 1.8, "cload": 10e-12})
    assert "slew_rate_unit_suspect" in codes(v, fz.WARN)
    assert v.passed


def test_unknown_technology_keys_are_reported():
    v = fz.check(GAIN, mode="concept", circuit_type="opamp",
                 technology_params={"capacitive_load": 1e-12, "vdd": 1.8})
    assert "unknown_technology_params" in codes(v, fz.WARN)
    assert v.details["technology_params_used"] == {"vdd": 1.8}


def test_bad_mode_or_circuit_type_is_an_error_not_a_guess():
    with pytest.raises(fz.FeasibilityError):
        fz.check(GAIN, mode="quantum")
    with pytest.raises(fz.FeasibilityError):
        fz.check(GAIN, circuit_type="teleporter")


# ---------------------------------------------------------------- wiring into start_run

def test_start_run_refuses_an_impossible_spec_and_creates_nothing(tmp_path):
    con = db.connect(tmp_path)
    out = engine.start_run(con, "bad", RC, SPACE,
                           objectives=[{"metric": "power", "direction": "min"}],
                           constraints={"power": "<0.005"})
    assert "error" in out and out["feasibility"]["status"] == "infeasible"
    assert "metric_not_measured" in {i["code"] for i in out["feasibility"]["violations"]}
    assert con.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 0


def test_bypass_creates_the_run_anyway(tmp_path):
    con = db.connect(tmp_path)
    out = engine.start_run(con, "bad", RC, SPACE,
                           objectives=[{"metric": "power", "direction": "min"}],
                           constraints={"power": "<0.005"},
                           preflight={"bypass": True})
    assert out["run_id"] == "bad" and "error" not in out
    assert "feasibility" not in out
    assert con.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 1


def test_warnings_do_not_block_but_do_ride_along(tmp_path):
    con = db.connect(tmp_path)
    out = engine.start_run(con, "warned", RC,
                           {"R1": {"min": 1e3, "max": 1e9}, "C1": SPACE["C1"]},
                           objectives=GAIN, constraints={"bandwidth": ">1e4"})
    assert out["run_id"] == "warned"
    assert out["feasibility"]["status"] == "impractical"
    assert out["feasibility"]["passed"] is True


def test_the_analytic_backend_skips_template_checks(tmp_path):
    """Analytic metrics come from a Python function, so `.meas` and {PARAM} mean nothing
    there and every template check would be a false positive."""
    con = db.connect(tmp_path)
    out = engine.start_run(con, "an", "R1 a b {R1}\n", {"R1": {"min": 1.0, "max": 2.0}},
                           objectives=[{"metric": "whatever", "direction": "max"}],
                           sim_config={"backend": "analytic", "model": "nope"})
    assert out["run_id"] == "an" and "feasibility" not in out


def test_unknown_preflight_key_is_rejected(tmp_path):
    con = db.connect(tmp_path)
    with pytest.raises(engine.EngineError):
        engine.start_run(con, "typo", RC, SPACE, objectives=GAIN,
                         preflight={"bypas": True})


def test_the_tool_is_registered_and_returns_plain_json(tmp_path, monkeypatch):
    """The MCP surface is the only thing a client sees, and a tool that raises instead of
    returning a dict takes the whole call down."""
    monkeypatch.setenv("SPICEMCP_PROJECT", str(tmp_path))
    from spicemcp import server

    names = {t.name for t in asyncio.run(server.mcp.list_tools())}
    assert "check_feasibility" in names

    params = [server.Param(name=k, **v) for k, v in SPACE.items()]
    out = server.check_feasibility([server.Goal(**g) for g in BW],
                                   [server.Limit(metric="gain_db", min=-3)], params, RC)
    assert json.loads(json.dumps(out))["status"] == "feasible"
    assert out["next_step"].startswith("Call start_optimization")
    bad = server.check_feasibility([server.Goal(**g) for g in GAIN], mode="quantum")
    assert bad["error"].startswith("mode") and bad["example"]
