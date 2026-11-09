"""Circuits with no template: block recognition and the drawing it leads to.

Every circuit here is drawn from a bare netlist, the way a user's own design arrives.
Each drawing must be the netlist's circuit (`draw` refuses otherwise), clean (no wire
through a part, nothing on top of anything), above a readability floor, and its blocks
must come out in their familiar shape: a diff pair side by side with gates outward, a
mirror's gates on one bar, one output transistor over the other, windings facing.
"""

from __future__ import annotations

import pytest

from spicemcp import blocks, layout, opamp, render

OA = opamp.subckt()
RAILS = "Vcc vcc 0 5\nVee vee 0 -5\n"
FLAT_OA = "\n".join(ln for ln in OA.splitlines()
                    if not ln.startswith((".subckt", ".ends", "*")))

HARD = {
    # the 8-MOSFET op-amp itself, transistor level, as a unity-gain buffer
    "cmos_opamp": "Vdd vdd 0 2.5\nVss vss 0 -2.5\nVin inp 0 AC 1\n"
                  + FLAT_OA.replace("inn", "out") + "\nCL out 0 10p\n",
    "bjt_diff_pair": "Vcc vcc 0 12\nVee vee 0 -12\nVin1 in1 0 AC 1\nVin2 in2 0 0\n"
                     "Q1 c1 in1 e Qn\nQ2 c2 in2 e Qn\nQ3 c1 c1 vcc Qp\nQ4 c2 c1 vcc Qp\n"
                     "Ree e vee 10k\nQ5 vcc c2 out Qn\nRe out vee 4.7k\nCL out 0 1n\n"
                     ".model Qn NPN(BF=150)\n.model Qp PNP(BF=100)\n",
    "class_ab": "Vcc vcc 0 15\nVee vee 0 -15\nVin in 0 SIN(0 1 1k)\nR1 in n 10k\n"
                "R2 n out 100k\nX1 0 n vcc vee drv SPMCP_OA\nR3 vcc bt 4.7k\nD1 bt drv D\n"
                "D2 drv bb D\nR4 bb vee 4.7k\nQ1 vcc bt e1 Qn\nQ2 vee bb e2 Qp\n"
                "Re1 e1 out 0.47\nRe2 e2 out 0.47\nRL out 0 8\n.model Qn NPN(BF=100)\n"
                ".model Qp PNP(BF=100)\n.model D D\n" + OA,
    "state_variable": RAILS + "Vin in 0 AC 1\nR1 in a 10k\nR2 hp a 10k\nR3 bp b 10k\n"
                      "R4 b 0 10k\nX1 b a vcc vee hp SPMCP_OA\nR5 hp c 15.9k\nC1 c bp 10n\n"
                      "X2 0 c vcc vee bp SPMCP_OA\nR6 bp d 15.9k\nC2 d lp 10n\n"
                      "X3 0 d vcc vee lp SPMCP_OA\nR7 lp a 10k\n" + OA,
    "instrumentation": RAILS + "Vp inp 0 AC 0.5\nVn inn 0 AC 0.5 180\n"
                       "X1 inp n1 vcc vee o1 SPMCP_OA\nX2 inn n2 vcc vee o2 SPMCP_OA\n"
                       "R1a o1 n1 9760\nR1b o2 n2 10200\nRg n1 n2 205\nR2a o1 a 9760\n"
                       "R3a a out 10000\nR2b o2 b 9760\nR3b b 0 10000\n"
                       "X3 b a vcc vee out SPMCP_OA\nRL out 0 10k\nCL out 0 100p\n" + OA,
    "wien_oscillator": RAILS + "R1 out p 10k\nC1 p q 10n\nR2 q 0 10k\nC2 q 0 10n\n"
                       "Rf out n 22k\nRg n 0 10k\nD1 out m D\nD2 m out D\nR3 m n 10k\n"
                       "X1 q n vcc vee out SPMCP_OA\n.model D D\n" + OA,
    "sallen_key_4th": RAILS + "Vin in 0 AC 1\nR1 in a 10k\nR2 a b 10k\nC1 a o1 22n\n"
                      "C2 b 0 10n\nX1 b o1 vcc vee o1 SPMCP_OA\nR3 o1 c 10k\nR4 c d 10k\n"
                      "C3 c out 47n\nC4 d 0 4.7n\nX2 d out vcc vee out SPMCP_OA\n" + OA,
    "flyback": "Vin in 0 12\nVg g 0 PULSE(0 10 0 10n 10n 2u 5u)\nLp in d 100u\n"
               "Ls 0 sec 11u\nK1 Lp Ls 0.99\nM1 d g 0 Msw\nRc in cl 10k\nCc in cl 10n\n"
               "Dc d cl D\nD1 sec out D\nCo out 0 100u\nRL out 0 5\n"
               ".model Msw VDMOS(Rds=0.05)\n.model D D\n",
}
# Readability floor per circuit (score 0-100, see layout.lint). Before block
# recognition and the placement search these drew at 4, 67, 0, 33, 26, 96, "refused"
# and 100 respectively.
FLOOR = {"cmos_opamp": 45, "bjt_diff_pair": 90, "class_ab": 80, "state_variable": 80,
         "instrumentation": 65, "wien_oscillator": 90, "flyback": 90, "sallen_key_4th": 85}


