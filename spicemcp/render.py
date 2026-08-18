"""Schematic pictures from a real `.asc`.

This draws the schematic that is in the file: every symbol's geometry comes out of its
own `.asy`, transformed by that instance's orientation, so a resistor is the zig-zag
LTspice draws and an op-amp is a triangle with the inverting input where the `.asy` puts
it. Nothing is idealised or re-imagined from a netlist -- if the picture is wrong, the
schematic is wrong, which is the only useful property a schematic renderer can have.

Three layers, and the split is the point:

  `asc.parse_asc`  ->  sheet geometry (no appearance)
  `build()`        ->  primitives tagged with a *role* (still no appearance)
  `STYLES[name]`   ->  what a role looks like
  `_svg` / `_png`  ->  two emitters over the same primitives

So `tech_minimal`, `classic` and `sketch` are three readings of identical geometry, and
the SVG and the PNG are the same drawing rather than two drawings that agree by luck.

Coordinates stay exactly as LTspice stores them -- y down, which is also SVG's
convention, so the SVG path is a straight copy and matplotlib inverts its y axis. No
flip means no chance of a mirrored schematic, which is a failure mode that looks
plausible right up until someone builds it.

The SVG emitter is stdlib-only: an MCP server has to keep importing on a machine with
no matplotlib, and vector output is the one that matters for documentation. matplotlib
is needed only for PNG.

ponytail: deliberately restrained. No auto-layout, no rats-nest routing, no colour per
component. Colour appears only where it carries information (an input, an output, a
warning); everything else is one ink. A schematic that looks generated is a schematic
nobody trusts.
"""

from __future__ import annotations

import dataclasses
import math
from pathlib import Path

from . import asc

# LTspice's grid is 16 units. 110 units/inch puts that grid at ~0.145in, which is the
# density a schematic is drawn at on paper -- and it makes a 600x400 sheet a 5.5x3.6in
# figure, printable without scaling.
UNITS_PER_INCH = 110.0
GRID = asc.GRID
MARGIN = 28                    # sheet units of clear space around everything drawn

# LTspice font size index -> multiplier on the base text height.
FONT_MULT = {0: 0.62, 1: 0.78, 2: 1.0, 3: 1.25, 4: 1.55, 5: 2.0, 6: 2.5, 7: 3.4}
TEXT_UNITS = 13.0              # height of a size-2 label, in sheet units
GLYPH_W = 0.56                 # mean glyph width / height, for bounding text

JUNCTION_R = 3.6               # solder-dot radius, sheet units

# A net whose name says what it is earns a colour; every other net is ink. Matched on
# the whole name only -- a node called `internal` is not an input because it ends in
# `in`, and mis-colouring a node is worse than not colouring it.
IN_NAMES = {"in", "vin", "input", "src", "vsrc", "vg", "vi"}
OUT_NAMES = {"out", "vout", "output", "vo", "load"}
FB_NAMES = {"fb", "feedback", "vfb", "sense", "vsense"}
RAIL_NAMES = {"vdd", "vcc", "vee", "vss", "vbat", "vbus", "v+", "v-", "vp", "vn",
              "vs", "vsupply", "vpos", "vneg", "com"}


class RenderError(RuntimeError):
    pass


# ------------------------------------------------------------------ primitives

@dataclasses.dataclass
class Prim:
    """One drawable thing, in sheet coordinates, tagged with what it *means*.

    `role` is the whole interface to styling. An emitter never asks "is this a wire" to
    decide a colour, it asks the style table what `role` looks like -- which is why a
    new style is a dict and not a new renderer.
    """

    kind: str                              # poly | rect | ellipse | arc | text | dot
    role: str
    pts: tuple = ()                        # poly/rect: flat x1,y1,x2,y2,...
    center: tuple = (0.0, 0.0)             # ellipse/arc
    radii: tuple = (0.0, 0.0)
    angles: tuple = (0.0, 0.0)             # arc: parametric degrees (start, end)
    ccw: bool = False
    text: str = ""
    size: float = TEXT_UNITS
    ha: str = "left"
    va: str = "center"
    rot: float = 0.0                       # text: degrees counter-clockwise *on screen*
    dash: int = 0

    def extent(self) -> tuple[float, float, float, float]:
        """x0, y0, x1, y1. Text is boxed from its metrics so a long value cannot be
        cropped by a canvas sized from the wires alone."""
        if self.kind in ("poly", "rect"):
            xs, ys = self.pts[0::2], self.pts[1::2]
            return (min(xs), min(ys), max(xs), max(ys))
        if self.kind in ("ellipse", "arc"):
            cx, cy = self.center
            rx, ry = self.radii
            return (cx - rx, cy - ry, cx + rx, cy + ry)
        if self.kind == "dot":
            cx, cy = self.center
            r = self.radii[0]
            return (cx - r, cy - r, cx + r, cy + r)
        w = GLYPH_W * self.size * max(len(ln) for ln in self.text.split("\n"))
        h = self.size * 1.25 * len(self.text.split("\n"))
        x = {"left": 0.0, "center": -w / 2, "right": -w}[self.ha]
        y = {"top": 0.0, "center": -h / 2, "bottom": -h}[self.va]
        cx, cy = self.center
        if self.rot:
            # Quarter-turned: the baseline runs up the screen, so the along-text run is
            # in -y and the line height is in x. `ha` therefore says whether the text
            # climbs from the anchor or hangs below it.
            return (cx + y, cy - x - w, cx + y + h, cy - x)
        return (cx + x, cy + y, cx + x + w, cy + y + h)


def _poly(role, pts, dash=0) -> Prim:
    return Prim("poly", role, pts=tuple(float(v) for v in pts), dash=dash)


def _text(role, at, s, size=TEXT_UNITS, ha="left", va="center", rot=0.0) -> Prim:
    return Prim("text", role, center=(float(at[0]), float(at[1])), text=s, size=size,
                ha=ha, va=va, rot=rot)


