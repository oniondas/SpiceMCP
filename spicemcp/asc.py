"""LTspice schematic (`.asc`) writer.

ponytail: exists so a schematic is described by *pin name* instead of by absolute
coordinates. Placing `SYMBOL`/`WIRE` numbers by hand is where a schematic silently
comes out mis-wired -- it opens, it simulates, and it is not the circuit you meant.
Pin offsets are read from the real `.asy` file, so LTspice's own parts work without a
table here: `res`, `cap`, `OpAmps/opamp`, `nmos`, `npn`, anything in `lib/sym`.
  skipped: auto-placement from a netlist. Parts still get a rough (x, y); only the
  pins and the routing are computed. Add a placer when a topology shows up that
  nobody wants to position by hand.

Pair every generated schematic with `check()`, which netlists it through LTspice and
hands back the element lines to compare against the netlist you meant to draw. That
is the only honest way to know the drawing matches -- and it costs one subprocess.
"""

from __future__ import annotations

import dataclasses
import functools
import os
import re
import subprocess
from pathlib import Path

from . import sim

GRID = 16

# Verified against a schematic LTspice itself authored: R90, R180, R270, M90 and M180
# reproduce that file's wire endpoints exactly. M0 and M270 are the remaining mirror
# partners, covered by the round-trip netlist check in tests/test_asc.py.
ORIENT = {
    "R0":   lambda x, y: (x, y),
    "R90":  lambda x, y: (-y, x),
    "R180": lambda x, y: (-x, -y),
    "R270": lambda x, y: (y, -x),
    "M0":   lambda x, y: (-x, y),
    "M90":  lambda x, y: (y, x),
    "M180": lambda x, y: (x, -y),
    "M270": lambda x, y: (-y, -x),
}

SYM_CANDIDATES = [
    r"{local}\LTspice\lib\sym",
    r"{local}\Programs\ADI\LTspice\lib\sym",
    r"{home}\Documents\LTspiceXVII\lib\sym",
    r"C:\Program Files\LTC\LTspiceXVII\lib\sym",
]

_PIN = re.compile(r"^PIN\s+(-?\d+)\s+(-?\d+)", re.M)


def find_sym_dir(explicit: str | None = None) -> Path | None:
    """LTspice's symbol library. `LTSPICE_SYM` overrides."""
    for cand in filter(None, [explicit, os.environ.get("LTSPICE_SYM")]):
        if Path(cand).is_dir():
            return Path(cand)
    local = os.environ.get("LOCALAPPDATA", "")
    home = os.environ.get("USERPROFILE", "")
    for tpl in SYM_CANDIDATES:
        p = Path(tpl.format(local=local, home=home))
        if p.is_dir():
            return p
    return None


@functools.lru_cache(maxsize=None)
def symbol(name: str, search: str | None = None) -> dict:
    """Parse a `.asy`: pin name -> local (x, y), plus SpiceOrder and Prefix.

    Pins are keyed by `PinName` as LTspice spells it -- `A`/`B` on a resistor, `+`/`-`
    on a source, `invin`/`noninvin`/`out` on an opamp -- and the netlist argument order
    is `SpiceOrder`, which is *not* the order the pins appear in the file for every
    part. Both are returned so a caller can wire by name and still predict the netlist.

    `search` is an extra directory tried before the library -- a schematic that ships
    its own `.asy` next to it (every vendor part does) is unrenderable otherwise.

    Also returns the symbol's *drawing*: `shapes` (LINE/RECTANGLE/CIRCLE/ARC/TEXT in
    symbol-local coordinates), `windows` (where the attribute text goes) and `pin_list`
    (pin name, position and whether LTspice displays that name). The renderer needs the
    real geometry -- a resistor drawn as a generic box is a different symbol.
    """
    path = _find_asy(name, search)
    text = _read_text(path)
    pins, order, cur, cur_dir, cur_off = {}, {}, None, "NONE", 8
    pin_list = []
    for line in text.splitlines():
        s = line.strip()
        if s.upper().startswith("PIN "):
            f = s.split()
            cur = _ints(f[1:3])
            cur_dir = f[3].upper() if len(f) > 3 else "NONE"
            cur_off = int(f[4]) if len(f) > 4 and f[4].lstrip("-").isdigit() else 8
            cur = tuple(cur) if cur else None
        elif s.startswith("PINATTR PinName") and cur:
            pname = s.split(None, 2)[2]
            pins[pname] = cur
            pin_list.append({"name": pname, "at": cur, "show": cur_dir != "NONE",
                             "dir": cur_dir, "offset": cur_off})
        elif s.startswith("PINATTR SpiceOrder") and cur:
            order[int(s.split()[2])] = next(k for k, v in pins.items() if v == cur)
    attrs = dict(re.findall(r"^SYMATTR\s+(\S+)\s+(.*)$", text, re.M))
    return {"pins": pins, "order": [order[k] for k in sorted(order)],
            "prefix": attrs.get("Prefix", ""), "default_value": attrs.get("Value", ""),
            "shapes": _shapes(text), "windows": _windows(text), "pin_list": pin_list,
            "attrs": attrs, "path": str(path)}