def _clean(s):
    assert s["through_parts"] == 0 and s["overlaps"] == 0, s
    assert s["ambiguous"] == 0 and s["dangling"] == 0, s


@pytest.mark.parametrize("name", sorted(HARD))
def test_untemplated_circuit_draws_correct_and_readable(name, tmp_path):
    dr = layout.draw(HARD[name])                      # raises if it doesn't match
    p = dr.write(tmp_path / f"{name}.asc")
    assert layout.verify(p, HARD[name]) == []
    s = layout.lint(p)
    _clean(s)
    assert s["score"] >= FLOOR[name], s


def _els(net):
    els, dirs, _ = layout.parse(net)
    return els, dirs


def test_blocks_are_found_by_wiring_alone():
    els, dirs = _els(HARD["cmos_opamp"])
    kinds = {(b.kind, b.parts) for b in blocks.find(els, dirs, rails={"vdd", "vss"})}
    assert ("diff_pair", ("M1", "M2")) in kinds        # M1's drain is the mirror diode
    assert ("mirror", ("M3", "M4")) in kinds
    assert ("mirror", ("M8", "M5", "M7")) in kinds
    # two NMOS on the negative supply are not a diff pair: their "tail" is the supply
    assert not any(b.kind == "diff_pair" and "M6" in b.parts
                   for b in blocks.find(els, dirs, rails={"vdd", "vss"}))
    els, dirs = _els(HARD["class_ab"])
    pp = [b for b in blocks.find(els, dirs, rails={"vcc", "vee"}) if b.kind == "push_pull"]
    assert len(pp) == 1 and pp[0].parts == ("Q1", "Q2")
    assert pp[0].nodes["emitters"] == ["e1", "out", "e2"]
    assert pp[0].nodes["bias"] == ["bt", "drv", "bb"]
    els, dirs = _els(HARD["flyback"])
    assert [b.parts for b in blocks.find(els, dirs)] == [("Lp", "Ls")]


def _pin(dr, name, i):
    e = next(x for x in dr.elements if x.name == name)
    return e.part[e.part.meta["order"][i]]


def test_diff_pair_is_drawn_side_by_side_gates_outward():
    dr = layout.draw(HARD["bjt_diff_pair"])
    (c1, b1, e1), (c2, b2, e2) = ([_pin(dr, q, i) for i in range(3)] for q in ("Q1", "Q2"))
    assert c1[1] == c2[1] and e1[1] == e2[1]           # level
    assert b1[0] < c1[0] and b2[0] > c2[0]             # bases point away from each other
    assert c1[0] < c2[0]                               # the mirror's diode side on the left
    # mirror load: both bases on one horizontal bar
    assert _pin(dr, "Q3", 1)[1] == _pin(dr, "Q4", 1)[1]


def test_push_pull_is_one_transistor_over_the_other():
    dr = layout.draw(HARD["class_ab"])
    e1, e2 = _pin(dr, "Q1", 2), _pin(dr, "Q2", 2)
    assert e1[0] == e2[0] and e1[1] < e2[1]


def test_transformer_windings_face_each_other():
    dr = layout.draw(HARD["flyback"])
    lp = next(e for e in dr.elements if e.name == "Lp").part
    ls = next(e for e in dr.elements if e.name == "Ls").part
    assert sorted((lp["A"][1], lp["B"][1])) == sorted((ls["A"][1], ls["B"][1]))
    assert abs(lp["A"][0] - ls["A"][0]) == 64
    assert any(d.startswith("K1 ") for d in dr.sheet.directives)  # coupling kept


def test_busy_supplies_are_drawn_as_rail_labels():
    text = layout.draw(HARD["cmos_opamp"]).sheet.render()
    flags = [ln.split()[-1] for ln in text.splitlines() if ln.startswith("FLAG")]
    # six parts sit on vdd and five on vss: each ends in a rail label, not a wire
    assert flags.count("vdd") >= 5 and flags.count("vss") >= 4


def test_search_is_deterministic():
    a = layout.draw(HARD["class_ab"]).sheet.render()
    assert a == layout.draw(HARD["class_ab"]).sheet.render()


def test_filled_in_formulas_are_shown_as_numbers():
    assert render.pretty_value("{5.2363636363636374e-05*0.472656}") == "24.7u"
    assert render.pretty_value("{duty*2e-06}") == "{duty*2e-06}"     # a name: kept
    assert render.pretty_value("{1/0}") == "{1/0}"