# LTspice justification -> (horizontal, vertical) anchor. The `V` prefix marks a
# justification that was set for a rotated instance; the text itself stays horizontal
# (checked against LTspice's own `astable.asc`, where the R90 capacitors' `VBottom`
# InstName and `VTop` Value sit above and below the part, both level).
JUST = {"left": ("left", "center"), "right": ("right", "center"),
        "center": ("center", "center"), "top": ("center", "top"),
        "bottom": ("center", "bottom")}


def _just(tok: str) -> tuple[str, str]:
    t = (tok or "Left").lower()
    if t.startswith("v") and t[1:] in JUST:
        t = t[1:]
    return JUST.get(t, ("left", "center"))


def _net_role(label: str) -> str:
    n = label.strip().lower()
    if n in IN_NAMES:
        return "net_in"
    if n in OUT_NAMES:
        return "net_out"
    if n in FB_NAMES:
        return "net_fb"
    return "net"


# ------------------------------------------------------------------ geometry -> prims

def build(sch: asc.Schematic) -> list[Prim]:
    """Sheet geometry -> role-tagged primitives. No colours, no line widths, no style.

    Draw order is the order a schematic is read: wires under symbols, symbols under
    their labels, junction dots last so a dot is never half-covered by the wire that
    made it.
    """
    out: list[Prim] = []
    for x1, y1, x2, y2 in sch.wires:
        out.append(_poly("wire", (x1, y1, x2, y2)))
    for s in sch.shapes:
        out += _shape_prims(s, "sheet")
    for p in sch.parts:
        out += _part_prims(p)
    for f in sch.grounds:
        out += _ground(f["at"])
    for f in sch.net_labels:
        out += _net_flag(f, f["at"] in _leaves_up(sch))
    for io in sch.iopins:
        out += _port(io)
    for d in sch.dataflags:
        out.append(_text("probe", (d["at"][0] + 8, d["at"][1] - 8), d["expr"],
                         TEXT_UNITS * 0.85))
    for t in sch.texts:
        out += _sheet_text(t)
    for x, y in sch.junctions():
        out.append(Prim("dot", "junction", center=(x, y), radii=(JUNCTION_R,) * 2))
    return out


def _shape_prims(s: dict, role: str) -> list[Prim]:
    """A LINE / RECTANGLE / CIRCLE / ARC from a `.asy` or a sheet, already transformed."""
    k, p = s["kind"], s["pts"]
    r = role + "_wide" if s.get("wide") else role
    if k == "line":
        return [_poly(r, p[:4], s.get("dash", 0))]
    if k in ("rect", "rectangle"):
        x0, y0, x1, y1 = p[:4]
        return [Prim("rect", r, pts=(x0, y0, x1, y1), dash=s.get("dash", 0))]
    if k == "circle":
        x0, y0, x1, y1 = p[:4]
        return [Prim("ellipse", r, center=((x0 + x1) / 2, (y0 + y1) / 2),
                     radii=(abs(x1 - x0) / 2, abs(y1 - y0) / 2), dash=s.get("dash", 0))]
    if k == "arc":
        return [_arc(p, r, s.get("ccw", False), s.get("dash", 0))]
    if k == "text":
        ha, va = _just(s.get("just", "Left"))
        return [_text("pinname", p[:2], s.get("text", ""),
                      TEXT_UNITS * FONT_MULT.get(s.get("size", 2), 1.0), ha, va)]
    return []


def _arc(p: list, role: str, ccw: bool, dash: int) -> Prim:
    """LTspice's ellipse-bbox-plus-two-rays form -> centre, radii and two angles.

    The angles are *parametric*, not geometric: on a non-circular ellipse the angle of
    the ray from the centre is not the angle that parameterises the point, and using the
    geometric one shifts both ends of the arc. Circles hide the bug; the ellipse arcs in
    LTspice's own transistor symbols do not.
    """
    x0, y0, x1, y1 = p[:4]
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    rx, ry = max(abs(x1 - x0) / 2, 1e-9), max(abs(y1 - y0) / 2, 1e-9)
    t = []
    for sx, sy in ((p[4], p[5]), (p[6], p[7])):
        t.append(math.degrees(math.atan2((sy - cy) / ry, (sx - cx) / rx)))
    return Prim("arc", role, center=(cx, cy), radii=(rx, ry),
                angles=(t[0], t[1]), ccw=ccw, dash=dash)


def _part_prims(p: asc.Placed) -> list[Prim]:
    """One placed symbol: its body, its pins, its designator and its value."""
    out: list[Prim] = []
    if p.meta is None:
        # An unresolvable symbol is drawn as an explicit gap, labelled with what was
        # asked for. Skipping it silently would produce a schematic missing a part. The
        # box is sized to the name it holds -- a fixed box lets a long vendor part number
        # spill out of it, which reads as a text label lying across the circuit rather
        # than as a part that could not be found.
        x, y = p.at
        size = TEXT_UNITS * 0.85
        w = max(64.0, GLYPH_W * size * (len(p.sym) + 1) + 12)
        out.append(Prim("rect", "placeholder",
                        pts=(x - w / 2, y - 24, x + w / 2, y + 24), dash=1))
        out.append(_text("placeholder", (x, y), f"?{p.sym}", size, "center", "center"))
        out.append(_text("designator", (x, y - 32), p.inst or "?", TEXT_UNITS,
                         "center", "bottom"))
        return out
    for s in p.shapes():
        out += _shape_prims(s, "body")
    for pin in p.pins():
        if pin["show"]:
            out.append(_pin_name(pin, p))
    for lab in p.labels():
        ha, va = _just(lab["just"])
        role = {"InstName": "designator", "Value": "value"}.get(lab["attr"], "spiceline")
        out.append(_text(role, lab["at"], lab["text"],
                         TEXT_UNITS * FONT_MULT.get(lab["size"], 1.0), ha, va))
    return out


