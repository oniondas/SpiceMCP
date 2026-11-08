"""From "optimal" to "buildable": standard part values, then yield under tolerance.

An optimizer's best design says C1 = 15.915494 nF. Nobody sells that part. The design
that ships is the nearest *standard* value (IEC 60063 E-series), with that part's real
tolerance -- and those are two different circuits from the one that was optimised:

1. Snapping moves every part at once, by up to half a series step (about 10 % for
   E12). A design that met its limits with 1 % margin can miss after snapping.
2. A 1 % resistor is anywhere in +-1 % on the board. A design that sits exactly on a
   limit passes nominally and fails on half the boards built.

`finish()` closes both gaps:

  * the parts from the coarsest series (usually capacitors, E12) are bracketed by the
    standard values just below and above, and those combinations are simulated;
  * for the best two, every remaining free value (the fine-step resistors, a duty
    cycle, a transistor width) is re-centred to maximise the worst margin to any
    limit, which absorbs the coarse parts' snapping error;
  * then the fine-step parts are bracketed and simulated the same way;
  * the combinations that meet every limit are ranked by their worst normalised
    margin, and the best few get a Monte Carlo yield run: each part drawn uniformly
    inside its tolerance (plus, optionally, the supply inside its own tolerance);
  * the combination with the highest yield is returned, as a parts list.

Uniform rather than Gaussian on purpose: it is what LTspice's `mc()` draws and it is the
pessimistic choice for a reel of parts sorted into a tolerance bin. Parameters that are
not parts (a MOSFET width, a duty cycle) are left at their optimised value.
"""

from __future__ import annotations

import itertools
import math
import random
from pathlib import Path

from . import db, engine, ir, progress, sim

E6 = [1.0, 1.5, 2.2, 3.3, 4.7, 6.8]
E12 = [1.0, 1.2, 1.5, 1.8, 2.2, 2.7, 3.3, 3.9, 4.7, 5.6, 6.8, 8.2]
E24 = [1.0, 1.1, 1.2, 1.3, 1.5, 1.6, 1.8, 2.0, 2.2, 2.4, 2.7, 3.0, 3.3, 3.6, 3.9, 4.3,
       4.7, 5.1, 5.6, 6.2, 6.8, 7.5, 8.2, 9.1]
E96 = [1.00, 1.02, 1.05, 1.07, 1.10, 1.13, 1.15, 1.18, 1.21, 1.24, 1.27, 1.30, 1.33,
       1.37, 1.40, 1.43, 1.47, 1.50, 1.54, 1.58, 1.62, 1.65, 1.69, 1.74, 1.78, 1.82,
       1.87, 1.91, 1.96, 2.00, 2.05, 2.10, 2.15, 2.21, 2.26, 2.32, 2.37, 2.43, 2.49,
       2.55, 2.61, 2.67, 2.74, 2.80, 2.87, 2.94, 3.01, 3.09, 3.16, 3.24, 3.32, 3.40,
       3.48, 3.57, 3.65, 3.74, 3.83, 3.92, 4.02, 4.12, 4.22, 4.32, 4.42, 4.53, 4.64,
       4.75, 4.87, 4.99, 5.11, 5.23, 5.36, 5.49, 5.62, 5.76, 5.90, 6.04, 6.19, 6.34,
       6.49, 6.65, 6.81, 6.98, 7.15, 7.32, 7.50, 7.68, 7.87, 8.06, 8.25, 8.45, 8.66,
       8.87, 9.09, 9.31, 9.53, 9.76]
SERIES = {"E6": E6, "E12": E12, "E24": E24, "E48": E96[::2], "E96": E96}

# What a designer orders by default: 1 % thin-film resistors, 5 % C0G/X7R capacitors in
# E12 values, 20 % power inductors in E12 values. Every one is overridable.
DEFAULT_SERIES = {"R": "E96", "C": "E12", "L": "E12"}
DEFAULT_TOL = {"R": 0.01, "C": 0.05, "L": 0.20}
MAX_COMBOS = 64
YIELD_CANDIDATES = 3
CENTRE_BUDGET = 40


class BuildError(engine.EngineError):
    pass


def snap(v: float, series: str) -> float:
    """Nearest standard value, measured in log space (the series is geometric)."""
    lo, hi = bracket(v, series)
    return lo if abs(math.log(v / lo)) <= abs(math.log(hi / v)) else hi


