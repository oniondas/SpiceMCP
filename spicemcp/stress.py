"""Part stress: how hard each part is worked in the circuit, and the rating to buy.

A design that meets its limits can still burn a resistor or punch through a capacitor.
This measures, in the circuit's transient decks (or its DC sweeps when it has none),
over the second half of each transient (steady state: start-up inrush depends on a
soft-start the templates do not model), with the load resistor and capacitor left out
(they are the test bench, not parts on the board):

  R  power it dissipates                 buy at least 2x (50 % derating)
  C  peak voltage across it              buy at least 1.5x, next standard rating
  L  peak and RMS current                saturation current at least 1.3x peak
  D  peak reverse voltage, avg and peak current   1.25x voltage, 1.5x average current
  M  peak |Vds|, |Vgs|, RMS and peak current, power   1.25x Vds, 1.5x RMS current
  Q  peak |Vce|, peak collector current  1.25x, 1.5x

Only parts at the top level are checked: the transistors inside a built-in op-amp
block are the model, not parts anyone buys. AC-only circuits (a filter or amplifier
checked by its frequency response) have no operating waveform to measure, so they are
reported as not checked rather than passed. A deck with a `* stress: skip` comment
line (a surge or fault test) is left out.
"""
from __future__ import annotations

import math
import re
from pathlib import Path

from . import ir, sim

R_W = (0.063, 0.1, 0.125, 0.25, 0.5, 0.75, 1, 2, 3, 5, 10, 25, 50)
C_V = (4, 6.3, 10, 16, 25, 35, 50, 63, 100, 160, 200, 250, 400, 450, 630, 1000)
D_V = (20, 30, 40, 60, 100, 150, 200, 400, 600, 800, 1000)
M_V = (12, 20, 30, 40, 60, 80, 100, 150, 200, 250, 400, 600, 800)
DERATE = {"r_power": 2.0, "c_volt": 1.5, "l_sat": 1.3, "volt": 1.25, "amp": 1.5}
SKIP = {"lbrk", "cinj"}                           # loop-break test fixtures
# The load is the test bench, not a part on the board: RL, CL, Rload, Cload, RL2...
_LOAD = re.compile(r"^[rc](l|load)\d*$", re.I)

_ANALYSIS = re.compile(r"^\s*\.(tran|dc)\b", re.I | re.M)
# A deck that tests a fault (a surge, a short) says so: its peaks are pulse ratings,
# not continuous ones, and it is left out of the stress check.
_SKIP_DECK = re.compile(r"^\*\s*stress:\s*skip", re.I | re.M)


def _up(x: float, table) -> float:
    for v in table:
        if v >= x * 0.999:
            return v
    return float(f"{x:.2g}")


def _a(x: float) -> str:
    if x < 1e-3:                        # leakage only: any part of the kind will do
        return "any current"
    return f"{x:.2g} A" if x >= 0.1 else f"{x * 1000:.2g} mA"


def _v(a: str, b: str) -> str:
    return f"V({a})" if b == "0" else f"V({a},{b})"


def parts(netlist: str) -> list[tuple[str, list[str], float | None]]:
    """Top-level R, C, L, D, M, Q: (name, nodes, value)."""
    out, depth = [], 0
    for ln in netlist.splitlines():
        t = ln.split()
        if not t or t[0].startswith(("*", ";", "+")):
            continue
        low = t[0].lower()
        if low == ".subckt":
            depth += 1
        elif low == ".ends":
            depth = max(0, depth - 1)
        elif depth == 0 and low[:1] in "rcldmq" and low not in SKIP and \
                not _LOAD.match(low):
            n = {"r": 2, "c": 2, "l": 2, "d": 2, "m": 4, "q": 3}[low[0]]
            if len(t) > n:
                val = ir.parse_value(t[n + 1]) if low[0] in "rcl" and len(t) > n + 1 else None
                out.append((t[0], t[1:n + 1], val))
    return out