def _pin_name(pin: dict, p: asc.Placed) -> Prim:
    """A pin name, written *inboard* of the pin.

    A `PIN`'s direction names the side of the symbol the pin sits on -- the way its wire
    leaves -- so the name goes the other way, into the body. `PIN -352 -448 LEFT 8` on
    LTC2992 is on the left edge of a 704-wide box with `Vdd` inside it; drawing the name
    outward puts it on top of the wire and, on a 40-pin part, on top of its neighbours.
    The trailing number is LTspice's own gap.

    A `V`-prefixed token means the name is quarter-turned, and then the same rule decides
    which way it runs: up out of a bottom-edge pin, down out of a top-edge one, always
    into the body. Vertical text reads bottom-to-top either way -- never upside down --
    so it is `ha` that flips, not the angle.
    """
    x, y = pin["at"]
    d = pin.get("dir", "LEFT").upper()
    off = pin.get("offset", 8) or 8
    if pin.get("vertical"):
        x0, y0, x1, y1 = p.bbox()
        below = y > (y0 + y1) / 2
        return _text("pinname", (x, y - off if below else y + off), pin["name"],
                     TEXT_UNITS * 0.7, "left" if below else "right", "center", rot=90)
    ha, va = "left", "center"
    if d == "LEFT":
        x, ha = x + off, "left"
    elif d == "RIGHT":
        x, ha = x - off, "right"
    elif d == "TOP":
        y, ha, va = y + off, "center", "top"
    elif d == "BOTTOM":
        y, ha, va = y - off, "center", "bottom"
    return _text("pinname", (x, y), pin["name"], TEXT_UNITS * 0.7, ha, va)


def _ground(at: tuple[int, int]) -> list[Prim]:
    """The conventional ground triangle, drawn below the connection point.

    Same glyph for every style: a ground symbol is not a place to be creative.
    """
    x, y = at
    return [_poly("gnd", (x, y, x, y + 8)),
            _poly("gnd", (x - 13, y + 8, x + 13, y + 8, x, y + 24, x - 13, y + 8))]


def _net_flag(f: dict, down: bool = False) -> list[Prim]:
    """A net label, and for a supply name the power-rail bar as well.

    The bar is worth drawing: on a schematic with rails and signals both labelled by
    text alone, a reader has to *read* every label to find the supplies.

    `down` flips the whole flag when its wire arrives from above -- a bottom rail such as
    `Vss` hangs below the circuit, and drawing it upward puts the bar and the name back
    inside the part they belong to, which reads as a strikethrough over the name.
    """
    x, y = f["at"]
    label = f["label"]
    role = _net_role(label)
    rail = label.strip().lower() in RAIL_NAMES
    s = -1 if down else 1
    out: list[Prim] = []
    if rail:
        out.append(_poly("rail", (x, y, x, y - 8 * s)))
        out.append(_poly("rail", (x - 13, y - 8 * s, x + 13, y - 8 * s)))
        out.append(_text(role, (x, y - 14 * s), label, TEXT_UNITS, "center",
                         "bottom" if not down else "top"))
    else:
        out.append(_poly("netstub", (x, y, x, y - 6 * s)))
        out.append(_text(role, (x, y - 8 * s), label, TEXT_UNITS, "center",
                         "bottom" if not down else "top"))
    return out


def _leaves_up(sch: asc.Schematic) -> set:
    """Flag points whose only wire goes *up* -- so the flag itself must hang downward.

    A flag is an endpoint of the net, and which side of it is free is decided by the
    wire, not by the label. Read off the geometry rather than from the name: `Vss` at the
    top of a sheet is drawn upward and `Vdd` at the bottom is drawn downward, because
    that is where the free space is.
    """
    up, other = set(), set()
    for x1, y1, x2, y2 in sch.wires:
        for a, b in (((x1, y1), (x2, y2)), ((x2, y2), (x1, y1))):
            (up if b[1] < a[1] else other).add(a)
    return up - other


def _port(io: dict) -> list[Prim]:
    """An IOPIN: the conventional flag/pentagon port outline plus its direction."""
    x, y = io["at"]
    d = (io.get("dir") or "BiDir").lower()
    tip = 10 if d != "in" else -10
    out = [_poly("port", (x, y - 8, x + 22, y - 8, x + 22 + tip, y,
                          x + 22, y + 8, x, y + 8, x, y - 8))]
    out.append(_text("port", (x + 26, y - 12), d, TEXT_UNITS * 0.7, "left", "bottom"))
    return out


def _sheet_text(t: dict) -> list[Prim]:
    """A sheet comment or SPICE directive. Multi-line boxes keep their line breaks."""
    role = {"directive": "directive", "comment": "comment"}.get(t["role"], "comment")
    ha, va = _just(t.get("just", "Left"))
    size = TEXT_UNITS * FONT_MULT.get(t.get("size", 2), 1.0)
    return [_text(role, (t["pts"][0], t["pts"][1] + i * size * 1.35), ln, size, ha, va)
            for i, ln in enumerate(t.get("lines") or [t["text"]])]


# --------------------------------------------------------------- label declutter

def declutter(prims: list[Prim]) -> list[Prim]:
    """Nudge a label that landed on top of the circuit, and only then.

    LTspice's own window position is trusted first -- it is what the schematic's author
    chose, and moving text that was already fine is how a renderer stops matching the
    file. A label is moved only when its box actually overlaps drawn geometry or another
    label, and then only to the first of a few fixed offsets that is clear. Fixed
    offsets, tried in a fixed order, on a list in a fixed order: the same schematic
    always produces the same picture.

    ponytail: greedy, first-fit, one pass. A dense sheet can still end up with a
    crossing label; the alternative is a placement solver, which is a lot of code to
    move text 8 units.
    """
    movable = {"designator", "value", "spiceline"}
    fixed = [p.extent() for p in prims if p.role not in movable and p.kind != "text"]
    placed = [p.extent() for p in prims if p.kind == "text" and p.role not in movable]
    out = []
    for p in prims:
        if p.kind != "text" or p.role not in movable:
            out.append(p)
            continue
        best = None
        for dx, dy in ((0, 0), (0, -14), (0, 14), (14, 0), (-14, 0), (16, -14),
                       (16, 14), (-16, -14), (-16, 14), (0, -28), (0, 28)):
            cand = dataclasses.replace(p, center=(p.center[0] + dx, p.center[1] + dy))
            n = sum(_hit(cand.extent(), b) for b in fixed + placed)
            if best is None or n < best[0]:
                best = (n, cand)
            if n == 0:
                break
        placed.append(best[1].extent())
        out.append(best[1])
    return out


