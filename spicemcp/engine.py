"""Optimization engine. All state lives in SQLite; nothing is remembered by the LLM.

Covers spec S3-S19. Scores are a *pure function* of a candidate's metrics (fixed
per-objective `ref` scales, fixed penalty), so a score never changes retroactively
and the best candidate can never be demoted by a later, worse iteration.
"""

from __future__ import annotations

import json
import math
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
              notes: str = "", preflight: dict | None = None) -> dict:
    """Create a run, refusing one whose spec cannot be met by any topology.

    `preflight` = {"bypass": bool, "circuit_type": ..., "mode": ..., "technology_params":
    {...}} -- see `feasibility.check`. A hard violation returns an error instead of
    creating the run, because the alternative is N simulations of a spec with no solution.
    Warnings ride along in the answer and block nothing.
    """
    if con.execute("SELECT 1 FROM runs WHERE run_id=?", (run_id,)).fetchone():
        raise EngineError(f"run {run_id!r} already exists (state is persistent)")
    cons = _norm_constraints(constraints)
    objs = _norm_objectives(objectives, cons)
    for name, spec in param_space.items():
        if "values" not in spec and not {"min", "max"} <= set(spec):
            raise EngineError(f"param {name!r} needs min+max or values")
    cfg = dict(sim_config or {})
    cfg.setdefault("backend", "ltspice")

    pre = dict(preflight or {})
    bypass = bool(pre.pop("bypass", False))
    ctype = pre.pop("circuit_type", "general")
    pmode = pre.pop("mode", "practical")
    tech = pre.pop("technology_params", None)
    if pre:
        raise EngineError(f"unknown preflight keys {sorted(pre)}; expected bypass, "
                          f"circuit_type, mode, technology_params")
    verdict = None
    # The analytic backend has no netlist semantics at all -- its metrics come from a
    # registered Python function, so `.meas` directives and placeholders mean nothing
    # there and every template check would be a false positive.
    if not bypass and cfg.get("backend") in ("ltspice", "ngspice"):
        from . import feasibility               # imported here: feasibility imports engine
        verdict = feasibility.check(objectives, constraints, param_space,
                                    netlist_template, circuit_type=ctype, mode=pmode,
                                    technology_params=tech)
        if not verdict.passed:
            return {"error": f"run not created: {len(verdict.errors)} requirement(s) "
                             f"cannot be met by any topology. Fix the spec, or pass "
                             f"preflight={{'bypass': true}} to proceed anyway.",
                    "feasibility": verdict.to_dict()}
    with db.transaction(con):
        con.execute(
            "INSERT INTO runs (run_id, created_at, status, netlist_template,"
            " param_space, objectives, constraints, sim_config, seed, notes)"
            " VALUES (?,?,'running',?,?,?,?,?,?,?)",
            (run_id, db.now(), netlist_template, db.jdumps(param_space),
             db.jdumps(objs), db.jdumps(cons), db.jdumps(cfg), int(seed), notes))
    out = {"run_id": run_id, "objectives": objs, "constraints": cons,
           "parameters": sorted(param_space), "seed": seed}
    if verdict is not None and (verdict.violations or verdict.checks_skipped):
        out["feasibility"] = verdict.to_dict()
    return out


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
    return evaluate_many(con, run_id, [params], parent_id, sim_config, force, origin)[0]


