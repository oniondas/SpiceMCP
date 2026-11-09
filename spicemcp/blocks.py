"""Find the building blocks an engineer recognises at a glance in a flat netlist.

A schematic reads well when its familiar blocks are drawn the familiar way: a
differential pair as two transistors side by side with their sources joined below, a
current mirror with its gates on one bar, a push-pull stage as one transistor over the
other, a transformer as two windings facing each other. A netlist names none of these,
so they are found here by their wiring alone, then `layout` draws each one as a unit.

Only wiring is used (never part names), so a block is found in any circuit, templated
or not. Each finder is a few lines and independent: add a block by adding a finder.
"""

from __future__ import annotations

import dataclasses
import re

GND = {"0", "gnd"}


@dataclasses.dataclass
class Block:
    kind: str                 # diff_pair | mirror | push_pull | transformer
    parts: tuple[str, ...]    # element names, left (or top) one first
    nodes: dict               # role -> node, per kind (see each finder)


def _gnd(n: str) -> bool:
    return n.lower() in GND


def _polarity(e) -> str:
    return "p" if e.sym.startswith(("pmos", "pnp")) else "n"


def _dgs(e):
    """(drain, gate, source) of a MOSFET, (collector, base, emitter) of a BJT."""
    n = e.nodes
    return n[0], n[1], n[2]


def _transistors(els):
    return [e for e in els if e.kind in "MQ"]


def diff_pairs(els, rails=()) -> list[Block]:
    """Two same-type transistors whose sources share a node that is not a supply,
    with different gates and different drains. Left device first: the one whose drain
    is diode-connected in a mirror load (the classic drawing), else netlist order."""
    out, used = [], set()
    ts = _transistors(els)
    diode = {_dgs(e)[0] for e in ts if _dgs(e)[0] == _dgs(e)[1]}
    for i, a in enumerate(ts):
        for b in ts[i + 1:]:
            if a.name in used or b.name in used or _polarity(a) != _polarity(b):
                continue
            da, ga, sa = _dgs(a)
            db, gb, sb = _dgs(b)
            if sa != sb or _gnd(sa) or sa in rails or ga == gb or da == db or da == sa or db == sb:
                continue
            if db in diode and da not in diode:
                a, b = b, a
                da, ga, db, gb = db, gb, da, ga
            out.append(Block("diff_pair", (a.name, b.name),
                             {"left": da, "right": db, "tail": sa,
                              "pol": _polarity(a)}))
            used |= {a.name, b.name}
    return out


def mirrors(els) -> list[Block]:
    """A diode-connected transistor (drain = gate) and the same-type transistors that
    share its gate and source. Reference device first, outputs after."""
    out = []
    ts = _transistors(els)
    for ref in ts:
        d, g, s = _dgs(ref)
        if d != g:
            continue
        outs = [e for e in ts if e is not ref and _polarity(e) == _polarity(ref)
                and _dgs(e)[1] == g and _dgs(e)[2] == s and _dgs(e)[0] != g]
        if outs:
            out.append(Block("mirror", (ref.name,) + tuple(e.name for e in outs),
                             {"gate": g, "source": s, "ref": d,
                              "outs": tuple(_dgs(e)[0] for e in outs),
                              "pol": _polarity(ref)}))
    return out


def push_pulls(els, rails=()) -> list[Block]:
    """A complementary pair (one N, one P) whose emitters meet, directly or through one
    small resistor each, and whose collectors go to different nodes: an output stage."""
    out = []
    ts = _transistors(els)
    res = [e for e in els if e.kind == "R"]

    def meet(x):
        """Node an emitter reaches: itself, plus the far end of each resistor on it."""
        far = {x}
        for r in res:
            if x in r.nodes[:2]:
                far.add(r.nodes[1] if r.nodes[0] == x else r.nodes[0])
        return far

    for a in ts:
        if _polarity(a) != "n":
            continue
        for b in ts:
            if _polarity(b) != "p":
                continue
            ca, _, ea = _dgs(a)
            cb, _, eb = _dgs(b)
            if ca == cb or _gnd(ea) or _gnd(eb):
                continue
            common = (meet(ea) & meet(eb)) - set(rails)
            if common:
                o = sorted(common)[0]
                bias = _path(els, _dgs(a)[1], _dgs(b)[1], set(rails))
                out.append(Block("push_pull", (a.name, b.name),
                                 {"out": o, "top": ea, "bottom": eb,
                                  "emitters": [x for x in dict.fromkeys((ea, o, eb))],
                                  "bias": bias}))
    return out


def _path(els, a, b, avoid, hops=3) -> list[str]:
    """Nodes on the shortest chain of two-terminal parts from a to b (both included),
    never through a supply or ground: e.g. the bias diodes between two output bases."""
    if a == b:
        return [a]
    nb: dict[str, set] = {}
    for e in els:
        if e.kind in "RCLD" and len(e.nodes) == 2:
            u, v = e.nodes
            nb.setdefault(u, set()).add(v)
            nb.setdefault(v, set()).add(u)
    prev, frontier = {a: None}, [a]
    for _ in range(hops):
        nxt = []
        for u in frontier:
            for v in sorted(nb.get(u, ())):
                if v in prev or _gnd(v) or (v in avoid and v != b):
                    continue
                prev[v] = u
                nxt.append(v)
        frontier = nxt
        if b in prev:
            out, x = [], b
            while x is not None:
                out.append(x)
                x = prev[x]
            return out[::-1]
    return []


def transformers(els, directives) -> list[Block]:
    """`K` lines coupling two drawn inductors."""
    out = []
    names = {e.name.lower(): e for e in els if e.kind == "L"}
    for d in directives:
        tok = d.split()
        if not tok or not re.match(r"k", tok[0], re.I):
            continue
        ls = [names[t.lower()] for t in tok[1:] if t.lower() in names]
        if len(ls) >= 2:
            out.append(Block("transformer", tuple(e.name for e in ls),
                             {"k": tok[0]}))
    return out


def find(els, directives=(), rails=()) -> list[Block]:
    """Every block found, one list. A transistor belongs to at most one diff pair but
    may also sit in a mirror (a mirror's output is often a diff pair's load). `rails`
    are supply nodes: two emitters both reaching a supply are not an output stage."""
    return (diff_pairs(els, rails) + mirrors(els) + push_pulls(els, rails)
            + transformers(els, directives))