def _hit(a, b) -> bool:
    """Boxes overlap, with a small gap requirement so text does not graze a wire."""
    pad = 1.5
    return not (a[2] + pad < b[0] or b[2] + pad < a[0]
                or a[3] + pad < b[1] or b[3] + pad < a[1])


# ------------------------------------------------------------------------ styles

SANS = ["Inter", "Helvetica Neue", "Helvetica", "Arial", "DejaVu Sans", "sans-serif"]
MONO = ["JetBrains Mono", "SF Mono", "Consolas", "DejaVu Sans Mono", "monospace"]
HAND = ["Segoe Print", "Comic Sans MS", "Segoe Script", "Bradley Hand", "Chalkboard",
        "DejaVu Sans", "cursive"]

# The data-visualisation sequence, in order. Every plot the server draws takes its traces
# from here so a Bode curve, a step response and a Monte Carlo histogram from the same run
# read as one document rather than as three tools' defaults.
#
# It is one ordered ramp -- dark blue through violet and magenta to coral and amber -- so
# it stays distinguishable in greyscale (the lightness rises monotonically) and the first
# two entries are far enough apart in hue to carry a two-trace plot on their own. Shared
# by all three styles: the chrome around a plot can be minimal, printed or hand-drawn, but
# a monochrome *trace* is unreadable, so the series does not change with the style.
SERIES = ("#003d5c", "#594e90", "#bc4c96", "#ff5f66", "#ffa600")


def _roles(ink, second, gnd, net, rail, designator, value, note, mono=MONO,
           sans=SANS) -> dict:
    """The role table. Widths are in points, so both emitters agree on weight.

    Component outlines are a touch heavier than wires (1.7 vs 1.35pt): it is the cue
    that separates a part from the net it sits on, and it is the whole reason a
    schematic reads as parts-and-connections rather than as a graph.
    """
    return {
        "wire":         {"c": ink, "lw": 1.35},
        "netstub":      {"c": ink, "lw": 1.35},
        "body":         {"c": ink, "lw": 1.70},
        "body_wide":    {"c": ink, "lw": 2.60},
        "sheet":        {"c": second, "lw": 1.10},
        "sheet_wide":   {"c": second, "lw": 1.90},
        "gnd":          {"c": gnd, "lw": 1.55},
        "rail":         {"c": rail, "lw": 1.70},
        "port":         {"c": second, "lw": 1.30, "font": sans, "fs": 0.78},
        "junction":     {"c": ink, "lw": 0.0, "fill": ink},
        "placeholder":  {"c": second, "lw": 1.20, "font": mono, "fs": 0.9},
        "designator":   {"c": designator, "lw": 0.0, "font": sans, "weight": "bold"},
        "value":        {"c": value, "lw": 0.0, "font": mono},
        "spiceline":    {"c": second, "lw": 0.0, "font": mono, "fs": 0.85},
        "pinname":      {"c": second, "lw": 0.0, "font": sans, "fs": 0.8},
        "net":          {"c": net, "lw": 0.0, "font": sans, "weight": "bold"},
        "net_in":       {"c": net, "lw": 0.0, "font": sans, "weight": "bold"},
        "net_out":      {"c": net, "lw": 0.0, "font": sans, "weight": "bold"},
        "net_fb":       {"c": net, "lw": 0.0, "font": sans, "weight": "bold"},
        "probe":        {"c": second, "lw": 0.0, "font": mono, "fs": 0.85},
        "comment":      {"c": second, "lw": 0.0, "font": sans, "fs": 0.9},
        "directive":    {"c": note, "lw": 0.0, "font": mono, "fs": 0.9},
        "annotation":   {"c": note, "lw": 1.4, "font": sans, "fs": 0.95},
    }


def _tech_minimal() -> dict:
    ink, second, accent = "#252A2E", "#697077", "#176B70"
    r = _roles(ink=ink, second=second, gnd=ink, net=ink, rail=ink, designator=ink,
               value=second, note=accent)
    # The only coloured things on the sheet, and each one is a claim: this is where the
    # signal comes in, this is where it leaves, this is the loop back. Everything else
    # stays charcoal, which is what keeps the colours meaning anything at all.
    r["net_in"] = {**r["net_in"], "c": "#1565C0"}
    r["net_out"] = {**r["net_out"], "c": "#D65A00"}
    r["net_fb"] = {**r["net_fb"], "c": "#6A4C93"}
    return {"bg": "#FAFAF8", "grid": {"kind": "dots", "step": GRID, "c": "#E7E7E3",
                                      "r": 0.75},
            "roles": r, "jitter": 0.0,
            "semantic": {"warning": "#B7791F", "error": "#B42318", "accent": accent}}


def _classic() -> dict:
    """Monochrome, no grid, slightly heavier ink: a figure for a paper or a datasheet.

    Line weights go up ~15% over `tech_minimal` because this style exists to be printed
    and a 1.35pt wire that reads well backlit is thin on paper.
    """
    ink, second = "#111111", "#555555"
    r = _roles(ink=ink, second=second, gnd=ink, net="#222222", rail=ink,
               designator="#111111", value="#222222", note="#222222", mono=SANS)
    for k, v in r.items():
        if v.get("lw"):
            r[k] = {**v, "lw": round(v["lw"] * 1.15, 2)}
    return {"bg": "#FFFFFF", "grid": None, "roles": r, "jitter": 0.0,
            "semantic": {"warning": "#555555", "error": "#111111", "accent": "#111111"}}


