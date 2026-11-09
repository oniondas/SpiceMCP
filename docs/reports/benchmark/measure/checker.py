"""Common checker: every arm's final netlist is measured by the same decks, at nominal
and over the same random boards (same seed), in ngspice. Nothing here comes from the
SpiceMCP code base except the E-series tables, so no arm is graded by its own tools.

    python checker.py <arm> <circuit> <netlist.cir> [boards]   -> JSON on stdout

Boards: every top-level R (not the RL/CL load the spec fixes) drawn uniformly within 1 %, C within 5 %, L within 20 %, the
supply within 5 %; each board is checked at -40, 25 and 85 C and passes only if all
three pass."""
from __future__ import annotations

import json
import math
import random
import re
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

HERE = Path(__file__).resolve().parent
LIB = (HERE.parent / "baselines" / "parts.lib").resolve()
TOL = {"R": 0.01, "C": 0.05, "L": 0.20}
SUPPLY_TOL = 0.05
TEMPS = (-40.0, 25.0, 85.0)
E12 = [1.0, 1.2, 1.5, 1.8, 2.2, 2.7, 3.3, 3.9, 4.7, 5.6, 6.8, 8.2]
E96 = [1.00, 1.02, 1.05, 1.07, 1.10, 1.13, 1.15, 1.18, 1.21, 1.24, 1.27, 1.30, 1.33,
       1.37, 1.40, 1.43, 1.47, 1.50, 1.54, 1.58, 1.62, 1.65, 1.69, 1.74, 1.78, 1.82,
       1.87, 1.91, 1.96, 2.00, 2.05, 2.10, 2.15, 2.21, 2.26, 2.32, 2.37, 2.43, 2.49,
       2.55, 2.61, 2.67, 2.74, 2.80, 2.87, 2.94, 3.01, 3.09, 3.16, 3.24, 3.32, 3.40,
       3.48, 3.57, 3.65, 3.74, 3.83, 3.92, 4.02, 4.12, 4.22, 4.32, 4.42, 4.53, 4.64,
       4.75, 4.87, 4.99, 5.11, 5.23, 5.36, 5.49, 5.62, 5.76, 5.90, 6.04, 6.19, 6.34,
       6.49, 6.65, 6.81, 6.98, 7.15, 7.32, 7.50, 7.68, 7.87, 8.06, 8.25, 8.45, 8.66,
       8.87, 9.09, 9.31, 9.53, 9.76]
SERIES = {"R": E96, "C": E12, "L": E12}
SUF = {"t": 1e12, "g": 1e9, "meg": 1e6, "k": 1e3, "m": 1e-3, "u": 1e-6, "µ": 1e-6,
       "n": 1e-9, "p": 1e-12, "f": 1e-15}

SPEC = {
    "rc": {"fc": (980.0, 1020.0)},
    "amp": {"gain_db": (19.9, 20.1), "bw": (100e3, None), "overshoot_pct": (None, 25.0)},
    "buck": {"vout": (4.9, 5.1), "ripple_mv": (None, 50.0)},
}
SOURCE = {"rc": "V1", "amp": "V1", "buck": "Vin"}


def value(tok: str) -> float | None:
    m = re.fullmatch(r"([-+]?[\d.]+(?:e[-+]?\d+)?)(meg|[tgkmunpfµ])?\w*", tok.lower())
    if not m:
        return None
    return float(m.group(1)) * SUF.get(m.group(2) or "", 1.0)


def in_series(v: float, kind: str) -> bool:
    d = 10 ** math.floor(math.log10(v) + 1e-9)
    m = v / d
    return any(abs(m - s) / s < 0.005 for s in SERIES[kind] + [10.0])


