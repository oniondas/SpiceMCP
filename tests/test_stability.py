"""Stability checks and the closed-loop buck.

A circuit can be checked several ways in one netlist: its response in one deck, its loop
gain in a second, a load step in a third. Each deck is run on its own and the
measurements merged, so the optimiser and the yield run see phase margin and load-step
numbers next to the gain and the output voltage.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from spicemcp import ir, layout, sim, topologies as T

from test_topologies import SPECS

needs_ngspice = pytest.mark.skipif(not sim.find_ngspice(), reason="ngspice not installed")


def _run(name: str, spec: dict, **override):
    d = T.get(name).design(spec)
    net, _ = ir.render(d.netlist, {**d.start, **override})
    r = sim.simulate(net, d.start, {"backend": "ngspice"}, Path(tempfile.mkdtemp()), name)
    assert r.status == "ok", r.failure_message
    return r.metrics


def test_a_netlist_splits_into_its_decks():
    two = "* a\nV1 a 0 1\n.op\n.end\n* b\nV1 a 0 2\n.op\n.end\n"
    assert [d.splitlines()[0] for d in sim.decks(two)] == ["* a", "* b"]
    assert sim.decks("* one\nV1 a 0 1\n.op\n.end\n") == ["* one\nV1 a 0 1\n.op\n.end\n"]
    assert len(sim.decks("* no end\nV1 a 0 1\n")) == 1


def test_a_complex_find_becomes_magnitude_and_phase_in_ngspice():
    ng = sim.to_ngspice("* x\n.ac dec 10 1 1e6\n"
                        ".meas AC pm FIND V(fb) WHEN mag(V(fb))=1 FALL=1\n.end\n").lower()
    assert "meas ac pm find vdb(fb) when vm(fb)=1 fall=1" in ng
    assert "meas ac pm_deg find vp(fb) when vm(fb)=1 fall=1" in ng
    assert "set units=degrees" in ng


@pytest.mark.parametrize("name", ["sallen_key_lowpass", "noninverting_amp", "ldo"])
def test_feedback_templates_carry_a_phase_margin_limit(name):
    d = T.get(name).design(SPECS[name])
    assert {"metric": "pm_deg", "min": T.PM_MIN} in d.limits
    assert len(sim.decks(d.netlist)) >= 2


def test_only_the_circuit_deck_is_drawn():
    d = T.get("ldo").design(SPECS["ldo"])
    names = {e.name for e in layout.parse(d.netlist)[0]}
    assert "Lbrk" not in names and "Iload" not in names and {"M1", "Rz", "Co"} <= names


def test_same_measurement_in_two_decks_is_refused(tmp_path):
    if not sim.find_ngspice():
        pytest.skip("ngspice not installed")
    deck = "* a\nV1 a 0 1\nR1 a 0 1k\n.op\n.meas OP x FIND V(a)\n.end\n"
    r = sim.simulate(deck + deck, {}, {"backend": "ngspice"}, tmp_path, "dup")
    assert r.status == "failed" and "more than one deck" in r.failure_message


@needs_ngspice
def test_phase_margin_falls_with_load_capacitance():
    spec = {"gain": 1, "bw": 100e3, "gbw": 10e6}
    light = _run("noninverting_amp", {**spec, "cload": 100e-12})
    heavy = _run("noninverting_amp", {**spec, "cload": 1e-9})
    assert light["pm_deg"] > 50 > T.PM_MIN > heavy["pm_deg"], (light, heavy)


@needs_ngspice
def test_ldo_needs_its_zero_resistor():
    good = _run("ldo", SPECS["ldo"])
    assert good["pm_deg"] > 60 and good["step_mv"] < 99 and good["kick_mv"] < 99, good
    bare = _run("ldo", SPECS["ldo"], RZ=0.01)          # a low-ESR capacitor, no zero
    assert bare["pm_deg"] < 15, bare
    assert bare["step_mv"] > good["step_mv"], (bare, good)


@needs_ngspice
def test_closed_loop_buck_holds_its_output_as_the_input_moves():
    d = T.get("buck").design(SPECS["buck"])                 # one board, sized for 12 V
    net, _ = ir.render(d.netlist, d.start)
    out = {}
    for v in (11.4, 12.6):                                    # the input ±5 %
        r = sim.simulate(net.replace("Vin in 0 12\n", f"Vin in 0 {v}\n"), d.start,
                         {"backend": "ngspice"}, Path(tempfile.mkdtemp()), "buck")
        out[v] = r.metrics["vout"]
    assert all(abs(x - 5) < 0.02 for x in out.values()), out   # within 0.4 %, not ±5 %


def test_buck_input_outside_the_controller_range_is_refused():
    with pytest.raises(T.TemplateError, match="4 to 20 V"):
        T.get("buck").design({"vin": 24, "vout": 5, "iout": 1})