def _find_asy(name: str, search: str | None = None) -> Path:
    """Locate a `.asy`. `search` first, then the library, case-insensitively.

    LTspice writes `SYMBOL RES` and `SYMBOL res` interchangeably into the same file
    (see any of its own examples), so an exact-case lookup fails on the half of the
    parts that happen to be shouted -- and on Windows it works, which is worse: the
    bug only appears once someone renders on Linux.

    A bare name that is not at the top of the library is then looked for anywhere under
    it, which is how LTspice resolves it too: `SYMBOL LTC2992` names a symbol that ships
    in `lib/sym/PowerProducts/`, and every vendor part in its own examples is like this.
    """
    rel = name.replace("\\", "/").split("/")
    dirs = [Path(search)] if search else []
    d = find_sym_dir()
    if d is not None:
        dirs.append(d)
    if not dirs:
        raise FileNotFoundError("LTspice symbol dir not found; set LTSPICE_SYM")
    for base in dirs:
        p = base.joinpath(*rel).with_suffix(".asy")
        if p.is_file():
            return p
        hit = _ci_path(base, rel)
        if hit:
            return hit
    if d is not None and len(rel) == 1:
        hit = _sym_index(d).get(rel[0].lower() + ".asy")
        if hit:
            return hit
    raise FileNotFoundError(f"no such LTspice symbol: {name} "
                            f"(looked in {[str(x) for x in dirs]})")


@functools.lru_cache(maxsize=8)
def _sym_index(root: Path) -> dict[str, Path]:
    """filename -> path for every `.asy` under the library. Built once, on first miss.

    ponytail: cached for the process, so a symbol installed while the server is running
    is not seen. Restarting an MCP server is cheaper than stat-ing 4000 files per lookup.
    """
    out = {}
    for p in root.rglob("*.asy"):
        out.setdefault(p.name.lower(), p)
    return out


def _ci_path(base: Path, rel: list[str]) -> Path | None:
    """Case-insensitive walk of `rel` under `base`, resolving the last part as `.asy`."""
    cur = base
    for i, part in enumerate(rel):
        want = (part + ".asy" if i == len(rel) - 1 else part).lower()
        try:
            nxt = next((c for c in cur.iterdir() if c.name.lower() == want), None)
        except OSError:
            return None
        if nxt is None:
            return None
        cur = nxt
    return cur if cur.is_file() else None


def _read_text(path) -> str:
    """Read a `.asc`/`.asy`. LTspice writes either UTF-8 or UTF-16LE, undeclared.

    Getting this wrong is not a garbled character: a UTF-16 file read as UTF-8 has a
    NUL after every letter, so *no* line matches `WIRE` and the schematic silently
    parses as empty. `.01u` written by an older LTspice is cp1252 `\xb5` (micro), which
    is not valid UTF-8 at all -- hence the third attempt rather than a raise.
    """
    b = Path(path).read_bytes()
    if b[:2] == b"\xff\xfe" or b[1:2] == b"\x00":
        return b.decode("utf-16-le", errors="replace").lstrip("﻿")
    for enc in ("utf-8", "cp1252"):
        try:
            return b.decode(enc)
        except UnicodeDecodeError:
            continue
    return b.decode("latin-1")


def _ints(toks) -> list[int] | None:
    try:
        return [int(float(t)) for t in toks]
    except (TypeError, ValueError):
        return None