def evaluate_many(con, run_id: str, params_list: list[dict], parent_id: str | None = None,
                  sim_config: dict | None = None, force: bool = False,
                  origin: str = "llm") -> list[dict]:
    """Evaluate several designs: candidates are created and cache-checked one by one,
    the simulations run side by side (`sim.workers()`), and the results are recorded in
    the order given, each in its own transaction. The same design twice in one batch is
    simulated once."""
    run = get_run(con, run_id)
    cfg = {**db.jloads(run, "sim_config"), **(sim_config or {})}
    objs, cons = db.jloads(run, "objectives", []), db.jloads(run, "constraints", [])
    cfp = ir.config_fp(cfg)
    root = db.state_root(_project(con))

    plan, todo = [], {}                          # per input: cached answer or a cid
    for params in params_list:
        cand = resolve_candidate(con, run_id, params, parent_id, origin=origin)
        cid = cand["candidate_id"]
        design = get_candidate(con, cid)
        efp = ir.experiment_fp(design["design_fp"], cfp)
        cached = con.execute(
            "SELECT * FROM experiments WHERE run_id=? AND experiment_fp=?"
            " ORDER BY iteration DESC LIMIT 1", (run_id, efp)).fetchone()
        if cached and not force:
            plan.append({"cached": True, "candidate_id": cid,
                         "previous_iteration": cached["iteration"],
                         **_experiment_summary(cached)})
            continue
        if cid not in todo:
            todo[cid] = {"params": params, "netlist": design["netlist"], "efp": efp,
                         "created": cand["created"]}
        plan.append(cid)

    # Simulation runs OUTSIDE the write transaction -- holding a SQLite write lock
    # for the duration of an LTspice run would serialise every other reader.
    started = db.now()
    cids = list(todo)
    sims = sim.parallel_map(
        lambda c: sim.simulate(todo[c]["netlist"], todo[c]["params"], cfg,
                               root / "simulations" / run_id, c), cids)
    done = {}
    for c, res in zip(cids, sims):
        done[c] = _record(con, run_id, c, todo[c], res, cfg, cfp, objs, cons, started)
    return [p if isinstance(p, dict) else dict(done[p]) for p in plan]