def clean(net: str) -> list[str]:
    """Components and models only: first deck, no analyses, sources we replace removed,
    parts.lib pointed at the shared copy, continuation lines joined."""
    net = re.split(r"^\s*\.end\s*$", net, flags=re.M | re.I)[0]
    first = net.lstrip().splitlines()[0] if net.strip() else ""
    if first and first[0] not in ".*":           # SPICE: the first line is the title
        net = net.lstrip().split("\n", 1)[1] if "\n" in net.lstrip() else ""
    out, skip = [], False
    for ln in net.splitlines():
        s = ln.strip()
        low = s.lower()
        if low.startswith(".control"):
            skip = True
        if skip:
            skip = not low.startswith(".endc")
            continue
        if s.startswith("+") and out:
            out[-1] += " " + s[1:]
            continue
        if not s or s.startswith("*") or low.startswith((
                ".ac", ".tran", ".meas", ".op", ".dc", ".noise", ".temp", ".save",
                ".print", ".plot", ".probe", ".step", ".four", ".title")):
            continue
        if low.startswith(".include") or low.startswith(".inc ") or low.startswith(".lib"):
            if "parts.lib" in low:
                out.append(f".include {LIB}")
            continue
        out.append(s)
    return out


def components(lines: list[str]) -> list[tuple[int, str, float]]:
    """Top-level R/C/L lines (not inside a .subckt): (line index, kind, value)."""
    res, depth = [], 0
    for i, ln in enumerate(lines):
        low = ln.lower()
        if low.startswith(".subckt"):
            depth += 1
        elif low.startswith(".ends"):
            depth -= 1
        elif depth == 0 and ln[0].upper() in "RCL" and ln.split()[0].upper() not in ("RL", "CL"):
            tok = ln.split()
            v = value(tok[3]) if len(tok) > 3 else None
            if v is not None and v > 0:
                res.append((i, ln[0].upper(), v))
    return res


def with_values(lines, comps, scale):
    out = list(lines)
    for (i, k, v), s in zip(comps, scale):
        tok = out[i].split()
        tok[3] = f"{v * s:.6g}"
        out[i] = " ".join(tok)
    return out


def set_supply(lines, circuit, s):
    """Scale the supplies: +-5 V rails for the amp, the input for the buck."""
    out = []
    for ln in lines:
        tok = ln.split()
        name = tok[0].lower()
        if circuit == "amp" and name in ("vcc", "vee"):
            ln = f"{tok[0]} {tok[1]} {tok[2]} {5.0 * s if name == 'vcc' else -5.0 * s:.6g}"
            if tok[1].lower() != ("vcc" if name == "vcc" else "vee"):
                ln = " ".join(tok)               # unknown orientation: leave as written
        out.append(ln)
    return out


def deck(lines, circuit, vin=12.0, temp=25.0, step="2n") -> str:
    src = SOURCE[circuit].lower()
    body = [ln for ln in lines if ln.split()[0].lower() != src]
    old = [ln.split() for ln in lines if ln.split()[0].lower() == src]
    node = old[0][1] if old else ("vin" if circuit == "buck" else "in")  # keep the arm's node
    if circuit == "rc":
        tail = [f"V1 {node} 0 DC 0 AC 1", ".control", "ac dec 400 10 100k",
                "meas ac fc when vm(out)=0.70711 fall=1"]
    elif circuit == "amp":
        tail = [f"V1 {node} 0 DC 0 AC 1 PULSE(0 0.01 2u 10n 10n 40u 100u)", ".control",
                "ac dec 100 10 100meg",
                "meas ac g1k find vm(out) at=1k",
                "let gain_db = 20*log10(g1k)", "print gain_db",
                "let thr = 0.7071*g1k",
                "meas ac bw when vm(out)=$&thr fall=1",
                "tran 5n 30u",
                "meas tran v0 avg v(out) from=0.5u to=1.9u",
                "meas tran vfin avg v(out) from=25u to=29u",
                "meas tran vpk max v(out) from=2u to=25u",
                "let overshoot_pct = 100*(vpk-vfin)/(vfin-v0)", "print overshoot_pct"]
    else:
        tail = [f"Vin {node} 0 PWL(0 0 20u {vin:.6g})", ".control", f"tran {step} 1.5m 0 {step}",
                "meas tran vout avg v(out) from=1.3m to=1.5m",
                "meas tran vmax max v(out) from=1.3m to=1.5m",
                "meas tran vmin min v(out) from=1.3m to=1.5m",
                "let ripple_mv = (vmax-vmin)*1000", "print ripple_mv"]
    tail += ["quit", ".endc"]
    return "\n".join(["* benchmark check", f".temp {temp:g}",
                      ".options reltol=1e-3 itl4=200"] + body + tail + [".end", ""])