def _shapes(text: str) -> list[dict]:
    """Drawing primitives from a `.asy` or `.asc`, in that file's own coordinates.

    `ARC` carries the bounding box of the whole ellipse plus two points naming the
    start and end rays from its centre -- not two angles. `TEXT` is a label drawn on
    the sheet or inside the symbol.
    """
    out = []
    for line in text.splitlines():
        f = line.strip().split()
        if len(f) < 4:
            continue
        kw = f[0].upper()
        if kw in ("LINE", "RECTANGLE", "CIRCLE") and len(f) >= 6:
            n = _ints(f[2:6])
            if n:
                out.append({"kind": kw.lower(), "pts": n, "wide": f[1].lower() == "wide",
                            "dash": _style(f, 6)})
        elif kw == "ARC" and len(f) >= 10:
            n = _ints(f[2:10])
            if n:
                out.append({"kind": "arc", "pts": n, "wide": f[1].lower() == "wide",
                            "dash": _style(f, 10)})
        elif kw == "TEXT" and len(f) >= 6:
            n = _ints(f[1:3])
            if n:
                out.append({"kind": "text", "pts": n, "just": f[3],
                            "size": _ints([f[4]])[0] if _ints([f[4]]) else 2,
                            "text": " ".join(f[5:])})
    return out


def _style(f: list[str], i: int) -> int:
    return int(f[i]) if len(f) > i and f[i].isdigit() else 0


_WINDOW = re.compile(r"^WINDOW\s+(\d+)\s+(-?\d+)\s+(-?\d+)\s+(\S+)\s+(\d+)", re.M)
# LTspice's attribute-window indices. 0 and 3 are the two every part has.
WINDOW_ATTR = {0: "InstName", 3: "Value", 39: "SpiceLine", 40: "SpiceLine2",
               123: "Value2"}


def _windows(text: str) -> dict[int, dict]:
    return {int(m.group(1)): {"at": (int(m.group(2)), int(m.group(3))),
                              "just": m.group(4), "size": int(m.group(5))}
            for m in _WINDOW.finditer(text)}



class Part:
    """A placed symbol. `part["out"]` is that pin's absolute sheet coordinate."""

    def __init__(self, sym: str, inst: str, at: tuple[int, int], rot: str,
                 attrs: dict[str, str]):
        if rot not in ORIENT:
            raise ValueError(f"bad orientation {rot!r}; one of {sorted(ORIENT)}")
        self.sym, self.inst, self.at, self.rot, self.attrs = sym, inst, at, rot, attrs
        self.meta = symbol(sym)

    def __getitem__(self, pin: str) -> tuple[int, int]:
        try:
            local = self.meta["pins"][pin]
        except KeyError:
            raise KeyError(f"{self.sym} has no pin {pin!r};"
                           f" has {sorted(self.meta['pins'])}") from None
        dx, dy = ORIENT[self.rot](*local)
        return (self.at[0] + dx, self.at[1] + dy)

    def lines(self) -> list[str]:
        out = [f"SYMBOL {self.sym.replace('/', chr(92) * 2)} "
               f"{self.at[0]} {self.at[1]} {self.rot}",
               f"SYMATTR InstName {self.inst}"]
        out += [f"SYMATTR {k} {v}" for k, v in self.attrs.items()]
        return out


