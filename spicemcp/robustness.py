"""DC bias audit, PVT corners and Monte Carlo yield.

The question all three answer is the one a nominal simulation cannot: *is this design
still a design when the parts, the temperature and the supply are not the datasheet
values?* A Sallen-Key that hits its corner frequency at 27 C with ideal parts and misses
it on every real board is a wrong answer that a nominal run reports as a win.

Two hard limits of LTspice 26.0.2 shape this module, both established by running it:

1. **There is no way to ask LTspice for a device's operating point.** `.save Vgs(M1)`,
   `.save @M1[id]` and `.meas OP vt PARAM @M1[vth]` are all *syntax errors* -- the
   ngspice `@device[param]` vector does not exist here -- and batch mode writes no
   operating-point table to the `.log`. A `.op` `.raw` carries node voltages and terminal
   currents, nothing else (pinned by `test_operating_point_exposes_node_voltages...`).
   So `audit_dc_operating_points` derives Vds/Vgs from node voltages plus each element
   line's terminal order, and reads Vth off the `.model` card. Where that cannot be done
   -- a BSIM model, an encrypted vendor library, a device inside a subcircuit -- the
   verdict is `unknown` and says why. It is never a guess: "saturated" is a claim someone
   will tape out on.

2. **Process corners need models nobody can synthesise.** Temperature and supply are
   sweepable from the netlist, so TT/SS/FF differ only in temperature and Vdd unless the
   caller supplies real corner libraries via `corner_lib`. For an all-passive filter the
   process axis has no meaning at all. `evaluate_pvt_corners` reports
   `process_applied: false` rather than implying a corner it did not run.

Monte Carlo is one LTspice launch, not N: `mc(nom,tol)` redraws per `.step` run, measured
uniform over +-tol (400 runs: sd = 0.572% = 1/sqrt(3) for tol=1%).
"""

from __future__ import annotations

import math
import re
import statistics
from pathlib import Path

from . import ir, raw, sim

# ponytail: no device-graph object model. These four element letters cover the bias
# question; add J/Z the day someone audits a JFET.
MOS_LETTERS = ("m",)
BJT_LETTERS = ("q",)

# `Mname d g s b model ...` and `Qname c b e [subs] model ...`. LTspice allows a 4th
# BJT node (substrate), which is why the BJT reader takes the model as "first token that
# names a .model", not "the 5th field".
_ELEM = re.compile(r"^([a-zA-Z])(\S*)\s+(.*)$")
_MODEL = re.compile(r"^\s*\.model\s+(\S+)\s+(\w+)\s*\((.*?)\)?\s*$", re.I | re.M)
_MODEL_PARAM = re.compile(r"(\w+)\s*=\s*([^\s()]+)")

DEFAULT_CORNERS = [
    # name, process, temp_c, vdd_scale
    ("TT", "TT", 27.0, 1.00),
    ("SS", "SS", 125.0, 0.90),
    ("FF", "FF", -40.0, 1.10),
    ("SF", "SF", 125.0, 1.10),
    ("FS", "FS", -40.0, 0.90),
]


class RobustnessError(RuntimeError):
    pass


def _deck(lines: list[str], *what: str) -> str:
    """Assemble a runnable deck, guaranteeing a title line and a single `.end`.

    SPICE *ignores the first line of a deck* -- it is the title, whatever it contains.
    Every rewriter here filters directives out, so without this the new first line gets
    eaten: a netlist starting with `.tran 0 1m` loses `V1` to the title slot and
    simulates a circuit with no source, which LTspice may run without complaint. When the
    line eaten is a `.model` card the error at least names itself; when it is a component
    it does not.
    """
    body = list(lines)
    first = body[0].strip() if body else ""
    if not first.startswith("*"):
        body.insert(0, "* spicemcp robustness deck")
    return "\n".join(body + list(what) + [".end", ""])


def _log_excerpt(path: str | None, keep: int = 6) -> list[str]:
    """The lines of an LTspice log that say what went wrong.

    Worth the ten lines: LTspice reports a syntax error only in the log, and a caller
    holding "no .raw was produced" cannot act on it while `Node or model name expected`
    names the fix exactly.
    """
    if not path or not Path(path).is_file():
        return []
    text = Path(path).read_text(errors="replace")
    hits = [ln.strip() for ln in text.splitlines()
            if re.search(r"error|expected|fail|ill-formed|cannot|could not|missing|\^\^",
                         ln, re.I)]
    return hits[-keep:]