def _sketch() -> dict:
    """Notebook paper: graph-paper grid, a hand's worth of wobble, ink and one red-brown.

    The wobble is applied to the drawn line only, never to a coordinate -- endpoints,
    pins and junctions stay exactly where the file puts them, so the topology is still
    exact and the schematic is still true. It is a pen that is not quite straight, not
    a hand-drawn circuit.
    """
    ink, second, note = "#252525", "#5B564A", "#7A3326"
    r = _roles(ink=ink, second=second, gnd=ink, net=ink, rail=ink, designator=ink,
               value=ink, note=note, mono=HAND, sans=HAND)
    return {"bg": "#F7F3E8",
            "grid": {"kind": "lines", "step": GRID, "c": "#DED8C8", "lw": 0.5,
                     "major": 5, "major_lw": 0.8},
            "roles": r, "jitter": 1.25,
            "semantic": {"warning": note, "error": note, "accent": note}}


STYLES = {"tech_minimal": _tech_minimal, "classic": _classic, "sketch": _sketch}
DEFAULT_STYLE = "tech_minimal"


def style(name: str) -> dict:
    if name not in STYLES:
        raise RenderError(f"unknown style {name!r}; one of {sorted(STYLES)}")
    return STYLES[name]()


# ------------------------------------------------------------------ hand wobble

def _wobble(pts: tuple, amp: float) -> tuple:
    """Subdivide a polyline and nudge only the interior points.

    Deterministic by construction: the offset is a hash of the coordinates themselves,
    so re-rendering the same schematic produces the same picture, byte for byte. A
    `random` call here would make every render a different file and make a diff
    meaningless.
    """
    if amp <= 0 or len(pts) < 4:
        return pts
    out = [pts[0], pts[1]]
    for i in range(0, len(pts) - 2, 2):
        x0, y0, x1, y1 = pts[i], pts[i + 1], pts[i + 2], pts[i + 3]
        dx, dy = x1 - x0, y1 - y0
        length = math.hypot(dx, dy)
        if length < 1e-9:
            continue
        nx, ny = -dy / length, dx / length
        n = 2 if length < 40 else 3
        for k in range(1, n):
            f = k / n
            j = _jitter(x0, y0, x1, y1, k) * amp * min(1.0, length / 24)
            out += [x0 + dx * f + nx * j, y0 + dy * f + ny * j]
        out += [x1, y1]
    return tuple(out)


def _jitter(*seed) -> float:
    """A repeatable value in [-1, 1] from the coordinates. No RNG, no state."""
    h = 2166136261
    for v in seed:
        h = (h ^ (int(round(v * 4)) & 0xFFFFFFFF)) * 16777619 & 0xFFFFFFFF
    return (h % 2001) / 1000.0 - 1.0


def _apply_style_geometry(prims: list[Prim], st: dict) -> list[Prim]:
    """Style-dependent geometry: only the sketch wobble, and only on strokes."""
    amp = st.get("jitter", 0.0)
    if not amp:
        return prims
    out = []
    for p in prims:
        if p.kind == "poly":
            out.append(dataclasses.replace(p, pts=_wobble(p.pts, amp)))
        elif p.kind == "rect":
            x0, y0, x1, y1 = p.pts
            out.append(dataclasses.replace(
                p, kind="poly",
                pts=_wobble((x0, y0, x1, y0, x1, y1, x0, y1, x0, y0), amp)))
        else:
            out.append(p)
    return out


# ------------------------------------------------------------------ canvas

def canvas(prims: list[Prim], margin: float = MARGIN) -> tuple[float, float, float, float]:
    """x0, y0, w, h covering every primitive including text, plus a margin.

    Sized from what is drawn rather than from the `SHEET` header: LTspice's sheet is
    whatever the window was when the file was saved, and using it puts a small circuit
    in the corner of a mostly empty page.
    """
    if not prims:
        return (0.0, 0.0, 200.0, 150.0)
    ext = [p.extent() for p in prims]
    x0 = min(e[0] for e in ext) - margin
    y0 = min(e[1] for e in ext) - margin
    x1 = max(e[2] for e in ext) + margin
    y1 = max(e[3] for e in ext) + margin
    return (x0, y0, max(x1 - x0, 1.0), max(y1 - y0, 1.0))


def _grid_lines(g: dict, box: tuple) -> tuple[list, list]:
    """Minor and major grid line coordinates covering `box`, snapped to the grid."""
    x0, y0, w, h = box
    step = g["step"]
    major = step * g.get("major", 5)
    minor, maj = [], []
    x = math.ceil(x0 / step) * step
    while x <= x0 + w:
        (maj if x % major == 0 else minor).append((x, y0, x, y0 + h))
        x += step
    y = math.ceil(y0 / step) * step
    while y <= y0 + h:
        (maj if y % major == 0 else minor).append((x0, y, x0 + w, y))
        y += step
    return minor, maj


# ------------------------------------------------------------------ annotations