class Sheet:
    """Build a `.asc`. Wire by pin, not by coordinate.

    Directives and notes are stacked automatically below / above the drawn content, so
    a caller never picks a text coordinate either.
    """

    def __init__(self, size: tuple[int, int] = (1200, 900)):
        self.size = size
        self.parts: list[Part] = []
        self.wires: list[tuple[int, int, int, int]] = []
        self.flags: list[tuple[int, int, str]] = []
        self.directives: list[str] = []
        self.notes: list[str] = []
        self._seen: set[tuple] = set()

    def part(self, sym: str, inst: str, at: tuple[int, int], rot: str = "R0",
             value=None, **attrs) -> Part:
        if value is not None:
            attrs = {"Value": _fmt(value), **attrs}
        p = Part(sym, inst, at, rot, attrs)
        self.parts.append(p)
        return p

    def _seg(self, a: tuple[int, int], b: tuple[int, int]) -> None:
        """One orthogonal segment, skipping zero-length and already-drawn ones.

        LTspice tolerates a duplicated WIRE, so this is not correctness -- but a
        re-drawn segment is invisible in the picture and shows up as a second identical
        line in every diff of the `.asc`, which makes a real change hard to see.
        """
        if a == b:
            return
        key = (a, b) if a <= b else (b, a)
        if key in self._seen:
            return
        self._seen.add(key)
        self.wires.append((*a, *b))

    def wire(self, a: tuple[int, int], b: tuple[int, int]) -> None:
        """A straight wire. A diagonal request is routed orthogonally instead.

        LTspice has no diagonal wire: it draws the line, and the two nodes it *looks*
        connected to are not connected. Raising here used to be the safe answer, but it
        pushed every caller into picking corner coordinates by hand -- which is the
        mistake this module exists to remove. So a diagonal is now an L-route.
        """
        if a[0] != b[0] and a[1] != b[1]:
            self.connect(a, b)
            return
        self._seg(a, b)

    def connect(self, pin_a: tuple[int, int], pin_b: tuple[int, int],
                style: str = "auto") -> list[tuple[int, int, int, int]]:
        """Wire two pins with right angles only. Returns the segments it added.

        Straight when the pins share an axis, otherwise a two-segment L through one
        corner:
          `HV`   across first, then up/down -- corner at (pin_b.x, pin_a.y)
          `VH`   up/down first, then across -- corner at (pin_a.x, pin_b.y)
          `auto` the longer leg first, which keeps the corner away from the pin it
                 leaves and reads as one line with a bend rather than two wires.

        Deterministic: `auto` is a function of the two coordinates, so the same pair
        always routes the same way and re-running a generator produces the same `.asc`.
        """
        a, b = tuple(pin_a), tuple(pin_b)
        before = len(self.wires)
        if a[0] == b[0] or a[1] == b[1]:
            self._seg(a, b)
        else:
            if style == "auto":
                style = "HV" if abs(b[0] - a[0]) >= abs(b[1] - a[1]) else "VH"
            if style not in ("HV", "VH"):
                raise ValueError(f"bad route style {style!r}; one of auto, HV, VH")
            mid = (b[0], a[1]) if style == "HV" else (a[0], b[1])
            self._seg(a, mid)
            self._seg(mid, b)
        return self.wires[before:]

    def net(self, *points: tuple[int, int]) -> None:
        """Chain wires through points, in order. A diagonal hop gets an L-route."""
        for a, b in zip(points, points[1:]):
            self.wire(a, b)

    def route(self, a: tuple[int, int], b: tuple[int, int], style: str = "HV") -> None:
        """Two-segment L route. `HV` goes across then up/down, `VH` the other way."""
        self.connect(a, b, style)

    def flag(self, at: tuple[int, int], label: str) -> None:
        self.flags.append((*at, label))

    def gnd(self, *points: tuple[int, int]) -> None:
        for p in points:
            self.flag(p, "0")

    def directive(self, *text: str) -> None:
        self.directives += text

    def note(self, *text: str) -> None:
        self.notes += text

    def _extent(self) -> tuple[int, int]:
        ys = [w[1] for w in self.wires] + [w[3] for w in self.wires]
        ys += [f[1] for f in self.flags]
        ys += [p[n][1] for p in self.parts for n in p.meta["pins"]]
        return (min(ys, default=0), max(ys, default=0))

    def render(self) -> str:
        top, bot = self._extent()
        out = ["Version 4.1", f"SHEET 1 {self.size[0]} {self.size[1]}"]
        out += [f"WIRE {a} {b} {c} {d}" for a, b, c, d in self.wires]
        out += [f"FLAG {x} {y} {lab}" for x, y, lab in self.flags]
        for p in self.parts:
            out += p.lines()
        for i, t in enumerate(self.notes):
            out.append(f"TEXT -8 {top - 48 - 24 * (len(self.notes) - 1 - i)} Left 2 ;{t}")
        for i, t in enumerate(self.directives):
            out.append(f"TEXT -8 {bot + 64 + 24 * i} Left 2 !{t}")
        return "\n".join(out) + "\n"

    def write(self, path) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.render(), encoding="utf-8")
        return path


def _fmt(v) -> str:
    """Component values must round-trip exactly.

    `%g` would be the obvious choice and it silently truncates to 6 significant
    figures: a 1849.60938 ohm resistor the optimizer measured is drawn as 1849.61, a
    different circuit that still simulates fine. `repr` of a float is the shortest
    string that reads back as the same double.
    """
    return v if isinstance(v, str) else repr(float(v))