# ------------------------------------------------------------------ netlist inspection

def _lines(netlist: str) -> list[str]:
    """Element lines only: no comments, no directives, continuations joined."""
    out: list[str] = []
    for ln in netlist.splitlines():
        ln = re.sub(r"\s*;.*$", "", ln).rstrip()
        if not ln or ln.lstrip().startswith("*"):
            continue
        if ln.lstrip().startswith("+") and out:
            out[-1] += " " + ln.lstrip()[1:].strip()
        else:
            out.append(ln.strip())
    return out


def models(netlist: str) -> dict[str, dict]:
    """`.model` cards -> {name: {"type": "nmos", "vto": 0.7, ...}}."""
    out = {}
    for m in _MODEL.finditer(netlist):
        params = {k.lower(): ir.parse_value(v)
                  for k, v in _MODEL_PARAM.findall(m.group(3) or "")}
        out[m.group(1).lower()] = {"type": m.group(2).lower(),
                                   **{k: v for k, v in params.items() if v is not None}}
    return out


def devices(netlist: str) -> list[dict]:
    """Active devices with their terminal->node mapping, from element line order.

    This mapping is the whole reason a DC audit is possible at all: LTspice will not tell
    us Vgs, but it tells us every node voltage, and `M1 d g s b` says which is which.
    """
    out = []
    for ln in _lines(netlist):
        m = _ELEM.match(ln)
        if not m:
            continue
        letter, rest = m.group(1).lower(), m.group(3).split()
        name = m.group(1) + m.group(2)
        if letter in MOS_LETTERS and len(rest) >= 5:
            out.append({"name": name, "kind": "mos", "model": rest[4].lower(),
                        "nodes": {"d": rest[0], "g": rest[1], "s": rest[2],
                                  "b": rest[3]}})
        elif letter in BJT_LETTERS and len(rest) >= 4:
            # 4 nodes = substrate form; the model is the last name-ish token before
            # any key=value pairs.
            named = [t for t in rest if "=" not in t]
            nodes = named[:4] if len(named) >= 5 else named[:3]
            out.append({"name": name, "kind": "bjt", "model": named[len(nodes)].lower(),
                        "nodes": dict(zip(("c", "b", "e", "subs"), nodes))})
    return out


# ---------------------------------------------------------------------------- DC audit

def _vth(model: dict) -> tuple[float | None, str]:
    """Threshold voltage and where it came from.

    `None` is a real answer here. A BSIM card computes Vth from geometry, doping and the
    body effect at runtime and does not carry it as a number -- reporting the `vth0`
    parameter as *the* threshold would be wrong by hundreds of millivolts on a short
    channel, and would be wrong silently.
    """
    for key in ("vto", "vth"):
        if model.get(key) is not None:
            return float(model[key]), key
    if model.get("vth0") is not None:
        return None, "vth0 present but this is a BSIM-style card; Vth is geometry- and " \
                     "bias-dependent and cannot be read off the model"
    return None, "no VTO/VTH on the .model card"


def audit_dc_operating_points(netlist: str, workdir: str | Path, *,
                              sim_config: dict | None = None,
                              margin_v: float = 0.05,
                              stem: str = "dc_audit") -> dict:
    """Check every MOSFET is saturated and every BJT is active, from a `.op` run.

    `margin_v` is overdrive headroom: Vds >= Vgs - Vth + margin. A device sitting exactly
    at the saturation boundary is in saturation on paper and in triode over temperature,
    so 50 mV of margin is the default rather than 0.
    """
    cfg = {**(sim_config or {}), "require_meas": False}
    devs = devices(netlist)
    if not devs:
        return {"status": "ok", "devices": [], "all_in_region": True,
                "note": "no MOSFETs or BJTs in this netlist; nothing to bias"}

    nl = _with_op(netlist)
    res = sim.simulate(nl, {}, cfg, Path(workdir), stem)
    if res.status != "ok" or not res.raw_path:
        return {"status": "failed", "failure_type": res.failure_type,
                "failure_message": res.failure_message or "LTspice produced no .raw",
                "log_errors": _log_excerpt(res.log_path),
                "log_path": res.log_path}

    w = raw.read_raw(res.raw_path)[0]
    mods = models(netlist)
    rows = []
    for d in devs:
        rows.append(_audit_one(d, mods.get(d["model"], {}), w, margin_v))
    bad = [r for r in rows if r["verdict"] == "fail"]
    unknown = [r for r in rows if r["verdict"] == "unknown"]
    return {"status": "ok", "raw_path": res.raw_path, "margin_v": margin_v,
            "devices": rows, "n_devices": len(rows),
            "n_fail": len(bad), "n_unknown": len(unknown),
            "all_in_region": not bad and not unknown,
            "violations": [f"{r['name']}: {r['reason']}" for r in bad],
            "unresolved": [f"{r['name']}: {r['reason']}" for r in unknown]}


