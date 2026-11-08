"""Benchmark the optimizer on two closed-form filter problems; print JSON.

Run against any checkout:  PYTHONPATH=<checkout> python bench.py
Closed-form (analytic backend) so the comparison is about the search, not the simulator.
"""
from __future__ import annotations

import json
import math
import sys
import tempfile
from pathlib import Path

from spicemcp import db, engine, sim

TWO_PI = 2 * math.pi


@sim.register_analytic("rc")
def _rc(p):
    return {"fc": 1 / (TWO_PI * p["R1"] * p["C1"]), "c_total_nf": p["C1"] * 1e9}


@sim.register_analytic("sallen_key")
def _sk(p):
    r1, r2, c1, c2 = p["R1"], p["R2"], p["C1"], p["C2"]
    w = 1 / math.sqrt(r1 * r2 * c1 * c2)
    q = math.sqrt(r1 * r2 * c1 * c2) / (c2 * (r1 + r2))
    return {"fc": w / TWO_PI, "q": q, "c_total_nf": (c1 + c2) * 1e9}


PROBLEMS = {
    "rc": dict(
        title="RC low-pass: fc = 1 kHz +-2%, R >= 1k, smallest C",
        space={"R1": {"min": 10.0, "max": 1e6}, "C1": {"min": 1e-9, "max": 10e-6}},
        cons=[{"metric": "fc", "op": ">=", "value": 980},
              {"metric": "fc", "op": "<=", "value": 1020}],
        objs=[{"metric": "c_total_nf", "direction": "min", "ref": 1.0}]),
    "sallen_key": dict(
        title="Sallen-Key low-pass: fc = 10 kHz +-3%, Q = 0.707 +-5%, smallest C",
        space={"R1": {"min": 100.0, "max": 1e6}, "R2": {"min": 100.0, "max": 1e6},
               "C1": {"min": 100e-12, "max": 1e-6}, "C2": {"min": 100e-12, "max": 1e-6}},
        cons=[{"metric": "fc", "op": ">=", "value": 9700},
              {"metric": "fc", "op": "<=", "value": 10300},
              {"metric": "q", "op": ">=", "value": 0.672},
              {"metric": "q", "op": "<=", "value": 0.742}],
        objs=[{"metric": "c_total_nf", "direction": "min", "ref": 1.0}]),
}


def run(name: str, seed: int, budget: int) -> dict:
    pb = PROBLEMS[name]
    con = db.connect(Path(tempfile.mkdtemp()))
    engine.start_run(con, "b", "* bench\n", pb["space"], pb["objs"], pb["cons"],
                     {"backend": "analytic", "model": name}, seed)
    engine.optimize(con, "b", budget)
    rows = con.execute(
        "SELECT e.iteration, e.feasible, e.metrics, d.params FROM experiments e JOIN"
        " designs d ON d.candidate_id=e.candidate_id WHERE e.run_id='b'"
        " ORDER BY e.iteration").fetchall()
    best, curve, first = None, [], None
    for r in rows:
        m = json.loads(r["metrics"])
        if r["feasible"]:
            first = first or r["iteration"]
            v = m["c_total_nf"]
            best = v if best is None else min(best, v)
        curve.append(best)
    return {"first_feasible": first, "best": best, "curve": curve,
            "points": [json.loads(r["params"]) for r in rows]}


if __name__ == "__main__":
    budget, seeds = int(sys.argv[1]), int(sys.argv[2])
    out = {n: [run(n, s, budget) for s in range(seeds)] for n in PROBLEMS}
    print(json.dumps(out))
