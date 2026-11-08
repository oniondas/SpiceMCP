"""The templates use real devices: a transistor-level op-amp and a power MOSFET switch,
not ideal controlled sources or switches. The op-amp's figures come from its
transistors, and are checked here in ngspice."""

from __future__ import annotations

import re
import tempfile
from pathlib import Path

import pytest

from spicemcp import ir, layout, opamp, sim, topologies as T

from test_topologies import SPECS

needs_ngspice = pytest.mark.skipif(not sim.find_ngspice(), reason="ngspice not installed")


@pytest.mark.parametrize("name", sorted(T.TEMPLATES))
def test_no_template_uses_an_ideal_source_or_switch(name):
    net = T.get(name).design(SPECS[name]).netlist
    top = [ln.split()[0] for ln in net.splitlines() if ln.strip()
           and not ln.lstrip().startswith(("*", "."))]
    assert not [x for x in top if x[0].upper() in "EFGHBS"], (name, top)


def test_the_compensation_follows_the_asked_gain_bandwidth():
    a, b = opamp.design(1e6), opamp.design(10e6)
    assert a["cc"] == pytest.approx(10 * b["cc"])
    assert b["slew_v_per_us"] == pytest.approx(10 * a["slew_v_per_us"])
    with pytest.raises(ValueError, match="supply"):
        opamp.design(1e6, 2.0)


@needs_ngspice
@pytest.mark.parametrize("gbw", [1e6, 10e6])
def test_open_loop_gain_and_bandwidth_come_from_the_transistors(gbw):
    deck = (f"* open-loop op-amp\nVcc vcc 0 5\nVee vee 0 -5\nV1 in 0 DC 0 AC 1\n"
            f"X1 in fb vcc vee out {opamp.NAME}\nRf out fb 1G\nCf fb 0 1\nRL out 0 10k\n"
            + opamp.subckt(gbw) + ".ac dec 50 1 1e9\n"
            ".meas AC a0 FIND mag(V(out)) AT 10\n"
            ".meas AC ugf WHEN mag(V(out))=1 FALL=1\n.end\n")
    r = sim.simulate(deck, {}, {"backend": "ngspice"}, Path(tempfile.mkdtemp()), "oa")
    assert r.metrics["a0"] > 75, r.metrics                 # dB, into 10 kOhm
    assert r.metrics["ugf"] == pytest.approx(gbw, rel=0.25), r.metrics


def test_op_amp_draws_as_the_five_pin_symbol_with_its_library(tmp_path):
    d = T.get("sallen_key_lowpass").design(SPECS["sallen_key_lowpass"])
    dr = layout.draw(d.netlist, d.start)
    x = next(e for e in dr.elements if e.kind == "X")
    assert x.sym == layout.OPAMP_SYM and x.pin_nodes[4] == "out"
    p = dr.write(tmp_path / "sk.asc")
    lib = tmp_path / f"{opamp.NAME}.lib"
    assert lib.is_file() and ".subckt" in lib.read_text()
    assert f".lib {opamp.NAME}.lib" in p.read_text()
    assert ".subckt" not in p.read_text()                    # shipped beside, not drawn
    assert layout.lint(p)["score"] >= 90


def test_a_power_mosfet_draws_as_a_three_pin_part():
    d = T.get("buck").design(SPECS["buck"])
    dr = layout.draw(d.netlist, d.start)
    m = next(e for e in dr.elements if e.name == "M1")
    assert m.sym == "pmos" and m.pin_nodes == ["sw", "g", "in"]


def test_a_subcircuit_that_is_not_an_op_amp_is_refused():
    with pytest.raises(layout.LayoutError, match="5-pin op-amp"):
        layout.draw("V1 a 0 1\nX1 a b c myblk\nR1 c 0 1k\n")
    assert re.search(r"\.ends", opamp.subckt())
    assert "{" not in ir.render(opamp.subckt(), {})[0]