def _node_v(w: raw.Wave, node: str) -> float | None:
    if node in ("0", "gnd", "GND"):
        return 0.0
    v = w.get(node)
    return float(v[0]) if v else None


def _audit_one(dev: dict, model: dict, w: raw.Wave, margin: float) -> dict:
    out = {"name": dev["name"], "kind": dev["kind"], "model": dev["model"],
           "nodes": dev["nodes"]}
    v = {t: _node_v(w, n) for t, n in dev["nodes"].items()}
    missing = [t for t, val in v.items() if val is None and t != "subs"]
    if missing or not model:
        out.update(verdict="unknown", region=None,
                   reason=(f"nodes {missing} not in the .op results"
                           if missing else
                           f"no .model card for {dev['model']!r}; it is a library or "
                           f"subcircuit device, so its terminals cannot be resolved here"))
        return out

    if dev["kind"] == "mos":
        p = model["type"].startswith("p")               # PMOS: every sign flips
        sgn = -1.0 if p else 1.0
        vds, vgs = sgn * (v["d"] - v["s"]), sgn * (v["g"] - v["s"])
        vth, src = _vth(model)
        out.update(type=model["type"], vds=raw._r(vds), vgs=raw._r(vgs),
                   vth=raw._r(sgn * vth if vth is not None else None), vth_source=src)
        if p:
            # Every PMOS number here is a magnitude, VTO included -- so `vds >= vgs - vth`
            # reads the same for both polarities. Said out loud because a card with
            # VTO=-0.7 reported back as vth 0.7 otherwise looks like a parsing error.
            out["sign_convention"] = ("p-channel: Vgs, Vds and Vth are reported as "
                                      "magnitudes, so the model's VTO=-0.7 appears as 0.7")
        if vth is None:
            out.update(verdict="unknown", region=None,
                       reason=f"cannot establish Vth: {src}")
            return out
        vov = vgs - abs(vth)
        out["v_overdrive"] = raw._r(vov)
        out["vds_headroom"] = raw._r(vds - vov)
        if vov <= 0:
            out.update(region="cutoff", verdict="fail",
                       reason=f"Vgs-Vth = {vov:.4g} V: off, not amplifying")
        elif vds < vov + margin:
            out.update(region="triode" if vds < vov else "saturated (no margin)",
                       verdict="fail",
                       reason=f"Vds = {vds:.4g} V needs >= Vgs-Vth+margin = "
                              f"{vov + margin:.4g} V")
        else:
            out.update(region="saturated", verdict="pass",
                       reason=f"Vds {vds:.4g} V exceeds Vgs-Vth {vov:.4g} V by "
                              f"{vds - vov:.4g} V")
        if model.get("gamma"):
            out["caveat"] = ("GAMMA is set, so Vth shifts with Vsb; this audit uses the "
                            "zero-bias VTO")
        return out

    # BJT: forward-biased BE, reverse-biased BC.
    p = model["type"].startswith("pnp")
    sgn = -1.0 if p else 1.0
    vbe, vbc = sgn * (v["b"] - v["e"]), sgn * (v["b"] - v["c"])
    on = 0.5                                            # a silicon BE junction conducting
    out.update(type=model["type"], vbe=raw._r(vbe), vbc=raw._r(vbc),
               vce=raw._r(sgn * (v["c"] - v["e"])))
    if vbe < on:
        out.update(region="cutoff", verdict="fail",
                   reason=f"Vbe = {vbe:.4g} V is below {on} V: not conducting")
    elif vbc > -margin:
        # The same `on` threshold decides both junctions. Calling everything above 0 V
        # "saturation" labelled a Vbc of +20 mV -- a BC junction passing picoamps -- as
        # a saturated transistor, when the honest report is "active, but with no reverse
        # bias to spare over temperature".
        out.update(region="saturation" if vbc >= on else "active (no margin)",
                   verdict="fail",
                   reason=f"Vbc = {vbc:.4g} V: collector junction is not reverse "
                          f"biased by at least {margin} V")
    else:
        out.update(region="active", verdict="pass",
                   reason=f"Vbe {vbe:.4g} V on, Vbc {vbc:.4g} V reverse")
    return out