def bracket(v: float, series: str) -> tuple[float, float]:
    """The standard values just at/below and at/above `v`. Equal when `v` is one."""
    if v <= 0:
        raise BuildError(f"cannot pick a standard part for a value of {v}")
    base = SERIES[series]
    d = math.floor(math.log10(v))
    vals = sorted({float(f"{m * 10.0 ** e:.6g}") for e in (d - 1, d, d + 1) for m in base})
    for x in vals:
        if math.isclose(x, v, rel_tol=1e-9):
            return x, x
    below = max(x for x in vals if x < v)
    above = min(x for x in vals if x > v)
    return below, above


def part_kind(name: str) -> str | None:
    k = name[:1].upper()
    return k if k in DEFAULT_SERIES else None


def _margin(metrics: dict, cons: list[dict]) -> float:
    """Worst normalised slack over the limits: >0 inside every limit, the larger the
    safer. Limits on part values themselves (`param_*`) are checked, not ranked."""
    worst = math.inf
    for c in cons:
        if c["metric"].startswith("param_"):
            continue
        a, t = metrics.get(c["metric"]), c["value"]
        if a is None:
            return -math.inf
        s = abs(t) or 1.0
        if c["op"] in (">", ">="):
            worst = min(worst, (a - t) / s)
        elif c["op"] in ("<", "<="):
            worst = min(worst, (t - a) / s)
    return worst if worst != math.inf else 0.0


def _passes(metrics: dict, cons: list[dict]) -> bool:
    real = [c for c in cons if not c["metric"].startswith("param_")]
    return engine.evaluate_constraints(metrics, real)[1]


