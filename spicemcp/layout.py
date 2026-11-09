"""Draw a SPICE netlist as a readable LTspice schematic, then prove it is the same circuit.

A netlist says what connects to what; a schematic has to say it so a person can read it.
The conventions an engineer reads without thinking are the ones this placer enforces:

  * signal flows left to right: an amplifier's inputs are left of its output,
  * ground is at the bottom: every part with one end on ground hangs downward from its
    node and ends in its own ground symbol, so there is no ground wire to follow,
  * the main path sits on one row; a part that bridges far-apart nodes (a feedback
    resistor, a bootstrap capacitor) goes on its own lane above or below it,
  * wires are right angles only, never run through a part, never end on another net's
    wire, and cross another net only where nothing else is possible. A connection that
    would need a long detour is drawn as a pair of net labels instead.

Three steps, each replaceable on its own:

  `parse`   -> elements, each node list mapped onto its symbol's pins
  `place`   -> a column and row per node (a small cost minimisation), then a position
               and an orientation per part
  `route`   -> one A* search per pin on a 16-unit grid, growing each net as a tree

Then `connectivity()` reads the drawn geometry back, the way LTspice would, and
`verify()` compares it with the netlist. A drawing that does not match is a bug in this
file, never a judgment call, so `draw()` refuses to return one. `lint()` scores how
readable the result is, so "readable" is a number, not an opinion.
"""

from __future__ import annotations

import dataclasses
import heapq
import math
import re
from pathlib import Path

from . import asc, blocks

GND = {"0", "gnd"}
G = asc.GRID
CX, RY = 288, 224              # column pitch and row pitch, sheet units
LANE = 112                     # spacing of the bypass lanes above/below the main rows
NODES = {"R": 2, "C": 2, "L": 2, "D": 2, "V": 2, "I": 2, "B": 2,
         "E": 4, "G": 4, "S": 4, "M": 4, "Q": 3, "X": 5}
OPAMP_SYM = "OpAmps/opamp2"    # in+ in- V+ V- out, the 5-pin op-amp convention
SYM = {"R": "res", "C": "cap", "L": "ind", "D": "diode", "V": "voltage",
       "I": "current", "B": "bv", "E": "e", "G": "g", "S": "sw"}


class LayoutError(ValueError):
    """The netlist cannot be drawn, with the reason a caller can act on."""


@dataclasses.dataclass
class El:
    name: str
    kind: str
    nodes: list[str]            # netlist order
    rest: str                   # everything after the nodes: value, model, params
    sym: str = ""
    pin_nodes: list[str] = dataclasses.field(default_factory=list)  # per symbol order
    role: str = ""              # series | shunt | amp | pair
    main: tuple = ()            # the two nodes a series/pair part bridges
    inputs: list[str] = dataclasses.field(default_factory=list)
    out: str = ""
    ground_pin: int = -1        # for a shunt with more than two pins
    out_pin: int = -1           # spice index of the output pin, where it is fixed
    supply_pins: tuple = ()     # spice indices drawn with a rail label, not a wire
    part: asc.Part | None = None
    rail_up: bool = False       # hangs from a positive rail: rail end up, node end down
    upright: bool = False       # stands in its node's own column (a block member)
    face: str = ""              # node the gate/base pin turns towards
    gate_side: int = 0          # or a fixed side for the gate: -1 left, +1 right


def _gnd(n: str) -> bool:
    return n.lower() in GND


# ---------------------------------------------------------------- parse

def subcircuits(netlist: str) -> dict[str, str]:
    """`.subckt` blocks by name (lower case) -> their full text."""
    out, cur = {}, None
    for raw in netlist.splitlines():
        s = raw.strip()
        m = re.match(r"\.subckt\s+(\S+)", s, re.I)
        if m:
            cur = m.group(1)
            out[cur.lower()] = []
        if cur is not None:
            out[cur.lower()].append(raw.rstrip())
            if re.match(r"\.ends\b", s, re.I):
                cur = None
    return {k: "\n".join(v) + "\n" for k, v in out.items()}


def parse(netlist: str) -> tuple[list[El], list[str], list[str]]:
    """Elements, directives and comment lines. Continuation lines (`+`) are joined.
    `.subckt` blocks are skipped: they are drawn as a symbol and shipped as a library."""
    lines: list[str] = []
    for raw in netlist.splitlines():
        s = raw.strip()
        if s.lower() == ".end":             # later decks (stability checks) are not drawn
            break
        if s.startswith("+") and lines:
            lines[-1] += " " + s[1:].strip()
        elif s:
            lines.append(s)
    els, dirs, notes = [], [], []
    models = {}
    subs = {k: v for k, v in subcircuits(netlist).items()}
    inside = False
    for s in lines:
        if re.match(r"\.subckt\b", s, re.I):
            inside = True
        if inside:
            inside = not re.match(r"\.ends\b", s, re.I)
            continue
        if s.startswith("*"):
            notes.append(s.lstrip("* ").strip())
        elif s.startswith("."):
            if s.lower() != ".end":
                dirs.append(s)
            m = re.match(r"\.model\s+(\S+)\s+(\w+)", s, re.I)
            if m:
                models[m.group(1).lower()] = m.group(2).upper() + (
                    " PCHAN" if re.search(r"\bpchan\b", s, re.I) else "")
        else:
            els.append(s)
    out = []
    for s in els:
        s = s.split(";")[0].strip()
        tok = s.split()
        kind = tok[0][0].upper()
        if kind == "K":                  # coupled windings: LTspice takes K as a directive
            dirs.append(s)
            continue
        if kind not in NODES:
            raise LayoutError(
                f"{tok[0]}: element type {kind!r} is not supported by the auto-placer "
                f"(supported: {''.join(sorted(NODES))}). Draw this part by hand.")
        n = NODES[kind]
        if kind == "X":
            n = _x_nodes(tok, subs)
        elif kind == "M" and len(tok) >= 5 and "VDMOS" in models.get(tok[4].lower(), ""):
            n = 3                                # power MOSFET: drain gate source
        if len(tok) < n + 1:
            raise LayoutError(f"{tok[0]}: needs {n} nodes, line is {s!r}")
        el = El(tok[0], kind, tok[1:1 + n], " ".join(tok[1 + n:]))
        _classify(el, models)
        if el.role == "pair" and any(_gnd(x) for x in el.main):
            if all(_gnd(x) for x in el.main):
                raise LayoutError(f"{el.name}: both main terminals on ground")
            el.role = "shunt"                    # a low-side switch hangs like a part
            el.ground_pin = el.pin_nodes.index(next(x for x in el.main if _gnd(x)))
            el.main = (next(x for x in el.main if not _gnd(x)),)
        out.append(el)
    names = [e.name.lower() for e in out]
    dup = {x for x in names if names.count(x) > 1}
    if dup:
        raise LayoutError(f"duplicate element names: {sorted(dup)}")
    return out, dirs, notes


def _x_nodes(tok: list[str], subs: dict) -> int:
    """Node count of a subcircuit instance: everything before the subcircuit's name.
    Only the 5-pin op-amp (in+ in- V+ V- out) has a symbol here."""
    args = [t for t in tok[1:] if "=" not in t and t.lower() != "params:"]
    names = [i for i, t in enumerate(args) if t.lower() in subs]
    n = names[0] if names else len(args) - 1
    if n != 5:
        raise LayoutError(
            f"{tok[0]}: only 5-pin op-amp subcircuits (in+ in- V+ V- out) can be "
            f"auto-placed; this one has {n} pins. Draw it by hand.")
    return n