def _annotation_prims(sch: asc.Schematic, notes: dict,
                      existing: list[Prim] = ()) -> list[Prim]:
    """Optional explanation notes: `{"in": "input", "R2": "sets the gain"}`.

    Off unless asked for. A schematic that annotates itself is a schematic that has
    decided what the reader cares about, and it is wrong about half the time.

    The note is placed at the first of a few fixed offsets around its anchor whose text
    box is clear of everything already drawn, including notes placed before it, and the
    leader is then drawn from wherever the text ended up. `declutter` cannot do this job
    afterwards: moving the text would leave the arrow pointing at nothing.
    """
    out: list[Prim] = []
    boxes = [p.extent() for p in existing]
    anchors = {f["label"].strip().lower(): f["at"] for f in sch.flags}
    for p in sch.parts:
        if p.inst:
            b = p.bbox()
            anchors[p.inst.lower()] = ((b[0] + b[2]) // 2, (b[1] + b[3]) // 2)
    for key, note in notes.items():
        at = anchors.get(str(key).strip().lower())
        if at is None:
            continue
        best = None
        for dx, dy in ((64, -48), (64, 48), (-64, -48), (-64, 48), (96, -80),
                       (-96, -80), (96, 80), (-96, 80)):
            t = _text("annotation", (at[0] + dx, at[1] + dy), str(note), TEXT_UNITS,
                      "left" if dx > 0 else "right", "bottom")
            n = sum(_hit(t.extent(), b) for b in boxes)
            if best is None or n < best[0]:
                best = (n, t, dx, dy)
            if n == 0:
                break
        _, t, dx, dy = best
        boxes.append(t.extent())
        # Leader from the text's inboard edge to a standoff short of the anchor, so the
        # arrowhead points at the part without sitting on top of it.
        sx, sy = t.center[0] + (-8 if dx > 0 else 8), t.center[1] + 5
        n = math.hypot(at[0] - sx, at[1] - sy) or 1.0
        tip = (at[0] - 14 * (at[0] - sx) / n, at[1] - 14 * (at[1] - sy) / n)
        out.append(_poly("annotation", _wobble((sx, sy, *tip), 1.4)))
        out += _arrowhead((sx, sy), tip)
        out.append(t)
    return out


def _arrowhead(frm: tuple, to: tuple, size: float = 9.0) -> list[Prim]:
    dx, dy = to[0] - frm[0], to[1] - frm[1]
    n = math.hypot(dx, dy) or 1.0
    ux, uy = dx / n, dy / n
    return [_poly("annotation",
                  (to[0] - size * (ux + 0.45 * uy), to[1] - size * (uy - 0.45 * ux),
                   to[0], to[1],
                   to[0] - size * (ux - 0.45 * uy), to[1] - size * (uy + 0.45 * ux)))]


# ------------------------------------------------------------------ SVG emitter

_ESC = str.maketrans({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;"})
DASHES = {0: None, 1: "10,6", 2: "2,4", 3: "12,5,2,5", 4: "12,4,2,4,2,4"}


def _n(v: float) -> str:
    """Two decimals, and no `-0.00`. Rounding here is what makes two renders of the
    same schematic byte-identical instead of differing in float noise."""
    s = f"{v:.2f}"
    return "0.00" if s == "-0.00" else s


def _svg(prims: list[Prim], box: tuple, st: dict, dpi: int) -> str:
    x0, y0, w, h = box
    roles = st["roles"]
    px = UNITS_PER_INCH / 72.0                       # points -> sheet units
    body = [f'<rect x="{_n(x0)}" y="{_n(y0)}" width="{_n(w)}" height="{_n(h)}" '
            f'fill="{st["bg"]}"/>']
    defs, grid = _svg_grid(st, box, px)
    body += grid
    for p in prims:
        r = roles.get(p.role, roles["wire"])
        stroke = (f'stroke="{r["c"]}" stroke-width="{_n(max(r.get("lw", 1.0), 0.1) * px)}"'
                  ' fill="none" stroke-linecap="round" stroke-linejoin="round"')
        dash = DASHES.get(p.dash)
        if dash:
            stroke += f' stroke-dasharray="{dash}"'
        if p.kind == "poly":
            pts = " ".join(f"{_n(p.pts[i])},{_n(p.pts[i + 1])}"
                           for i in range(0, len(p.pts), 2))
            body.append(f'<polyline points="{pts}" {stroke}/>')
        elif p.kind == "rect":
            a, b, c, d = p.pts
            body.append(f'<rect x="{_n(min(a, c))}" y="{_n(min(b, d))}" '
                        f'width="{_n(abs(c - a))}" height="{_n(abs(d - b))}" {stroke}/>')
        elif p.kind == "ellipse":
            body.append(f'<ellipse cx="{_n(p.center[0])}" cy="{_n(p.center[1])}" '
                        f'rx="{_n(p.radii[0])}" ry="{_n(p.radii[1])}" {stroke}/>')
        elif p.kind == "arc":
            body.append(f'<path d="{_arc_path(p)}" {stroke}/>')
        elif p.kind == "dot":
            body.append(f'<circle cx="{_n(p.center[0])}" cy="{_n(p.center[1])}" '
                        f'r="{_n(p.radii[0])}" fill="{r.get("fill", r["c"])}"/>')
        elif p.kind == "text":
            body.append(_svg_text(p, r))
    wi, hi = w / UNITS_PER_INCH, h / UNITS_PER_INCH
    return ("<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n"
            f'<svg xmlns="http://www.w3.org/2000/svg" version="1.1" '
            f'width="{_n(wi * dpi)}" height="{_n(hi * dpi)}" '
            f'viewBox="{_n(x0)} {_n(y0)} {_n(w)} {_n(h)}">\n'
            f'<desc>spicemcp schematic</desc>\n'
            + (f"<defs>{''.join(defs)}</defs>\n" if defs else "")
            + "\n".join(body) + "\n</svg>\n")


def _svg_grid(st: dict, box: tuple, px: float) -> tuple[list, list]:
    """The grid as a `<pattern>` (dots) or a handful of `<path>`s (lines).

    One element per dot would be tens of thousands of nodes on a full sheet, which makes
    the file slow to open and the drawing itself the smaller half of it.
    """
    g = st.get("grid")
    if not g:
        return [], []
    x0, y0, w, h = box
    s = g["step"]
    if g["kind"] == "dots":
        return ([f'<pattern id="g" width="{s}" height="{s}" '
                 f'patternUnits="userSpaceOnUse" patternTransform="translate('
                 f'{_n(math.ceil(x0 / s) * s)},{_n(math.ceil(y0 / s) * s)})">'
                 f'<circle cx="0" cy="0" r="{_n(g["r"])}" fill="{g["c"]}"/></pattern>'],
                [f'<rect x="{_n(x0)}" y="{_n(y0)}" width="{_n(w)}" height="{_n(h)}" '
                 f'fill="url(#g)"/>'])
    minor, maj = _grid_lines(g, box)
    out = []
    for lines, lw in ((minor, g.get("lw", 0.5)), (maj, g.get("major_lw", 0.8))):
        if lines:
            d = " ".join(f"M{_n(a)} {_n(b)}L{_n(c)} {_n(e)}" for a, b, c, e in lines)
            out.append(f'<path d="{d}" stroke="{g["c"]}" '
                       f'stroke-width="{_n(lw * px)}" fill="none"/>')
    return [], out


_ANCHOR = {"left": "start", "center": "middle", "right": "end"}
# Baselines by explicit offset rather than `dominant-baseline`, which several SVG
# consumers (older Inkscape, some browsers' print path, LaTeX includegraphics via
# rsvg) ignore -- and when it is ignored every label jumps half a line.
_BASE = {"top": 0.80, "center": 0.36, "bottom": -0.22}


def _svg_text(p: Prim, r: dict) -> str:
    fam = ", ".join(f'{f}' for f in r.get("font", SANS))
    fs = p.size * r.get("fs", 1.0)
    y = p.center[1] + _BASE[p.va] * fs
    w = f' font-weight="{r["weight"]}"' if r.get("weight") else ""
    # SVG's y grows downward, so a counter-clockwise turn on screen is a negative angle.
    # Rotating about the unshifted anchor carries the baseline offset round with it.
    t = (f' transform="rotate({_n(-p.rot)} {_n(p.center[0])} {_n(p.center[1])})"'
         if p.rot else "")
    return (f'<text x="{_n(p.center[0])}" y="{_n(y)}" font-family="{fam}" '
            f'font-size="{_n(fs)}" fill="{r["c"]}" '
            f'text-anchor="{_ANCHOR[p.ha]}"{w}{t}'
            f' xml:space="preserve">{p.text.translate(_ESC)}</text>')


def _arc_path(p: Prim) -> str:
    """An SVG elliptical-arc path from parametric start/end angles.

    `sweep-flag` is 1 for the increasing-angle direction, which is what `ccw` means in
    these coordinates -- so the flag is the geometry, not a guess, and an inductor's
    coils come out on the correct side under every one of the eight orientations.
    """
    cx, cy = p.center
    rx, ry = p.radii
    t0, t1 = math.radians(p.angles[0]), math.radians(p.angles[1])
    sx, sy = cx + rx * math.cos(t0), cy + ry * math.sin(t0)
    ex, ey = cx + rx * math.cos(t1), cy + ry * math.sin(t1)
    span = (p.angles[1] - p.angles[0]) % 360 if p.ccw else (p.angles[0] - p.angles[1]) % 360
    large = 1 if span > 180 else 0
    sweep = 1 if p.ccw else 0
    if abs(span) < 1e-9:                             # a full ellipse stored as an arc
        return (f"M {_n(cx - rx)} {_n(cy)} A {_n(rx)} {_n(ry)} 0 1 1 {_n(cx + rx)} "
                f"{_n(cy)} A {_n(rx)} {_n(ry)} 0 1 1 {_n(cx - rx)} {_n(cy)}")
    return (f"M {_n(sx)} {_n(sy)} A {_n(rx)} {_n(ry)} 0 {large} {sweep} "
            f"{_n(ex)} {_n(ey)}")


# ------------------------------------------------------------------ PNG emitter

def _png(prims: list[Prim], box: tuple, st: dict, path: Path, dpi: int) -> None:
    """The same primitives through matplotlib/Agg.

    Agg and no pyplot state: this runs inside a stdio MCP server, where the default
    Windows backend tries to open a window and the process simply hangs with no error.
    The y axis is inverted rather than the coordinates negated, so text stays upright
    and the numbers in the figure are the numbers in the `.asc`.
    """
    try:
        import matplotlib
    except ImportError as e:                         # pragma: no cover - env dependent
        raise RenderError("PNG output needs matplotlib (pip install "
                          "'spicemcp[plots]'); .svg needs nothing") from e
    matplotlib.use("Agg", force=True)
    from matplotlib.collections import LineCollection
    from matplotlib.figure import Figure
    from matplotlib.patches import Arc, Circle, Ellipse, Rectangle

    x0, y0, w, h = box
    roles = st["roles"]
    fig = Figure(figsize=(w / UNITS_PER_INCH, h / UNITS_PER_INCH), dpi=dpi)
    fig.patch.set_facecolor(st["bg"])
    ax = fig.add_axes([0, 0, 1, 1])
    ax.set_facecolor(st["bg"])
    ax.set_xlim(x0, x0 + w)
    ax.set_ylim(y0 + h, y0)                          # inverted: LTspice y points down
    ax.set_aspect("equal", adjustable="box")
    ax.axis("off")
    dash_pat = {0: None, 1: (0, (7, 4)), 2: (0, (1.5, 3)), 3: (0, (8, 3, 1.5, 3)),
                4: (0, (8, 3, 1.5, 3, 1.5, 3))}

    g = st.get("grid")
    if g and g["kind"] == "dots":
        # One scatter, not one patch per dot: a full sheet is ~15k dots and patches make
        # that a minute of drawing.
        step = g["step"]
        xs = [x for x in range(math.ceil(x0 / step) * step, int(x0 + w) + 1, step)]
        ys = [y for y in range(math.ceil(y0 / step) * step, int(y0 + h) + 1, step)]
        ax.scatter([x for x in xs for _ in ys], [y for _ in xs for y in ys],
                   s=(_pt(g["r"]) * 2) ** 2 / 4, c=g["c"], marker=".", linewidths=0,
                   zorder=0)
    elif g:
        minor, maj = _grid_lines(g, box)
        for lines, lw in ((minor, g.get("lw", 0.5)), (maj, g.get("major_lw", 0.8))):
            if lines:
                ax.add_collection(LineCollection(
                    [((a, b), (c, d)) for a, b, c, d in lines], colors=g["c"],
                    linewidths=lw, zorder=0))

    for p in prims:
        r = roles.get(p.role, roles["wire"])
        kw = {"color": r["c"], "linewidth": r.get("lw", 1.0),
              "solid_capstyle": "round", "solid_joinstyle": "round"}
        ls = dash_pat.get(p.dash)
        if ls:
            kw["linestyle"] = ls
        if p.kind == "poly":
            ax.plot(p.pts[0::2], p.pts[1::2], **kw)
        elif p.kind == "rect":
            a, b, c, d = p.pts
            ax.add_patch(Rectangle((min(a, c), min(b, d)), abs(c - a), abs(d - b),
                                   fill=False, edgecolor=r["c"],
                                   linewidth=r.get("lw", 1.0),
                                   linestyle=ls or "solid"))
        elif p.kind == "ellipse":
            ax.add_patch(Ellipse(p.center, 2 * p.radii[0], 2 * p.radii[1], fill=False,
                                 edgecolor=r["c"], linewidth=r.get("lw", 1.0),
                                 linestyle=ls or "solid"))
        elif p.kind == "arc":
            # Arc draws counterclockwise from theta1 to theta2 in *data* space, and the
            # data space here is LTspice's -- so the increasing-angle direction is
            # exactly `ccw` and the ends swap when it is not.
            t1, t2 = (p.angles if p.ccw else (p.angles[1], p.angles[0]))
            ax.add_patch(Arc(p.center, 2 * p.radii[0], 2 * p.radii[1], theta1=t1,
                             theta2=t2, edgecolor=r["c"], linewidth=r.get("lw", 1.0),
                             linestyle=ls or "solid"))
        elif p.kind == "dot":
            ax.add_patch(Circle(p.center, p.radii[0], color=r.get("fill", r["c"]),
                                linewidth=0))
        elif p.kind == "text":
            # `rotation_mode="anchor"` turns the text about the anchor and then applies
            # ha/va in the turned frame, which is the same convention `extent()` boxes.
            # matplotlib's angle is counter-clockwise on screen regardless of the
            # inverted data axis, so `rot` goes in unchanged.
            ax.text(p.center[0], p.center[1], p.text,
                    color=r["c"], family=r.get("font", SANS),
                    fontsize=_pt(p.size * r.get("fs", 1.0)),
                    fontweight=r.get("weight", "normal"),
                    rotation=p.rot, rotation_mode="anchor",
                    ha=p.ha, va={"center": "center_baseline", "top": "top",
                                 "bottom": "bottom"}[p.va])
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, dpi=dpi, facecolor=st["bg"])


def _pt(units: float) -> float:
    return units / UNITS_PER_INCH * 72.0


# ------------------------------------------------------------------ entry point

def render(asc_path, output_path: str = "schematic.png",
           style_name: str = DEFAULT_STYLE, dpi: int = 300,
           annotations: dict | None = None, margin: float = MARGIN) -> dict:
    """Render a `.asc` to `.png` or `.svg`. Format comes from the output extension.

    Deterministic: same file, same style, same bytes out. Every offset that could have
    been random -- the sketch wobble, the label nudges -- is a function of the geometry.
    """
    src = Path(asc_path)
    if not src.is_file():
        raise RenderError(f"no such schematic: {src}")
    out = Path(output_path)
    fmt = out.suffix.lower().lstrip(".") or "png"
    if fmt not in ("png", "svg"):
        raise RenderError(f"unsupported output format {fmt!r}; use .png or .svg")
    if dpi < 24 or dpi > 1200:
        raise RenderError(f"dpi {dpi} is outside 24..1200")

    st = style(style_name)
    sch = asc.parse_asc(src)
    prims = build(sch)
    if annotations:
        prims += _annotation_prims(sch, annotations, prims)
    prims = declutter(prims)
    prims = _apply_style_geometry(prims, st)
    box = canvas(prims, margin)

    out.parent.mkdir(parents=True, exist_ok=True)
    if fmt == "svg":
        out.write_text(_svg(prims, box, st, dpi), encoding="utf-8")
    else:
        _png(prims, box, st, out, dpi)

    return {"success": True, "output_path": str(out.resolve()), "format": fmt,
            "style": style_name, "dpi": dpi,
            "components": len(sch.parts), "wires": len(sch.wires),
            "labels": len(sch.net_labels) + len(sch.iopins),
            "grounds": len(sch.grounds), "junctions": len(sch.junctions()),
            "texts": len(sch.texts), "primitives": len(prims),
            "size_units": [round(box[2], 1), round(box[3], 1)],
            "size_inches": [round(box[2] / UNITS_PER_INCH, 2),
                            round(box[3] / UNITS_PER_INCH, 2)],
            "unresolved_symbols": [p.sym for p in sch.parts if p.meta is None],
            "warnings": sch.warnings}


if __name__ == "__main__":                            # smallest useful self-check
    import sys

    if len(sys.argv) > 1:
        for f in sys.argv[1:]:
            for s in STYLES:
                for ext in ("svg", "png"):
                    p = Path(f).with_name(f"{Path(f).stem}_{s}.{ext}")
                    print(render(f, str(p), s)["output_path"])
    else:
        # The arc round-trip is the one piece of this file that is not obvious by
        # inspection, and getting it backwards is a picture that still looks like a
        # circuit. ind.asy's coils must bulge to +x of the pin axis at x=16.
        a = _arc([0, 40, 32, 72, 4, 68, 4, 44], "body", False, 0)
        assert a.center == (16.0, 56.0) and a.radii == (16.0, 16.0), a
        # 135 -> -135 the short way through 0 deg, i.e. bulging to +x, not the long way
        # round the far side of the coil axis.
        assert round(a.angles[0]) == 135 and round(a.angles[1]) == -135, a.angles
        assert not a.ccw and "A 16.00 16.00 0 1 0" in _arc_path(a), _arc_path(a)
        assert _jitter(1, 2, 3, 4) == _jitter(1, 2, 3, 4)
        assert _just("VBottom") == ("center", "bottom") == _just("Bottom")
        assert _net_role("Vout") == "net_out" and _net_role("internal") == "net"
        # A rail whose wire comes down from above hangs downward, or its own bar strikes
        # through its name.
        bar_up = _net_flag({"at": (0, 0), "label": "Vdd"}, False)
        bar_dn = _net_flag({"at": (0, 0), "label": "Vdd"}, True)
        assert bar_up[1].pts[1] == -8 and bar_dn[1].pts[1] == 8, (bar_up, bar_dn)
        assert bar_up[2].va == "bottom" and bar_dn[2].va == "top"
        print("ok")
