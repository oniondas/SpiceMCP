"""Optimization engine. All state lives in SQLite; nothing is remembered by the LLM.

Covers spec S3-S19. Scores are a *pure function* of a candidate's metrics (fixed
per-objective `ref` scales, fixed penalty), so a score never changes retroactively
and the best candidate can never be demoted by a later, worse iteration.
"""

from __future__ import annotations

import json
import random
import re
import statistics
from pathlib import Path

from . import db, ir, sim

PENALTY = 10.0          # relative-violation weight in the scalar score
STEP_FRAC = 0.25        # default move as a fraction of a parameter's range
_OPS = {">": lambda a, t: a > t, ">=": lambda a, t: a >= t,
        "<": lambda a, t: a < t, "<=": lambda a, t: a <= t,
        "==": lambda a, t: a == t, "!=": lambda a, t: a != t}
_CONSTRAINT_STR = re.compile(r"^\s*(>=|<=|==|!=|>|<)\s*([-+0-9.eE]+)\s*$")


class EngineError(RuntimeError):
    pass


# --- run setup -----------------------------------------------------------------

def _norm_constraints(raw) -> list[dict]:
    """Accepts [{metric,op,value}] or {metric: '>100000'} (spec S9 shorthand)."""
    out = []
    items = raw.items() if isinstance(raw, dict) else [
        (c["metric"], f"{c['op']}{c['value']}") if isinstance(c, dict) and "op" in c
        else (c["metric"], c["value"]) for c in (raw or [])
    ]
    for metric, spec in items:
        if isinstance(spec, dict):
            op, val = spec["op"], float(spec["value"])
        else:
            m = _CONSTRAINT_STR.match(str(spec))
            if not m:
                raise EngineError(f"bad constraint for {metric!r}: {spec!r}")
            op, val = m.group(1), float(m.group(2))
        if op not in _OPS:
            raise EngineError(f"unknown operator {op!r}")
        out.append({"metric": metric.lower(), "op": op, "value": val})
    return out


def _norm_objectives(raw, constraints: list[dict]) -> list[dict]:
    by_metric = {c["metric"]: c for c in constraints}
    out = []
    for o in raw or []:
        metric = o["metric"].lower()
        direction = o.get("direction", "max").lower()
        if direction not in ("max", "min"):
            raise EngineError(f"direction must be max|min, got {direction!r}")
        ref = o.get("ref")
        if ref in (None, 0):
            c = by_metric.get(metric)
            ref = abs(c["value"]) if c and c["value"] else 1.0
        out.append({"metric": metric, "direction": direction,
                    "weight": float(o.get("weight", 1.0)), "ref": abs(float(ref))})
    if not out:
        raise EngineError("at least one objective is required")
    return out


def start_run(con, run_id: str, netlist_template: str, param_space: dict,
              objectives: list, constraints=None, sim_config=None, seed: int = 0,
              notes: str = "") -> dict:
    if con.execute("SELECT 1 FROM runs WHERE run_id=?", (run_id,)).fetchone():
        raise EngineError(f"run {run_id!r} already exists (state is persistent)")
    cons = _norm_constraints(constraints)
    objs = _norm_objectives(objectives, cons)
    for name, spec in param_space.items():
        if "values" not in spec and not {"min", "max"} <= set(spec):
            raise EngineError(f"param {name!r} needs min+max or values")
    cfg = dict(sim_config or {})
    cfg.setdefault("backend", "ltspice")
    with db.transaction(con):
        con.execute(
            "INSERT INTO runs (run_id, created_at, status, netlist_template,"
            " param_space, objectives, constraints, sim_config, seed, notes)"
            " VALUES (?,?,'running',?,?,?,?,?,?,?)",
            (run_id, db.now(), netlist_template, db.jdumps(param_space),
             db.jdumps(objs), db.jdumps(cons), db.jdumps(cfg), int(seed), notes))
    return {"run_id": run_id, "objectives": objs, "constraints": cons,
            "parameters": sorted(param_space), "seed": seed}


def get_run(con, run_id: str):
    row = con.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
    if not row:
        raise EngineError(f"unknown run {run_id!r}")
    return row


# --- scoring / constraints -----------------------------------------------------

def evaluate_constraints(metrics: dict, constraints: list[dict]) -> tuple[list[dict], bool]:
    results, feasible = [], True
    for c in constraints:
        actual = metrics.get(c["metric"])
        if actual is None:
            results.append({"constraint": c["metric"],
                            "required": f"{c['op']}{c['value']:g}", "actual": None,
                            "passed": False, "missing": True})
            feasible = False
            continue
        ok = _OPS[c["op"]](actual, c["value"])
        results.append({"constraint": c["metric"],
                        "required": f"{c['op']}{c['value']:g}",
                        "actual": actual, "passed": ok})
        feasible &= ok
    return results, feasible


def _sig(v: float, digits: int = 9) -> float:
    """Round to significant figures, never decimal places -- rounding 1e-12 to 12 dp
    would silently zero out every capacitor in the design."""
    return float(f"{float(v):.{digits}g}")


def _violation(metrics: dict, constraints: list[dict]) -> float:
    total = 0.0
    for c in constraints:
        a, t = metrics.get(c["metric"]), c["value"]
        scale = abs(t) or 1.0
        if a is None:
            total += 1.0
            continue
        if c["op"] in (">", ">="):
            total += max(0.0, (t - a) / scale)
        elif c["op"] in ("<", "<="):
            total += max(0.0, (a - t) / scale)
        elif c["op"] == "==" and a != t:
            total += abs(a - t) / scale
    return total


def score(metrics: dict, objectives: list[dict], constraints: list[dict]) -> float | None:
    """Deterministic scalar score. None if an objective metric is missing."""
    s = 0.0
    for o in objectives:
        v = metrics.get(o["metric"])
        if v is None:
            return None
        s += o["weight"] * (1.0 if o["direction"] == "max" else -1.0) * (v / o["ref"])
    return s - PENALTY * _violation(metrics, constraints)


