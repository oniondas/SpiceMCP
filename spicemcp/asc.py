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
def symbol(name: str) -> dict:
    """Parse a `.asy`: pin name -> local (x, y), plus SpiceOrder and Prefix.

    Pins are keyed by `PinName` as LTspice spells it -- `A`/`B` on a resistor, `+`/`-`
    on a source, `invin`/`noninvin`/`out` on an opamp -- and the netlist argument order
    is `SpiceOrder`, which is *not* the order the pins appear in the file for every
    part. Both are returned so a caller can wire by name and still predict the netlist.
    """
    d = find_sym_dir()
    if d is None:
        raise FileNotFoundError("LTspice symbol dir not found; set LTSPICE_SYM")
    path = d.joinpath(*name.replace("\\", "/").split("/")).with_suffix(".asy")
    if not path.is_file():
        raise FileNotFoundError(f"no such LTspice symbol: {name} ({path})")
    text = path.read_text(errors="replace")
    pins, order, cur = {}, {}, None
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("PIN "):
            f = s.split()
            cur = (int(f[1]), int(f[2]))
        elif s.startswith("PINATTR PinName") and cur:
            pins[s.split(None, 2)[2]] = cur
        elif s.startswith("PINATTR SpiceOrder") and cur:
            order[int(s.split()[2])] = next(k for k, v in pins.items() if v == cur)
    attrs = dict(re.findall(r"^SYMATTR\s+(\S+)\s+(.*)$", text, re.M))
    return {"pins": pins, "order": [order[k] for k in sorted(order)],
            "prefix": attrs.get("Prefix", ""), "default_value": attrs.get("Value", "")}


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

    def part(self, sym: str, inst: str, at: tuple[int, int], rot: str = "R0",
             value=None, **attrs) -> Part:
        if value is not None:
            attrs = {"Value": _fmt(value), **attrs}
        p = Part(sym, inst, at, rot, attrs)
        self.parts.append(p)
        return p

    def wire(self, a: tuple[int, int], b: tuple[int, int]) -> None:
        if a[0] != b[0] and a[1] != b[1]:
            raise ValueError(f"wire {a}->{b} is diagonal; use route()")
        if a != b:
            self.wires.append((*a, *b))

    def net(self, *points: tuple[int, int]) -> None:
        """Chain straight wires through points, in order."""
        for a, b in zip(points, points[1:]):
            self.wire(a, b)

    def route(self, a: tuple[int, int], b: tuple[int, int], style: str = "HV") -> None:
        """Two-segment L route. `HV` goes across then up/down, `VH` the other way."""
        mid = (b[0], a[1]) if style == "HV" else (a[0], b[1])
        self.net(a, mid, b)

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
