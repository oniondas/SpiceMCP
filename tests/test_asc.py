"""Checks for the .asc writer, its router and its reader.

The pin math is the whole risk: a wrong orientation transform produces a schematic
that opens, simulates, and is not the circuit you drew. So the transforms are pinned
against coordinates LTspice itself wrote, and -- when LTspice is installed -- every
orientation is round-tripped back through the netlister.
"""

from __future__ import annotations

import pytest

from spicemcp import asc, sim

# The routing and text tests need no symbol library, so the skip is per-test rather than
# on the module: a machine without LTspice should still be told if the router breaks.
needs_syms = pytest.mark.skipif(asc.find_sym_dir() is None,
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


@needs_syms
@pytest.mark.parametrize("sym,at,rot,want", KNOWN)
def test_pin_placement_matches_ltspice_authored_coordinates(sym, at, rot, want):
    p = asc.Sheet().part(sym, "X1", at, rot)
    assert {k: p[k] for k in want} == want


@needs_syms
def test_netlist_argument_order_is_spiceorder_not_file_order():
    """A resistor's pins happen to be declared in SpiceOrder; an opamp's are not
    guaranteed to be, and getting them backwards inverts the amplifier."""
    assert asc.symbol("res")["order"] == ["A", "B"]
    assert asc.symbol("OpAmps/opamp")["order"] == ["invin", "noninvin", "out"]
    assert asc.symbol("voltage")["order"] == ["+", "-"]


@needs_syms
def test_unknown_pin_name_fails_loudly():
    p = asc.Sheet().part("res", "R1", (0, 0))
    with pytest.raises(KeyError, match="no pin"):
        p["OUT"]
    with pytest.raises(ValueError, match="orientation"):
        asc.Sheet().part("res", "R1", (0, 0), "R45")


# --- routing -------------------------------------------------------------------
#
# LTspice has no diagonal wire: a WIRE that is neither horizontal nor vertical is not a
# connection, so the router's contract is that no segment it emits can ever be one.


def _diagonals(wires):
    return [w for w in wires if w[0] != w[2] and w[1] != w[3]]


def test_connect_routes_a_diagonal_pair_as_two_orthogonal_segments():
    sh = asc.Sheet()
    segs = sh.connect((100, 100), (200, 200))
    assert len(segs) == 2
    assert not _diagonals(segs)
    assert segs[0][:2] == (100, 100) and segs[-1][2:] == (200, 200)
    assert segs[0][2:] == segs[1][:2]                  # the corner actually joins


@pytest.mark.parametrize("style,want", [
    ("HV", [(0, 0, 32, 0), (32, 0, 32, 48)]),
    ("VH", [(0, 0, 0, 48), (0, 48, 32, 48)]),
])
def test_route_style_picks_the_corner(style, want):
    sh = asc.Sheet()
    assert sh.connect((0, 0), (32, 48), style) == want
    assert sh.wires == want


def test_auto_style_is_deterministic_and_still_manhattan():
    a, b = (0, 0), (96, 16)
    first = asc.Sheet().connect(a, b)
    assert first == asc.Sheet().connect(a, b)          # no randomness
    assert not _diagonals(first) and len(first) == 2


@pytest.mark.parametrize("a,b", [((0, 0), (64, 0)), ((0, 0), (0, 64))])
def test_already_orthogonal_pins_get_one_segment(a, b):
    assert asc.Sheet().connect(a, b) == [(*a, *b)]


def test_diagonal_wire_is_routed_not_rejected():
    """Was a ValueError. A diagonal pair is a routing request, not a mistake -- but the
    wire that comes out still has to be one LTspice accepts."""
    sh = asc.Sheet()
    sh.wire((0, 0), (16, 16))
    assert not _diagonals(sh.wires) and len(sh.wires) == 2
    sh.route((0, 0), (32, 48), "HV")
    assert (0, 0, 32, 0) in sh.wires


def test_zero_length_and_duplicate_segments_are_dropped():
    sh = asc.Sheet()
    sh.wire((0, 0), (0, 0))
    assert sh.wires == []
    sh.wire((0, 0), (64, 0))
    sh.wire((0, 0), (64, 0))
    sh.wire((64, 0), (0, 0))                           # same segment, other way round
    assert sh.wires == [(0, 0, 64, 0)]


def test_bad_route_style_is_an_error():
    with pytest.raises(ValueError, match="route style"):
        asc.Sheet().connect((0, 0), (32, 48), "sideways")


@needs_syms
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


@needs_syms
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


# --- reading a .asc back -------------------------------------------------------

# A hand-written sheet, so the reader is checked against known geometry rather than
# against whatever happens to be installed. Only `res` and `cap` are needed for the
# symbol-dependent parts. The `out` branch leaves the tap wire mid-span, which is the
# T-junction case worth having in a fixture.
MINIMAL = """Version 4
SHEET 1 880 680
WIRE 240 96 240 112
WIRE 240 192 240 240
WIRE 240 208 352 208
WIRE 240 304 240 336
FLAG 240 96 in
FLAG 352 208 out
FLAG 240 336 0
SYMBOL res 224 96 R0
SYMATTR InstName R1
SYMATTR Value 4k7
SYMBOL cap 224 240 R0
SYMATTR InstName C1
SYMATTR Value 100n
TEXT 88 424 Left 2 !.tran 1m
TEXT 88 456 Left 2 ;an RC divider
"""


def test_junction_dots_need_three_directions_or_a_tee():
    """Two wires crossing are not connected in LTspice; three meeting are. A dot on a
    crossover claims a short that is not there, and no dot on a tee hides a real one."""
    sh = asc.Schematic(wires=[(0, 0, 100, 0), (50, -50, 50, 50)])         # crossover
    assert sh.junctions() == []
    sh = asc.Schematic(wires=[(0, 0, 100, 0), (50, 0, 50, 50)])           # tee
    assert sh.junctions() == [(50, 0)]
    sh = asc.Schematic(wires=[(0, 0, 50, 0), (50, 0, 100, 0)])            # collinear
    assert sh.junctions() == []


@needs_syms
def test_parse_asc_reads_geometry_labels_and_directives(tmp_path):
    f = tmp_path / "rc.asc"
    f.write_text(MINIMAL, encoding="utf-8")
    sch = asc.parse_asc(f)
    assert sch.sheet == (880, 680)
    assert len(sch.wires) == 4
    assert [p.inst for p in sch.parts] == ["R1", "C1"]
    assert [p.value for p in sch.parts] == ["4k7", "100n"]
    assert not sch.warnings
    assert {f["label"] for f in sch.grounds} == {"0"}
    assert {f["label"] for f in sch.net_labels} == {"in", "out"}
    assert [t["role"] for t in sch.texts] == ["directive", "comment"]
    assert [t["text"] for t in sch.texts] == [".tran 1m", "an RC divider"]
    assert sch.junctions() == [(240, 208)]            # the `out` branch leaves mid-span
    x0, y0, x1, y1 = sch.bbox()
    assert x0 <= 88 and y1 >= 456

    # The wire ends are the pins, which is the only reason this fixture is a circuit and
    # not a drawing. If the pin math drifts, this catches it without LTspice.
    assert {p["name"]: p["at"] for p in sch.parts[0].pins()} == {"A": (240, 112),
                                                                "B": (240, 192)}
    assert {p["name"]: p["at"] for p in sch.parts[1].pins()} == {"A": (240, 240),
                                                                "B": (240, 304)}


@needs_syms
def test_a_line_whose_last_field_is_a_word_is_not_dropped(tmp_path):
    """`FLAG`, `IOPIN` and `DATAFLAG` all end in text, not a number. Parsing the whole
    leading run of fields as integers returns nothing for those lines and skips them
    silently -- and `FLAG x y 0` still parses, so a schematic loses every net label and
    port while keeping its grounds, which looks like a renderer bug rather than a parse
    one."""
    f = tmp_path / "ports.asc"
    f.write_text("Version 4\nSHEET 1 880 680\nWIRE 0 0 64 0\n"
                 "FLAG 0 0 in\nFLAG 64 0 0\nFLAG 32 0 V+\n"
                 "IOPIN 0 0 In\nDATAFLAG 64 0 V(out)\n", encoding="utf-8")
    sch = asc.parse_asc(f)
    assert {fl["label"] for fl in sch.net_labels} == {"in", "V+"}
    assert {fl["label"] for fl in sch.grounds} == {"0"}
    assert sch.iopins == [{"at": (0, 0), "dir": "In"}]
    assert sch.dataflags == [{"at": (64, 0), "expr": "V(out)"}]
    assert len(sch.wires) == 1 and not sch.warnings


def test_a_v_prefixed_pin_token_means_the_name_is_quarter_turned(tmp_path):
    """`VLEFT` is how a dense part stays legible: LTC2992 marks the 30 names it packs
    onto a 32-unit pitch along the top and bottom edges, and leaves its four
    widely-spaced `Sense` pins plain `TOP`. Read as a plain `LEFT` they all come out
    horizontal and overprint. Turning the instance a quarter swaps the text axis back."""
    sym = tmp_path / "dense.asy"
    sym.write_text("Version 4\nSymbolType BLOCK\n"
                   "RECTANGLE Normal -32 -32 32 32\n"
                   "PIN -16 32 VLEFT 8\nPINATTR PinName MaxPower1\n"
                   "PINATTR SpiceOrder 1\n"
                   "PIN -32 0 LEFT 8\nPINATTR PinName Vdd\n"
                   "PINATTR SpiceOrder 2\n", encoding="utf-8")
    meta = asc.symbol("dense", str(tmp_path))
    flat = {p["name"]: p for p in asc.Placed("dense", "U1", (0, 0), "R0",
                                            meta=meta).pins()}
    assert flat["MaxPower1"]["vertical"] and not flat["Vdd"]["vertical"]
    turned = {p["name"]: p for p in asc.Placed("dense", "U1", (0, 0), "R90",
                                               meta=meta).pins()}
    assert not turned["MaxPower1"]["vertical"] and turned["Vdd"]["vertical"]


@needs_syms
def test_pins_and_labels_follow_the_instance_orientation():
    """A mirrored part's default label justification mirrors with it. Otherwise a
    mirrored transistor writes its own designator across its own base."""
    r0 = asc.Placed("npn", "Q1", (0, 0), "R0", {"InstName": "Q1"},
                    meta=asc.symbol("npn"))
    m0 = asc.Placed("npn", "Q2", (0, 0), "M0", {"InstName": "Q2"},
                    meta=asc.symbol("npn"))
    assert r0.labels()[0]["just"] == "Left"
    assert m0.labels()[0]["just"] == "Right"
    assert r0.labels()[0]["at"][0] == -m0.labels()[0]["at"][0]
    assert [p["at"] for p in r0.pins()] != [p["at"] for p in m0.pins()]


def test_a_missing_symbol_is_reported_not_raised(tmp_path):
    f = tmp_path / "vendor.asc"
    f.write_text("Version 4\nSHEET 1 880 680\nWIRE 0 0 64 0\n"
                 "SYMBOL NoSuchPart$$ 100 100 R0\nSYMATTR InstName U1\n",
                 encoding="utf-8")
    sch = asc.parse_asc(f)
    assert len(sch.warnings) == 1 and "NoSuchPart$$" in sch.warnings[0]
    assert sch.parts[0].meta is None and sch.parts[0].inst == "U1"
    assert sch.wires == [(0, 0, 64, 0)]               # the rest still parsed