# --- candidates (spec S2, S4) --------------------------------------------------

def resolve_candidate(con, run_id: str, params: dict, parent_id: str | None = None,
                      netlist_template: str | None = None, origin: str = "llm") -> dict:
    """Return an existing candidate with this exact design, or create a new one.

    Never mutates an existing candidate -- a change is always a new candidate (S4).
    """
    run = get_run(con, run_id)
    template = netlist_template or run["netlist_template"]
    space = db.jloads(run, "param_space")
    params = {k: params[k] for k in sorted(params)}
    unknown = set(params) - set(space)
    if unknown:
        raise EngineError(f"params not in param_space: {sorted(unknown)}")

    netlist, missing = ir.render(template, params)
    dfp = ir.design_fp(netlist, params)
    hit = con.execute("SELECT * FROM designs WHERE run_id=? AND design_fp=?",
                      (run_id, dfp)).fetchone()
    if hit:
        return {"candidate_id": hit["candidate_id"], "created": False,
                "duplicate_of": hit["candidate_id"], "design_fp": dfp,
                "generation": hit["generation"]}

    if parent_id:
        prow = con.execute("SELECT generation FROM designs WHERE candidate_id=?",
                           (parent_id,)).fetchone()
        if not prow:
            raise EngineError(f"unknown parent {parent_id!r}")
        gen = prow["generation"] + 1
    else:
        gen = 0
    with db.transaction(con):
        cid = db.next_candidate_id(con, run_id)
        con.execute(
            "INSERT INTO designs (candidate_id, run_id, parent_id, generation, params,"
            " netlist, topology_fp, design_fp, origin, created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (cid, run_id, parent_id, gen, db.jdumps(params), netlist,
             ir.topology_fp(netlist), dfp, origin, db.now()))
    (db.state_root(_project(con)) / "candidates" / f"{cid}.cir").write_text(
        netlist, encoding="utf-8")
    return {"candidate_id": cid, "created": True, "design_fp": dfp,
            "generation": gen, "unfilled_placeholders": missing}


def _project(con) -> Path:
    """Project dir that owns this connection (state.db lives in .ltspice-mcp/)."""
    path = con.execute("PRAGMA database_list").fetchall()[0]["file"]
    return Path(path).parent.parent


def get_candidate(con, candidate_id: str):
    row = con.execute("SELECT * FROM designs WHERE candidate_id=?",
                      (candidate_id,)).fetchone()
    if not row:
        raise EngineError(f"unknown candidate {candidate_id!r}")
    return row


# --- evaluation: one transactional iteration (spec S3, S16) --------------------

def evaluate(con, run_id: str, params: dict, parent_id: str | None = None,
             sim_config: dict | None = None, force: bool = False,
             origin: str = "llm") -> dict:
    run = get_run(con, run_id)
    cfg = {**db.jloads(run, "sim_config"), **(sim_config or {})}
    objs, cons = db.jloads(run, "objectives", []), db.jloads(run, "constraints", [])

    cand = resolve_candidate(con, run_id, params, parent_id, origin=origin)
    cid = cand["candidate_id"]
    design = get_candidate(con, cid)
    cfp = ir.config_fp(cfg)
    efp = ir.experiment_fp(design["design_fp"], cfp)

    cached = con.execute(
        "SELECT * FROM experiments WHERE run_id=? AND experiment_fp=?"
        " ORDER BY iteration DESC LIMIT 1", (run_id, efp)).fetchone()
    if cached and not force:
        return {"cached": True, "candidate_id": cid,
                "previous_iteration": cached["iteration"],
                **_experiment_summary(cached)}

    # Simulation runs OUTSIDE the write transaction -- holding a SQLite write lock
    # for the duration of an LTspice run would serialise every other reader.
    started = db.now()
    root = db.state_root(_project(con))
    res = sim.simulate(design["netlist"], params, cfg,
                       root / "simulations" / run_id, cid)

    metrics = {k.lower(): v for k, v in res.metrics.items()}
    if res.status == "ok":
        cres, feasible = evaluate_constraints(metrics, cons)
        sc = score(metrics, objs, cons)
        ftype, fmsg, fdetail = None, None, None
        if sc is None:
            ftype = sim.STRUCTURAL_ERROR
            fmsg = "objective metric missing from .meas results"
            fdetail = {"expected": [o["metric"] for o in objs],
                       "found": sorted(metrics)}
        elif not feasible:
            ftype = sim.CONSTRAINT_VIOLATION
            fmsg = "; ".join(
                f"{c.get('constraint', c.get('metric'))} needs "
                f"{c.get('required')} got {c.get('actual')}"
                for c in cres if not c["passed"])
            fdetail = {"violations": [c for c in cres if not c["passed"]]}
    else:
        cres, feasible, sc = [], False, None
        ftype, fmsg, fdetail = res.failure_type, res.failure_message, res.failure_detail

    with db.transaction(con):
        it = db.next_iteration(con, run_id)
        con.execute(
            "INSERT INTO experiments (run_id, candidate_id, iteration,"
            " sim_config, sim_config_fp, experiment_fp, status, metrics,"
            " constraint_results, feasible, score, failure_type, failure_message,"
            " failure_detail, warnings, log_path, started_at, finished_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (run_id, cid, it, db.jdumps(cfg), cfp, efp, res.status,
             db.jdumps(metrics), db.jdumps(cres), int(feasible), sc, ftype, fmsg,
             db.jdumps(fdetail) if fdetail else None,
             db.jdumps(res.warnings) if res.warnings else None, res.log_path,
             started, db.now()))
        con.execute("UPDATE runs SET current_candidate=? WHERE run_id=?", (cid, run_id))

    _update_sensitivities(con, run_id)
    best = get_best(con, run_id)
    return {"cached": False, "candidate_id": cid, "iteration": it,
            "new_candidate": cand["created"], "status": res.status,
            "metrics": metrics, "constraints": cres, "feasible": bool(feasible),
            "score": sc, "failure_type": ftype, "failure_message": fmsg,
            "warnings": res.warnings,
            "best_candidate": best["best"]["candidate_id"] if best["best"] else None,
            # true only when THIS iteration took the lead; a worse iteration leaves
            # the incumbent untouched (spec S5/S18).
            "improved_best": bool(best["best"] and best["best"]["iteration"] == it)}