def elements(net_text: str) -> list[str]:
    """Element and directive lines only -- no comments, `.backanno` or `.end`.

    LTspice stamps the source path into the first comment, so a byte compare against a
    hand-written `.cir` always fails; this is what you actually want to compare.

    Two things a subcircuit call picks up on the way out of the netlister, both LTspice
    bookkeeping rather than circuit: a trailing `;` comment listing the symbol's pin
    names, and a separator byte wedged between the prefix and the instance name, so
    `U1` on an `X`-prefix symbol emits as `X<sep>U1`. On 26.0.2 <sep> is U+00A7, and
    depending on the codec used to read the file it arrives as `§`, as `Â§` (UTF-8 read
    as cp1252), or as U+FFFD. Only the first is punctuation -- `Â` is a *letter* and
    survives `\\W` -- so the instance token is filtered to ASCII, which every real
    instance name is.
    """
    keep = []
    for ln in net_text.splitlines():
        s = ln.split(";")[0].strip()               # `;` is a SPICE inline comment
        if not s or s.startswith("*") or s.lower() in (".backanno", ".end"):
            continue
        if not s.startswith("."):
            tok, _, rest = s.partition(" ")
            s = " ".join(filter(None, (re.sub(r"[^A-Za-z0-9_]", "", tok), rest)))
        keep.append(s)
    return keep


def check(asc_path, exe: str | None = None) -> list[str]:
    """Netlist a `.asc` through LTspice and return its element lines.

    Raises if LTspice won't netlist it, which is the useful failure: a symbol that
    isn't in the library or a pin left unwired shows up here rather than as a quietly
    different circuit.
    """
    asc_path = Path(asc_path)
    exe = exe or sim.find_ltspice()
    if not exe:
        raise FileNotFoundError("LTspice executable not found; set LTSPICE_EXE")
    net = asc_path.with_suffix(".net")
    net.unlink(missing_ok=True)
    proc = subprocess.run([exe, "-netlist", str(asc_path)], capture_output=True,
                          text=True, timeout=120, cwd=asc_path.parent)
    if not net.is_file():
        raise RuntimeError(f"LTspice did not netlist {asc_path.name} "
                           f"(exit {proc.returncode}): {proc.stdout}{proc.stderr}")
    return elements(net.read_text(encoding="utf-8", errors="replace"))


# ============================ reading a .asc back ==================================
#
# Everything below turns a `.asc` on disk into absolute sheet geometry. It is the input
# to `render.py` and deliberately knows nothing about how any of it will look: a symbol
# resolves to the lines, arcs and text its `.asy` actually draws, transformed by the
# instance's orientation, and a junction is decided by wire topology. Style lives in
# `render.py`, so the same geometry backs all three looks.
#
# LTspice's y axis points DOWN. Nothing here flips it -- these are sheet coordinates,
# the same numbers the file holds, so they can be compared against it directly.


def orient_det(rot: str) -> int:
    """+1 for a rotation, -1 for a mirror.

    Derived from `ORIENT` rather than from `rot[0] == "R"` so the two cannot disagree.
    The only thing that depends on it is which way an arc sweeps -- a mirrored symbol
    draws its arcs backwards, which turns an inductor's coils inside out.
    """
    (ax, ay), (bx, by) = ORIENT[rot](1, 0), ORIENT[rot](0, 1)
    return 1 if ax * by - ay * bx > 0 else -1


# A justification names the direction the text runs from its anchor, and a pin side names
# the direction its name is written -- so both are vectors, and both rotate with the
# instance. Getting this wrong is what makes a mirrored transistor write its own name
# across its base: the anchor mirrors, the text still runs the old way, and the two
# disagree. LTspice's `VTop` / `VBottom` tokens are this transform already applied to a
# rotated instance's default, which is why they only ever appear on rotated instances.
_JUST_DIR = {"left": (1, 0), "right": (-1, 0), "top": (0, 1), "bottom": (0, -1),
             "center": (0, 0)}
_PIN_DIR = {"left": (-1, 0), "right": (1, 0), "top": (0, -1), "bottom": (0, 1)}


def _turn(table: dict, tok: str, rot: str, default: str) -> str:
    """Re-express a direction token in the sheet frame, for `rot`."""
    key = (tok or default).lower().lstrip("v")
    v = table.get(key)
    if v is None or v == (0, 0):
        return key.capitalize() if v else default
    x, y = ORIENT[rot](*v)
    back = {d: n for n, d in table.items()}
    return back[(1 if x > 0 else -1 if x < 0 else 0,
                 1 if y > 0 else -1 if y < 0 else 0)].capitalize()