def _with_op(netlist: str) -> str:
    """Force a `.op` analysis, replacing whatever analysis the netlist asked for.

    A `.tran` left in place would run the whole transient and hand back a `.raw` whose
    first point is the *initial* solution, not the bias point.
    """
    keep = [ln for ln in netlist.splitlines()
            if not re.match(r"\s*\.(tran|ac|dc|noise|tf|op|step|end)\b", ln, re.I)]
    return _deck(keep, ".op")


# ------------------------------------------------------------------------ PVT corners

def evaluate_pvt_corners(netlist: str, workdir: str | Path, *,
                         metrics: list[str] | None = None,
                         corners: list | None = None,
                         supply: str | None = None,
                         corner_lib: dict | None = None,
                         sim_config: dict | None = None,
                         spec: dict | None = None) -> dict:
    """Run the netlist at each PVT corner and report the worst case per metric.

    `metrics` names the `.meas` results to track (default: all of them). `supply` is the
    voltage source to scale for the V axis -- without it the supply axis is not run, and
    said so, because scaling the wrong source (a signal generator, say) would produce a
    confident number about the wrong thing. `corner_lib` maps a process corner name to a
    `.lib`/`.include` line; absent, the process axis is reported as not applied.
    """
    cfg = dict(sim_config or {})
    corners = corners or DEFAULT_CORNERS
    lib = {k.upper(): v for k, v in (corner_lib or {}).items()}
    rows, failures, unscaled = [], [], []
    for name, proc, temp, vscale in corners:
        nl, scaled = _corner_netlist(netlist, temp, supply, vscale, lib.get(proc.upper()))
        res = sim.simulate(nl, {}, cfg, Path(workdir), f"corner_{name}")
        row = {"corner": name, "process": proc, "temp_c": temp, "vdd_scale": vscale,
               "status": res.status,
               "process_applied": proc.upper() in lib,
               "supply_applied": bool(supply) and scaled}
        if supply and not scaled:
            unscaled.append(name)
        if res.status == "ok":
            row["metrics"] = {k.lower(): v for k, v in res.metrics.items()}
        else:
            row["failure_message"] = res.failure_message
            row["log_errors"] = _log_excerpt(res.log_path)
            failures.append(name)
        rows.append(row)

    ok = [r for r in rows if r["status"] == "ok"]
    worst = _worst_case(ok, metrics)
    checks = _spec_checks(spec, worst, ok)
    return {"status": "ok" if ok else "failed",
            "corners_run": len(rows), "corners_ok": len(ok), "failed_corners": failures,
            "process_applied": bool(lib),
            "notes": _corner_notes(bool(lib), supply, netlist, unscaled),
            "per_corner": rows, "worst_case": worst,
            "spec_checks": checks,
            "pass": bool(ok) and not failures and all(c["passed"] for c in checks)}


def _worst_case(ok_rows: list[dict], metrics: list[str] | None = None) -> dict:
    """Per-metric min/max/deviation across corners, and which corner produced each.

    Split out from `evaluate_pvt_corners` so it can be checked without launching LTspice:
    it is the part that turns five simulations into the one sentence a review reads, and
    a wrong corner name here is invisible in the numbers.
    """
    names = metrics or sorted({k for r in ok_rows for k in r["metrics"]})
    nominal = next((r for r in ok_rows if r["corner"] == "TT"),
                   ok_rows[0] if ok_rows else None)
    worst = {}
    for k in names:
        vals = [(r["metrics"][k], r["corner"]) for r in ok_rows if k in r["metrics"]]
        if not vals:
            continue
        nom = nominal["metrics"].get(k) if nominal else None
        # `key=`, not bare min/max: on a tuple those compare the corner *name* when the
        # values tie, so an all-passive netlist (where SS and SF are the same run) blamed
        # SF for SS's number purely because "SF" sorts first. Keyed min is stable, so a
        # tie reports the corner that ran first.
        lo = min(vals, key=lambda v: v[0])
        hi = max(vals, key=lambda v: v[0])
        entry = {"nominal": nom, "min": lo[0], "min_corner": lo[1],
                 "max": hi[0], "max_corner": hi[1],
                 "spread_pct": raw._r(abs(hi[0] - lo[0]) / abs(nom) * 100)
                 if nom else None}
        if nom:
            dev = max(vals, key=lambda v: abs(v[0] - nom))
            entry["worst_deviation_pct"] = raw._r((dev[0] - nom) / abs(nom) * 100)
            entry["worst_corner"] = dev[1]
        worst[k] = entry
    return worst


