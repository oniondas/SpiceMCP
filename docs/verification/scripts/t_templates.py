"""The five ready-made circuits, through the MCP tools: start_from_template ->
run_optimization -> finish_design (with yield). Usage: t_templates.py BACKEND BOARDS"""
import asyncio, json, sys, tempfile, time
from mcpc import OUT, call, session
SPECS = {"rc_lowpass": {"fc": 1000}, "sallen_key_lowpass": {"fc": 10000},
         "noninverting_amp": {"gain": 10, "bw": 100e3},
         "ldo": {"vin": 5, "vout": 3.3, "iload": 0.1},
         "buck": {"vin": 12, "vout": 5, "iout": 1}}
SUP = {"ldo": "Vin", "buck": "Vin"}
BACKEND, N = sys.argv[1], int(sys.argv[2])
ONLY = sys.argv[3:] or list(SPECS)

async def go(s):
    out = {}
    for name in ONLY:
        t0, r = time.time(), {}
        r["start"] = await call(s, "start_from_template", run_id=name, template=name,
                                spec=SPECS[name], sim_options={"backend": BACKEND})
        r["opt"] = await call(s, "run_optimization", run_id=name, iterations=20)
        sup = SUP.get(name, "")
        r["finish"] = await call(s, "finish_design", run_id=name, yield_runs=N, supply=sup,
                                 supply_tol_pct=5 if sup else 0)
        r["secs"] = round(time.time() - t0)
        out[name] = r
        print(name, r["secs"], "s; start meets spec:",
              r["start"].get("start_result", {}).get("feasible"),
              "; yield:", (r["finish"].get("yield") or {}).get("yield_pct"), flush=True)
    return out

res = asyncio.run(session(tempfile.mkdtemp(), go))
(OUT / f"templates_{BACKEND}_{N}.json").write_text(json.dumps(res, indent=1, default=str))