def _experiment_summary(row) -> dict:
    return {"iteration": row["iteration"], "status": row["status"],
            "metrics": db.jloads(row, "metrics"),
            "constraints": db.jloads(row, "constraint_results", []),
            "feasible": bool(row["feasible"]), "score": row["score"],
            "failure_type": row["failure_type"],
            "failure_message": row["failure_message"]}


# --- best-so-far, derived not stored (spec S5, S18) ---------------------------

def get_best(con, run_id: str) -> dict:
    run = get_run(con, run_id)

    def top(where: str):
        return con.execute(
            "SELECT * FROM experiments WHERE run_id=? AND status='ok'"
            f" AND score IS NOT NULL {where}"
            " ORDER BY score DESC, iteration ASC LIMIT 1", (run_id,)).fetchone()

    best, best_feas = top(""), top("AND feasible=1")
    return {"run_id": run_id, "current_candidate": run["current_candidate"],
            "best": _with_id(best), "best_feasible": _with_id(best_feas)}


def _with_id(row) -> dict | None:
    if not row:
        return None
    return {"candidate_id": row["candidate_id"], **_experiment_summary(row)}


# --- Pareto frontier (spec S7) -------------------------------------------------

def pareto(con, run_id: str, include_infeasible: bool = False) -> list[dict]:
    objs = db.jloads(get_run(con, run_id), "objectives", [])
    sql = ("SELECT * FROM experiments WHERE run_id=? AND status='ok'"
           " AND score IS NOT NULL")
    if not include_infeasible:
        sql += " AND feasible=1"
    rows = con.execute(sql + " ORDER BY score DESC", (run_id,)).fetchall()

    # One point per candidate (its best-scoring experiment); ties keep the earlier one.
    seen, pts = set(), []
    for r in rows:
        if r["candidate_id"] in seen:
            continue
        seen.add(r["candidate_id"])
        m = db.jloads(r, "metrics")
        if all(m.get(o["metric"]) is not None for o in objs):
            # Compare at 9 significant figures, report raw. Exact float equality would
            # let a 1-ulp difference count as "strictly better in this objective" and
            # silently drop the co-located point -- no simulator resolves that finely.
            pts.append((r, m, tuple(_sig(m[o["metric"]]) for o in objs)))

    def better(a, b, o):
        return a > b if o["direction"] == "max" else a < b

    def dominates(k2, k1):
        return (all(a == b or better(a, b, o) for a, b, o in zip(k2, k1, objs))
                and any(better(a, b, o) for a, b, o in zip(k2, k1, objs)))

    live = [(r, m, k) for r, m, k in pts
            if not any(dominates(k2, k) for _, _, k2 in pts)]

    # Designs differing only in a parameter with no measurable effect land on the same
    # point. All are non-dominated, but N copies of one trade-off is noise. Keep the
    # candidate that found it first -- not whichever carries a favourable last bit of
    # float noise in its score. (len-then-value so cand_10000 sorts after cand_9999.)
    chosen: dict[tuple, str] = {}
    for r, _, k in live:
        cid = r["candidate_id"]
        if k not in chosen or (len(cid), cid) < (len(chosen[k]), chosen[k]):
            chosen[k] = cid

    return [{"id": r["candidate_id"], "score": r["score"],
             **{o["metric"]: m[o["metric"]] for o in objs}}
            for r, m, k in live if r["candidate_id"] == chosen[k]]


# --- history / trace / failures (spec S8, S9, S17) ----------------------------

def history(con, run_id: str, limit: int = 20, offset: int = 0) -> list[dict]:
    rows = con.execute(
        "SELECT candidate_id, iteration, status, score, feasible, metrics,"
        " failure_type FROM experiments WHERE run_id=?"
        " ORDER BY iteration DESC LIMIT ? OFFSET ?",
        (run_id, limit, offset)).fetchall()
    return [{"iteration": r["iteration"], "candidate_id": r["candidate_id"],
             "status": r["status"], "score": r["score"],
             "feasible": bool(r["feasible"]), "metrics": db.jloads(r, "metrics"),
             "failure_type": r["failure_type"]} for r in rows]


def trace(con, run_id: str) -> str:
    """Spec S17: rendered from the database, never from memory."""
    rows = con.execute("SELECT * FROM experiments WHERE run_id=? ORDER BY iteration",
                       (run_id,)).fetchall()
    out = [f"Optimization Run: {run_id}", ""]
    for r in rows:
        out.append(f"[{r['iteration']:03d}] {r['candidate_id']}")
        for k, v in sorted(db.jloads(r, "metrics").items()):
            out.append(f"  {k} = {v:g}")
        if r["status"] != "ok":
            out.append(f"  FAILED ({r['failure_type']}): {r['failure_message']}")
        elif not r["feasible"]:
            out.append(f"  FAILED: {r['failure_message']}")
        else:
            out.append(f"  PASSED  score={r['score']:.4g}")
        out.append("")
    best = get_best(con, run_id)
    pick = best["best_feasible"] or best["best"]
    out.append(f"BEST: {pick['candidate_id']}" if pick else "BEST: none")
    return "\n".join(out)