def finish(con, run_id: str, series: dict | None = None, tol: dict | None = None,
           yield_runs: int = 30, supply: str | None = None, supply_tol: float = 0.0,
           seed: int | None = None, temps: list[float] | None = None) -> dict:
    run = engine.get_run(con, run_id)
    space = db.jloads(run, "param_space")
    cons = db.jloads(run, "constraints", [])
    series = {**DEFAULT_SERIES, **(series or {})}
    tol = {**DEFAULT_TOL, **(tol or {})}
    if supply and supply_tol:
        _scale_supply(ir.render(run["netlist_template"], {})[0], supply, 1.0)  # exists?
    best = engine.get_best(con, run_id)
    src = best["best_feasible"] or best["best"]
    if not src:
        raise BuildError("no design has simulated cleanly yet; call run_optimization first")
    base = db.jloads(engine.get_candidate(con, src["candidate_id"]), "params")

    snapped = {}                                       # name -> kind
    for k in base:
        kind = part_kind(k)
        if kind is None or series.get(kind, "none") == "none" or "values" in space[k]:
            continue
        snapped[k] = kind
    names = sorted(snapped)
    # The coarsest series is chosen first, the way a designer works: capacitors come in
    # E12, so pick those, then re-solve the resistors (and every other free value) to
    # put the design back in the middle of its limits, then pick those from E96.
    step = {k: len(SERIES[series[k]]) for k in set(snapped.values())}
    finest = max(step.values(), default=0)
    coarse = [n for n in names if step[snapped[n]] < finest]
    fine_parts = [n for n in names if n not in coarse]
    free = fine_parts + sorted(k for k in base if k not in snapped
                               and "values" not in space[k]
                               and isinstance(base[k], (int, float)))

    tried: dict[tuple, dict] = {}

    def _key(p):
        return tuple(sorted((k, float(f"{v:.9g}")) for k, v in p.items()))

    def run_many(choices: list[dict]) -> list[dict]:
        """Simulate every choice not seen yet, side by side, then look all up."""
        ps = [{**base, **c} for c in choices]
        new, seen = [], set()
        for p in ps:
            k = _key(p)
            if k not in tried and k not in seen:
                seen.add(k)
                new.append(p)
        if new:
            rs = engine.evaluate_many(con, run_id, new, src["candidate_id"], None, False,
                                      "buildable")
            for p, r in zip(new, rs):
                ok = r.get("status", "ok") == "ok"
                m = r.get("metrics") or {}
                tried[_key(p)] = {"params": p, "candidate_id": r.get("candidate_id"),
                                  "metrics": m, "feasible": bool(r.get("feasible")),
                                  "margin": _margin(m, cons) if ok else -math.inf,
                                  "score": r.get("score")}
            progress.report(len(tried), None,
                            f"standard values: {len(tried)} combinations simulated")
        return [tried[_key(p)] for p in ps]

    def run_combo(choice: dict) -> dict:
        return run_many([choice])[0]

    def rank(t):
        return (t["feasible"], t["margin"], t["score"] if t["score"] is not None
                else -math.inf)

    def combos(fixed: dict, which: list[str], at: dict) -> list[dict]:
        opts = [sorted(set(bracket(float(at[n]), series[snapped[n]]))) for n in which]
        if math.prod(len(o) for o in opts) <= MAX_COMBOS:
            return run_many([{**fixed, **dict(zip(which, c))}
                             for c in itertools.product(*opts)])
        cur = {**fixed, **{n: snap(float(at[n]), series[snapped[n]]) for n in which}}
        out = [run_combo(cur)]
        for _ in range(2):                             # greedy walk from nearest
            for n, o in zip(which, opts):
                for v in o:
                    t = run_combo({**cur, n: v})
                    out.append(t)
                    if rank(t) > rank(run_combo(cur)):
                        cur = {**cur, n: v}
        return out

    def centre(fixed: dict, start: dict, budget: int = CENTRE_BUDGET) -> dict:
        """Compass search on the worst margin over the free values, inside each
        value's own range. Moves every free value, so the coarse parts' snapping
        error is absorbed by the parts that come in fine steps."""
        cur = run_combo({**fixed, **start})
        frac, used = 0.08, 0
        while frac >= 0.0025 and used < budget and free:
            # every neighbour (each free value one step up and down) at once, side by
            # side; move to the best one that improves the worst margin
            moves = []
            for f in free:
                spec = space[f]
                for sgn in (1, -1):
                    v = cur["params"][f]
                    if engine.is_log(spec):
                        v = v * math.exp(sgn * frac)
                    else:
                        v = v + sgn * frac * (spec["max"] - spec["min"])
                    v = min(max(v, spec["min"]), spec["max"])
                    if v != cur["params"][f]:
                        moves.append({**fixed, **{k: cur["params"][k] for k in free}, f: v})
            if not moves:
                break
            ts = run_many(moves)
            used += len(moves)
            top = max(ts, key=lambda t: (t["feasible"], t["margin"]))
            if (top["feasible"], top["margin"]) > (cur["feasible"], cur["margin"]):
                cur = top
            else:
                frac /= 2
        return cur

    start_free = {k: base[k] for k in free}
    coarse_tops = sorted(combos({}, coarse, base), key=rank, reverse=True)[:2] \
        if coarse else [run_combo({})]
    finals = []
    for c in coarse_tops:
        fixed = {n: c["params"][n] for n in coarse}
        centred = centre(fixed, start_free)
        finals += combos({**fixed, **{k: centred["params"][k] for k in free
                                      if k not in fine_parts}},
                         fine_parts, centred["params"])
    ranked = sorted({id(t): t for t in finals}.values(), key=rank, reverse=True)
    nearest = {n: snap(float(base[n]), series[snapped[n]]) for n in names}
    nearest_res = run_combo(nearest)

    shortlist = [t for t in ranked if t["feasible"]][:YIELD_CANDIDATES] or ranked[:1]
    tols = {n: tol[snapped[n]] for n in names}
    if seed is None:
        seed = new_seed()
    if yield_runs > 0:
        for t in shortlist:
            t["yield"] = monte_carlo(con, run_id, t, tols, cons, yield_runs, supply,
                                     supply_tol, seed, temps)
        chosen = max(shortlist, key=lambda t: (t["yield"]["yield_pct"], rank(t)))
    else:
        chosen = shortlist[0]

    engine.stop(con, run_id)
    stressed = _stress(con, run_id, run, chosen["params"], supply, supply_tol)
    parts = {n: {"value": chosen["params"][n], "label": eng_value(chosen["params"][n]),
                 "series": series[snapped[n]],
                 "tolerance_pct": round(tols[n] * 100, 3),
                 "optimised_value": base[n],
                 "change_pct": round((chosen["params"][n] / base[n] - 1) * 100, 2)}
             for n in names}
    out = {
        "run_id": run_id,
        "from_candidate": src["candidate_id"],
        "buildable_candidate": chosen["candidate_id"],
        "parts": parts,
        "other_values": {k: chosen["params"][k] for k in base if k not in snapped},
        "metrics": chosen["metrics"],
        "meets_all_limits": chosen["feasible"],
        "worst_margin_pct": _pct(chosen["margin"]),
        "optimised_metrics": src["metrics"],
        "nearest_values_meet_limits": nearest_res["feasible"],
        "nearest_values_margin_pct": _pct(nearest_res["margin"]),
        "combinations_simulated": len(tried),
        "yield": chosen.get("yield"),
        "stress": stressed,
        "alternatives": [{"candidate_id": t["candidate_id"],
                          "parts": {n: eng_value(t["params"][n]) for n in names},
                          "meets_all_limits": t["feasible"],
                          "worst_margin_pct": _pct(t["margin"]),
                          "yield_pct": (t.get("yield") or {}).get("yield_pct")}
                         for t in ranked[:6]],
    }
    return out


