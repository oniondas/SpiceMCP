"""Auto-placed schematics: every drawing must be the netlist's circuit, and readable.

"Same circuit" is checked by reading the drawn geometry back the way LTspice joins it
(wire ends, tees, pins, same-named labels) and comparing pin groups with the netlist.
That check runs without LTspice, so it guards every drawing on every machine.
"""

from __future__ import annotations

import pytest

from spicemcp import asc, layout, opamp, render, server, topologies as T

from test_topologies import SPECS

OA = opamp.subckt()
RAILS = "Vcc vcc 0 5\nVee vee 0 -5\n"
UNSEEN = {
    "ladder5": "V1 in 0 AC 1\n" + "".join(
        f"R{i} {'in' if i == 0 else f'n{i}'} n{i + 1} 1k\nC{i} n{i + 1} 0 1n\n"
        for i in range(5)),
    "bridge": "V1 a 0 10\nR1 a b 1k\nR2 a c 1k\nR3 b 0 1k\nR4 c 0 1.1k\nR5 b c 10k\n",
    "inverting": "V1 in 0 AC 1\nR1 in inn 1k\nR2 inn out 10k\n"
                 "X1 0 inn vcc vee out SPMCP_OA\nRL out 0 2k\n" + RAILS + OA,
    "pi_filter": "V1 in 0 AC 1\nRs in a 50\nC1 a 0 1n\nL1 a b 1u\nC2 b 0 1n\n"
                 "RL b out 1\nR9 out 0 50\n",
    "cs_amp": "Vdd vdd 0 5\nVin g 0 AC 1 1.5\nRd vdd d 2k\nM1 d g s s NM\nRs s 0 200\n"
              "Cs s 0 10u\nRL d 0 10k\n.model NM NMOS(KP=1m VTO=1)\n",
    "two_stage": "V1 in 0 AC 1\nX1 in fb vcc vee a SPMCP_OA\nR1 a b 1k\nC1 b 0 1n\n"
                 "X2 b out vcc vee out SPMCP_OA\nR2 out fb 9k\nR3 fb 0 1k\n" + RAILS + OA,
    "boost": "Vin in 0 5\nL1 in sw 10u\nM1 sw g 0 NSW\nVg g 0 PULSE(0 5 0 1n 1n 1u 2u)\n"
             "D1 sw out DS\nC1 out 0 10u\nRL out 0 50\n"
             ".model NSW VDMOS(Vto=2 Kp=20 Rd=0.05)\n.model DS D\n",
    "lc_tank": "I1 0 t AC 1m\nL1 t 0 1u\nC1 t 0 1n\nR1 t 0 1k\n",
}


def _clean(s):
    assert s["through_parts"] == 0 and s["overlaps"] == 0, s
    assert s["ambiguous"] == 0 and s["dangling"] == 0, s


@pytest.mark.parametrize("name", sorted(SPECS))
def test_every_template_draws_as_the_same_circuit(name, tmp_path):
    d = T.get(name).design(SPECS[name])
    dr = layout.draw(d.netlist, d.start)               # raises if it doesn't match
    p = dr.write(tmp_path / f"{name}.asc")
    assert layout.verify(p, _filled(d)) == []
    s = layout.lint(p)
    _clean(s)
    # The regulator and the converter carry their controller and output network
    # (13 parts and two ICs for the buck): two wire crossings are allowed there.
    lo, cross = (65, 2) if name in ("ldo", "buck") else (85, 1)
    assert s["score"] >= lo and s["crossings"] <= cross, s


@pytest.mark.parametrize("name", sorted(UNSEEN))
def test_circuits_it_was_not_tuned_on(name, tmp_path):
    p = layout.draw(UNSEEN[name]).write(tmp_path / f"{name}.asc")
    s = layout.lint(p)
    _clean(s)
    assert s["score"] >= 90, s


def _filled(d):
    from spicemcp import ir
    return ir.render(d.netlist, d.start)[0]


