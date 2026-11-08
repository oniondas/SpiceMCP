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

from . import asc

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


def _columns(els: list[El]) -> dict[str, int]:
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
    for u, v in pairs:
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
        if e.kind not in "MQ" or e.role != "pair":
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
        self.shunts: dict[str, int] = {}
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
        if e.role in ("series", "pair"):
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
        else:                                            # shunt: node pin up, gnd down
            node = e.main[0]
            k = self.shunts.get(node, 0)
            self.shunts[node] = k + 1
            ni = e.pin_nodes.index(node)
            gi = e.ground_pin if e.ground_pin >= 0 else 1 - ni
            want = {i: (-1.0, 0.0) for i in range(len(e.pin_nodes))}
            want.update({ni: (0.0, -1.0), gi: (0.0, 1.0)})
            rot = _orient(meta, want)
            px, py = asc.ORIENT[rot](*meta["pins"][order[ni]])
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
    def search(self, net: str, start: tuple, targets: set) -> tuple[list, float] | None:
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
        for dx, dy in ((G, 0), (-G, 0), (0, G), (0, -G)):
            r = (q[0] + dx, q[1] + dy)
            if self._other(net, r) or (r in self.blocked):
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
    """
    if values:
        from . import ir
        netlist, _ = ir.render(netlist, values)
    els, dirs, notes = parse(netlist)
    if not els:
        raise LayoutError("no elements in the netlist")
    col = _columns(els)
    tails = _tails(els, col, dirs)
    row = _rows(els, col, _stacks(els, col) + tails)
    for _, n in tails:                  # keep wires from the left clear of the tail
        for x in col:
            if col[x] < col[n] and row[x] >= row[n]:
                row[x] += 1
    _tuck_rails(els, col, row)
    if label_nets is None:
        # what the user is looking at (.meas) and what a behavioural source reads: a
        # B source has no input pins, so the label is the only visible link to it
        text = dirs + [e.rest for e in els if e.kind == "B"]
        label_nets = sorted({m.group(1).lower() for d in text
                             for m in re.finditer(r"\bV\(\s*([^,\s)]+)", d, re.I)}
                            | {x.lower() for d in text
                               for m in re.finditer(r"\bV\(\s*[^,\s)]+\s*,\s*([^\s)]+)",
                                                    d, re.I) for x in [m.group(1)]})
    best = None
    for order in ("short", "long", "name"):
        d = _attempt(els, dirs, notes, col, row, order, label_nets)
        k = _badness(d)
        if best is None or k < best[0]:
            best = (k, d)
        if k == 0:
            break
    d = best[1]
    used = {e.rest.split()[0].lower() for e in els if e.kind == "X" and e.rest}
    for name, text in subcircuits(netlist).items():
        if name in used:
            fname = f"{text.split()[1]}.lib"
            d.libs[fname] = text
            d.sheet.directive(f".lib {fname}")
    problems = verify(d.sheet, netlist)
    if problems:
        raise LayoutError("auto-placed drawing does not match the netlist: "
                          + "; ".join(problems))
    return d


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


def _badness(d: Drawing) -> float:
    s = lint_sheet(d.sheet)
    return s["crossings"] * 10 + len(d.labels) * 4 + s["bends"] + s["wire_length"] / 400


def _attempt(els, dirs, notes, col, row, order, label_nets) -> Drawing:
    els = [dataclasses.replace(e) for e in els]
    sheet = asc.Sheet(size=(1600, 1200))
    pl = _Placer(els, col, row)
    rank = {"amp": 0, "series": 1, "pair": 1, "shunt": 2}
    for e in sorted(els, key=lambda e: (rank[e.role], e.name)):
        pl.put(sheet, e)
    r = _Router(els)
    # op-amp supply pins take a rail label, as on any drawn schematic, not a wire
    rail_pins = [(e.part[e.part.meta["order"][i]], e.pin_nodes[i])
                 for e in els for i in e.supply_pins if not _gnd(e.pin_nodes[i])]
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
            got = r.search(n, p, tree)
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
    length = sum(abs(x2 - x1) + abs(y2 - y1) for x1, y1, x2, y2 in W)
    labels = len([f for f in sch.flags if f["label"] not in ("0", "GND")])
    score = 100 - (12 * crossings + 20 * through + 20 * overlaps + 25 * ambiguous
                   + 10 * dangling + 1 * max(0, bends - len(sch.parts)))
    return {"score": max(0, score), "crossings": crossings, "through_parts": through,
            "overlaps": overlaps, "ambiguous": ambiguous, "dangling": dangling,
            "bends": bends, "wire_length": length, "net_labels": labels,
            "parts": len(sch.parts)}