def _stress(con, run_id, run, params, supply, supply_tol) -> dict:
    """Part stress of the chosen design, at the top of the supply tolerance."""
    from . import stress
    net, _ = ir.render(run["netlist_template"], params)
    if supply and supply_tol:
        net = _scale_supply(net, supply, 1 + supply_tol)
    where = db.state_root(engine._project(con)) / "simulations" / run_id / "stress"
    try:
        out = stress.check(net, db.jloads(run, "sim_config"), where)
    except Exception as e:                       # a probe must never sink the parts list
        return {"checked": False, "why": f"stress probe failed: {e}"}
    if supply and supply_tol:
        out["at"] = f"supply +{supply_tol * 100:g}%"
    return out


def _pct(x):
    return None if x is None or not math.isfinite(x) else round(x * 100, 3)


def monte_carlo(con, run_id: str, design: dict, tols: dict, cons: list[dict], n: int,
                supply: str | None, supply_tol: float, seed: int,
                temps: list[float] | None = None) -> dict:
    """N simulations with every part drawn uniformly inside its tolerance. Samples are
    not candidates (they are not designs anyone chose), so they are not stored as such;
    their numbers come back here and in the report."""
    run = engine.get_run(con, run_id)
    cfg = {**db.jloads(run, "sim_config")}
    template = run["netlist_template"]
    root = db.state_root(engine._project(con)) / "simulations" / run_id / "yield"
    rng = random.Random(seed * 7919 + 17)
    boards = []                                  # drawn first, in order: seed-exact
    for i in range(n):
        p = dict(design["params"])
        for k, t in tols.items():
            p[k] = p[k] * (1 + rng.uniform(-t, t))
        net, _ = ir.render(template, p)
        s = 1.0
        if supply and supply_tol:
            s = 1 + rng.uniform(-supply_tol, supply_tol)
            net = _scale_supply(net, supply, s)
        boards.append((p, s, net))
    where = Path(root) / str(design["candidate_id"])
    temps = list(temps) if temps else [None]
    jobs = [(i, t) for i in range(n) for t in temps]
    count = [0]

    def one(job):
        i, t = job
        p, _, net = boards[i]
        if t is not None:
            net = at_temperature(net, t)
        tag = "" if t is None else f"_t{t:+g}".replace("+", "p").replace("-", "m")
        r = sim.simulate(net, p, cfg, where, f"mc{i:03d}{tag}")
        count[0] += 1
        progress.report(None, None, f"yield: {count[0]} of {len(jobs)} simulations")
        return r

    results = dict(zip(jobs, sim.parallel_map(one, jobs)))
    real = [c for c in cons if not c["metric"].startswith("param_")]
    passed, rows = 0, []
    for i, (p, s, _) in enumerate(boards):
        row = {"passed": True, "metrics": {}, "parts": {k: p[k] for k in tols},
               "supply_scale": s, "failed": [], "failed_at_temps": [],
               "worst": {}, "worst_temp": {}}
        for t in temps:
            res = results[(i, t)]
            m = {k.lower(): v for k, v in res.metrics.items()} if res.status == "ok" else {}
            if t == temps[0] or t == 25:
                row["metrics"] = m
            bad = [c["metric"] for c in real
                   if res.status != "ok" or not _passes(m, [c])]
            if bad:
                row["passed"] = False
                row["failed"] = sorted(set(row["failed"]) | set(bad))
                if t is not None:
                    row["failed_at_temps"].append(t)
            for c in real:                         # keep each metric's worst corner
                k = c["metric"]
                if k not in m:
                    continue
                hi = c["op"] in ("<", "<=")
                w = row["worst"].get(k)
                if w is None or (m[k] > w if hi else m[k] < w):
                    row["worst"][k] = m[k]
                    row["worst_temp"][k] = t
        passed += row["passed"]
        rows.append(row)
    spread = {}
    for c in cons:
        k = c["metric"]
        vals = [r["worst"][k] for r in rows if k in r["worst"]]
        if vals and not k.startswith("param_"):
            spread[k] = {"min": min(vals), "max": max(vals),
                         "limit": f"{c['op']}{c['value']:g}"}
    lo = yield_lower_bound(passed, n)
    corners = [t for t in temps if t is not None]
    return {"runs": n, "passed": passed, "yield_pct": round(100 * passed / n, 1),
            "yield_at_least_pct": round(100 * lo, 1), "seed": seed,
            "statement": yield_statement(passed, n),
            "covers": "part tolerance"
                      + (f", supply ±{supply_tol * 100:g}%" if supply and supply_tol else "")
                      + (f", {'/'.join(f'{t:g}' for t in corners)} °C" if corners
                         else ", 27 °C only"),
            "distribution": "uniform inside each part's tolerance; a board passes only "
                            "if it passes at every temperature",
            "worst_case": worst_case(rows, cons, tols, design["params"]),
            "spread": spread, "samples": rows}