def _corner_netlist(netlist: str, temp: float, supply: str | None, scale: float,
                    libline: str | None) -> tuple[str, bool]:
    """Set `.temp`, scale the named supply, pull in a corner library -> (deck, scaled).

    `scaled` is reported rather than assumed: a caller can name a supply that is not in
    the netlist, or name a PWL/behavioural source that has no DC value to scale, and both
    must read as "the V axis did not run" instead of as a corner result.
    """
    out, seen_temp, scaled = [], False, False
    for ln in netlist.splitlines():
        if re.match(r"\s*\.temp\b", ln, re.I):
            out.append(f".temp {temp:g}")
            seen_temp = True
            continue
        if supply and scale != 1.0:
            ln, hit = _scale_source(ln, supply, scale)
            scaled = scaled or hit
        if re.match(r"\s*\.end\s*$", ln, re.I):
            continue
        out.append(ln)
    head = ([libline] if libline else []) + ([] if seen_temp else [f".temp {temp:g}"])
    return _deck(out, *head), scaled or scale == 1.0


_DC_VAL = re.compile(r"^(\s*([vV])(\S*)\s+\S+\s+\S+\s+)(?:(dc)\s+)?([^\s;]+)(.*)$", re.I)


def _scale_source(line: str, name: str, scale: float) -> tuple[str, bool]:
    """Multiply a DC voltage source's value -> (line, did_it_change).

    `re.I` on `_DC_VAL` is not cosmetic: without it `Vdd vdd 0 DC 5` fails the optional
    `dc` group, so `DC` itself lands in the value slot, `parse_value` returns None and the
    line comes back untouched -- while the corner report said `supply_applied: True`. Every
    corner then ran at the same supply and the spread was reported as if it had not.
    """
    m = _DC_VAL.match(line)
    if not m or (m.group(2) + m.group(3)).lower() != name.lower():
        return line, False
    val = ir.parse_value(m.group(5))
    if val is None:                                     # AC/PULSE/behavioural: leave alone
        return line, False
    dc = f"{m.group(4)} " if m.group(4) else ""
    return f"{m.group(1)}{dc}{ir.fmt_value(val * scale)}{m.group(6)}", True


def _corner_notes(has_lib: bool, supply: str | None, netlist: str,
                  unscaled: list[str] | None = None) -> list[str]:
    notes = []
    if not has_lib:
        notes.append("process corners NOT applied: SS/FF/SF/FS differ from TT only in "
                     "temperature and supply. Real process corners need vendor corner "
                     "models -- pass corner_lib={'SS': '.lib ss.lib', ...}.")
    if not supply:
        notes.append("supply axis NOT applied: pass supply='V1' to scale a DC source. "
                     "vdd_scale is reported but was not used.")
    elif unscaled:
        notes.append(f"supply {supply!r} was NOT scaled in corners {unscaled}: no line "
                     f"matched it with a numeric DC value. Those corners ran at the "
                     f"nominal supply, so their spread understates the real one.")
    if not devices(netlist):
        notes.append("this netlist has no active devices, so the process axis has no "
                     "meaning for it; temperature is the only real axis here.")
    return notes


def _spec_checks(spec: dict | None, worst: dict, ok_rows: list) -> list[dict]:
    """Check every corner against `{metric: (lo, hi)}`, not just the nominal one.

    A spec that holds at TT and fails at SS is a failing design, so the check is on the
    worst case rather than on the mean.
    """
    out = []
    for k, bounds in (spec or {}).items():
        lo, hi = bounds if isinstance(bounds, (list, tuple)) else (None, bounds)
        k = k.lower()
        if k not in worst:
            out.append({"metric": k, "passed": False,
                        "reason": "metric not measured in any corner"})
            continue
        bad = [r["corner"] for r in ok_rows
               if k in r["metrics"] and not (
                   (lo is None or r["metrics"][k] >= lo)
                   and (hi is None or r["metrics"][k] <= hi))]
        out.append({"metric": k, "limits": [lo, hi], "passed": not bad,
                    "failing_corners": bad,
                    "min": worst[k]["min"], "max": worst[k]["max"]})
    return out