def _meas(kind: str, name: str, nodes: list[str], tran: bool) -> list[tuple[str, str]]:
    """(metric, .meas body) for one part."""
    k, p = kind, name.lower()
    a, b = nodes[0], nodes[1]
    avg = "AVG" if tran else "MAX"
    if k == "r":
        return [(f"st_{p}_vsq", f"{avg} {_v(a, b)}*{_v(a, b)}")]
    if k == "c":
        return [(f"st_{p}_vmax", f"MAX {_v(a, b)}"), (f"st_{p}_vmin", f"MIN {_v(a, b)}")]
    if k == "l":
        out = [(f"st_{p}_imax", f"MAX I({name})"), (f"st_{p}_imin", f"MIN I({name})")]
        return out + ([(f"st_{p}_irms", f"RMS I({name})")] if tran else [])
    if k == "d":
        return [(f"st_{p}_vr", f"MAX {_v(b, a)}"), (f"st_{p}_ipk", f"MAX I({name})"),
                (f"st_{p}_iavg", f"{avg} I({name})")]
    if k == "m":
        d, g, s = nodes[0], nodes[1], nodes[2]
        return [(f"st_{p}_vdsmax", f"MAX {_v(d, s)}"), (f"st_{p}_vdsmin", f"MIN {_v(d, s)}"),
                (f"st_{p}_vgsmax", f"MAX {_v(g, s)}"), (f"st_{p}_vgsmin", f"MIN {_v(g, s)}"),
                (f"st_{p}_imax", f"MAX Id({name})"), (f"st_{p}_imin", f"MIN Id({name})"),
                (f"st_{p}_pavg", f"{avg} {_v(d, s)}*Id({name})")] + (
                    [(f"st_{p}_irms", f"RMS Id({name})")] if tran else [])
    if k == "q":
        c, _b, e = nodes
        return [(f"st_{p}_vcemax", f"MAX {_v(c, e)}"), (f"st_{p}_vcemin", f"MIN {_v(c, e)}"),
                (f"st_{p}_imax", f"MAX Ic({name})"), (f"st_{p}_imin", f"MIN Ic({name})")]
    return []


def _tran_stop(deck: str) -> float | None:
    """The stop time of `.tran [tstep] tstop ...` (LTspice also accepts `.tran tstop`)."""
    m = re.search(r"^\s*\.tran\s+(.*)$", deck, re.I | re.M)
    if not m:
        return None
    nums = [ir.parse_value(t) for t in m.group(1).split() if "=" not in t]
    nums = [x for x in nums if x is not None]
    return nums[1] if len(nums) >= 2 else (nums[0] if nums else None)


def instrument(deck: str) -> str | None:
    """The deck with a stress .meas per part, or None when it has no tran/dc analysis."""
    m = _ANALYSIS.search(deck)
    if not m or _SKIP_DECK.search(deck):
        return None
    kind = m.group(1).upper()
    window = ""
    if kind == "TRAN":                  # steady state: the second half of the run
        stop = _tran_stop(deck)
        if stop:
            window = f" FROM {stop / 2:g} TO {stop:g}"
    lines = [f".meas {kind} {met} {body}{window}"
             for name, nodes, _ in parts(deck)
             for met, body in _meas(name[0].lower(), name, nodes, kind == "TRAN")]
    if not lines:
        return None
    body = [ln for ln in deck.splitlines() if not ln.strip().lower().startswith(".meas")]
    end = max(i for i, ln in enumerate(body) if ln.strip().lower() == ".end")
    return "\n".join(body[:end] + lines + body[end:]) + "\n"


def _worst(vals: list[dict], key: str, fn=max):
    xs = [v[key] for v in vals if key in v and math.isfinite(v[key])]
    return fn(xs) if xs else None


