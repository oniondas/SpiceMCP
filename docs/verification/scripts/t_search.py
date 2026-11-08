"""Search reliability, beyond the step-2 benchmark: (a) the same closed-form problems
with 20 unseen seeds; (b) the Sallen-Key with the transistor-level op-amp in a real
simulator, started from scratch through start_optimization (no template sizing), 10 seeds.
Usage: t_search.py BACKEND"""
import asyncio, json, math, sys, tempfile
from pathlib import Path
ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / "docs/reports/step2"))
from mcpc import OUT, call, session
import bench
from spicemcp import sim, topologies as T
BACKEND = sys.argv[1] if len(sys.argv) > 1 else "ngspice"

res = {"analytic": {}}
for name in ("rc", "sallen_key"):
    runs = [bench.run(name, s, 60) for s in range(100, 120)]
    ff = [r["first_feasible"] for r in runs]
    res["analytic"][name] = {"seeds": "100-119", "found_within_20": sum(1 for f in ff if f and f <= 20),
                             "found_within_60": sum(1 for f in ff if f), "first_feasible": ff}
    print(name, res["analytic"][name])

d = T.get("sallen_key_lowpass").design({"fc": 10000})
net = sim.decks(d.netlist)[0]
params = [{"name": n, "min": 1e3, "max": 1e6} for n in ("R1", "R2")] + \
         [{"name": n, "min": 100e-12, "max": 100e-9} for n in ("C1", "C2")]
limits = [{"metric": "f3db", "min": 9700, "max": 10300}, {"metric": "peak_db", "max": 0.1},
          {"metric": "g_half_db", "min": -0.5}, {"metric": "atten10_db", "max": -39}]

async def go(s):
    out = []
    for seed in range(10):
        rid = f"sk{seed}"
        await call(s, "start_optimization", run_id=rid, netlist=net, params=params,
                   goals=[{"metric": "f3db", "direction": "max", "weight": 0.001}], limits=limits,
                   circuit_type="filter", seed=seed, sim_options={"backend": BACKEND})
        first = None
        for k in range(1, 4):
            st = await call(s, "run_optimization", run_id=rid, iterations=20)
            if st.get("best_is_feasible") and first is None:
                hist = await call(s, "query_run", run_id=rid, what="history", limit=200)
                its = [h for h in hist.get("iterations", []) if h.get("feasible")]
                first = min(h["iteration"] for h in its) if its else k * 20
                break
        out.append({"seed": seed, "first_feasible": first})
        print("sk real", seed, first, flush=True)
    return out

res["real_sallen_key"] = asyncio.run(session(tempfile.mkdtemp(), go))
(OUT / f"search_{BACKEND}.json").write_text(json.dumps(res, indent=1))