# ------------------------------------------------------------------------ Monte Carlo

# `R1 n1 n2 <value>` -- the leading fields, the value, and whatever trails it.
_PASSIVE = re.compile(r"^(\s*([rRcClL])(\S*)\s+\S+\s+\S+\s+)([^\s;]+)(.*)$")


def tolerance_netlist(netlist: str, r_tol: float, c_tol: float,
                      l_tol: float | None = None) -> tuple[str, list[dict]]:
    """Wrap every passive value in `mc(nom, tol)`. Returns (netlist, what changed).

    Components whose value is already an expression (`{R1}`, `{mc(...)}`) are left alone
    and reported, so a run that toleranced nothing cannot look like a run that did.
    """
    out, changed, skipped = [], [], []
    tol = {"r": r_tol, "c": c_tol, "l": r_tol if l_tol is None else l_tol}
    for ln in netlist.splitlines():
        m = _PASSIVE.match(ln)
        if not m:
            out.append(ln)
            continue
        letter = m.group(2).lower()
        name = m.group(2) + m.group(3)
        val = ir.parse_value(m.group(4))
        if val is None or val == 0:
            skipped.append({"name": name, "value": m.group(4),
                            "reason": "value is an expression or zero"})
            out.append(ln)
            continue
        t = tol[letter]
        out.append(f"{m.group(1)}{{mc({ir.fmt_value(val)},{ir.fmt_value(t)})}}"
                   f"{m.group(5)}")
        changed.append({"name": name, "nominal": val, "tol": t})
    return "\n".join(out), changed + [dict(s, tol=None) for s in skipped]