def failed_candidates(con, run_id: str, limit: int = 20) -> list[dict]:
    rows = con.execute(
        "SELECT * FROM experiments WHERE run_id=? AND (status!='ok' OR feasible=0)"
        " ORDER BY iteration DESC LIMIT ?", (run_id, limit)).fetchall()
    return [{"candidate_id": r["candidate_id"], "iteration": r["iteration"],
             "failure_type": r["failure_type"],
             "message": r["failure_message"],
             "detail": db.jloads(r, "failure_detail", {})} for r in rows]


def compare(con, run_id: str, ids: list[str]) -> dict:
    objs = db.jloads(get_run(con, run_id), "objectives", [])
    rows = []
    for cid in ids:
        d = get_candidate(con, cid)
        e = con.execute(
            "SELECT * FROM experiments WHERE candidate_id=? AND score IS NOT NULL"
            " ORDER BY score DESC LIMIT 1", (cid,)).fetchone()
        rows.append({"candidate_id": cid, "params": db.jloads(d, "params"),
                     "generation": d["generation"], "parent_id": d["parent_id"],
                     **(_experiment_summary(e) if e else {"status": "unevaluated"})})
    deltas = {}
    if len(rows) == 2 and all(r.get("metrics") for r in rows):
        a, b = rows[0]["metrics"], rows[1]["metrics"]
        deltas = {k: b[k] - a[k] for k in a if k in b}
        deltas["params"] = {k: rows[1]["params"].get(k, 0) - v
                            for k, v in rows[0]["params"].items()
                            if rows[1]["params"].get(k) != v}
    return {"candidates": rows, "objectives": [o["metric"] for o in objs],
            "delta": deltas}


def similar_candidates(con, run_id: str, candidate_id: str, k: int = 5) -> list[dict]:
    ref = get_candidate(con, candidate_id)
    rp, space = db.jloads(ref, "params"), db.jloads(get_run(con, run_id), "param_space")
    rows = con.execute(
        "SELECT * FROM designs WHERE run_id=? AND candidate_id!=?",
        (run_id, candidate_id)).fetchall()
    scored = []
    for r in rows:
        p = db.jloads(r, "params")
        dist = 0.0
        for name, v in rp.items():
            span = _span(space.get(name, {})) or 1.0
            dist += ((p.get(name, v) - v) / span) ** 2
        scored.append((dist ** 0.5, r))
    scored.sort(key=lambda t: t[0])
    return [{"candidate_id": r["candidate_id"], "distance": round(d, 4),
             "same_topology": r["topology_fp"] == ref["topology_fp"],
             "params": db.jloads(r, "params")} for d, r in scored[:k]]


# --- empirical sensitivity (spec S10) ------------------------------------------

def _span(spec: dict) -> float:
    if "values" in spec and spec["values"]:
        vals = [float(v) for v in spec["values"]]
        return max(vals) - min(vals)
    return float(spec.get("max", 1)) - float(spec.get("min", 0))


def _ok_points(con, run_id: str) -> list[tuple[dict, dict]]:
    rows = con.execute(
        "SELECT d.params AS params, e.metrics AS metrics FROM experiments e"
        " JOIN designs d ON d.candidate_id = e.candidate_id"
        " WHERE e.run_id=? AND e.status='ok'", (run_id,)).fetchall()
    return [(json.loads(r["params"]), json.loads(r["metrics"])) for r in rows]


def _ols(xs: list[float], ys: list[float]) -> tuple[float, float]:
    n = len(xs)
    mx, my = sum(xs) / n, sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    if sxx == 0:
        return 0.0, 0.0
    slope = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sxx
    syy = sum((y - my) ** 2 for y in ys)
    r2 = 0.0 if syy == 0 else (slope ** 2) * sxx / syy
    return slope, max(0.0, min(1.0, r2))


def _sensitivity_one(points, param: str, metric: str) -> dict | None:
    """Prefer true finite differences -- pairs differing in `param` alone. Those are
    causal; an OLS fit over all points mixes in whatever else moved. Fall back to
    OLS when no clean pair exists, and say which method was used.
    """
    usable = [(p, m) for p, m in points if param in p and metric in m]
    if len(usable) < 2:
        return None

    slopes = []
    for i in range(len(usable)):
        pi, mi = usable[i]
        for j in range(i + 1, len(usable)):
            pj, mj = usable[j]
            if pi[param] == pj[param]:
                continue
            if any(pi.get(k) != pj.get(k) for k in set(pi) | set(pj) if k != param):
                continue
            slopes.append((mj[metric] - mi[metric]) / (pj[param] - pi[param]))

    if slopes:
        slope = statistics.median(slopes)
        agree = sum(1 for s in slopes if (s > 0) == (slope > 0)) / len(slopes)
        conf = agree * min(1.0, len(slopes) / 3.0)
        return {"slope": slope, "r2": None, "n": len(slopes),
                "method": "finite_difference", "confidence": round(conf, 3)}

    xs = [p[param] for p, _ in usable]
    if len(set(xs)) < 2:
        return None
    slope, r2 = _ols(xs, [m[metric] for _, m in usable])
    conf = r2 * min(1.0, (len(usable) - 1) / 4.0)
    return {"slope": slope, "r2": round(r2, 4), "n": len(usable),
            "method": "ols_marginal", "confidence": round(conf, 3)}


def _update_sensitivities(con, run_id: str) -> None:
    # ponytail: O(points^2) per (param, metric), recomputed every iteration. Fine to
    # a few hundred candidates; switch to incremental pair updates if a run gets big.
    run = get_run(con, run_id)
    params = sorted(db.jloads(run, "param_space"))
    points = _ok_points(con, run_id)
    if len(points) < 2:
        return
    metrics = sorted({k for _, m in points for k in m})
    with db.transaction(con):
        for p in params:
            for met in metrics:
                s = _sensitivity_one(points, p, met)
                if not s:
                    continue
                con.execute(
                    "INSERT OR REPLACE INTO sensitivities (run_id, parameter, metric,"
                    " slope, r2, n, method, confidence, updated_at)"
                    " VALUES (?,?,?,?,?,?,?,?,?)",
                    (run_id, p, met, s["slope"], s["r2"], s["n"], s["method"],
                     s["confidence"], db.now()))