CONFIDENCE = 0.95


def _binom_cdf(k: int, n: int, p: float) -> float:
    """P(X <= k) for X ~ Binomial(n, p), summed in log space (n is at most a few
    thousand, so this is exact enough and needs no scipy)."""
    if p <= 0:
        return 1.0
    if p >= 1:
        return 1.0 if k >= n else 0.0
    lp, lq, tot = math.log(p), math.log1p(-p), 0.0
    for i in range(0, k + 1):
        tot += math.exp(math.lgamma(n + 1) - math.lgamma(i + 1) - math.lgamma(n - i + 1)
                        + i * lp + (n - i) * lq)
    return min(tot, 1.0)


def yield_lower_bound(passed: int, n: int, conf: float = CONFIDENCE) -> float:
    """One-sided Clopper-Pearson lower bound on the true yield: the smallest p for which
    seeing `passed` or more passes in `n` boards is still at least (1-conf) likely.
    30 of 30 passing gives 0.905: it proves yield above about 90 %, not 100 %."""
    if n <= 0 or passed <= 0:
        return 0.0
    a, b = 0.0, 1.0                  # P(X >= passed | p) rises with p: bisect on it
    for _ in range(60):
        m = (a + b) / 2
        if 1 - _binom_cdf(passed - 1, n, m) < 1 - conf:
            a = m
        else:
            b = m
    return a


def yield_statement(passed: int, n: int, conf: float = CONFIDENCE) -> str:
    lo = 100 * yield_lower_bound(passed, n, conf)
    return (f"{passed}/{n} pass: yield is at least {lo:.0f}% "
            f"({conf * 100:.0f}% confidence). More yield_runs narrow this.")


def new_seed() -> int:
    """A fresh seed per yield run, so a re-run draws different boards. It is reported,
    so any run can be repeated exactly."""
    return random.SystemRandom().randrange(1, 1_000_000)


def _scale_supply(netlist: str, name: str, scale: float) -> str:
    """Scale one DC source, or several given as "Vcc,Vee" (both rails drift together)."""
    from . import robustness
    out, hit = netlist.splitlines(), set()
    names = [n.strip() for n in name.split(",") if n.strip()]
    for i, ln in enumerate(out):
        for n in names:
            new, ok = robustness._scale_source(ln, n, scale)
            if ok:
                out[i] = new
                hit.add(n.lower())
    missing = [n for n in names if n.lower() not in hit]
    if missing:
        raise BuildError(f"supply source {missing[0]!r} not found or not a plain DC value")
    return "\n".join(out)