@dataclasses.dataclass
class Placed:
    """One `SYMBOL` line, resolved to sheet coordinates.

    `meta` is None when the `.asy` could not be found -- a vendor part or a symbol from
    another machine's library. That is reported, not raised: the rest of the schematic
    is still worth drawing, with a placeholder where the part is.
    """

    sym: str
    inst: str
    at: tuple[int, int]
    rot: str
    attrs: dict[str, str] = dataclasses.field(default_factory=dict)
    windows: dict[int, dict] = dataclasses.field(default_factory=dict)
    meta: dict | None = None
    error: str = ""

    def xf(self, x: int, y: int) -> tuple[int, int]:
        dx, dy = ORIENT[self.rot](x, y)
        return (self.at[0] + dx, self.at[1] + dy)

    @property
    def value(self) -> str:
        return self.attrs.get("Value", "")

    def pins(self) -> list[dict]:
        """Pins in sheet coordinates, each with the side it sits on and `vertical`.

        A `V`-prefixed `PIN` token (`VLEFT`) means LTspice draws that pin's *name*
        rotated, reading bottom-to-top. It is not decoration: LTC2992 puts 30 names up to
        nine characters long on a 32-unit pitch along the top and bottom edges, and marks
        exactly those `VLEFT` while leaving its four widely-spaced `Sense` pins plain
        `TOP`. Drawn horizontally they overprint into a smear. A quarter-turn on the
        instance swaps which axis the text runs along, so the flag is the symbol's
        intent XOR the instance's turn.
        """
        if not self.meta:
            return []
        swap = ORIENT[self.rot](1, 0)[0] == 0
        out = []
        for p in self.meta["pin_list"]:
            tok = p.get("dir", "NONE").upper()
            out.append({**p, "at": self.xf(*p["at"]),
                        "dir": ("NONE" if tok == "NONE" else
                                _turn(_PIN_DIR, tok, self.rot, "LEFT").upper()),
                        "vertical": tok != "NONE" and tok.startswith("V") != swap})
        return out

    def shapes(self) -> list[dict]:
        """The symbol's drawing in sheet coordinates.

        Bounding boxes (circle, rectangle) are re-normalised after the transform, since
        rotating a box swaps which corner is which. Arcs additionally carry `ccw`, which
        answers the one question the stored form does not: an `ARC` names its start and
        end *rays*, not its direction, and the two possible sweeps between them are
        different pictures -- an inductor's coils bulge the wrong way, or a 90-degree
        corner becomes a 270-degree one.

        `ccw` means "sweep towards increasing atan2 angle, measured in these
        coordinates". LTspice stores the decreasing direction (verified against
        `ind.asy`, whose three coils must bulge to the right of the pin axis), and a
        mirror reverses handedness -- so it is exactly the mirrored orientations that
        sweep the other way.
        """
        out = []
        ccw = orient_det(self.rot) < 0
        for s in (self.meta or {}).get("shapes", []):
            k, p = s["kind"], s["pts"]
            if k == "line":
                out.append({**s, "pts": [*self.xf(p[0], p[1]), *self.xf(p[2], p[3])]})
            elif k in ("rect", "rectangle", "circle"):
                a, b = self.xf(p[0], p[1]), self.xf(p[2], p[3])
                out.append({**s, "kind": k,
                            "pts": [min(a[0], b[0]), min(a[1], b[1]),
                                    max(a[0], b[0]), max(a[1], b[1])]})
            elif k == "arc":
                a, b = self.xf(p[0], p[1]), self.xf(p[2], p[3])
                out.append({**s,
                            "pts": [min(a[0], b[0]), min(a[1], b[1]),
                                    max(a[0], b[0]), max(a[1], b[1]),
                                    *self.xf(p[4], p[5]), *self.xf(p[6], p[7])],
                            "ccw": ccw})
            elif k == "text":
                out.append({**s, "pts": list(self.xf(p[0], p[1]))})
        return out

    def bbox(self) -> tuple[int, int, int, int]:
        xs, ys = [self.at[0]], [self.at[1]]
        for s in self.shapes():
            p = s["pts"]
            xs += p[0::2][:4]
            ys += p[1::2][:4]
        for p in self.pins():
            xs.append(p["at"][0])
            ys.append(p["at"][1])
        return (min(xs), min(ys), max(xs), max(ys))

    def labels(self) -> list[dict]:
        """The attribute text LTspice draws for this instance, placed on the sheet.

        A `WINDOW` in the `.asc` overrides the `.asy` default, which is how LTspice
        lets you drag a value out of the way -- honouring it is the difference between
        matching the file and second-guessing it. An override's justification is already
        in the sheet frame (LTspice wrote it while the instance was rotated); a `.asy`
        default is in the symbol frame and is turned to match the instance.

        Where a part carries an attribute with no window at all (`opamp` has no
        `WINDOW 3`, so LTspice hides its subcircuit name) the text is stacked to the
        right of the symbol instead of dropped. This renderer is for documentation: an
        unlabelled op-amp is the one thing a reader needs identified.
        """
        out, used = [], set()
        defaults = (self.meta or {}).get("windows", {})
        for idx in sorted(set(defaults) | set(self.windows)):
            attr = WINDOW_ATTR.get(idx)
            text = self.attrs.get(attr, "") if attr else ""
            if not attr or not text:
                continue
            over = self.windows.get(idx)
            w = over or defaults[idx]
            just = w["just"] if over else _turn(_JUST_DIR, w["just"], self.rot, "Left")
            used.add(attr)
            out.append({"attr": attr, "text": text, "at": self.xf(*w["at"]),
                        "just": just, "size": w["size"]})
        x0, y0, x1, y1 = self.bbox()
        for n, attr in enumerate(a for a in ("InstName", "Value", "SpiceLine")
                                 if a not in used and self.attrs.get(a)):
            out.append({"attr": attr, "text": self.attrs[attr],
                        "at": (x1 + 12, y0 + 8 + 20 * n), "just": "Left", "size": 2,
                        "placed_by": "spicemcp"})
        return out