def sensitivity(con, run_id: str, parameter: str | None = None,
                metric: str | None = None) -> list[dict]:
    sql = "SELECT * FROM sensitivities WHERE run_id=?"
    args: list = [run_id]
    if parameter:
        sql += " AND parameter=?"
        args.append(parameter)
    if metric:
        sql += " AND metric=?"
        args.append(metric.lower())
    rows = con.execute(sql + " ORDER BY confidence DESC, ABS(slope) DESC",
                       args).fetchall()
    return [{"parameter": r["parameter"], "metric": r["metric"],
             "direction": "positive" if r["slope"] > 0 else "negative",
             "estimated_sensitivity": r["slope"], "r2": r["r2"], "n": r["n"],
             "method": r["method"], "confidence": r["confidence"]} for r in rows]


# --- explored parameter space (spec S11) ---------------------------------------

def explored(con, run_id: str, parameter: str | None = None) -> dict:
    space = db.jloads(get_run(con, run_id), "param_space")
    rows = con.execute("SELECT params FROM designs WHERE run_id=?", (run_id,)).fetchall()
    all_params = [json.loads(r["params"]) for r in rows]
    out = {}
    for name, spec in space.items():
        if parameter and name != parameter:
            continue
        vals = sorted({p[name] for p in all_params if name in p})
        gap = _largest_gap(vals, spec)
        out[name] = {
            "bounds": [spec.get("min"), spec.get("max")] if "values" not in spec
            else spec["values"],
            "evaluated": vals, "count": len(vals),
            "explored_min": vals[0] if vals else None,
            "explored_max": vals[-1] if vals else None,
            "largest_unexplored_gap": gap,
        }
    return out


def _largest_gap(vals: list[float], spec: dict) -> dict | None:
    """Widest untried interval, bounds included as edges."""
    if "values" in spec:
        untried = [v for v in spec["values"] if v not in vals]
        return {"untried_values": untried} if untried else None
    lo, hi = float(spec["min"]), float(spec["max"])
    edges = [lo] + [v for v in vals if lo <= v <= hi] + [hi]
    edges = sorted(set(edges))
    if len(edges) < 2:
        return None
    a, b = max(zip(edges, edges[1:]), key=lambda t: t[1] - t[0])
    if b - a <= 0:
        return None
    return {"low": a, "high": b, "width": b - a, "midpoint": (a + b) / 2}


def _clamp(v: float, spec: dict) -> float:
    if "values" in spec:
        return min(spec["values"], key=lambda c: abs(c - v))
    return max(float(spec["min"]), min(float(spec["max"]), v))


# --- next-experiment selection (spec S12) --------------------------------------

def _can_move(base_val: float, spec: dict, slope: float, want_up: bool) -> bool:
    """Is there room left to move this parameter in the direction that would help?

    Without this the search keeps picking the single strongest parameter after it has
    saturated at a bound, the proposal collides with an existing design every time,
    and it falls back to random jitter instead of moving on to the next-strongest
    parameter. That reads as "converged" while actually being stuck at a limit.
    """
    up = _step(spec, 1.0, slope, want_up) > 0
    if "values" in spec:
        return max(spec["values"]) > base_val if up else min(spec["values"]) < base_val
    return (base_val < float(spec["max"])) if up else (base_val > float(spec["min"]))


def _least_violating(con, run_id: str, cons: list[dict]) -> dict | None:
    """The closest candidate to feasibility, by summed relative violation.

    Needed because a fixed scalar score can rank an infeasible-but-high-objective
    point above a nearly-feasible one: stepping from the top-scoring point then moves
    *away* from the feasible region and the search stalls. While nothing feasible
    exists, feasibility is the objective.
    """
    rows = con.execute(
        "SELECT * FROM experiments WHERE run_id=? AND status='ok' AND score IS NOT NULL",
        (run_id,)).fetchall()
    best = None
    for r in rows:
        key = (_violation(db.jloads(r, "metrics"), cons), -r["score"], r["iteration"])
        if best is None or key < best[0]:
            best = (key, r)
    return _with_id(best[1]) if best else None