def test_signal_flows_left_to_right_and_ground_hangs_down(tmp_path):
    dr = layout.draw(UNSEEN["inverting"])
    x = {e.name: e.part.at[0] for e in dr.elements}
    assert x["V1"] < x["R1"] < x["X1"] < x["RL"]
    # R2 is feedback: it goes on a lane above the main row, not through the op-amp
    y = {e.name: e.part[e.part.meta["order"][0]][1] for e in dr.elements}
    assert y["R2"] < y["R1"]
    sch = asc.parse_asc(dr.write(tmp_path / "i.asc"))
    for f in sch.grounds:                        # nothing hangs below a ground symbol
        assert not any(w[1] > f["at"][1] or w[3] > f["at"][1] for w in sch.wires
                       if f["at"][0] in (w[0], w[2]) and min(w[1], w[3]) >= f["at"][1])


def test_mosfet_amplifier_stands_upright():
    dr = layout.draw(UNSEEN["cs_amp"])
    m = next(e for e in dr.elements if e.name == "M1")
    d, s = (m.part[m.part.meta["order"][i]] for i in (0, 2))
    assert d[0] == s[0] and d[1] < s[1]                # drain directly above source


def test_verify_catches_a_miswired_drawing(tmp_path):
    net = UNSEEN["pi_filter"]
    dr = layout.draw(net)
    assert layout.verify(dr.sheet, net) == []
    dr.sheet.wires.pop()                               # break one connection
    assert layout.verify(dr.sheet, net)
    swapped = net.replace("L1 a b", "L1 b a").replace("C2 b 0", "C2 a 0")
    assert layout.verify(layout.draw(net).sheet, swapped)


def test_drawing_is_deterministic():
    a = layout.draw(UNSEEN["two_stage"]).sheet.render()
    assert a == layout.draw(UNSEEN["two_stage"]).sheet.render()


def test_unsupported_parts_are_refused_with_a_reason():
    with pytest.raises(layout.LayoutError, match="by hand"):
        layout.draw("V1 in 0 1\nX1 in out myamp\n")
    with pytest.raises(layout.LayoutError, match="no elements"):
        layout.draw(".op\n")


def test_turned_parts_get_name_above_value_below(tmp_path):
    sch = asc.parse_asc(layout.draw("V1 a 0 1\nR1 a b 4.7k\nR2 b 0 1k\n")
                        .write(tmp_path / "d.asc"))
    r1 = next(p for p in sch.parts if p.inst == "R1")
    lab = {x["attr"]: x["at"][1] for x in r1.labels()}
    assert lab["InstName"] < r1.bbox()[1] + 1 and lab["Value"] > r1.bbox()[3] - 1


def test_values_are_drawn_the_way_people_write_them():
    assert render.pretty_value("1.5915494309189535e-08") == "15.9n"
    assert render.pretty_value("10000.0") == "10k"
    assert render.pretty_value("1e6") == "1Meg"
    assert render.pretty_value("PMOD W=0.0036432 L=1e-06") == "PMOD W=3.64m L=1u"
    assert render.pretty_value("2000") == "2000"                  # typed exactly: kept
    assert render.pretty_value("V=max(0, 2000*V(fb))") == "V=max(0, 2000*V(fb))"
    assert render.pretty_value("{duty*2e-06}") == "{duty*2e-06}"


def test_bundled_symbols_stand_in_for_a_missing_library():
    if asc.find_sym_dir() is None:
        assert asc.symbol("res")["path"].startswith(str(asc.BUNDLED_SYMS))
    assert asc.symbol("res")["pins"] == {"A": (16, 16), "B": (16, 96)}


def test_render_tool_draws_a_netlist(tmp_path):
    cir = tmp_path / "f.cir"
    cir.write_text(UNSEEN["pi_filter"])
    out = server.render_schematic(str(cir), str(tmp_path / "f.svg"))
    assert out["success"] and out["matches_netlist"], out
    assert out["readability"]["score"] >= 90 and out["asc_path"].endswith("f.asc")
    bad = tmp_path / "x.cir"
    bad.write_text("X1 a b sub\n")
    out = server.render_schematic(str(bad), str(tmp_path / "x.svg"))
    assert not out["success"] and "by hand" in out["error"] and out["fix"]