@dataclasses.dataclass
class Schematic:
    """A parsed `.asc`: absolute sheet geometry, nothing about appearance."""

    path: str = ""
    version: str = "4"
    sheet: tuple[int, int] = (0, 0)
    wires: list[tuple[int, int, int, int]] = dataclasses.field(default_factory=list)
    flags: list[dict] = dataclasses.field(default_factory=list)
    iopins: list[dict] = dataclasses.field(default_factory=list)
    dataflags: list[dict] = dataclasses.field(default_factory=list)
    parts: list[Placed] = dataclasses.field(default_factory=list)
    texts: list[dict] = dataclasses.field(default_factory=list)
    shapes: list[dict] = dataclasses.field(default_factory=list)
    warnings: list[str] = dataclasses.field(default_factory=list)

    @property
    def grounds(self) -> list[dict]:
        return [f for f in self.flags if f["label"] in ("0", "GND")]

    @property
    def net_labels(self) -> list[dict]:
        return [f for f in self.flags if f["label"] not in ("0", "GND")]

    def junctions(self) -> list[tuple[int, int]]:
        """Points that need a solder dot, by the rule LTspice itself draws them.

        Three or more wire directions meeting is the whole rule; the work is counting the
        directions right. A wire end landing in the *middle* of another wire contributes
        both of that wire's directions, not one -- the wire continues past the point --
        which is what makes a tee a junction while two collinear wires meeting end to end
        stay at two directions and get no dot. Without that distinction a tee is
        indistinguishable from a crossover, which is the one ambiguity a schematic must
        never have.
        """
        deg: dict[tuple[int, int], set] = {}
        for x1, y1, x2, y2 in self.wires:
            for a, b in (((x1, y1), (x2, y2)), ((x2, y2), (x1, y1))):
                d = (1 if b[0] > a[0] else -1 if b[0] < a[0] else 0,
                     1 if b[1] > a[1] else -1 if b[1] < a[1] else 0)
                deg.setdefault(a, set()).add(d)
        for pt in list(deg):
            if len(deg[pt]) >= 3:
                continue
            for x1, y1, x2, y2 in self.wires:
                if _interior(pt, (x1, y1), (x2, y2)):
                    d = (1 if x2 > x1 else -1 if x2 < x1 else 0,
                         1 if y2 > y1 else -1 if y2 < y1 else 0)
                    deg[pt] |= {d, (-d[0], -d[1])}
        return sorted(pt for pt, dirs in deg.items() if len(dirs) >= 3)

    def bbox(self) -> tuple[int, int, int, int]:
        """Everything drawn, in sheet coordinates. Text extent is added by the renderer,
        which is the only layer that knows how wide a glyph is."""
        xs: list[int] = []
        ys: list[int] = []
        for x1, y1, x2, y2 in self.wires:
            xs += [x1, x2]
            ys += [y1, y2]
        for coll in (self.flags, self.iopins, self.dataflags):
            for f in coll:
                xs.append(f["at"][0])
                ys.append(f["at"][1])
        for p in self.parts:
            b = p.bbox()
            xs += [b[0], b[2]]
            ys += [b[1], b[3]]
        for s in self.shapes + self.texts:
            pts = s["pts"]
            xs += pts[0::2][:4]
            ys += pts[1::2][:4]
        if not xs:
            return (0, 0, self.sheet[0] or 100, self.sheet[1] or 100)
        return (min(xs), min(ys), max(xs), max(ys))