def select_next(con, run_id: str, step_frac: float = STEP_FRAC) -> dict:
    """Sensitivity-guided local search with unexplored-gap fallback, seeded.

    ponytail: no Gaussian-process surrogate. Priority order is
      (1) screen any parameter never yet varied -- unmeasured beats unexploited,
      (2) fix a violated constraint by moving the parameter with the strongest
          empirical influence on that metric in the helpful direction,
      (3) else push the weighted objective the same way,
      (4) else probe the widest untried interval,
      (5) else seeded random sample.
    Every proposal is dedup-checked against known designs and known failures.
      skipped: Bayesian optimisation. Add when a simulation is expensive enough
      that ~30 evaluations of slack is worth a surrogate model's complexity.
    """
    run = get_run(con, run_id)
    space = db.jloads(run, "param_space")
    objs, cons = db.jloads(run, "objectives", []), db.jloads(run, "constraints", [])
    n_designs = con.execute("SELECT COUNT(*) FROM designs WHERE run_id=?",
                            (run_id,)).fetchone()[0]
    rng = random.Random(run["seed"] * 1000003 + n_designs)

    known = {r["design_fp"] for r in con.execute(
        "SELECT design_fp FROM designs WHERE run_id=?", (run_id,)).fetchall()}
    bad = {r["design_fp"] for r in con.execute(
        "SELECT DISTINCT d.design_fp FROM experiments e JOIN designs d"
        " ON d.candidate_id=e.candidate_id WHERE e.run_id=? AND e.status!='ok'",
        (run_id,)).fetchall()}

    best = get_best(con, run_id)
    base_row = best["best_feasible"] or _least_violating(con, run_id, cons)
    if base_row is None:
        params = {n: _seed_value(s, rng) for n, s in space.items()}
        return _propose(con, run_id, params, None, "no evaluated candidate yet:"
                        " sampling the parameter space", "seed", known, bad, rng, space)

    base_id = base_row["candidate_id"]
    base = db.jloads(get_candidate(con, base_id), "params")
    sens = {(s["parameter"], s["metric"]): s for s in sensitivity(con, run_id)}
    tried = explored(con, run_id)

    # (1) screen any parameter that has never been varied. It has no sensitivity row,
    # so the exploit stages below can never pick it -- whichever parameter the initial
    # seed happened to move monopolises every later decision, and the gap stage can't
    # rescue it either (a gap needs two evaluated values). You cannot rank a parameter
    # you have not moved, and screening each one costs one simulation, once, ever.
    for name, spec in space.items():
        if name not in base or len(tried.get(name, {}).get("evaluated", [])) > 1:
            continue
        alt = _other_value(spec, base[name], rng)
        if alt == base[name]:
            continue
        params = dict(base)
        params[name] = alt
        return _propose(con, run_id, params, base_id,
                        f"{name} has never been varied, so its effect is unmeasured",
                        "probe_unvaried", known, bad, rng, space)

    # (2) violated constraints next, worst relative miss first -- nudging a 1% miss
    # while an 80x one stands is how a run burns its budget without becoming feasible.
    template = run["netlist_template"]
    for c in sorted((c for c in base_row.get("constraints", []) if not c.get("passed")),
                    key=_rel_miss, reverse=True):
        met = c.get("constraint") or c.get("metric")
        want_up = _wants_increase(c.get("required", ""))
        ranked = [(n, s["estimated_sensitivity"], want_up,
                   f"{met} fails ({c.get('required')} vs {c.get('actual')}); {n} moves"
                   f" {met} {'up' if want_up else 'down'} at"
                   f" {s['estimated_sensitivity']:.4g}/unit (conf {s['confidence']})")
                  for n, s in _ranked_for(sens, space, met, want_up, base)]
        params, why = _first_untried(template, base, space, ranked, step_frac,
                                     known, bad, n_designs)
        if params:
            return _propose(con, run_id, params, base_id, why, "constraint_repair",
                            known, bad, rng, space)

    # (3) push the objectives.
    scored = []
    for o in objs:
        want_up = o["direction"] == "max"
        for name in space:
            s = sens.get((name, o["metric"]))
            if (s and s["confidence"] > 0 and name in base
                    and _can_move(base[name], space[name],
                                  s["estimated_sensitivity"], want_up)):
                gain = _leverage(s, space[name]) * o["weight"] / o["ref"]
                if gain > 0:                       # a measured-dead knob wastes a turn
                    scored.append((gain, name, o, s))
    scored.sort(reverse=True, key=lambda t: t[0])
    ranked = [(name, s["estimated_sensitivity"], o["direction"] == "max",
               f"{name} has the strongest measured effect on {o['metric']} "
               f"({s['estimated_sensitivity']:.4g}/unit, conf {s['confidence']})")
              for _, name, o, s in scored]
    params, why = _first_untried(template, base, space, ranked, step_frac, known, bad,
                                 n_designs)
    if params:
        return _propose(con, run_id, params, base_id, why, "objective_push",
                        known, bad, rng, space)

    # (4) widest untried interval.
    gaps = [(v["largest_unexplored_gap"], n) for n, v in tried.items()
            if v["largest_unexplored_gap"] and "midpoint" in v["largest_unexplored_gap"]]
    if gaps:
        g, name = max(gaps, key=lambda t: t[0]["width"] / (_span(space[t[1]]) or 1))
        params = dict(base)
        params[name] = g["midpoint"]
        return _propose(con, run_id, params, base_id,
                        f"{name} is unexplored between {g['low']:g} and {g['high']:g}",
                        "explore_gap", known, bad, rng, space)

    params = {n: _seed_value(s, rng) for n, s in space.items()}
    return _propose(con, run_id, params, base_id, "no signal yet: random probe",
                    "random", known, bad, rng, space)


def _wants_increase(required: str) -> bool:
    return required.strip().startswith(">")


def _step(spec: dict, frac: float, slope: float, want_up: bool) -> float:
    mag = (_span(spec) or 1.0) * frac
    helpful_dir = 1.0 if (slope > 0) == want_up else -1.0
    return mag * helpful_dir


def _move(spec: dict, cur: float, frac: float, slope: float, want_up: bool) -> float:
    """One directed step, respecting whether the parameter is continuous or a list.

    A discrete list must step to the *adjacent entry*, not by a continuous distance:
    `_step` sizes the move as a fraction of the list's span, and `_clamp` then snaps
    to the nearest allowed value -- so for E-series parts, whose spacing grows by
    decade, a step smaller than the gap to the next entry rounds straight back to
    where it started. The move becomes a permanent no-op, and because the parameter
    is silently unchanged it re-collides forever. Verified: C2 in
    [1n, 2.2n, 4.7n, 10n] at 10n, span 9n, frac 0.25 -> 7.75n -> snaps to 10n.

    `frac` scales the move, so a discrete list reads it as a number of entries to
    advance (frac == STEP_FRAC means one).
    """
    up = _step(spec, 1.0, slope, want_up) > 0
    if "values" in spec:
        vals = sorted(spec["values"])
        side = ([v for v in vals if v > cur] if up
                else [v for v in reversed(vals) if v < cur])
        if not side:
            return cur
        return side[min(max(1, round(frac / STEP_FRAC)), len(side)) - 1]
    return _clamp(cur + _step(spec, frac, slope, want_up), spec)


def _fp_of(template: str, params: dict) -> tuple[dict, str]:
    # Significant figures, not decimal places: rounding 1e-12 to 12 dp would
    # silently zero out every capacitor in the design.
    p = {k: _sig(v) for k, v in params.items()}
    return p, ir.design_fp(ir.render(template, p)[0], p)


