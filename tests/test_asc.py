"""Checks for the .asc writer.

The pin math is the whole risk: a wrong orientation transform produces a schematic
that opens, simulates, and is not the circuit you drew. So the transforms are pinned
against coordinates LTspice itself wrote, and -- when LTspice is installed -- every
orientation is round-tripped back through the netlister.
"""

from __future__ import annotations

import pytest

from spicemcp import asc, sim

pytestmark = pytest.mark.skipif(asc.find_sym_dir() is None,
                                reason="LTspice symbol library not installed")

# Ground truth lifted from a schematic LTspice authored itself (its own WIRE endpoints
# are where the pins provably are). Covers 5 of the 8 orientations; the LTspice
# round-trip below covers all 8, including the three no hand-drawn file happened to use.
KNOWN = [
    ("res",          (368, 16),  "R90",  {"A": (352, 32), "B": (272, 32)}),
    ("res",          (32, 16),   "M90",  {"A": (48, 32), "B": (128, 32)}),
    ("res",          (272, 416), "R180", {"A": (256, 400), "B": (256, 320)}),
    ("res",          (128, 416), "M180", {"A": (144, 400), "B": (144, 320)}),
    ("cap",          (-112, 112), "R0",  {"A": (-96, 112), "B": (-96, 176)}),
    ("OpAmps/opamp", (96, 224),  "R180", {"invin": (128, 176), "noninvin": (128, 144),
                                          "out": (64, 160)}),
    ("OpAmps/opamp", (304, 224), "M180", {"invin": (272, 176), "noninvin": (272, 144),
                                          "out": (336, 160)}),
]


@pytest.mark.parametrize("sym,at,rot,want", KNOWN)
def test_pin_placement_matches_ltspice_authored_coordinates(sym, at, rot, want):
    p = asc.Sheet().part(sym, "X1", at, rot)
    assert {k: p[k] for k in want} == want


def test_netlist_argument_order_is_spiceorder_not_file_order():
    """A resistor's pins happen to be declared in SpiceOrder; an opamp's are not
    guaranteed to be, and getting them backwards inverts the amplifier."""
    assert asc.symbol("res")["order"] == ["A", "B"]
    assert asc.symbol("OpAmps/opamp")["order"] == ["invin", "noninvin", "out"]
    assert asc.symbol("voltage")["order"] == ["+", "-"]


def test_unknown_pin_name_fails_loudly():
    p = asc.Sheet().part("res", "R1", (0, 0))
    with pytest.raises(KeyError, match="no pin"):
        p["OUT"]
    with pytest.raises(ValueError, match="orientation"):
        asc.Sheet().part("res", "R1", (0, 0), "R45")


def test_diagonal_wire_is_rejected():
    sh = asc.Sheet()
    with pytest.raises(ValueError, match="diagonal"):
        sh.wire((0, 0), (16, 16))
    sh.route((0, 0), (32, 48), "HV")
    assert sh.wires == [(0, 0, 32, 0), (32, 0, 32, 48)]


def test_component_values_round_trip_exactly():
    """`%g` truncates at 6 significant figures, which silently draws a *different*
    resistor than the one the optimizer measured."""
    p = asc.Sheet().part("res", "R1", (0, 0), value=1849.60938)
    assert "SYMATTR Value 1849.60938" in p.lines()
    assert "SYMATTR Value 33n" in asc.Sheet().part("cap", "C1", (0, 0),
                                                   value="33n").lines()


@pytest.mark.parametrize("sep", ["\u00a7", "\u00c2\u00a7", "\ufffd"])
def test_subckt_instance_name_normalises_whatever_the_separator_decodes_to(sep):
    """LTspice wedges U+00A7 between an X-prefix and the instance name. Read as cp1252
    that UTF-8 byte pair becomes `\u00c2\u00a7`, and `\u00c2` is a *letter* -- so a `\\W` filter leaves
    `X\u00c2U1` behind and the compare fails on a machine with a different default codec."""
    net = (f"* C:\\x.asc\nV1 in 0 AC 1\n"
           f"X{sep}U1 out b out opamp Aol=1Meg ;{sep}pnba invin)noninvin)out\n"
           ".ac dec 400 100 1Meg\n.backanno\n.end\n")
    assert asc.elements(net) == ["V1 in 0 AC 1",
                                 "XU1 out b out opamp Aol=1Meg",
                                 ".ac dec 400 100 1Meg"]


@pytest.mark.skipif(not sim.find_ltspice(), reason="LTspice not installed")
def test_every_orientation_round_trips_through_the_netlister(tmp_path):
    """Place one resistor per orientation, label both pins, and let LTspice tell us
    which node each pin landed on. If a transform is wrong the labels come out swapped
    or the flag misses the pin entirely and LTspice invents an N00x node."""
    sh = asc.Sheet()
    for i, rot in enumerate(sorted(asc.ORIENT)):
        p = sh.part("res", f"R{i}", (400 * i, 0), rot, value="1k")
        sh.flag(p["A"], f"a{i}")
        sh.flag(p["B"], f"b{i}")
    sh.gnd((400 * len(asc.ORIENT), 0))          # LTspice wants one ground node
    sh.directive(".op")
    got = asc.check(sh.write(tmp_path / "orients.asc"))
    for i, rot in enumerate(sorted(asc.ORIENT)):
        assert f"R{i} a{i} b{i} 1k" in got, f"{rot} misplaced a pin: {got}"