def check(netlist: str, sim_config: dict, workdir: Path) -> dict:
    """Simulate each tran (else dc) deck with stress probes; return per-part stress
    and the rating to buy. Never raises for a failed probe: it says what is missing."""
    decks = sim.decks(netlist)
    tran = [d for d in decks if re.search(r"^\s*\.tran\b", d, re.I | re.M)]
    use = tran or [d for d in decks if re.search(r"^\s*\.dc\b", d, re.I | re.M)]
    probed = [x for x in (instrument(d) for d in use) if x]
    if not probed:
        return {"checked": False,
                "why": "no transient or DC-sweep deck: there is no operating waveform to "
                       "measure (AC-only circuits run at signal level)"}
    cfg = {**sim_config, "require_meas": False}
    res = sim.parallel_map(
        lambda job: sim.simulate(job[1], {}, cfg, workdir, f"stress{job[0]}"),
        list(enumerate(probed)))
    got = [{k.lower(): v for k, v in r.metrics.items()} for r in res if r.status == "ok"]
    values = {n.lower(): v for n, _, v in parts(netlist)}
    out: dict = {}
    for name, nodes, _ in parts(probed[0]):
        p, k = name.lower(), name[0].lower()
        m = {key[len(f"st_{p}_"):]: v for g in got for key, v in g.items()
             if key.startswith(f"st_{p}_")}
        if not m:
            continue
        mx = lambda a, b: max(abs(_worst(got, f"st_{p}_{a}") or 0),
                              abs(_worst(got, f"st_{p}_{b}", min) or 0))
        e: dict = {}
        if k == "r" and values.get(p):
            w = (_worst(got, f"st_{p}_vsq") or 0) / values[p]
            e = {"dissipates_w": w, "buy": f"{_up(w * DERATE['r_power'], R_W):g} W"}
        elif k == "c":
            v = mx("vmax", "vmin")
            e = {"peak_v": v, "buy": f"{_up(v * DERATE['c_volt'], C_V):g} V"}
        elif k == "l":
            i = mx("imax", "imin")
            e = {"peak_a": i, "buy": f"Isat {_a(i * DERATE['l_sat'])}"}
            if "irms" in m:
                e["rms_a"] = abs(_worst(got, f"st_{p}_irms") or 0)
                e["buy"] += f", Irms {_a(e['rms_a'])}"
        elif k == "d":
            vr, ip = max(0.0, _worst(got, f"st_{p}_vr") or 0), _worst(got, f"st_{p}_ipk") or 0
            ia = abs(_worst(got, f"st_{p}_iavg") or 0)
            e = {"reverse_v": vr, "avg_a": ia, "peak_a": ip,
                 "buy": f"{_up(vr * DERATE['volt'], D_V):g} V, {_a(ia * DERATE['amp'])}"}
        elif k == "m":
            vds, vgs, i = mx("vdsmax", "vdsmin"), mx("vgsmax", "vgsmin"), mx("imax", "imin")
            pw = abs(_worst(got, f"st_{p}_pavg") or 0)
            # A datasheet's Id is continuous (thermal, so RMS); its pulsed Idm covers the
            # nanosecond spikes at each edge, which are a few times the load current.
            irms = abs(_worst(got, f"st_{p}_irms") or 0) or i
            e = {"vds_v": vds, "vgs_v": vgs, "rms_a": irms, "peak_a": i, "dissipates_w": pw,
                 "buy": f"{_up(vds * DERATE['volt'], M_V):g} V, Id {_a(irms * DERATE['amp'])}"
                        + (f", Idm {_a(i)}" if i > 2 * irms else "")
                        + (", Vgs 20 V" if vgs > 10 else "")}
        elif k == "q":
            vce, i = mx("vcemax", "vcemin"), mx("imax", "imin")
            e = {"vce_v": vce, "peak_a": i,
                 "buy": f"{_up(vce * DERATE['volt'], M_V):g} V, {_a(i * DERATE['amp'])}"}
        if e:
            out[name] = {k2: (round(v, 6) if isinstance(v, float) else v)
                         for k2, v in e.items()}
    kinds = "transient (second half: steady state)" if tran else "DC sweep"
    missed = sorted(n for n, _, _ in parts(probed[0]) if n not in out)
    return {"checked": True, "from": f"{len(got)} of {len(probed)} {kinds} decks",
            "parts": out, **({"not_measured": missed} if missed else {})}


REFDES = {"switch": "M1", "diode": "D1"}


def against_ratings(st: dict, chosen: dict) -> dict:
    """Each chosen real part's measured stress (with derating) against its datasheet
    ratings from the model card: {"D1 1N5819": "ok: ..." | "too small: ..."}."""
    out = {}
    for slot, (name, rat) in chosen.items():
        ref = REFDES.get(slot)
        p = (st.get("parts") or {}).get(ref) if ref else None
        if not p or not rat:
            continue
        checks = []
        if slot == "diode":
            checks = [("reverse voltage", p["reverse_v"] * DERATE["volt"], rat.get("vpk"), "V"),
                      ("average current", p["avg_a"] * DERATE["amp"], rat.get("iave"), "A")]
        elif slot == "switch":
            checks = [("Vds", p["vds_v"] * DERATE["volt"], rat.get("vds"), "V")]
        bad, good = [], []
        for what, need, have, u in checks:
            if not isinstance(have, (int, float)):
                continue
            (bad if need > abs(have) else good).append(
                f"{what} needs {need:.3g} {u}, rated {abs(have):g} {u}")
        if bad or good:
            out[f"{ref} {name}"] = ("too small: " + "; ".join(bad)) if bad else \
                ("ok: " + "; ".join(good))
        if slot == "switch" and isinstance(rat.get("ron"), (int, float)):
            loss = p.get("rms_a", p["peak_a"]) ** 2 * rat["ron"]
            out[f"{ref} {name}"] += f"; conduction loss {loss:.2g} W at Ron {rat['ron']:g} Ω"
    return out