def evaluate_monte_carlo(netlist: str, workdir: str | Path, *,
                         n: int = 30, r_tol: float = 0.01, c_tol: float = 0.05,
                         metrics: list[str] | None = None,
                         spec: dict | None = None,
                         sim_config: dict | None = None,
                         plot: bool = True, project=None) -> dict:
    """N-run Monte Carlo on passive tolerances, in ONE LTspice launch.

    `mc(nom,tol)` redraws per `.step` run and per component, so `.step param run 1 N 1`
    is a full Monte Carlo -- measured over 400 runs, uniform over +-tol with no sample
    outside it. Launching N subprocesses would cost N times as much for the same numbers.

    Yield is counted against `spec` ({metric: (lo, hi)}), not against 3 sigma: "3 sigma
    fits inside the spec" and "how many parts pass" are different claims.
    """
    if n < 2:
        raise RobustnessError(f"a Monte Carlo needs >=2 runs, got {n}")
    nl, comps = tolerance_netlist(netlist, r_tol, c_tol)
    toleranced = [c for c in comps if c.get("tol")]
    if not toleranced:
        raise RobustnessError(
            "nothing to randomise: no R/C/L line carried a plain numeric value. "
            f"skipped: {[c['name'] for c in comps]}")
    nl = _with_step(nl, n)

    cfg = {**(sim_config or {}), "require_meas": False}
    res = sim.simulate(nl, {}, cfg, Path(workdir), "monte_carlo")
    if res.status != "ok":
        return {"status": "failed", "failure_type": res.failure_type,
                "failure_message": res.failure_message, "log_path": res.log_path}

    log_text = Path(res.log_path).read_text(errors="replace") if res.log_path else ""
    series = sim.parse_meas_steps(log_text)
    if not series:
        return {"status": "failed", "failure_type": sim.SIMULATION_FAILURE,
                "failure_message": "no per-run .meas results in the stepped log; add "
                                   ".meas directives naming the metrics to sample",
                "log_path": res.log_path}

    wanted = [k.lower() for k in (metrics or series)]
    out_stats, plots_made = {}, {}
    for k in wanted:
        vals = series.get(k)
        if not vals:
            out_stats[k] = {"error": "not measured in this run"}
            continue
        out_stats[k] = _stats(vals, (spec or {}).get(k))
        if plot:
            try:
                from . import plots as _plots
                p = _plots.plot_histogram(vals, label=k, bins=min(12, max(4, n // 3)),
                                          spec=_bounds((spec or {}).get(k)),
                                          project=project)
                plots_made[k] = p["plot_path"]
            except Exception as e:                       # plots are optional, MC is not
                plots_made[k] = f"not plotted: {e}"

    runs = min(len(v) for v in series.values())
    return {"status": "ok", "runs_requested": n, "runs_measured": runs,
            "tolerances": {"r": r_tol, "c": c_tol},
            "components": toleranced,
            "components_skipped": [c for c in comps if not c.get("tol")],
            "distribution": "uniform over +-tol (LTspice mc()); use gauss() for normal",
            "statistics": out_stats, "plots": plots_made,
            "raw_path": res.raw_path, "log_path": res.log_path,
            "pass": all(s.get("yield_pct") == 100.0
                        for s in out_stats.values() if "yield_pct" in s)}


def _bounds(spec) -> tuple | None:
    if spec is None:
        return None
    return tuple(spec) if isinstance(spec, (list, tuple)) else (None, spec)


def _stats(vals: list[float], spec=None) -> dict:
    n = len(vals)
    mu = statistics.fmean(vals)
    sd = statistics.stdev(vals) if n > 1 else 0.0
    out = {"n": n, "mean": raw._r(mu), "std": raw._r(sd),
           "min": raw._r(min(vals)), "max": raw._r(max(vals)),
           "cv_pct": raw._r(sd / mu * 100) if mu else None,
           "sigma3_lo": raw._r(mu - 3 * sd), "sigma3_hi": raw._r(mu + 3 * sd)}
    b = _bounds(spec)
    if b:
        lo, hi = b
        inside = [v for v in vals
                  if (lo is None or v >= lo) and (hi is None or v <= hi)]
        out.update(spec_lo=lo, spec_hi=hi, n_pass=len(inside),
                   yield_pct=raw._r(len(inside) / n * 100))
        # Cpk: how many sigma of margin the tighter limit has. The number that says
        # whether a 30/30 pass was comfortable or a coin flip.
        if sd > 0:
            k = [(mu - lo) / (3 * sd) for lo in [lo] if lo is not None] + \
                [(hi - mu) / (3 * sd) for hi in [hi] if hi is not None]
            out["cpk"] = raw._r(min(k)) if k else None
        # ponytail: 30 runs resolves yield to ~3%. A 99% spec needs hundreds of runs;
        # the sample size is reported so nobody reads 30/30 as "100% yield".
        out["yield_ci_note"] = (f"{n} runs; a pass rate this size cannot resolve better "
                                f"than about {100 / n:.0f}%")
    return out


def _with_step(netlist: str, n: int) -> str:
    keep = [ln for ln in netlist.splitlines()
            if not re.match(r"\s*\.(step|end)\b", ln, re.I)]
    return _deck(keep, f".step param run 1 {n} 1")


def _hist(vals: list[float], bins: int = 10) -> list[tuple[float, int]]:
    """Text-mode histogram, for when matplotlib is not installed."""
    lo, hi = min(vals), max(vals)
    if hi == lo:
        return [(lo, len(vals))]
    w = (hi - lo) / bins
    counts = [0] * bins
    for v in vals:
        counts[min(bins - 1, int((v - lo) / w))] += 1
    return [(raw._r(lo + i * w), c) for i, c in enumerate(counts)]


if __name__ == "__main__":                              # smallest useful self-check
    NL = ("* divider\nV1 in 0 5\nR1 in out 1k\nC1 out 0 100n\n"
          ".model NM NMOS(VTO=0.7 KP=200u)\nM1 out g 0 0 NM\n.op\n.end\n")
    assert [d["name"] for d in devices(NL)] == ["M1"]
    assert models(NL)["nm"]["vto"] == 0.7
    nl, comps = tolerance_netlist(NL, 0.01, 0.05)
    assert "mc(1000.0,0.01)" in nl and "mc(1e-07,0.05)" in nl, nl
    assert [c["name"] for c in comps] == ["R1", "C1"]
    assert _scale_source("V1 in 0 5", "V1", 0.9) == ("V1 in 0 4.5", True)
    assert _scale_source("V1 in 0 DC 5", "V1", 0.9) == ("V1 in 0 DC 4.5", True)
    assert _scale_source("V2 in 0 5", "V1", 0.9) == ("V2 in 0 5", False)
    assert _with_op(".tran 1m\n.model NM NMOS(VTO=0.7)\n").splitlines()[0][0] == "*"
    print("ok")