def _first_untried(template: str, base: dict, space: dict, ranked: list,
                   frac: float, known: set, bad: set, start: int = 0):
    """First design in priority order that nobody has evaluated, or (None, None).

    `ranked` is [(name, slope, want_up, why)], strongest influence first. Step size is
    the outer loop, so a nominal move on any parameter is preferred to a 16x move on
    one; `start` rotates the walk so coordinates take turns.

    Three things this has to get right, each of which stalled a real filter run:

    * Without retrying at all, a collision on the best-ranked move falls through to
      `_propose`'s random jitter, which discards the direction. The base is usually
      unchanged next iteration, so the same colliding move is proposed again and the
      run degenerates into a +-15% random walk that cannot cross a decade: 55 of 61
      iterations were jitter, and both resistors stayed inside 7k..11k of a 500..20k
      range while the answer sat at 1.1k.
    * Escalating steps let the search reach a bound at all. One 25% step from the seed
      cannot get to 500 from 10k inside a budget.
    * `start` prevents starvation. Greedy coordinate descent never revisits a
      lower-ranked coordinate while a higher-ranked one still has untried values --
      and a continuous parameter always does. Two resistors with identical physics
      rank identically, so R2 was never moved once in 61 iterations.
    """
    if ranked:
        start %= len(ranked)
        ranked = ranked[start:] + ranked[:start]
    for mult in (1, 2, 4, 8, 16, 0.5, 0.25):
        for name, slope, want_up, why in ranked:
            v = _move(space[name], base[name], frac * mult, slope, want_up)
            if v == base[name]:
                continue
            p, fp = _fp_of(template, {**base, name: v})
            if fp not in known and fp not in bad:
                return p, (why if mult == 1 else f"{why}; step x{mult:g}")
    return None, None


def _rel_miss(c: dict) -> float:
    """How badly a constraint result misses, relative to its bound."""
    m = _CONSTRAINT_STR.match(str(c.get("required", "")))
    if not m or c.get("actual") is None:
        return 0.0
    v = float(m.group(2))
    return abs(float(c["actual"]) - v) / (abs(v) or 1.0)


def _leverage(s: dict, spec: dict) -> float:
    """How much of `metric` a full-range move of this parameter buys, times confidence.

    Ranking on raw d(metric)/d(param) is meaningless across parameters, because it
    carries the parameter's units: a capacitor's sensitivity is per farad, so it
    outranks a resistor's per-ohm figure by ~1e8 regardless of which knob actually
    does more. Multiplying by the range makes the comparison scale-free -- the real
    question is what one move can achieve, not what one farad could.
    """
    return abs(s["estimated_sensitivity"]) * (_span(spec) or 1.0) * s["confidence"]


def _ranked_for(sens: dict, space: dict, metric: str, want_up: bool, base: dict):
    """Parameters that can still move `metric` the helpful way, most leverage first.

    Zero leverage is excluded: a parameter measured to have no effect on this metric
    cannot repair it, and with coordinate rotation it would otherwise consume a turn.
    """
    cands = [(_leverage(s, space[n]), n, s)
             for (n, m), s in sens.items()
             if m == metric and n in space and s["confidence"] > 0
             and n in base
             and _leverage(s, space[n]) > 0
             and _can_move(base[n], space[n], s["estimated_sensitivity"], want_up)]
    return [(n, s) for _, n, s in sorted(cands, reverse=True, key=lambda t: t[0])]


def _seed_value(spec: dict, rng: random.Random):
    if "values" in spec:
        return rng.choice(spec["values"])
    return rng.uniform(float(spec["min"]), float(spec["max"]))


def _other_value(spec: dict, cur: float, rng: random.Random) -> float:
    """A value deliberately different from `cur`.

    For a discrete list this must pick a different entry, not scale `cur`: component
    lists are decade-spaced, and +-15% of 1e-8 snaps straight back to 1e-8.
    """
    if "values" in spec:
        others = [v for v in spec["values"] if v != cur]
        return rng.choice(others) if others else cur
    lo, hi = float(spec["min"]), float(spec["max"])
    far = hi if abs(hi - cur) > abs(cur - lo) else lo   # halfway into the roomier side
    return _clamp(cur + (far - cur) * 0.5, spec)


def _propose(con, run_id, params, parent_id, why, method, known, bad, rng,
             space) -> dict:
    """Last-resort dedup guard. The directed stages already walk their ranked moves
    for an untried design, so reaching the jitter below means direction is exhausted."""
    template = get_run(con, run_id)["netlist_template"]
    tried = []
    for attempt in range(48):
        p, fp = _fp_of(template, params)
        if fp not in known and fp not in bad:
            return {"params": p, "parent_id": parent_id, "rationale": why,
                    "method": method, "attempts": attempt + 1,
                    "duplicate_avoided": tried[:3]}
        tried.append(dict(p))
        # collision: nudge every free parameter within bounds, seeded. Discrete lists
        # resample instead of scaling -- see _other_value.
        params = {n: (rng.choice(s["values"]) if "values" in s
                      else _clamp(p[n] * (1 + rng.uniform(-0.15, 0.15)), s))
                  for n, s in space.items()}
    return {"params": None, "parent_id": parent_id, "method": "exhausted",
            "rationale": "parameter space appears exhausted at this resolution;"
                         " widen param_space or lower step_frac",
            "attempts": 48}


# --- rollback (spec S6) --------------------------------------------------------