def _classify(el: El, models: dict) -> None:
    k, n = el.kind, el.nodes
    if k == "X":
        el.sym, el.pin_nodes = OPAMP_SYM, list(n)
        el.out, el.out_pin, el.supply_pins = n[4], 4, (2, 3)
        el.role, el.main = "amp", (n[4],)
        el.inputs = [x for x in n[:2] if not _gnd(x) and x != n[4]]
        return
    if k == "M":
        typ = models.get(el.rest.split()[0].lower(), "NMOS") if el.rest else "NMOS"
        ch = "pmos" if "PMOS" in typ or "PCHAN" in typ else "nmos"
        if len(n) == 3:                          # VDMOS power device
            el.sym, el.pin_nodes = ch, list(n)
        elif n[3] == n[2]:                       # bulk tied to source: 3-pin symbol
            el.sym, el.pin_nodes = ch, n[:3]
        else:
            el.sym, el.pin_nodes = ch + "4", list(n)
        el.role, el.main, el.inputs = "pair", (n[2], n[0]), [n[1]]
        return
    if k == "Q":                                 # collector base emitter, like D G S
        typ = models.get(el.rest.split()[0].lower(), "NPN") if el.rest else "NPN"
        el.sym, el.pin_nodes = ("pnp" if "PNP" in typ else "npn"), list(n)
        el.role, el.main, el.inputs = "pair", (n[2], n[0]), [n[1]]
        return
    el.sym, el.pin_nodes = SYM[k], list(n)
    if k in "EG" or k == "B":
        ins = n[2:] if k in "EG" else _refs(el.rest)
        el.inputs = [x for x in ins if not _gnd(x)]
        if _gnd(n[1]) or _gnd(n[0]):
            el.out = n[0] if not _gnd(n[0]) else n[1]
            el.role = "amp" if k in "EG" else "shunt"
            el.main = (el.out,)
            return
        el.role, el.main = "pair", (n[0], n[1])
        return
    if k == "S":
        el.role, el.main = "pair", (n[0], n[1])
        el.inputs = [x for x in n[2:] if not _gnd(x)]
        return
    a, b = n[0], n[1]
    if _gnd(a) and _gnd(b):
        raise LayoutError(f"{el.name}: both ends on ground")
    if _gnd(a) or _gnd(b):
        el.role, el.main = "shunt", ((b if _gnd(a) else a),)
    else:
        el.role, el.main = "series", (a, b)


def _refs(expr: str) -> list[str]:
    out = []
    for m in re.finditer(r"\bV\(\s*([^,\s)]+)\s*(?:,\s*([^\s)]+))?\s*\)", expr, re.I):
        out += [x for x in m.groups() if x]
    return list(dict.fromkeys(out))


# ---------------------------------------------------------------- place: columns, rows

def _rails(els: list[El]) -> set[str]:
    """Nodes held by a DC source to ground."""
    out = set()
    for e in els:
        if e.kind == "V" and e.role == "shunt" and not re.search(
                r"\b(ac|pulse|sin|pwl|exp|sffm)\b", e.rest, re.I):
            out.add(e.main[0])
    return out


def _columns(els: list[El], extra=(), back=False) -> dict[str, int]:
    """Column per node: layered-graph placement, the way schematics are drawn by hand.

    1. Distance from the sources (breadth-first over every connection) says which way
       is "downstream", and orients each two-terminal path element that way.
    2. Amplifier and behavioural blocks are directed input -> output regardless.
    3. Feedback makes cycles; the edges that close them are dropped (depth-first back
       edges), and what remains is layered by longest path, so a node sits one column
       right of the furthest node that feeds it. That is what puts a Sallen-Key's op-amp
       output last even though its capacitor reaches it in two steps.
    """
    # A behavioural block that reads the supply it is clamped to is not *fed* by the
    # supply; a rail that also carries the power path is excluded from its inputs.
    power = _rails(els) & {x for e in els if e.role in ("series", "pair") for x in e.main}
    nodes = sorted({x for e in els for x in e.nodes if not _gnd(x)})
    pairs, flows, adj = [], [], {n: set() for n in nodes}
    for e in els:
        if e.role in ("series", "pair"):
            pairs.append(e.main)
        ins = e.inputs
        if e.kind == "B":
            ins = [x for x in ins if x not in power] or ins
        if e.role in ("amp", "shunt") and e.out:
            flows += [(i, e.out) for i in ins if i != e.out]
        links = list(pairs[-1:]) if e.role in ("series", "pair") else []
        links += [(i, x) for i in ins for x in e.main if i != x]
        for u, v in links:
            adj[u].add(v)
            adj[v].add(u)
    for u, v in extra:                   # a block's own left-to-right order
        if u in adj and v in adj:
            flows.append((u, v))
            adj[u].add(v)
            adj[v].add(u)
    sources = sorted({e.main[0] for e in els if e.kind in "VI" and e.role == "shunt"})
    dist: dict[str, int] = {}
    frontier = sources or nodes[:1]
    for n in frontier:
        dist[n] = 0
    while frontier:
        nxt = []
        for u in frontier:
            for v in sorted(adj[u]):
                if v not in dist:
                    dist[v] = dist[u] + 1
                    nxt.append(v)
        frontier = nxt
    for n in nodes:
        dist.setdefault(n, 0)
    out_edges: dict[str, list] = {n: [] for n in nodes}
    for u, v in flows:
        out_edges[u].append(v)
    fixed = {frozenset(x) for x in extra}
    if back:     # feedback parts run back from an amplifier's output to its input
        fixed |= {frozenset(x) for x in flows}
    for u, v in pairs:
        if frozenset((u, v)) in fixed:
            continue                     # a block already says which way this goes
        if dist[u] != dist[v]:
            a, b = (u, v) if dist[u] < dist[v] else (v, u)
            out_edges[a].append(b)
    # drop the edges that close a loop (depth-first back edges), flows explored first
    state: dict[str, int] = {}
    keep: dict[str, list] = {n: [] for n in nodes}

    def dfs(u):
        state[u] = 1
        for v in out_edges[u]:
            if state.get(v) == 1:
                continue                                  # back edge: feedback
            keep[u].append(v)
            if v not in state:
                dfs(v)
        state[u] = 2

    for n in sorted(nodes, key=lambda n: (dist[n], n)):
        if n not in state:
            dfs(n)
    col = {n: 0 for n in nodes}
    order, seen = [], set()

    def topo(u):
        seen.add(u)
        for v in keep[u]:
            if v not in seen:
                topo(v)
        order.append(u)

    for n in nodes:
        if n not in seen:
            topo(n)
    for u in reversed(order):
        for v in keep[u]:
            col[v] = max(col[v], col[u] + 1)
    return col


def _stacks(els: list[El], col: dict[str, int]) -> list[tuple[str, str]]:
    """Transistors outside the power path stand upright, as in any amplifier drawing:
    source moved into the drain's column, one row below it (above it for a PMOS, whose
    source faces the positive rail). A pass device whose source sits on a supply rail
    stays on its side in the power path, the way an LDO or a high-side switch is drawn.
    Returns (upper, lower) node pairs for `_rows`."""
    rails = _rails(els)
    out = []
    for e in els:
        if e.kind not in "MQ" or e.role != "pair" or e.upright:
            continue
        s_, d_ = e.main
        if s_ in rails or d_ in rails:
            continue
        p = e.sym.startswith(("pmos", "pnp"))
        col[s_] = col[d_]
        out.append((s_, d_) if p else (d_, s_))
    return out


def _tails(els: list[El], col: dict[str, int], dirs=()) -> list[tuple[str, str]]:
    """A capacitor to ground behind a series resistor (an output capacitor and its ESR,
    a snubber) hangs straight below the node it comes from, as it is drawn by hand.
    Only for an internal node: one nothing measures, so a filter's output stays in the
    signal path. Returns (upper, lower) pairs."""
    seen = " ".join(dirs).lower()
    out = []
    for n in list(col):
        here = [e for e in els if n in e.nodes]
        series = [e for e in here if e.role == "series" and e.kind == "R"]
        caps = [e for e in here if e.role == "shunt" and e.kind == "C"]
        if len(series) != 1 or len(caps) != 1 or len(here) != 2 or \
                re.search(rf"\bv\(\s*{re.escape(n.lower())}\s*[,)]", seen):
            continue
        m = next(x for x in series[0].main if x != n)
        if _gnd(m) or m not in col:
            continue
        col[n] = col[m]
        out.append((m, n))
    return out


def _rows(els: list[El], col: dict[str, int], stacks=()) -> dict[str, int]:
    """Within a column, the node on the shortest, most forward path takes row 0."""
    sources = {e.main[0] for e in els if e.kind in "VI" and e.role == "shunt"}
    score = {n: 0.0 for n in col}
    for e in els:
        links = []
        if e.role in ("series", "pair"):
            links.append(e.main)
        if e.out:
            links += [(i, e.out) for i in e.inputs]
        for u, v in links:
            if u in col and v in col:
                short = abs(col[u] - col[v]) <= 1
                score[u] += 1.0 if short else -0.5
                score[v] += 1.0 if short else -0.5
    for s in sources:
        score[s] += 0.75
    row = {}
    for c in sorted(set(col.values())):
        here = sorted((n for n in col if col[n] == c), key=lambda n: (-score[n], n))
        for up, lo in stacks:                     # keep each stacked pair in order
            if up in here and lo in here and here.index(lo) < here.index(up):
                here.remove(lo)
                here.insert(here.index(up) + 1, lo)
        for r, n in enumerate(here):
            row[n] = r
    return row


# ---------------------------------------------------------------- place: parts