def _interior(pt, a, b) -> bool:
    """`pt` lies strictly between the ends of the orthogonal segment a-b."""
    if pt in (a, b):
        return False
    if a[0] == b[0] == pt[0]:
        return min(a[1], b[1]) < pt[1] < max(a[1], b[1])
    if a[1] == b[1] == pt[1]:
        return min(a[0], b[0]) < pt[0] < max(a[0], b[0])
    return False


def parse_asc(path) -> Schematic:
    """Read a `.asc` into sheet geometry, resolving every `SYMBOL` against its `.asy`.

    Tolerant on purpose. A schematic that names one symbol this machine's library does
    not have still parses, with that part flagged in `warnings` -- refusing the whole
    file would make the renderer useless for exactly the vendor-part schematics people
    most want a picture of.
    """
    p = Path(path)
    text = _read_text(p)
    sch = Schematic(path=str(p))
    cur: Placed | None = None
    for line in text.splitlines():
        f = line.strip().split()
        if not f:
            continue
        kw = f[0].upper()
        # Only the coordinate pair is numeric. Parsing four tokens deep would return None
        # for every line whose last field is a word -- a FLAG's net name, an IOPIN's
        # direction, a DATAFLAG's expression -- and drop it without a warning. `FLAG x y 0`
        # would survive, because "0" parses, so grounds appeared and net labels did not.
        n = _ints(f[1:3])
        if kw == "VERSION" and len(f) > 1:
            sch.version = f[1]
        elif kw == "SHEET" and len(f) >= 4:
            s = _ints(f[2:4])
            sch.sheet = (s[0], s[1]) if s else (0, 0)
        elif kw == "WIRE":
            w = _ints(f[1:5])
            if w and len(w) == 4 and (w[0], w[1]) != (w[2], w[3]):
                sch.wires.append((w[0], w[1], w[2], w[3]))
        elif kw == "FLAG" and n and len(f) >= 4:
            sch.flags.append({"at": (n[0], n[1]), "label": " ".join(f[3:])})
        elif kw == "IOPIN" and n and len(f) >= 4:
            sch.iopins.append({"at": (n[0], n[1]), "dir": f[3]})
        elif kw == "DATAFLAG" and n and len(f) >= 4:
            sch.dataflags.append({"at": (n[0], n[1]), "expr": " ".join(f[3:])})
        elif kw == "SYMBOL" and len(f) >= 5 and _ints(f[2:4]):
            xy = _ints(f[2:4])
            cur = Placed(sym=f[1], inst="", at=(xy[0], xy[1]),
                         rot=f[4] if f[4] in ORIENT else "R0")
            if f[4] not in ORIENT:
                sch.warnings.append(f"{f[1]}: unknown orientation {f[4]!r}, drew it R0")
            try:
                cur.meta = symbol(f[1], str(p.parent))
            except (FileNotFoundError, OSError, ValueError) as e:
                cur.error = str(e)
                sch.warnings.append(f"symbol {f[1]!r} not found: {e}")
            sch.parts.append(cur)
        elif kw == "SYMATTR" and cur is not None and len(f) >= 2:
            val = " ".join(f[2:])
            cur.attrs[f[1]] = val
            if f[1] == "InstName":
                cur.inst = val
        elif kw == "WINDOW" and cur is not None:
            cur.windows.update(_windows(line.strip()))
    for s in _shapes(text):
        (sch.texts if s["kind"] == "text" else sch.shapes).append(s)
    for t in sch.texts:
        body = t["text"]
        t["role"] = ("directive" if body.startswith("!") else
                     "comment" if body.startswith(";") else "plain")
        t["text"] = body[1:] if t["role"] != "plain" else body
        # LTspice keeps a multi-line text box on one file line, escaping the breaks.
        t["lines"] = t["text"].replace("\\n", "\n").split("\n")
    return sch

