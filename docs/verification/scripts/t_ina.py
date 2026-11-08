"""Usage: t_ina.py BACKEND BOARDS.
Design a 3-op-amp instrumentation amplifier end to end, only through SpiceMCP's MCP tools."""
import asyncio, json, re, sys
import tempfile
from mcpc import OUT, call, session
from ina_netlist import ina
BACKEND = sys.argv[1] if len(sys.argv) > 1 else "ngspice"
N = int(sys.argv[2]) if len(sys.argv) > 2 else 40
LOG = []
async def c(s, name, **a):
    r = await call(s, name, **a); LOG.append({"tool": name, "args": a, "answer": r}); return r
async def go(s):
    so = {"backend": BACKEND}
    await c(s, "list_templates")
    # 1. borrow the transistor-level op-amp model from a template run
    await c(s, "start_from_template", run_id="oa_src", template="noninverting_amp",
            spec={"gain": 10, "bw": 100e3}, sim_options=so)
    st = await c(s, "query_run", run_id="oa_src", what="status")
    best = await c(s, "query_run", run_id="oa_src", what="best")
    cid = best.get("candidate_id") or best.get("best", {}).get("candidate_id")
    nl = (await c(s, "query_run", run_id="oa_src", what="netlist", candidate_id=cid))["netlist"]
    oa = re.search(r"^\.subckt SPMCP_OA.*?^\.ends SPMCP_OA\n", nl, re.S | re.M).group(0)
    net = ina(oa)
    params = [{"name": n, "min": 1e3, "max": 100e3} for n in ("R1A", "R1B", "R2A", "R2B", "R3A", "R3B")]
    params.append({"name": "RG", "min": 20, "max": 2e3})
    limits = [{"metric": "gain_db", "min": 39.9, "max": 40.1},
              {"metric": "bw", "min": 20e3},
              {"metric": "cm_db", "max": -40},      # CMRR >= 80 dB at 60 Hz
              {"metric": "pm_deg", "min": 45}]
    goals = [{"metric": "bw", "direction": "max"}]
    await c(s, "check_feasibility", goals=goals, limits=limits, params=params, netlist=net,
            circuit_type="opamp")
    await c(s, "start_optimization", run_id="ina", netlist=net, params=params, goals=goals,
            limits=limits, circuit_type="opamp", sim_options=so)
    hand = {"R1A": 10e3, "R1B": 10e3, "RG": 200, "R2A": 10e3, "R2B": 10e3, "R3A": 10e3, "R3B": 10e3}
    await c(s, "evaluate_candidate", run_id="ina", params=hand)
    await c(s, "run_optimization", run_id="ina", iterations=30)
    await c(s, "query_run", run_id="ina", what="status")
    await c(s, "query_run", run_id="ina", what="best")
    await c(s, "finish_design", run_id="ina", resistors="E96", yield_runs=N)
    await c(s, "finish_design", run_id="ina", resistors="E96", yield_runs=N,
            tolerance_pct={"R": 0.1})
    await c(s, "generate_design_report", run_id="ina", plots=True)
    return LOG
asyncio.run(session(tempfile.mkdtemp(), go))
(OUT / f"ina_{BACKEND}.json").write_text(json.dumps(LOG, indent=1, default=str))
for e in LOG:
    a = e["answer"]; print("==", e["tool"], json.dumps(a, default=str)[:900])