def _record(con, run_id, cid, job, res, cfg, cfp, objs, cons, started) -> dict:
    params = job["params"]
    metrics = {k.lower(): v for k, v in res.metrics.items()}
    if res.status == "ok":
        # `param_<name>` lets a goal or limit price a part directly ("smallest C1",
        # "W under 2 mm") -- size and cost are real objectives with no .meas to read.
        wanted = {o["metric"] for o in objs} | {c["metric"] for c in cons}
        for k, v in params.items():
            if f"param_{k.lower()}" in wanted:
                metrics[f"param_{k.lower()}"] = float(v)
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
            (run_id, cid, it, db.jdumps(cfg), cfp, job["efp"], res.status,
             db.jdumps(metrics), db.jdumps(cres), int(feasible), sc, ftype, fmsg,
             db.jdumps(fdetail) if fdetail else None,
             db.jdumps(res.warnings) if res.warnings else None, res.log_path,
             started, db.now()))
        con.execute("UPDATE runs SET current_candidate=? WHERE run_id=?", (cid, run_id))

    _update_sensitivities(con, run_id)
    best = get_best(con, run_id)
    return {"cached": False, "candidate_id": cid, "iteration": it,
            "new_candidate": job["created"], "status": res.status,
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


LOG_RATIO = 10.0     # scale="auto" searches a range in log space once max/min >= this


def is_log(spec: dict) -> bool:
    """Search this parameter in log space?

    Component values are ratio quantities: 1k -> 2k matters as much as 100k -> 200k.
    A linear step of 25% of a 1k..1M range is 250k, so the whole bottom decade (where
    most real answers live) is crossed in a single step and never sampled. Searching
    log(value) gives every decade the same share of the budget, as every sizing tool
    does. `auto` (the default) picks log for any positive range spanning 10x or more.
    """
    if "values" in spec:
        return False
    lo, hi = float(spec["min"]), float(spec["max"])
    mode = spec.get("scale", "auto")
    if mode == "linear" or lo <= 0:
        return False
    return mode == "log" or hi / lo >= LOG_RATIO


def _to_unit(spec: dict, v: float) -> float:
    """Position of `v` inside a continuous range, 0..1, in the search's own scale."""
    lo, hi = float(spec["min"]), float(spec["max"])
    if hi == lo:
        return 0.0
    if is_log(spec):
        return math.log(max(v, lo) / lo) / math.log(hi / lo)
    return (v - lo) / (hi - lo)


def _from_unit(spec: dict, u: float) -> float:
    lo, hi = float(spec["min"]), float(spec["max"])
    u = max(0.0, min(1.0, u))
    if is_log(spec):
        return lo * (hi / lo) ** u
    return lo + (hi - lo) * u


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
    # widest in the search's own scale: in log space 1k..10k is as wide as 10k..100k
    a, b = max(zip(edges, edges[1:]), key=lambda t: _to_unit(spec, t[1]) - _to_unit(spec, t[0]))
    frac = _to_unit(spec, b) - _to_unit(spec, a)
    if b - a <= 0:
        return None
    mid = _from_unit(spec, (_to_unit(spec, a) + _to_unit(spec, b)) / 2)
    return {"low": a, "high": b, "width": b - a, "width_frac": frac, "midpoint": mid,
            "scale": "log" if is_log(spec) else "linear"}


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

    # (0) global sample first: a space-filling Latin-hypercube spread over the whole
    # box (log-spaced where the range is), so the local search starts from the best
    # basin found rather than from wherever one random seed landed. Local descent from
    # a single start can only find the optimum nearest to it.
    # Skipped once the caller evaluated a design of its own: a hand-sized start point
    # is better information than a blind sample, so refine from it instead.
    k = n_global_samples(space)
    own = con.execute("SELECT 1 FROM designs WHERE run_id=? AND origin!='engine' LIMIT 1",
                      (run_id,)).fetchone()
    if n_designs < k and not own:
        params = lhs_point(space, run["seed"], n_designs, k)
        return _propose(con, run_id, params, None,
                        f"global sample {n_designs + 1}/{k}: spreading points over the "
                        f"whole range before refining the best one", "global_sample",
                        known, bad, rng, space)

    best = get_best(con, run_id)
    base_row = best["best_feasible"] or _least_violating(con, run_id, cons)
    if best["best_feasible"]:
        base_row = _repair_target(con, run_id, best["best_feasible"], objs, cons) or base_row
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
    failing = sorted((c for c in base_row.get("constraints", []) if not c.get("passed")),
                     key=_rel_miss, reverse=True)
    if failing:
        incumbent = best["best_feasible"]
        frozen = set()
        if incumbent and incumbent["candidate_id"] != base_id:
            inc = db.jloads(get_candidate(con, incumbent["candidate_id"]), "params")
            frozen = {n for n in base if inc.get(n) != base[n]}   # the knob just pushed
        points = _ok_points(con, run_id)
        hit = _newton_repair(base, space, cons, base_row["metrics"], points, frozen)
        if hit:
            params, why = hit
            p, fp = _fp_of(template, params)
            if fp not in known and fp not in bad:
                return _propose(con, run_id, p, base_id, why, "newton_repair",
                                known, bad, rng, space)
    for c in failing:
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
                gain = _leverage(s, space[name], base[name]) * o["weight"] / o["ref"]
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
        g, name = max(gaps, key=lambda t: t[0]["width_frac"])
        params = dict(base)
        params[name] = g["midpoint"]
        return _propose(con, run_id, params, base_id,
                        f"{name} is unexplored between {g['low']:g} and {g['high']:g}",
                        "explore_gap", known, bad, rng, space)

    params = {n: _seed_value(s, rng) for n, s in space.items()}
    return _propose(con, run_id, params, base_id, "no signal yet: random probe",
                    "random", known, bad, rng, space)


NEWTON_MAX_FACTOR = 2.0     # largest single-move change of one part, as a ratio
REPAIR_MAX_VIOLATION = 20.0   # relative miss small enough to be worth repairing


def _objective_only(metrics: dict, objs: list[dict]) -> float | None:
    s = score(metrics, objs, [])
    return s


def _repair_target(con, run_id: str, incumbent: dict, objs: list[dict],
                   cons: list[dict]) -> dict | None:
    """The latest experiment, if it beat the incumbent's objective but broke a limit
    by a little. Repairing it is how the search slides ALONG a constraint.

    Coordinate moves from the best feasible point cannot follow a coupled limit: on an
    RC filter, fc = 1/(2*pi*R*C), so shrinking C alone always breaks the fc window and
    the run sits on its first feasible point forever. "Push one knob, then repair the
    limit with another" is the zig-zag that walks the fc line toward the smaller C.
    """
    last = con.execute("SELECT * FROM experiments WHERE run_id=? ORDER BY iteration DESC"
                       " LIMIT 1", (run_id,)).fetchone()
    if not last or last["status"] != "ok" or last["feasible"] or last["score"] is None:
        return None
    m = db.jloads(last, "metrics")
    viol = _violation(m, cons)
    better = (_objective_only(m, objs) or float("-inf")) > (
        _objective_only(incumbent["metrics"], objs) or float("-inf"))
    if better and 0 < viol <= REPAIR_MAX_VIOLATION:
        return _with_id(last)
    return None


def _log_metric(points, metric: str) -> bool:
    """Model this metric in log space? Yes when every value seen is positive (fc, Q,
    vout); no for dB and other signed quantities, which are already logarithmic."""
    vals = [m[metric] for _, m in points if m.get(metric) is not None]
    return bool(vals) and all(v > 0 for v in vals)


def _elasticity(points, param: str, metric: str, log_m: bool = True) -> float | None:
    """d ln(metric) / d ln(param) (or d metric / d ln(param) for a dB metric), median
    over clean finite-difference pairs.

    Most circuit laws are power laws in their component values (fc ~ 1/RC, gain ~
    R2/R1, SR ~ I/C), and a power law's log-log slope is the same everywhere. So one
    clean pair anywhere in the space predicts the move needed here, which a linear
    per-ohm slope cannot do across decades.
    """
    usable = [(p, m) for p, m in points
              if p.get(param, 0) > 0 and m.get(metric) is not None
              and (m[metric] > 0 or not log_m)]
    es = []
    for i, (pi, mi) in enumerate(usable):
        for pj, mj in usable[i + 1:]:
            if pi[param] == pj[param] or any(
                    pi.get(k) != pj.get(k) for k in set(pi) | set(pj) if k != param):
                continue
            dm = (math.log(mj[metric] / mi[metric]) if log_m
                  else mj[metric] - mi[metric])
            es.append(dm / math.log(pj[param] / pi[param]))
    return statistics.median(es) if es else None


def _window(cons: list[dict], metric: str) -> tuple[float | None, float | None]:
    lo = max((c["value"] for c in cons if c["metric"] == metric and c["op"] in (">", ">=")),
             default=None)
    hi = min((c["value"] for c in cons if c["metric"] == metric and c["op"] in ("<", "<=")),
             default=None)
    return lo, hi


def _target(cons: list[dict], metric: str, cur: float, log_m: bool = True) -> float | None:
    """Where a repair should aim: the window centre, or 2% inside a one-sided limit.
    None when the metric is comfortably inside a one-sided limit (leave it free)."""
    lo, hi = _window(cons, metric)
    if lo is not None and hi is not None:
        if log_m:
            return math.sqrt(lo * hi) if lo > 0 else None
        return (lo + hi) / 2
    if lo is not None:
        t = lo * 1.02 if log_m else lo + max(0.02 * abs(lo), 1e-3)
        return t if (not log_m or lo > 0) and cur < t else None
    if hi is not None:
        t = hi * 0.98 if log_m else hi - max(0.02 * abs(hi), 1e-3)
        return t if (not log_m or hi > 0) and cur > t else None
    return None


def _solve(a: list[list[float]], b: list[float]) -> list[float] | None:
    """Gaussian elimination with partial pivoting; None if singular. Pure Python so the
    engine keeps its no-numpy import."""
    n = len(b)
    m = [row[:] + [b[i]] for i, row in enumerate(a)]
    for col in range(n):
        piv = max(range(col, n), key=lambda r: abs(m[r][col]))
        if abs(m[piv][col]) < 1e-12:
            return None
        m[col], m[piv] = m[piv], m[col]
        for r in range(n):
            if r != col:
                f = m[r][col] / m[col][col]
                m[r] = [x - f * y for x, y in zip(m[r], m[col])]
    return [m[i][n] / m[i][i] for i in range(n)]


def _newton_repair(base: dict, space: dict, cons: list[dict], metrics: dict, points,
                   frozen: set) -> tuple[dict, str] | None:
    """One joint move that puts every limited metric back in its window.

    Linear in log space: d ln(metric) = sum_j E[metric][j] * d ln(param_j), with E the
    measured log elasticities. Several coupled limits (a Sallen-Key's fc AND Q) need a
    joint solve: fixing fc with one knob and then Q with another just undoes fc. The
    minimum-norm solution moves as little as possible; knobs the last push touched are
    weighted down so the repair continues the slide instead of reversing it.
    """
    names = [n for n, sp in space.items()
             if "values" not in sp and n in base and base[n] > 0 and is_log(sp)]
    rows, rhs, used = [], [], []
    for met in sorted({c["metric"] for c in cons}):
        cur = metrics.get(met)
        log_m = _log_metric(points, met)
        if cur is None or (log_m and cur <= 0):
            continue
        tgt = _target(cons, met, cur, log_m)
        if tgt is None:
            continue
        e = [_elasticity(points, n, met, log_m) or 0.0 for n in names]
        if not any(abs(x) > 0.05 for x in e):
            continue
        rows.append(e)
        rhs.append(math.log(tgt / cur) if log_m else tgt - cur)
        used.append(met)
    if not rows or not any(abs(x) > 1e-3 for x in rhs):
        return None
    free = [k for k, n in enumerate(names) if n not in frozen]
    if frozen and not any(abs(r[k]) > 0.05 for r in rows for k in free):
        return None   # only the pushed knob is measured: repairing would just undo it
    w = [0.05 if n in frozen else 1.0 for n in names]
    # Active set: a knob pinned at a bound cannot deliver its share of the step, so
    # drop it and re-solve with the rest -- otherwise the same unreachable move is
    # proposed again and again with a few ppm of difference.
    for _ in range(len(names)):
        gram = [[sum(ri[k] * w[k] * rj[k] for k in range(len(names)))
                 + (1e-6 if i == j else 0) for j, rj in enumerate(rows)]
                for i, ri in enumerate(rows)]
        y = _solve(gram, rhs)
        if y is None:
            return None
        step = [w[k] * sum(rows[i][k] * y[i] for i in range(len(rows)))
                for k in range(len(names))]
        # Trust region: elasticities measured elsewhere are only a local model (a
        # filter's passband droop is not a power law in R), so never move a part by
        # more than NEWTON_MAX_FACTOR in one go. Scaling the whole step keeps its
        # direction; the next repair continues from wherever this one lands.
        big = max((abs(d) for d in step), default=0.0)
        if big > math.log(NEWTON_MAX_FACTOR):
            step = [d * math.log(NEWTON_MAX_FACTOR) / big for d in step]
        params = dict(base)
        pinned = []
        for k, (n, d) in enumerate(zip(names, step)):
            want = base[n] * math.exp(d)
            params[n] = _clamp(want, space[n])
            if w[k] and abs(math.log(params[n] / want)) > 1e-6:
                pinned.append(k)
        if not pinned:
            break
        for k in pinned:
            w[k] = 0.0
        if not any(w):
            break
    if all(abs(math.log(params[n] / base[n])) < 1e-3 for n in names):
        return None
    moved = ", ".join(f"{n} -> {params[n]:.4g}" for n in names if params[n] != base[n])
    return params, (f"solve for {', '.join(used)} inside their limits using measured log "
                    f"elasticities: {moved}")


def n_global_samples(space: dict) -> int:
    """Size of the opening global sample: two points per parameter, 4..12."""
    return min(12, max(4, 2 * len(space)))


def lhs_point(space: dict, seed: int, i: int, k: int) -> dict:
    """Point `i` of a seeded k-point Latin hypercube over `space` (deterministic).

    Each parameter's range is cut into k equal slices in its search scale, and every
    slice is used exactly once across the k points, so no decade is left unvisited.
    Recomputed from the seed on every call: nothing to store, nothing to drift.
    """
    rng = random.Random(seed * 7919 + 17)
    out = {}
    for name in sorted(space):
        perm = rng.sample(range(k), k)
        jitter = [rng.random() for _ in range(k)]
        u = (perm[i] + jitter[i]) / k
        spec = space[name]
        if "values" in spec:
            vals = sorted(spec["values"])
            out[name] = vals[min(int(u * len(vals)), len(vals) - 1)]
        else:
            out[name] = _from_unit(spec, u)
    return out


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
    sign = 1.0 if up else -1.0
    return _clamp(_from_unit(spec, _to_unit(spec, cur) + sign * frac), spec)


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


def _leverage(s: dict, spec: dict, base_val: float | None = None) -> float:
    """How much of `metric` a full-range move of this parameter buys, times confidence.

    Ranking on raw d(metric)/d(param) is meaningless across parameters, because it
    carries the parameter's units: a capacitor's sensitivity is per farad, so it
    outranks a resistor's per-ohm figure by ~1e8 regardless of which knob actually
    does more. Multiplying by the range makes the comparison scale-free -- the real
    question is what one move can achieve, not what one farad could.

    For a log-scaled parameter the full move is a factor, not a distance, so the
    local estimate is slope * value * ln(max/min): what sweeping every decade buys.
    """
    if base_val and is_log(spec):
        reach = abs(base_val) * math.log(float(spec["max"]) / float(spec["min"]))
        return abs(s["estimated_sensitivity"]) * reach * s["confidence"]
    return abs(s["estimated_sensitivity"]) * (_span(spec) or 1.0) * s["confidence"]


def _ranked_for(sens: dict, space: dict, metric: str, want_up: bool, base: dict):
    """Parameters that can still move `metric` the helpful way, most leverage first.

    Zero leverage is excluded: a parameter measured to have no effect on this metric
    cannot repair it, and with coordinate rotation it would otherwise consume a turn.
    """
    cands = [(_leverage(s, space[n], base.get(n)), n, s)
             for (n, m), s in sens.items()
             if m == metric and n in space and s["confidence"] > 0
             and n in base
             and _leverage(s, space[n], base.get(n)) > 0
             and _can_move(base[n], space[n], s["estimated_sensitivity"], want_up)]
    return [(n, s) for _, n, s in sorted(cands, reverse=True, key=lambda t: t[0])]


def _seed_value(spec: dict, rng: random.Random):
    if "values" in spec:
        return rng.choice(spec["values"])
    return _from_unit(spec, rng.random())


def _other_value(spec: dict, cur: float, rng: random.Random) -> float:
    """A value deliberately different from `cur`.

    For a discrete list this must pick a different entry, not scale `cur`: component
    lists are decade-spaced, and +-15% of 1e-8 snaps straight back to 1e-8.
    """
    if "values" in spec:
        others = [v for v in spec["values"] if v != cur]
        return rng.choice(others) if others else cur
    # A screening probe measures a local slope, so it stays local: 15% of the range
    # (in the search's own scale) toward the roomier side. A big jump from a good
    # hand-sized start throws the start away and measures a slope that does not hold
    # where the design actually is.
    u = _to_unit(spec, cur)
    return _clamp(_from_unit(spec, u + (0.15 if 1.0 - u > u else -0.15)), spec)


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
    from . import progress
    done, skipped, stall = 0, 0, 0
    frac, prev = step_frac, _best_score(con, run_id)
    stopped_early = False
    progress.report(0, max(1, iterations), "searching")
    for _ in range(max(1, iterations)):
        if get_run(con, run_id)["status"] != "running":
            break
        if done and progress.out_of_time():     # always make progress: >= 1 iteration
            stopped_early = True
            break
        nxt = select_next(con, run_id, frac)
        if not nxt["params"]:
            break
        batch = [nxt["params"]] + _independent_moves(
            con, run_id, nxt, min(sim.workers(), max(1, iterations) - done) - 1)
        rs = evaluate_many(con, run_id, batch, nxt["parent_id"], origin="engine")
        r = rs[0]
        done += len(rs)
        skipped += sum(bool(x.get("cached")) for x in rs[1:])
        progress.report(done, None, f"{done} of {max(1, iterations)} simulations")
        skipped += int(bool(r.get("cached")))
        cur = _best_score(con, run_id)
        if cur > prev:
            prev, stall, frac = cur, 0, step_frac
        elif (stall := stall + 1) >= 3:
            stall, frac = 0, max(frac / 2, step_frac / 64)
    out = {"iterations_run": done, "cached_hits": skipped,
           "step_frac_final": frac, **summary(con, run_id)}
    if stopped_early:
        out["stopped_early"] = (f"time limit reached after {done} of {iterations} "
                                f"iterations; call again to continue")
    return out


def _independent_moves(con, run_id: str, nxt: dict, n: int) -> list[dict]:
    """More proposals that do not depend on `nxt`'s result, to simulate alongside it.

    Only two stages qualify: the global Latin-hypercube samples (fixed in advance) and
    the one-at-a-time screening of parameters never varied (each from the same base).
    Every later stage reads the previous result, so it stays one at a time."""
    if n <= 0 or nxt.get("method") not in ("global_sample", "probe_unvaried"):
        return []
    run = get_run(con, run_id)
    space = db.jloads(run, "param_space")
    if nxt["method"] == "global_sample":
        k = n_global_samples(space)
        have = con.execute("SELECT COUNT(*) FROM designs WHERE run_id=?",
                           (run_id,)).fetchone()[0]
        return [lhs_point(space, run["seed"], i, k) for i in range(have, min(k, have + n))]
    if not nxt.get("parent_id"):
        return []
    base = db.jloads(get_candidate(con, nxt["parent_id"]), "params")
    tried = explored(con, run_id)
    moved = {k for k in nxt["params"] if nxt["params"][k] != base.get(k)}
    rng = random.Random(run["seed"] * 7 + len(tried))
    out = []
    for name, spec in space.items():
        if len(out) >= n:
            break
        if name in moved or name not in base or \
                len(tried.get(name, {}).get("evaluated", [])) > 1:
            continue
        alt = _other_value(spec, base[name], rng)
        if alt != base[name]:
            out.append({**base, name: alt})
    return out


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

