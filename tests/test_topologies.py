"""The topology library: every template must build a valid run, and its hand-sized
start point must land on the spec when actually simulated.

The simulation checks use ngspice (via the LTspice-deck translator in `sim.py`) and are
skipped where it is not installed; LTspice users get the same decks unchanged.
"""

from __future__ import annotations

import math
import tempfile
from pathlib import Path

import pytest

from spicemcp import db, engine, feasibility, ir, server, sim, topologies as T

SPECS = {
    "rc_lowpass": {"fc": 1000},
    "sallen_key_lowpass": {"fc": 10000},
    "noninverting_amp": {"gain": 10, "bw": 100e3},
    "ldo": {"vin": 5, "vout": 3.3, "iload": 0.1},
    "buck": {"vin": 12, "vout": 5, "iout": 1},
    "inverting_amp": {"gain": 10, "bw": 100e3},
    "bandpass": {"f0": 1000},
    "current_sense": {"imax": 2},
    "adc_driver": {"fs": 100e3},
    "voltage_reference": {"vout": 2.5},
    "protection": {"vin": 12, "vsurge": 40, "vmax": 20, "iload": 0.5},
    "boost": {"vin": 5, "vout": 12, "iout": 0.5},
    "flyback": {"vin": 12, "vout": 5, "iout": 1},
}
needs_ngspice = pytest.mark.skipif(not sim.find_ngspice(), reason="ngspice not installed")


def test_every_template_is_covered_here():
    assert set(SPECS) == set(T.TEMPLATES)


@pytest.mark.parametrize("name", sorted(SPECS))
def test_template_builds_a_consistent_run(name):
    d = T.get(name).design(SPECS[name])
    names = {p["name"] for p in d.params}
    assert {m.group(1) for m in ir.PLACEHOLDER.finditer(d.netlist)} == names
    assert set(d.start) == names
    for p in d.params:                                  # start inside its own range
        assert p["min"] <= d.start[p["name"]] <= p["max"], (p, d.start)
    space = {p["name"]: {k: v for k, v in p.items() if k != "name"} for p in d.params}
    cons = server._constraints([server.Limit(**x) for x in d.limits])
    v = feasibility.check(d.goals, cons, space, d.netlist, d.circuit_type)
    assert v.passed, v.to_dict()
    assert not [i for i in v.violations if i.code == "metric_not_measured"]
    assert d.working and all(isinstance(w, str) for w in d.working)


def test_bad_specs_are_refused_with_the_reason():
    with pytest.raises(T.TemplateError, match="fc"):
        T.get("rc_lowpass").design({})
    with pytest.raises(T.TemplateError, match="unknown spec"):
        T.get("rc_lowpass").design({"fc": 1e3, "fcc": 2})
    with pytest.raises(T.TemplateError, match="GBW"):
        T.get("noninverting_amp").design({"gain": 100, "bw": 1e6})
    with pytest.raises(T.TemplateError, match="dropout"):
        T.get("ldo").design({"vin": 3.4, "vout": 3.3, "iload": 0.1})
    with pytest.raises(T.TemplateError, match="2·Q²"):
        T.get("bandpass").design({"f0": 1e3, "q": 1, "gain": 3})
    with pytest.raises(T.TemplateError, match="duty"):
        T.get("boost").design({"vin": 5, "vout": 40, "iout": 0.1})
    with pytest.raises(T.TemplateError, match="vmax"):
        T.get("protection").design({"vin": 12, "vsurge": 40, "vmax": 12, "iload": 1})
    with pytest.raises(T.TemplateError, match="unknown template"):
        T.get("opamp_5t")


def test_hand_sizing_matches_the_textbook_equations():
    d = T.get("sallen_key_lowpass").design({"fc": 1000})
    r, c1, c2 = d.start["R1"], d.start["C1"], d.start["C2"]
    w0 = 1 / math.sqrt(r * r * c1 * c2)
    q = math.sqrt(r * r * c1 * c2) / (c2 * 2 * r)
    assert w0 / (2 * math.pi) == pytest.approx(1000)
    assert q == pytest.approx(1 / math.sqrt(2))
    d = T.get("noninverting_amp").design({"gain": 11, "bw": 1e5})
    assert 1 + d.start["R2"] / d.start["R1"] == pytest.approx(11)
    d = T.get("ldo").design({"vin": 5, "vout": 2.4, "iload": 0.05})
    assert 1.2 * (1 + d.start["R1"] / 100e3) == pytest.approx(2.4)
    d = T.get("inverting_amp").design({"gain": 4, "bw": 1e5})
    assert d.start["R2"] / d.start["R1"] == pytest.approx(4)
    d = T.get("bandpass").design({"f0": 1000, "q": 5, "gain": 2})
    r1, r2, r3, c = d.start["R1"], d.start["R2"], d.start["R3"], d.start["C1"]
    w0 = 1 / (c * math.sqrt(r2 * r1 * r3 / (r1 + r3)))    # MFB: equal capacitors
    assert w0 / (2 * math.pi) == pytest.approx(1000) and r2 / (2 * r1) == pytest.approx(2)
    d = T.get("current_sense").design({"imax": 2})
    g = 1 + d.start["R2"] / d.start["R1"]
    assert 2 * d.start["RS"] * g == pytest.approx(2.5)


@needs_ngspice
@pytest.mark.parametrize("name", sorted(SPECS))
def test_hand_sized_start_meets_the_spec_in_spice(name, tmp_path):
    d = T.get(name).design(SPECS[name])
    net, _ = ir.render(d.netlist, d.start)
    res = sim.simulate(net, d.start, {"backend": "ngspice"}, tmp_path, name)
    assert res.status == "ok", res.failure_message
    for lim in d.limits:
        m = lim["metric"]
        if m.startswith("param_"):
            continue
        got = res.metrics[m]
        slack = 0.02 * abs(lim.get("max") or lim.get("min") or 1)   # hand calc, not exact
        if lim.get("min") is not None:
            assert got >= lim["min"] - slack, (m, got, lim)
        if lim.get("max") is not None:
            assert got <= lim["max"] + slack, (m, got, lim)


@needs_ngspice
def test_start_from_template_simulates_the_start_and_says_what_next(monkeypatch):
    tmp = Path(tempfile.mkdtemp())
    monkeypatch.setattr(server, "PROJECT", str(tmp))
    monkeypatch.setattr(server, "_con", db.connect(tmp))
    out = server.start_from_template("lpf", "sallen_key_lowpass", {"fc": 2000},
                                     server.SimOptions(backend="ngspice"))
    assert out["start_result"]["feasible"], out
    assert out["hand_calculation"] and "run_optimization" in out["next_step"]
    assert Path(out["schematic_asc"]).is_file()
    assert engine.select_next(server.con(), "lpf")["method"] != "global_sample"
    bad = server.start_from_template("x", "nope", {})
    assert "list_templates" in bad["fix"]


def test_ngspice_translation_keeps_ltspice_meaning():
    deck = (".ac dec 10 1 1k\n.meas AC fc WHEN mag(V(out))=0.7071 FALL=1\n"
            ".meas AC g FIND mag(V(out)) AT 1k\n.meas TRAN v AVG V(out) FROM 1m TO 2m\n"
            ".meas TRAN r PARAM (a-b)*1000\n.end\n")
    ng = sim.to_ngspice(deck).lower()
    assert "meas ac fc when vm(out)=0.7071" in ng          # threshold stays linear
    assert "meas ac g find vdb(out) at=1k" in ng           # LTspice reports dB
    assert "from=1m to=2m" in ng
    assert "let r = (a-b)*1000" in ng and ng.rstrip().endswith(".end")