def _body_center(meta: dict, rot: str) -> tuple[float, float]:
    xs, ys = [], []
    for s in meta["shapes"]:
        p = s["pts"]
        k = s["kind"]
        if k == "text":
            continue
        pts = [(p[0], p[1]), (p[2], p[3])]
        for x, y in pts:
            X, Y = asc.ORIENT[rot](x, y)
            xs.append(X)
            ys.append(Y)
    if not xs:
        for v in meta["pins"].values():
            X, Y = asc.ORIENT[rot](*v)
            xs.append(X)
            ys.append(Y)
    return ((min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2)


def _orient(meta: dict, want: dict[int, tuple[float, float]]) -> str:
    """The orientation whose pins point most nearly where `want` says (by spice order).

    Read from the symbol's own pin geometry, so a library whose `.asy` puts a pin
    somewhere else still gets a sensible orientation rather than a hard-coded one.
    """
    best, best_s = "R0", -1e9
    for rot in ("R0", "R90", "R180", "R270", "M0", "M90", "M180", "M270"):
        cx, cy = _body_center(meta, rot)
        s = 0.0
        for idx, (wx, wy) in want.items():
            px, py = asc.ORIENT[rot](*meta["pins"][meta["order"][idx]])
            dx, dy = px - cx, py - cy
            n = math.hypot(dx, dy) or 1.0
            s += (dx * wx + dy * wy) / n
        if rot.startswith("M"):
            s -= 0.01                          # a rotation reads better than a mirror
        if s > best_s + 1e-9:
            best, best_s = rot, s
    return best


def _side_labels(part: asc.Part) -> None:
    """Name above, value below a part turned on its side.

    Left to the symbol's own windows, a turned resistor writes both texts side by side
    at the same height, on top of each other. LTspice itself writes these overrides when
    you rotate a part (`WINDOW 0 0 56 VBottom 2`); this computes them from the body.
    """
    meta = part.meta
    xs, ys = [], []
    for s in meta["shapes"]:
        if s["kind"] == "text":
            continue
        p = s["pts"]
        for x, y in ((p[0], p[1]), (p[2], p[3])):
            X, Y = asc.ORIENT[part.rot](x, y)
            xs.append(X)
            ys.append(Y)
    cx = (min(xs) + max(xs)) / 2
    inv = {r: next(q for q in asc.ORIENT
                   if all(asc.ORIENT[q](*asc.ORIENT[r](*v)) == v
                          for v in ((1, 0), (0, 1)))) for r in asc.ORIENT}[part.rot]
    for idx, (y, just) in ((0, (min(ys) - 4, "VBottom")), (3, (max(ys) + 4, "VTop"))):
        lx, ly = asc.ORIENT[inv](int(round(cx)), int(round(y)))
        part.windows[idx] = (lx, ly, just, 2)


def _snap(v: float) -> int:
    return int(round(v / G)) * G


class _Placer:
    def __init__(self, els, col, row):
        self.els, self.col, self.row = els, col, row
        self.top_lanes: list[list[tuple]] = []
        self.bot_lanes: list[list[tuple]] = []
        self.slots: set = set()
        self.shunts: dict[tuple, int] = {}
        self.max_row = max(row.values(), default=0)

    def X(self, n):
        return self.col[n] * CX

    def Y(self, n):
        return self.row[n] * RY

    def _lane(self, lanes, lo, hi):
        for i, used in enumerate(lanes):
            if all(hi + 48 <= a or lo - 48 >= b for a, b in used):
                used.append((lo, hi))
                return i
        lanes.append([(lo, hi)])
        return len(lanes) - 1

    def put(self, sheet: asc.Sheet, e: El) -> None:
        meta = asc.symbol(e.sym)
        order = meta["order"]
        idx = {i: n for i, n in enumerate(e.pin_nodes)}
        if e.role == "pair" and e.upright:
            self._upright(sheet, e, meta)
        elif e.role in ("series", "pair"):
            self._series(sheet, e, meta, idx)
        elif e.role == "amp":
            want = {}
            if e.kind == "X":                    # in+ in- V+ V- out
                ip, im = e.nodes[0], e.nodes[1]
                # in- goes on the side its feedback arrives from: on top for a
                # follower (loop over the top), below when the divider sits lower
                if im == e.out:                  # follower: where else does out go?
                    far = [x for o in self.els if o is not e and e.out in o.nodes
                           for x in o.nodes if x != e.out and not _gnd(x)]
                    low = any(self.row.get(x, 0) > self.row.get(ip, 0) for x in far)
                else:
                    low = not _gnd(ip) and not _gnd(im) and \
                        self.row.get(im, 0) > self.row.get(ip, 0)
                low = low and not _gnd(e.nodes[3])  # a flip would hang ground upward
                s = -1.0 if low else 1.0
                want = {0: (-1.0, 0.4 * s), 1: (-1.0, -0.4 * s), 4: (1.0, 0.0)}
                if not low:                      # flipped, the rails swap ends too
                    want.update({2: (0.0, -1.0), 3: (0.0, 1.0)})
            else:
                for i, n in idx.items():
                    if i == 0:
                        want[i] = (0.4, -1.0)
                    elif i == 1:
                        want[i] = (0.4, 1.0)
                    else:
                        want[i] = (-1.0, -0.4 if i == 2 else 0.4)
            rot = _orient(meta, want)
            out = e.out
            pin = order[e.out_pin if e.out_pin >= 0 else e.nodes.index(out)]
            px, py = asc.ORIENT[rot](*meta["pins"][pin])
            tx, ty = self.X(out) - 64, self.Y(out)
            if self.col[out] > 0:
                self.slots.add((self.row[out], self.col[out] - 1))
            e.part = sheet.part(e.sym, e.name, (_snap(tx - px), _snap(ty - py)), rot,
                                value=e.rest or None)
        else:            # shunt: node pin up, ground (or negative rail) down
            node = e.main[0]                 # a positive rail's part hangs the other way
            up = e.rail_up
            k = self.shunts.get((node, up), 0)
            self.shunts[(node, up)] = k + 1
            ni = e.pin_nodes.index(node)
            gi = e.ground_pin if e.ground_pin >= 0 else 1 - ni
            want = {i: (self._side(e), 0.0) for i in range(len(e.pin_nodes))}
            want.update({ni: (0.0, 1.0 if up else -1.0), gi: (0.0, -1.0 if up else 1.0)})
            rot = _orient(meta, want)
            px, py = asc.ORIENT[rot](*meta["pins"][order[ni]])
            if up or e.upright:
                # a block member stands in its node's own column; a part from a
                # positive rail hangs above its node, the way a pull-up is drawn
                tx = self.X(node) + (0 if e.upright else 80 + 96 * k)
                ty = self.Y(node) + (-64 if up else 64)
                e.part = sheet.part(e.sym, e.name, (_snap(tx - px), _snap(ty - py)),
                                    rot, value=e.rest or None)
                return
            tail = k == 0 and sum(node in o.nodes for o in self.els) == 2 and any(
                o.role == "series" and node in o.nodes and
                self.col.get(next(x for x in o.main if x != node)) == self.col[node]
                for o in self.els)
            tx = self.X(node) + (0 if e.kind in "VI" or tail else 80) + 96 * k
            if e.kind in "VI":
                tx = self.X(node) - 64 - 96 * k
            ty = self.Y(node) + 64
            e.part = sheet.part(e.sym, e.name, (_snap(tx - px), _snap(ty - py)), rot,
                                value=e.rest or None)

    def _side(self, e) -> float:
        """Which way a transistor's gate (base) points: towards the node it faces
        (a mirror's gates meet on one bar), a fixed side, or left by default."""
        if e.face and e.face in self.col and e.main and e.main[0] in self.col:
            d = self.X(e.face) - self.X(e.main[0])
            if d:
                return 1.0 if d > 0 else -1.0
        return float(e.gate_side or -1)

    def _upright(self, sheet, e, meta):
        """A diff-pair transistor: standing in its drain's column, drain towards the
        load, source towards the shared tail, gate pointing away from its partner."""
        s_, d_ = e.main
        down = self.Y(s_) > self.Y(d_)              # tail below the drains (N pair)
        order = meta["order"]
        di, si = e.pin_nodes.index(d_), e.pin_nodes.index(s_)
        want = {i: (float(e.gate_side or -1), 0.0) for i in range(len(e.pin_nodes))}
        want.update({di: (0.0, -1.0 if down else 1.0), si: (0.0, 1.0 if down else -1.0)})
        rot = _orient(meta, want)
        pd = asc.ORIENT[rot](*meta["pins"][order[di]])
        ps = asc.ORIENT[rot](*meta["pins"][order[si]])
        ty = (self.Y(d_) + self.Y(s_)) / 2
        ox, oy = self.X(d_) - pd[0], ty - (pd[1] + ps[1]) / 2
        e.part = sheet.part(e.sym, e.name, (_snap(ox), _snap(oy)), rot,
                            value=e.rest or None)

    def _series(self, sheet, e, meta, idx):
        a, b = e.main
        if (self.col[a], self.row[a]) > (self.col[b], self.row[b]):
            a, b = b, a
        ia = e.pin_nodes.index(a)
        ib = e.pin_nodes.index(b)
        ca, cb = self.col[a], self.col[b]
        same = self.row[a] == self.row[b] and abs(ca - cb) == 1
        key = (self.row[a], min(ca, cb))
        horizontal = ca != cb
        if same and key not in self.slots:
            self.slots.add(key)
            ty = self.Y(a)
        elif horizontal:
            lo, hi = sorted((self.X(a), self.X(b)))
            if self.row[a] == 0 and self.row[b] == 0:
                k = self._lane(self.top_lanes, lo, hi)
                ty = -LANE * (k + 1)
            else:
                k = self._lane(self.bot_lanes, lo, hi)
                ty = self.max_row * RY + 176 + LANE * k
        want = {ia: (-1.0, 0.0), ib: (1.0, 0.0)} if horizontal else \
            {ia: (0.0, -1.0), ib: (0.0, 1.0)}
        for i in range(len(e.pin_nodes)):
            if i not in (ia, ib):
                want[i] = (0.0, 1.0) if horizontal else (-1.0, 0.0)
        rot = _orient(meta, want)
        pa = asc.ORIENT[rot](*meta["pins"][meta["order"][ia]])
        pb = asc.ORIENT[rot](*meta["pins"][meta["order"][ib]])
        if horizontal:
            tx = (self.X(a) + self.X(b)) / 2
            ox, oy = tx - (pa[0] + pb[0]) / 2, ty - pa[1]
        else:
            ty = (self.Y(a) + self.Y(b)) / 2      # pins on the node's own column
            ox, oy = self.X(a) - pa[0], ty - (pa[1] + pb[1]) / 2
        e.part = sheet.part(e.sym, e.name, (_snap(ox), _snap(oy)), rot,
                            value=e.rest or None)
        if horizontal and asc.ORIENT[rot](1, 0)[0] == 0:
            _side_labels(e.part)


# ---------------------------------------------------------------- route

def _pin_points(e: El) -> list[tuple[tuple[int, int], str]]:
    meta = e.part.meta
    return [(e.part[meta["order"][i]], n) for i, n in enumerate(e.pin_nodes)]


def _boxes(els: list[El]) -> list[tuple[int, int, int, int]]:
    out = []
    for e in els:
        p = asc.Placed(e.sym, e.name, e.part.at, e.part.rot, meta=e.part.meta)
        xs, ys = [], []
        for s in p.shapes():
            if s["kind"] == "text":
                continue
            q = s["pts"]
            xs += [q[0], q[2]]
            ys += [q[1], q[3]]
        out.append((min(xs), min(ys), max(xs), max(ys)))
    return out


class _Router:
    """A* over the 16-unit lattice. Wires may cross another net only straight through,
    never turn or end on it (LTspice would join them there)."""

    TURN, CROSS, NEAR = 3.0, 8.0, 0.6

    def __init__(self, els: list[El]):
        self.els = els
        self.pins = [(pt, n, e) for e in els for pt, n in _pin_points(e)]
        self.pin_at: dict[tuple, str] = {}
        for pt, n, _ in self.pins:
            self.pin_at.setdefault(pt, n)
        self.blocked: set = set()
        self.escape: dict[tuple, tuple] = {}
        boxes = _boxes(els)
        for e, (x0, y0, x1, y1) in zip(els, boxes):
            cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
            for x in range(_floor(x0) - G, x1 + 2 * G, G):
                for y in range(_floor(y0) - G, y1 + 2 * G, G):
                    if x0 - G <= x <= x1 + G and y0 - G <= y <= y1 + G:
                        self.blocked.add((x, y))
            for pt, _ in _pin_points(e):
                dx, dy = pt[0] - cx, pt[1] - cy
                d = (1 if dx > 0 else -1, 0) if abs(dx) >= abs(dy) else \
                    (0, 1 if dy > 0 else -1)
                self.escape[pt] = d
                q = pt
                while q in self.blocked and _inside_grow(q, (x0, y0, x1, y1)):
                    self.blocked.discard(q)
                    q = (q[0] + d[0] * G, q[1] + d[1] * G)
                self.blocked.discard(q)
        for pt in self.pin_at:
            self.blocked.discard(pt)
        xs = [p[0] for p, _, _ in self.pins] + [b[0] for b in boxes] + [b[2] for b in boxes]
        ys = [p[1] for p, _, _ in self.pins] + [b[1] for b in boxes] + [b[3] for b in boxes]
        self.box = (_floor(min(xs)) - 8 * G, _floor(min(ys)) - 8 * G,
                    max(xs) + 8 * G, max(ys) + 8 * G)
        self.wire_at: dict[tuple, dict] = {}     # point -> {net: set(axes)}
        self.vertex: dict[tuple, str] = {}       # wire end/corner -> net
        self.segs: dict[str, list] = {}
        self.flags: list[tuple[tuple, str]] = []

    # -- bookkeeping
    def add_path(self, net: str, pts: list[tuple]) -> None:
        segs = _compress(pts)
        for a, b in segs:
            self._split_at(net, a)
            self._split_at(net, b)
            self.segs.setdefault(net, []).append((a, b))
            ax = "h" if a[1] == b[1] else "v"
            for q in _cells(a, b):
                self.wire_at.setdefault(q, {}).setdefault(net, set()).add(ax)
            self.vertex[a] = net
            self.vertex[b] = net

    def _split_at(self, net, pt):
        segs = self.segs.get(net, [])
        for i, (a, b) in enumerate(segs):
            if asc._interior(pt, a, b):
                segs[i:i + 1] = [(a, pt), (pt, b)]
                self.vertex[pt] = net
                return

    def tree(self, net: str) -> set:
        pts = {q for a, b in self.segs.get(net, []) for q in _cells(a, b)}
        return pts

    # -- search
    def search(self, net: str, start: tuple, targets: set,
               limit: float = math.inf) -> tuple[list, float] | None:
        """Cheapest path from `start` to any target, or None. `limit`: a path dearer
        than this would not be used anyway, so the search stops once every open path
        costs more (the heuristic never overestimates, so nothing cheaper is lost)."""
        x0, y0, x1, y1 = self.box
        tx0 = min(t[0] for t in targets)
        tx1 = max(t[0] for t in targets)
        ty0 = min(t[1] for t in targets)
        ty1 = max(t[1] for t in targets)

        def h(p):
            dx = max(tx0 - p[0], 0, p[0] - tx1)
            dy = max(ty0 - p[1], 0, p[1] - ty1)
            return (dx + dy) / G

        esc = self.escape.get(start)
        dirs = ((G, 0), (-G, 0), (0, G), (0, -G))
        openq = [(h(start), 0.0, start, None)]
        best = {(start, None): 0.0}
        parent = {}
        n_exp = 0
        while openq:
            f, g, p, d = heapq.heappop(openq)
            if f > limit + 1e-9:
                return None
            if best.get((p, d), 1e18) < g - 1e-9:
                continue
            if p in targets and p != start:
                path = [p]
                key = (p, d)
                while key in parent:
                    key = parent[key]
                    path.append(key[0])
                return path[::-1], g
            n_exp += 1
            if n_exp > 60000:
                return None
            crossing = self._other(net, p)
            for nd in dirs:
                if crossing and d is not None and nd != d:
                    continue                    # straight through a crossing only
                if d is None and esc and (nd[0] // G, nd[1] // G) != esc:
                    continue                    # leave a pin outward
                if d is not None and nd == (-d[0], -d[1]):
                    continue
                q = (p[0] + nd[0], p[1] + nd[1])
                if not (x0 <= q[0] <= x1 and y0 <= q[1] <= y1):
                    continue
                if q in self.blocked and q not in targets:
                    continue
                pn = self.pin_at.get(q)
                if pn is not None and q not in targets:
                    continue                    # never run through any pin
                if self.vertex.get(q, net) != net:
                    continue                    # never touch another net's wire end
                fl = [f_ for f_ in self.flags if f_[0] == q and f_[1] != net]
                if fl:
                    continue
                other = self._other(net, q)
                axis = "h" if nd[1] == 0 else "v"
                c = 1.0
                if other:
                    if any(axis in axes for axes in other.values()):
                        continue                # never overlap another net's wire
                    if q in targets:
                        continue
                    c += self.CROSS
                if d is not None and nd != d:
                    c += self.TURN
                if q in targets and pn is not None:
                    e2 = self.escape.get(q)
                    if e2 and (nd[0] // G, nd[1] // G) != (-e2[0], -e2[1]):
                        c += 6.0                # enter a pin head-on
                c += self.NEAR * self._near(net, q)
                ng = g + c
                key = (q, nd)
                if ng < best.get(key, 1e18) - 1e-9:
                    best[key] = ng
                    parent[key] = (p, d)
                    heapq.heappush(openq, (ng + h(q), ng, q, nd))
        return None

    def _other(self, net, p):
        w = self.wire_at.get(p)
        if not w:
            return {}
        return {k: v for k, v in w.items() if k != net}

    def _near(self, net, q):
        n = 0
        x, y = q
        for r in ((x + G, y), (x - G, y), (x, y + G), (x, y - G)):
            w = self.wire_at.get(r)
            if (w and (len(w) > 1 or net not in w)) or r in self.blocked:
                n += 1
        return n


def _floor(v):
    return int(math.floor(v / G)) * G


def _inside_grow(q, box):
    x0, y0, x1, y1 = box
    return x0 - G <= q[0] <= x1 + G and y0 - G <= q[1] <= y1 + G


def _cells(a, b):
    if a[0] == b[0]:
        s = 1 if b[1] >= a[1] else -1
        return [(a[0], y) for y in range(a[1], b[1] + s, s * G)] if a[1] % G == 0 == b[1] % G \
            else [a, b]
    s = 1 if b[0] >= a[0] else -1
    return [(x, a[1]) for x in range(a[0], b[0] + s, s * G)]


def _compress(pts):
    if len(pts) < 2:
        return []
    out = []
    a = pts[0]
    for i in range(1, len(pts) - 1):
        p, q = pts[i], pts[i + 1]
        if (p[0] - a[0]) * (q[1] - p[1]) != (p[1] - a[1]) * (q[0] - p[0]):
            out.append((a, p))
            a = p
    out.append((a, pts[-1]))
    return [s for s in out if s[0] != s[1]]


# ---------------------------------------------------------------- the whole drawing

@dataclasses.dataclass
class Drawing:
    sheet: asc.Sheet
    elements: list[El]
    labels: list[str]
    score: dict | None = None
    libs: dict = dataclasses.field(default_factory=dict)   # file name -> text
    blocks: list = dataclasses.field(default_factory=list)  # blocks.Block found

    def write(self, path) -> Path:
        """The `.asc`, plus any subcircuit library it names, written beside it."""
        out = self.sheet.write(path)
        for name, text in self.libs.items():
            (out.parent / name).write_text(text, encoding="utf-8")
        return out


def draw(netlist: str, values: dict | None = None, label_nets=None) -> Drawing:
    """Netlist text -> a placed, routed, *verified* `asc.Sheet`.

    `values` fills `{name}` placeholders first, so a template's start point is drawn
    with real part values. `label_nets` are nets that get a name label even when wired
    (default: every node a `.meas` reads, i.e. what the user is looking at).

    Placement is a search, not a single guess: the familiar blocks (`blocks.find`) are
    placed as units first, then a handful of candidate layouts are built (supplies
    wired or drawn as rail labels; feedback parts laid out forward or back from the
    amplifier output; row orders from crossing-reduction sweeps and seeded restarts). Every candidate is routed and checked to be the netlist's circuit;
    the most readable one is kept. Deterministic: same netlist, same drawing.
    """
    if values:
        from . import ir
        netlist, _ = ir.render(netlist, values)
    els0, dirs, notes = parse(netlist)
    if not els0:
        raise LayoutError("no elements in the netlist")
    if label_nets is None:
        # what the user is looking at (.meas) and what a behavioural source reads: a
        # B source has no input pins, so the label is the only visible link to it
        text = dirs + [e.rest for e in els0 if e.kind == "B"]
        label_nets = sorted({m.group(1).lower() for d in text
                             for m in re.finditer(r"\bV\(\s*([^,\s)]+)", d, re.I)}
                            | {x.lower() for d in text
                               for m in re.finditer(r"\bV\(\s*[^,\s)]+\s*,\s*([^\s)]+)",
                                                    d, re.I) for x in [m.group(1)]})
    signs = _rail_signs(els0)
    found = blocks.find(els0, dirs, rails=set(signs))
    cands = []
    for rails in _rail_choices(els0, signs):
        els = _railify(els0, rails) if rails else [dataclasses.replace(e) for e in els0]
        extra, rules = _block_hints(els, found)
        for back in (False, True):
            col = _columns(els, extra, back)
            tails = _tails(els, col, dirs)
            stacks = _stacks(els, col) + tails
            base = _rows(els, col, stacks)
            links = _links(els)
            orders = [base, _bary(col, base, links),
                      _bary(col, base, links, down_first=False)]
            orders += [_bary(col, _shuffle(col, base, k), links)
                       for k in range(SEARCH_RESTARTS)]
            for row in orders:
                c2, r2 = _finish_rows(els, dict(col), row, stacks, tails, rules, rails)
                cands.append((els, c2, r2, rails))
    # every candidate routed once; the best few get the other two routing orders
    scored, seen = [], set()
    for els, col, row, rails in cands:
        key = (tuple(sorted(rails)), tuple(sorted(col.items())), tuple(sorted(row.items())))
        if key in seen:
            continue
        seen.add(key)
        d = _attempt(els, dirs, notes, col, row, "short", label_nets, rails, found)
        scored.append((_badness(d, found), len(scored), d, (els, col, row, rails)))
        if scored[-1][0] <= 0:
            break
    scored.sort(key=lambda t: t[:2])
    best = None
    for k, i, d, args in scored:
        if best is not None and k >= best[0]:
            break
        if not verify(d.sheet, netlist):        # a wrong circuit is never a candidate
            best = (k, d)
    for k, i, d, (els, col, row, rails) in scored[:6]:
        if best is not None and best[0] <= 0:
            break
        for order in ("long", "name"):
            d2 = _attempt(els, dirs, notes, col, row, order, label_nets, rails, found)
            k2 = _badness(d2, found)
            if (best is None or k2 < best[0]) and not verify(d2.sheet, netlist):
                best = (k2, d2)
    if best is None:
        raise LayoutError("auto-placed drawing does not match the netlist "
                          "in any layout tried")
    d = best[1]
    used = {e.rest.split()[0].lower() for e in els0 if e.kind == "X" and e.rest}
    for name, text in subcircuits(netlist).items():
        if name in used:
            fname = f"{text.split()[1]}.lib"
            d.libs[fname] = text
            d.sheet.directive(f".lib {fname}")
    d.blocks = found
    return d


SEARCH_RESTARTS = 2          # seeded random row orders, each refined by sweeps


def _finish_rows(els, col, row, stacks, tails, rules, rails):
    """Make one candidate row order legal: stacked pairs in order, tails clear, block
    rules applied, supplies tucked into their own column at the left."""
    row = dict(row)
    for c in set(col.values()):
        here = sorted((n for n in col if col[n] == c), key=lambda n: (row[n], n))
        for up, lo in stacks:                     # keep each stacked pair in order
            if up in here and lo in here and here.index(lo) < here.index(up):
                here.remove(lo)
                here.insert(here.index(up) + 1, lo)
        for r, n in enumerate(here):
            row[n] = r
    for kind, a, b, k in rules:
        if a not in col or b not in col:
            continue
        if kind == "level":                       # b on a's row, swapping in b's column
            _swap_to(col, row, b, row[a] + k)
        elif kind == "under":                     # b in a's column, k rows from it
            col[b] = col[a]
            _swap_to(col, row, b, row[a] + k)
    for _, n in tails:                  # keep wires from the left clear of the tail
        for x in col:
            if col[x] < col[n] and row[x] >= row[n]:
                row[x] += 1
    _tuck_rails(els, col, row)
    for i, n in enumerate(sorted(rails)):
        col[n], row[n] = -1, i
    return col, row


def _swap_to(col, row, n, r):
    other = next((m for m in col if m != n and col[m] == col[n] and row[m] == r), None)
    if other is not None:
        row[other] = row[n]
    row[n] = r


def _links(els):
    out = []
    for e in els:
        if e.role in ("series", "pair"):
            out.append(e.main)
        out += [(i, x) for i in e.inputs for x in e.main if i != x]
    return out


def _bary(col, row, links, sweeps=4, down_first=True):
    """Crossing reduction, the classic layered-drawing step: each node's row follows
    the mean row of its neighbours in other columns, swept left to right and back."""
    row = dict(row)
    nb = {n: [] for n in col}
    for u, v in links:
        if u in col and v in col and u != v:
            nb[u].append(v)
            nb[v].append(u)
    cols = sorted(set(col.values()))
    for k in range(sweeps):
        for c in (cols if (k % 2 == 0) == down_first else cols[::-1]):
            here = [n for n in col if col[n] == c]

            def key(n):
                ws = [row[m] for m in nb[n] if col[m] != c]
                return (sum(ws) / len(ws) if ws else row[n], row[n], n)
            for r, n in enumerate(sorted(here, key=key)):
                row[n] = r
    return row


def _shuffle(col, row, seed):
    import random
    rnd = random.Random(seed)
    row = dict(row)
    for c in sorted(set(col.values())):
        here = sorted(n for n in col if col[n] == c)
        rnd.shuffle(here)
        for r, n in enumerate(here):
            row[n] = r
    return row


def _rail_signs(els) -> dict[str, int]:
    """Nodes held by a DC source to ground -> +1 (positive supply) or -1."""
    out = {}
    for e in els:
        if e.kind != "V" or e.role != "shunt" or re.search(
                r"\b(ac|pulse|sin|pwl|exp|sffm)\b", e.rest, re.I):
            continue
        try:
            v = float(re.sub(r"(?i)^dc\s*", "", e.rest.split()[-1]) if e.rest else 0)
        except ValueError:
            v = 1.0                          # a {param}: assume a positive supply
        sgn = 1 if v >= 0 else -1
        node = e.main[0]
        out[node] = sgn if node == e.nodes[0] else -sgn
    return out


def _rail_choices(els, signs) -> list[dict]:
    """Supplies drawn as wires, then (if any feeds three or more pins) as rail labels."""
    count: dict[str, int] = {}
    for e in els:
        for n in e.pin_nodes:
            count[n] = count.get(n, 0) + 1
    busy = {n: s for n, s in signs.items() if count.get(n, 0) >= 3}
    return [{}, busy] if busy else [{}]


def _railify(els, rails) -> list[El]:
    """Parts with one end on a supply drawn as a rail hang from their other node, the
    supply end ending in a rail label instead of a wire back to the source."""
    els = [dataclasses.replace(e) for e in els]
    for e in els:
        if e.kind not in "RCLDMQ" or e.role not in ("series", "pair"):
            continue
        on = [x for x in e.main if x in rails]
        other = [x for x in e.main if x not in rails]
        if len(on) != 1 or not other or _gnd(other[0]) or on[0] not in e.pin_nodes:
            continue
        e.role, e.main = "shunt", (other[0],)
        e.ground_pin = e.pin_nodes.index(on[0])
        e.rail_up = rails[on[0]] > 0
        if e.kind in "MQ":                   # a transistor still amplifies: gate -> drain
            e.out = other[0]
    return els


def _block_hints(els, found):
    """What each block asks of the placement: extra left-to-right edges for the column
    layering, row rules, and per-part orientation. Returns (edges, rules)."""
    by = {e.name: e for e in els}
    extra, rules = [], []
    for b in found:
        ps = [by[n] for n in b.parts if n in by]
        if b.kind == "diff_pair" and len(ps) == 2 and all(p.role == "pair" for p in ps):
            left, right, tail = b.nodes["left"], b.nodes["right"], b.nodes["tail"]
            extra += [(left, tail), (tail, right)]
            # N pair: tail below the drains; P pair: tail above them
            rules += [("level", left, right, 0),
                      ("level", left, tail, 1 if b.nodes["pol"] == "n" else -1)]
            for p, side in zip(ps, (-1, 1)):
                p.upright, p.gate_side = True, side
        elif b.kind == "mirror":
            ref = b.nodes["ref"]
            for p, n in zip(ps, (ref,) + tuple(b.nodes["outs"])):
                p.face = ref if n != ref else (b.nodes["outs"][0])
                if p.role == "shunt":
                    p.upright = True             # stands in its drain's column
            tails = {t.nodes["tail"] for t in found if t.kind == "diff_pair"}
            for o in b.nodes["outs"]:
                if o not in tails:                 # a tail current source sits apart
                    rules.append(("level", ref, o, 0))
        elif b.kind == "push_pull" and len(ps) == 2:
            # one transistor over the other: emitters (and the resistors between them)
            # in one column, the bias string between the bases in the column before it
            for chain in (b.nodes["emitters"], b.nodes["bias"]):
                for u, v in zip(chain, chain[1:]):
                    rules.append(("under", u, v, 1))
            for p in ps:
                if p.role == "shunt":
                    p.upright = True
    return extra, rules


def _asymmetry(d: Drawing, found) -> int:
    """How many blocks are not drawn in their familiar shape: a diff pair whose drains
    are not level or whose gates do not point outward, a mirror whose gates are not on
    one bar, a push-pull not one above the other, windings not side by side."""
    by = {e.name: e for e in d.elements}

    def pin(name, i):
        e = by[name]
        return e.part[e.part.meta["order"][i]]
    bad = 0
    for b in found:
        if any(n not in by or by[n].part is None for n in b.parts):
            continue
        if b.kind == "diff_pair":
            a, c = b.parts
            (da, ga), (dc, gc) = (pin(a, 0), pin(a, 1)), (pin(c, 0), pin(c, 1))
            bad += (da[1] != dc[1]) + (not (ga[0] < da[0] and gc[0] > dc[0]))
        elif b.kind == "mirror":
            g0 = pin(b.parts[0], 1)[1]
            bad += sum(pin(n, 1)[1] != g0 for n in b.parts[1:])
        elif b.kind == "push_pull":
            a, c = b.parts
            bad += not (pin(a, 2)[1] < pin(c, 2)[1] and pin(a, 2)[0] == pin(c, 2)[0])
        elif b.kind == "transformer":
            pa, pc = (by[n].part for n in b.parts[:2])
            ya = sorted((pa["A"][1], pa["B"][1]))
            yc = sorted((pc["A"][1], pc["B"][1]))
            bad += not (ya == yc and abs(pa["A"][0] - pc["A"][0]) <= 96)
    return bad


def _tuck_rails(els: list[El], col: dict, row: dict) -> None:
    """A supply that only feeds op-amp supply pins is drawn the usual way: its sources
    in a column of their own at the left, joined to the op-amp by rail labels."""
    use: dict[str, set] = {}
    for e in els:
        for i, n in enumerate(e.pin_nodes):
            if not _gnd(n):
                use.setdefault(n, set()).add("rail" if i in e.supply_pins else
                                             ("src" if e.kind == "V" else "other"))
    rails = sorted(n for n, u in use.items() if u == {"rail", "src"})
    if not rails:
        return
    for c in set(col.values()):                  # close the gaps the rails leave
        here = sorted((n for n in col if col[n] == c and n not in rails), key=row.get)
        for r, n in enumerate(here):
            row[n] = r
    for i, n in enumerate(rails):
        col[n], row[n] = -1, i


def _badness(d: Drawing, found=()) -> float:
    """What the search minimises: the readability lint, plus how far each block is
    from its familiar shape. Anything that makes a drawing wrong-looking (a wire through
    a part, parts on top of each other) costs more than any amount of tidiness."""
    s = lint_sheet(d.sheet)
    # the lint penalty first (the score a user sees), block shape next, then the
    # small things that separate two equally scored drawings
    return (10 * s["penalty"] + 60 * _asymmetry(d, found)
            + len(d.labels) * 4 + s["bends"] + s["wire_length"] / 400)


def _attempt(els, dirs, notes, col, row, order, label_nets, rails=frozenset(),
             blocks=()) -> Drawing:
    els = [dataclasses.replace(e) for e in els]
    sheet = asc.Sheet(size=(1600, 1200))
    pl = _Placer(els, col, row)
    rank = {"amp": 0, "series": 1, "pair": 1, "shunt": 2}
    for e in sorted(els, key=lambda e: (rank[e.role], e.name)):
        pl.put(sheet, e)
    for b in blocks:
        if b.kind == "transformer":
            _face_windings(els, b)
    r = _Router(els)
    # op-amp supply pins, and every pin on a supply drawn as a rail, take a rail
    # label, as on any drawn schematic, not a wire back to the source
    rail_pins = [(e.part[e.part.meta["order"][i]], e.pin_nodes[i])
                 for e in els for i in range(len(e.pin_nodes))
                 if not _gnd(e.pin_nodes[i])
                 and (i in e.supply_pins or e.pin_nodes[i] in rails)]
    skip = {pt for pt, _ in rail_pins}
    nets: dict[str, list] = {}
    for pt, n, _ in r.pins:
        if not _gnd(n) and pt not in skip:
            nets.setdefault(n, [])
            if pt not in nets[n]:
                nets[n].append(pt)

    def span(n):
        xs = [p[0] for p in nets[n]]
        ys = [p[1] for p in nets[n]]
        return (max(xs) - min(xs)) + (max(ys) - min(ys))

    names = sorted(nets)
    if order == "short":
        names.sort(key=lambda n: (span(n), n))
    elif order == "long":
        names.sort(key=lambda n: (-span(n), n))
    labels = []
    for n in names:
        pts = sorted(nets[n])
        tree_pins = {pts[0]}
        rest = pts[1:]
        while rest:
            tree = r.tree(n) | tree_pins
            rest.sort(key=lambda p: min(abs(p[0] - t[0]) + abs(p[1] - t[1]) for t in tree))
            p = rest.pop(0)
            manh = min(abs(p[0] - t[0]) + abs(p[1] - t[1]) for t in tree) / G
            got = r.search(n, p, tree, min(2.2 * manh + 24, 110))
            if got and got[1] <= 2.2 * manh + 24 and got[1] <= 110:
                r.add_path(n, got[0])
                tree_pins.add(p)
            else:
                labels.append(n)
                _label(r, n, p)
                if not any(f[1] == n for f in r.flags if f[0] in tree):
                    anchor = sorted(tree_pins)[0]
                    _label(r, n, anchor)
                tree_pins.add(p)
    for n in label_nets:
        if n in nets and not any(f[1] == n for f in r.flags):
            _label(r, n, _label_spot(r, n, nets[n]))
    for _, n in rail_pins:
        if n in nets and not any(f[1] == n for f in r.flags):
            _label(r, n, _label_spot(r, n, nets[n]))
    for pt, n in rail_pins:
        _ground(r, pt, n)
    for pt, n, e in r.pins:                          # every ground pin gets a symbol
        if _gnd(n):
            _ground(r, pt)
    for a_b in r.segs.values():
        for a, b in a_b:
            sheet.wire(a, b)
    for pt, n in r.flags:
        sheet.flag(pt, n)
    sheet.note(*notes)
    sheet.directive(*dirs)
    return Drawing(sheet, els, sorted(set(labels)))


def _face_windings(els: list[El], b) -> None:
    """Two coupled windings drawn the way a transformer is: side by side, coils facing
    across the core, the same height; the secondary's grounded end at the bottom, like
    any part to ground. Secondary to the right of the primary if there is room, else to
    the left; if neither spot is free both stay where the placer put them."""
    by = {e.name: e for e in els}
    ws = [by[n] for n in b.parts if n in by and by[n].part is not None][:2]
    if len(ws) < 2:
        return
    p1, p2 = ws[0].part, ws[1].part
    a1, b1 = p1["A"], p1["B"]
    if a1[0] != b1[0]:
        return                                     # primary on its side: leave both
    old = [(p1.at, p1.rot), (p2.at, p2.rot)]
    top, bottom = min(a1[1], b1[1]), max(a1[1], b1[1])
    na, nb = ws[1].pin_nodes[0], ws[1].pin_nodes[1]
    a_up = (not _gnd(na)) if _gnd(na) != _gnd(nb) else a1[1] < b1[1]
    others = [bx for e, bx in zip(els, _boxes(els)) if e is not ws[1] and e is not ws[0]]
    pins = {pt for e in els if e not in ws for pt, _ in _pin_points(e)}
    flip = {"R0": "M0", "M0": "R0", "R180": "M180", "M180": "R180"}
    for side in (1, -1):
        (p1.at, p1.rot), (p2.at, p2.rot) = old
        cx = _body_center(p1.meta, p1.rot)[0] + p1.at[0]
        if (cx - a1[0]) * side < 0:                # coils bulge the wrong way: mirror
            p1.rot = flip.get(p1.rot, p1.rot)
            q = p1["A"]
            p1.at = (p1.at[0] + a1[0] - q[0], p1.at[1] + a1[1] - q[1])
        rot = {(1, True): "M0", (1, False): "R180",
               (-1, True): "R0", (-1, False): "M180"}[(side, a_up)]
        pa = asc.ORIENT[rot](*p2.meta["pins"]["A"])
        target = (a1[0] + side * 64, top if a_up else bottom)
        p2.at, p2.rot = (target[0] - pa[0], target[1] - pa[1]), rot
        hit = any(me[0] - G < o[2] and o[0] < me[2] + G and me[1] - G < o[3]
                  and o[1] < me[3] + G for me in _boxes(ws) for o in others) or any(
            pt in pins for pt in (p2["A"], p2["B"]))
        if not hit:
            # name and value outside the pair, never in the gap between the coils
            for e, out in ((ws[0], -side), (ws[1], side)):
                x0, y0, x1, y1 = _boxes([e])[0]
                x = x1 + 8 if out > 0 else x0 - 8
                just = "Left" if out > 0 else "Right"
                cy = (y0 + y1) // 2
                _window_abs(e.part, 0, (x, cy - 16), just)
                _window_abs(e.part, 3, (x, cy + 16), just)
            return
    (p1.at, p1.rot), (p2.at, p2.rot) = old


def _window_abs(part: asc.Part, idx: int, at: tuple, just: str) -> None:
    """Put attribute window `idx` at sheet point `at` (justification in sheet frame,
    as LTspice writes an override; the position is stored in the symbol's frame)."""
    inv = next(q for q in asc.ORIENT if all(
        asc.ORIENT[q](*asc.ORIENT[part.rot](*v)) == v for v in ((1, 0), (0, 1))))
    lx, ly = asc.ORIENT[inv](at[0] - part.at[0], at[1] - part.at[1])
    part.windows[idx] = (int(lx), int(ly), just, 2)


def _label_spot(r: _Router, net: str, pins: list) -> tuple:
    """Where a name label reads best: a free wire corner or end of the net (not on top
    of a symbol), rightmost for an output-like name, leftmost otherwise."""
    pinset = {p for p, _, _ in r.pins}
    spots = [q for a, b in r.segs.get(net, []) for q in (a, b) if q not in pinset]
    pool = spots or pins
    right = net.startswith(("out", "vo")) or net in ("load",)
    return max(pool, key=lambda q: (q[0], -q[1])) if right else \
        min(pool, key=lambda q: (q[0], q[1]))


def _ground(r: _Router, pt: tuple, name: str = "0") -> None:
    """A ground symbol at a pin. A pin that faces sideways or up gets a short stub
    first, so the symbol hangs below the wire instead of sitting on the part.
    With a `name`, a rail label on a short stub in the pin's own direction."""
    d = r.escape.get(pt, (0, 1))
    if name != "0":
        q = (pt[0] + d[0] * 2 * G, pt[1] + d[1] * 2 * G)
        path = _cells(pt, q)
        if all(c not in r.blocked or c == pt for c in path[1:]) and not any(
                r.wire_at.get(c) or (c in r.pin_at and c != pt) for c in path[1:]):
            r.add_path(name, path)
            r.flags.append((q, name))
        else:
            r.flags.append((pt, name))
        return
    if d == (0, 1):
        r.flags.append((pt, "0"))
        return
    q = (pt[0] + d[0] * 2 * G, pt[1] + d[1] * 2 * G)
    path = _cells(pt, q)
    free = all(c not in r.blocked or c == pt for c in path[1:]) and not any(
        r.wire_at.get(c) or (c in r.pin_at and c != pt) for c in path[1:])
    if d[1] == 0 and free:
        r.add_path("0", path)
        r.flags.append((q, "0"))
    else:
        r.flags.append((pt, "0"))


def _label(r: _Router, net: str, pt: tuple) -> None:
    if not any(f == (pt, net) for f in r.flags):
        r.flags.append((pt, net))


# ---------------------------------------------------------------- read back + verify

def connectivity(sch: asc.Schematic) -> tuple[dict, list[str]]:
    """(instance, pin index in spice order) -> net id, as LTspice would join them.

    Joined: the two ends of a wire; a wire end lying on another wire; a pin or flag on
    a wire end or a wire's interior; flags with the same name. Returned with a list of
    ambiguities (a pin sitting mid-wire), which a hand-drawn schematic might rely on but
    a generated one must not.
    """
    parent: dict = {}

    def find(x):
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    warns = []
    ends = set()
    for x1, y1, x2, y2 in sch.wires:
        union(("p", x1, y1), ("p", x2, y2))
        ends |= {(x1, y1), (x2, y2)}
    for pt in ends:
        for x1, y1, x2, y2 in sch.wires:
            if asc._interior(pt, (x1, y1), (x2, y2)):
                union(("p", *pt), ("p", x1, y1))

    def attach(key, pt, what):
        union(key, ("p", *pt))
        for x1, y1, x2, y2 in sch.wires:
            if asc._interior(pt, (x1, y1), (x2, y2)):
                union(key, ("p", x1, y1))
                warns.append(f"{what} at {pt} sits in the middle of a wire")

    for f in sch.flags:
        attach(("flag", f["label"].lower()), f["at"], f"flag {f['label']}")
    pins = {}
    for p in sch.parts:
        if not p.meta:
            warns.append(f"{p.inst}: symbol {p.sym} not found")
            continue
        for i, name in enumerate(p.meta["order"]):
            pt = p.xf(*p.meta["pins"][name])
            key = ("pin", p.inst, i)
            attach(key, pt, f"{p.inst}.{name}")
            pins[(p.inst, i)] = key
    out = {k: find(v) for k, v in pins.items()}
    gnd = find(("flag", "0")) if ("flag", "0") in parent else None
    names = {}
    for k in list(parent):
        if k[0] == "flag":
            names.setdefault(find(k), set()).add(k[1])
    return {k: ("0" if v == gnd else "/".join(sorted(names.get(v, ()))) or repr(v))
            for k, v in out.items()}, warns


def verify(sheet_or_path, netlist: str) -> list[str]:
    """Problems found comparing a drawing with the netlist it should be. Empty = same.

    Compares *partitions*: the set of pins on each node must be exactly the set of pins
    on one drawn net, and the ground node must be the drawn ground. Net names inside
    the drawing are free (an internal node needs no label).
    """
    if isinstance(sheet_or_path, asc.Sheet):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            p = sheet_or_path.write(Path(td) / "v.asc")
            sch = asc.parse_asc(p)
    else:
        sch = asc.parse_asc(sheet_or_path)
    els, _, _ = parse(netlist)
    got, warns = connectivity(sch)
    problems = list(warns)
    want_groups: dict[str, set] = {}
    for e in els:
        for i, n in enumerate(e.pin_nodes):
            want_groups.setdefault("0" if _gnd(n) else n, set()).add((e.name, i))
    got_groups: dict[str, set] = {}
    for k, v in got.items():
        got_groups.setdefault(v, set()).add(k)
    drawn = {frozenset(g) for g in got_groups.values()}
    for n, g in want_groups.items():
        if frozenset(g) not in drawn:
            problems.append(f"node {n}: pins {sorted(g)} are not exactly one drawn net")
        elif n == "0" and got_groups.get("0") != g:
            problems.append("ground pins are not on the ground symbol")
    missing = {e.name for e in els} - {p.inst for p in sch.parts}
    if missing:
        problems.append(f"parts missing from the drawing: {sorted(missing)}")
    return problems


# ---------------------------------------------------------------- readability lint

def lint(asc_path) -> dict:
    return lint_schematic(asc.parse_asc(asc_path))


def lint_sheet(sheet: asc.Sheet) -> dict:
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        return lint_schematic(asc.parse_asc(sheet.write(Path(td) / "l.asc")))


def lint_schematic(sch: asc.Schematic) -> dict:
    """What makes a schematic hard to read, counted. `score` is 100 minus penalties.

    crossings      two wires crossing with no junction (the eye has to check each one)
    through_parts  a wire drawn across a part's body
    overlaps       two part bodies on top of each other
    ambiguous      a pin or wire end sitting mid-wire: connected in LTspice, invisible
                   in a picture
    dangling       a wire end that connects to nothing
    bends          right-angle corners (a few are normal; many make a maze)
    """
    _, warns = connectivity(sch)
    W = sch.wires
    pins = []
    boxes = []
    for p in sch.parts:
        if not p.meta:
            continue
        for name in p.meta["order"]:
            pins.append(p.xf(*p.meta["pins"][name]))
        xs, ys = [], []
        for s in p.shapes():
            if s["kind"] == "text":
                continue
            q = s["pts"]
            xs += [q[0], q[2]]
            ys += [q[1], q[3]]
        if xs:
            boxes.append((min(xs), min(ys), max(xs), max(ys)))
    flags = [f["at"] for f in sch.flags]
    crossings = 0
    for i, (a1, b1, a2, b2) in enumerate(W):
        for c1, d1, c2, d2 in W[i + 1:]:
            h1, h2 = b1 == b2, d1 == d2
            if h1 == h2:
                continue
            (hx1, hx2, hy), (vx, vy1, vy2) = (
                ((min(a1, a2), max(a1, a2), b1), (c1, min(d1, d2), max(d1, d2))) if h1
                else ((min(c1, c2), max(c1, c2), d1), (a1, min(b1, b2), max(b1, b2))))
            if hx1 < vx < hx2 and vy1 < hy < vy2:
                crossings += 1
    through = 0
    for x1, y1, x2, y2 in W:
        for bx0, by0, bx1, by1 in boxes:
            if x1 == x2:
                if bx0 < x1 < bx1 and min(y1, y2) < by1 and max(y1, y2) > by0 and \
                        not (min(y1, y2) >= by1 - 0 or max(y1, y2) <= by0 + 0):
                    lo, hi = max(min(y1, y2), by0), min(max(y1, y2), by1)
                    if hi - lo > G:
                        through += 1
            else:
                if by0 < y1 < by1 and min(x1, x2) < bx1 and max(x1, x2) > bx0:
                    lo, hi = max(min(x1, x2), bx0), min(max(x1, x2), bx1)
                    if hi - lo > G:
                        through += 1
    overlaps = 0
    for i, a in enumerate(boxes):
        for b in boxes[i + 1:]:
            if a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]:
                overlaps += 1
    deg: dict = {}
    for x1, y1, x2, y2 in W:
        for pt, other in (((x1, y1), (x2, y2)), ((x2, y2), (x1, y1))):
            deg.setdefault(pt, []).append(other)
    dangling = 0
    bends = 0
    for pt, others in deg.items():
        if len(others) == 1 and pt not in pins and pt not in flags and not any(
                asc._interior(pt, (x1, y1), (x2, y2)) for x1, y1, x2, y2 in W):
            dangling += 1
        if len(others) == 2 and pt not in pins and pt not in flags:
            (ax, ay), (bx, by) = others
            if (ax - pt[0]) * (by - pt[1]) != (ay - pt[1]) * (bx - pt[0]):
                bends += 1
    ambiguous = sum(1 for w in warns if "middle of a wire" in w)
    # A signal carried by a pair of name labels instead of a wire (a "jump"): fine
    # once or twice, a treasure hunt when many. Supplies and ground are exempt: a VCC
    # flag on every pull-up is how schematics are drawn.
    got, _ = connectivity(sch)
    supplies = set()
    for p in sch.parts:
        if p.sym == "voltage" and re.fullmatch(
                r"(?i)\s*(dc\s*)?[-+]?[\d.]+(e[-+]?\d+)?[a-z]*\s*",
                p.attrs.get("Value", "") or ""):
            supplies |= {got.get((p.inst, i)) for i in (0, 1)}
    count: dict = {}
    for f in sch.flags:
        count[f["label"].lower()] = count.get(f["label"].lower(), 0) + 1
    jumps = sum(1 for n, c in count.items() if c >= 2 and n not in ("0", "gnd")
                and n not in supplies)
    xs = [v for w in W for v in (w[0], w[2])] + [b[0] for b in boxes] + [b[2] for b in boxes]
    ys = [v for w in W for v in (w[1], w[3])] + [b[1] for b in boxes] + [b[3] for b in boxes]
    w_, h_ = (max(xs) - min(xs), max(ys) - min(ys)) if xs else (1, 1)
    aspect = w_ / max(1, h_)            # width / height: schematics read left to right
    # a page much taller than wide (a stack of parts) or a very long thin strip
    shape = 8 * max(0.0, 1 / max(aspect, 1e-3) - 1.5) + 4 * max(0.0, aspect - 5)
    length = sum(abs(x2 - x1) + abs(y2 - y1) for x1, y1, x2, y2 in W)
    labels = len([f for f in sch.flags if f["label"] not in ("0", "GND")])
    score = 100 - (12 * crossings + 20 * through + 20 * overlaps + 25 * ambiguous
                   + 10 * dangling + 1 * max(0, bends - len(sch.parts))
                   + 5 * max(0, jumps - 1) + round(shape))
    return {"score": max(0, score), "penalty": 100 - score,
            "crossings": crossings, "through_parts": through,
            "jump_labels": jumps, "aspect": round(aspect, 2),
            "overlaps": overlaps, "ambiguous": ambiguous, "dangling": dangling,
            "bends": bends, "wire_length": length, "net_labels": labels,
            "parts": len(sch.parts)}