def default_supply(netlist: str) -> str | None:
    """The supply a template run drifts in yield: both rails if it has Vcc and Vee,
    else Vin. None when neither is a plain DC source."""
    from . import robustness
    def plain(n):
        return any(robustness._scale_source(ln, n, 1.0)[1] for ln in netlist.splitlines())
    if plain("Vcc") and plain("Vee"):
        return "Vcc,Vee"
    return "Vin" if plain("Vin") else None


def has_semiconductors(netlist: str) -> bool:
    """True when temperature can matter: transistors, diodes or a built-in block.
    Ideal R, L, C and sources have no temperature coefficient in SPICE."""
    from . import opamp
    for ln in netlist.splitlines():
        t = ln.strip().upper()
        if t[:1] in ("M", "D", "Q", "J"):
            return True
        if t[:1] == "X" and any(b in t.split() for b in opamp.BLOCKS):
            return True
    return False


def at_temperature(netlist: str, temp_c: float) -> str:
    """Every deck simulated at `temp_c` (a `.temp` line before each `.end`)."""
    out = []
    for ln in netlist.splitlines():
        if ln.strip().lower() == ".end":
            out.append(f".temp {temp_c:g}")
        out.append(ln)
    return "\n".join(out) + "\n"


def worst_case(rows: list[dict], cons: list[dict], tols: dict, nominal: dict) -> dict:
    """Which limit fails first, at which corner, and which part drives it.

    A part 'drives' a failure when failing boards drew it consistently high or low:
    the mean of (value / nominal - 1) / tolerance over failing boards minus that over
    passing boards. +1 means the failures had it at the top of its tolerance."""
    fails = [r for r in rows if not r["passed"]]
    out: dict = {"boards_failing": len(fails)}
    limits = {}
    for c in cons:
        k = c["metric"]
        if k.startswith("param_"):
            continue
        vals = [(v, r) for r in rows for v in [(r.get("worst") or {}).get(k)] if v is not None]
        if not vals:
            continue
        hi = c["op"] in ("<", "<=")
        v, r = max(vals, key=lambda x: x[0]) if hi else min(vals, key=lambda x: x[0])
        s = abs(c["value"]) or 1.0
        margin = (c["value"] - v) / s if hi else (v - c["value"]) / s
        e = limits.setdefault(k, {"limit": f"{c['op']}{c['value']:g}", "worst": v,
                                  "worst_margin_pct": round(100 * margin, 3),
                                  "at_temp_c": r.get("worst_temp", {}).get(k)})
        if margin * 100 < e["worst_margin_pct"]:
            e.update(limit=f"{c['op']}{c['value']:g}", worst=v,
                     worst_margin_pct=round(100 * margin, 3),
                     at_temp_c=r.get("worst_temp", {}).get(k))
    out["limits"] = dict(sorted(limits.items(), key=lambda kv: kv[1]["worst_margin_pct"]))
    if limits:
        first = next(iter(out["limits"]))
        out["tightest_limit"] = first
    counts: dict = {}
    temps: dict = {}
    for r in fails:
        for k in r.get("failed", []):
            counts[k] = counts.get(k, 0) + 1
        for t in r.get("failed_at_temps", []):
            temps[t] = temps.get(t, 0) + 1
    if counts:
        out["fails_most"] = max(counts, key=counts.get)
        out["fail_counts"] = counts
    if temps:
        out["most_failures_at_c"] = max(temps, key=temps.get)
    if fails and len(fails) < len(rows):
        def dev(r, k):
            return (r["parts"][k] / nominal[k] - 1) / (tols[k] or 1)
        ok = [r for r in rows if r["passed"]]
        drive = {k: (sum(dev(r, k) for r in fails) / len(fails)
                     - sum(dev(r, k) for r in ok) / len(ok)) for k in tols}
        if drive:
            k = max(drive, key=lambda x: abs(drive[x]))
            out["part_driving_failures"] = {
                "part": k, "direction": "high" if drive[k] > 0 else "low",
                "strength": round(drive[k], 2)}
    return out


_SUF = ((1e9, "G"), (1e6, "Meg"), (1e3, "k"), (1, ""), (1e-3, "m"), (1e-6, "u"),
        (1e-9, "n"), (1e-12, "p"))


def eng_value(v: float) -> str:
    """A part value as it is ordered: 4.7k, 2.2n, 15.8k."""
    a = abs(v)
    for k, s in _SUF:
        if a >= k * 0.9995:
            return f"{v / k:.3g}{s}"
    return f"{v:.3g}"