def rollback(con, run_id: str, candidate_id: str) -> dict:
    """Restores the exact stored netlist -- byte-for-byte, not reconstructed."""
    d = get_candidate(con, candidate_id)
    if d["run_id"] != run_id:
        raise EngineError(f"{candidate_id} belongs to run {d['run_id']!r}")
    with db.transaction(con):
        con.execute("UPDATE runs SET current_candidate=? WHERE run_id=?",
                    (candidate_id, run_id))
    path = db.state_root(_project(con)) / "candidates" / f"{candidate_id}.cir"
    path.write_text(d["netlist"], encoding="utf-8")
    e = con.execute("SELECT * FROM experiments WHERE candidate_id=? AND score IS NOT NULL"
                    " ORDER BY score DESC LIMIT 1", (candidate_id,)).fetchone()
    return {"run_id": run_id, "current_candidate": candidate_id,
            "params": db.jloads(d, "params"), "netlist_path": str(path),
            "netlist_sha": d["design_fp"], "generation": d["generation"],
            "parent_id": d["parent_id"],
            "restored_result": _experiment_summary(e) if e else None}


# --- the local loop + compact summary (spec S14, S19) -------------------------

def _best_score(con, run_id: str) -> float:
    r = con.execute("SELECT MAX(score) s FROM experiments WHERE run_id=? AND"
                    " status='ok'", (run_id,)).fetchone()
    return r["s"] if r and r["s"] is not None else float("-inf")


def optimize(con, run_id: str, iterations: int = 10,
             step_frac: float = STEP_FRAC) -> dict:
    """Run many experiments locally, return one compact summary. No LLM round-trips.

    The step shrinks on stagnation (compass search). A fixed fraction of each
    parameter's range can bracket an optimum but never enter a tolerance window
    narrower than one step -- 25% of a 500..20k range is 4.9k, so a "10 kHz +-5%"
    constraint stays unreachable no matter how many iterations you spend, and the run
    reports a confident near-miss. Best score is monotone by construction, so it is a
    sound stagnation signal for both feasibility and objective progress.
      ponytail: fixed halving after 3 misses, floor at range/64. skipped: a real line
      search / trust region -- add when a simulation is slow enough that the extra
      evaluations spent bracketing cost more than the bookkeeping.
    """
    run = get_run(con, run_id)
    if run["status"] != "running":
        raise EngineError(f"run {run_id!r} is {run['status']}; nothing to do")
    done, skipped, stall = 0, 0, 0
    frac, prev = step_frac, _best_score(con, run_id)
    for _ in range(max(1, iterations)):
        if get_run(con, run_id)["status"] != "running":
            break
        nxt = select_next(con, run_id, frac)
        if not nxt["params"]:
            break
        r = evaluate(con, run_id, nxt["params"], nxt["parent_id"], origin="engine")
        done += 1
        skipped += int(bool(r.get("cached")))
        cur = _best_score(con, run_id)
        if cur > prev:
            prev, stall, frac = cur, 0, step_frac
        elif (stall := stall + 1) >= 3:
            stall, frac = 0, max(frac / 2, step_frac / 64)
    return {"iterations_run": done, "cached_hits": skipped,
            "step_frac_final": frac, **summary(con, run_id)}


def summary(con, run_id: str) -> dict:
    """Spec S14: only what the next decision needs. Full history stays in the DB."""
    run = get_run(con, run_id)
    counts = con.execute(
        "SELECT COUNT(*) n, COUNT(DISTINCT candidate_id) c,"
        " SUM(status!='ok') f FROM experiments WHERE run_id=?", (run_id,)).fetchone()
    best = get_best(con, run_id)
    pick = best["best_feasible"] or best["best"]
    sens = sensitivity(con, run_id)[:1]
    obs = None
    if sens:
        s = sens[0]
        verb = "increases" if s["direction"] == "positive" else "decreases"
        obs = (f"{s['parameter']} {verb} {s['metric']} "
               f"({s['estimated_sensitivity']:.4g}/unit, {s['method']},"
               f" conf {s['confidence']})")
    region = None
    if sens:
        g = explored(con, run_id, sens[0]["parameter"])[sens[0]["parameter"]]
        gap = g["largest_unexplored_gap"]
        if gap and "low" in gap:
            region = f"{sens[0]['parameter']} = {gap['low']:g}..{gap['high']:g}"

    # A bounded search reports its optimum without saying whether the optimum is the
    # circuit's or the box's. Pinned parameters mean the answer is "widen param_space",
    # which is the single most common thing to miss when reading a converged run.
    pinned = []
    if pick:
        space = db.jloads(run, "param_space")
        params = db.jloads(get_candidate(con, pick["candidate_id"]), "params")
        for name, v in params.items():
            spec = space.get(name) or {}
            edges = (spec["values"] if "values" in spec
                     else [spec.get("min"), spec.get("max")])
            edges = [float(e) for e in edges if e is not None]
            if not edges:
                continue
            if _sig(v) == _sig(min(edges)):
                pinned.append(f"{name}={v:g} (at min)")
            elif _sig(v) == _sig(max(edges)):
                pinned.append(f"{name}={v:g} (at max)")
    if len(pinned) > 4:                       # keep the summary bounded, say what's cut
        pinned = pinned[:4] + [f"+{len(pinned) - 4} more at bounds"]
    return {
        "run_id": run_id, "status": run["status"],
        "iterations_evaluated": counts["n"], "unique_candidates": counts["c"],
        "failures": counts["f"] or 0,
        "current_candidate": run["current_candidate"],
        "best_candidate": pick["candidate_id"] if pick else None,
        "best_metrics": pick["metrics"] if pick else None,
        "best_score": pick["score"] if pick else None,
        "best_is_feasible": bool(best["best_feasible"]),
        "constraints": [
            {"constraint": c.get("constraint") or c.get("metric"),
             "required": c.get("required"), "actual": c.get("actual"),
             "passed": c.get("passed")}
            for c in (pick["constraints"] if pick else [])],
        "pareto_size": len(pareto(con, run_id)),
        "useful_observation": obs,
        "promising_unexplored_region": region,
        "at_param_space_bound": pinned or None,
    }


def stop(con, run_id: str) -> dict:
    with db.transaction(con):
        con.execute("UPDATE runs SET status='stopped' WHERE run_id=?", (run_id,))
    return summary(con, run_id)