def run(text: str, timeout=600) -> dict:
    with tempfile.TemporaryDirectory() as d:
        f = Path(d) / "c.cir"
        f.write_text(text)
        try:
            p = subprocess.run(["ngspice", "-b", str(f)], capture_output=True, text=True,
                               timeout=timeout, cwd=d)
            log = p.stdout + p.stderr
        except subprocess.TimeoutExpired:
            return {"error": "timeout"}
    m = {}
    for k, v in re.findall(r"^\s*(\w+)\s*=\s*([-+\d.eE]+)", log, re.M):
        k = k.lower()
        if k in ("fc", "gain_db", "bw", "overshoot_pct", "vout", "ripple_mv"):
            try:
                m[k] = float(v)
            except ValueError:
                pass
    if "simulation(s) aborted" in log or "Timestep too small" in log:
        m = {"error": "simulator stopped: timestep too small"}
    if not m:
        m["error"] = (re.findall(r"(?i)^.*error.*$", log, re.M) or ["no result"])[0][:200]
    return m


STEPS = ("2n", "5n", "20n")      # buck: finest time step that runs to the end


def run_deck(lines, circuit, vin=12.0, temp=25.0) -> dict:
    """The buck needs a fine time step (2 ns) for an honest ripple number; a design the
    simulator cannot finish at 2 ns is retried coarser, and the step used is recorded."""
    if circuit != "buck":
        return run(deck(lines, circuit, vin, temp))
    for st in STEPS:
        m = run(deck(lines, circuit, vin, temp, st))
        if "error" not in m:
            return {**m, "step": st}
    return {**m, "step": "none finished"}


def passes(m: dict, circuit: str) -> bool:
    for k, (lo, hi) in SPEC[circuit].items():
        v = m.get(k)
        if v is None or (lo is not None and v < lo) or (hi is not None and v > hi):
            return False
    return True


def check(circuit: str, net: str, boards: int = 30, seed: int = 12) -> dict:
    lines = clean(net)
    comps = components(lines)
    buy = {f"{lines[i].split()[0]}": in_series(v, k) for i, k, v in comps}
    vins = (11.4, 12.0, 12.6) if circuit == "buck" else (None,)
    nominal = {}
    for v in vins:
        m = run_deck(lines, circuit, v or 12.0)
        nominal[f"{v:g} V" if v else "nominal"] = {**m, "pass": passes(m, circuit)}
    rng = random.Random(seed)
    jobs = []
    for b in range(boards):
        scale = [1 + rng.uniform(-TOL[k], TOL[k]) for _, k, _ in comps]
        s = 1 + rng.uniform(-SUPPLY_TOL, SUPPLY_TOL)
        bl = set_supply(with_values(lines, comps, scale), circuit, s)
        for t in TEMPS:
            jobs.append((b, t, bl, 12.0 * s))
    with ThreadPoolExecutor(4) as ex:
        res = list(ex.map(lambda j: (j[0], j[1], run_deck(j[2], circuit, j[3], j[1])), jobs))
    rows = {}
    for b, t, m in res:
        rows.setdefault(b, {})[f"{t:g}"] = {**m, "pass": passes(m, circuit)}
    ok = sum(all(r["pass"] for r in temps.values()) for temps in rows.values())
    return {"circuit": circuit, "parts": {lines[i].split()[0]: v for i, _, v in comps},
            "all_buyable": all(buy.values()), "not_buyable": [k for k, v in buy.items() if not v],
            "nominal": nominal, "nominal_pass": all(x["pass"] for x in nominal.values()),
            "boards": boards, "boards_pass": ok, "yield_pct": round(100 * ok / boards, 1) if boards else None,
            "board_rows": rows}


if __name__ == "__main__":
    arm, circuit, path = sys.argv[1:4]
    n = int(sys.argv[4]) if len(sys.argv) > 4 else 30
    out = check(circuit, Path(path).read_text(), n)
    out["arm"] = arm
    print(json.dumps(out))
