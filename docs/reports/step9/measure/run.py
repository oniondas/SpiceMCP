"""Run each circuit's flow over real MCP stdio and log every request/response text."""
import asyncio, json, os, re, sys, tempfile, time
from pathlib import Path
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

HERE = Path(__file__).parent
REPO = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(REPO / "tests"))
from test_layout import UNSEEN  # the 8 drawing-test netlists, verbatim
SO = {"backend": "ngspice"}
SPECS = {"rc_lowpass": {"fc": 1000}, "sallen_key_lowpass": {"fc": 10000},
         "noninverting_amp": {"gain": 10, "bw": 100e3},
         "ldo": {"vin": 5, "vout": 3.3, "iload": 0.1},
         "buck": {"vin": 12, "vout": 5, "iout": 1}}

class Log:
    def __init__(s): s.calls = []
    async def __call__(s, ses, name, **a):
        t = time.time()
        r = await ses.call_tool(name, a)
        txt = "".join(c.text for c in r.content if hasattr(c, "text"))
        s.calls.append({"tool": name, "args": a, "answer": txt, "secs": round(time.time() - t, 1)})
        try: return json.loads(txt)
        except ValueError: return {"raw": txt}

async def with_server(project, fn):
    p = StdioServerParameters(command=sys.executable, args=[str(HERE / "srv.py")],
                              env={**os.environ, "SPICEMCP_PROJECT": str(project),
                                   "PYTHONPATH": str(REPO)}, cwd=str(project))
    async with stdio_client(p) as (r, w):
        async with ClientSession(r, w) as s:
            init = await s.initialize()
            return await fn(s, init)

def subckt(nl, name):
    return re.search(rf"^\.subckt {name}.*?^\.ends {name}\s*\n", nl, re.S | re.M | re.I).group(0)

async def template_flow(s, L, t, spec, proj):
    await L(s, "list_templates")
    st = await L(s, "start_from_template", run_id=t, template=t, spec=spec, sim_options=SO)
    await L(s, "run_optimization", run_id=t, iterations=20)
    await L(s, "finish_design", run_id=t)
    await L(s, "generate_design_report", run_id=t, plots=True)
    if st.get("schematic_asc"):
        await L(s, "render_schematic", path=st["schematic_asc"], output_path=str(proj / f"{t}.png"))

async def get_netlist(s, L, rid, template, spec):
    await L(s, "start_from_template", run_id=rid, template=template, spec=spec, sim_options=SO)
    b = await L(s, "query_run", run_id=rid, what="best")
    cid = b.get("candidate_id") or (b.get("best") or {}).get("candidate_id")
    return (await L(s, "query_run", run_id=rid, what="netlist", candidate_id=cid))["netlist"]

async def ina_flow(s, L, proj):
    sys.path.insert(0, str(HERE))
    from ina_netlist import ina
    await L(s, "list_templates")
    nl = await get_netlist(s, L, "oa_src", "noninverting_amp", {"gain": 10, "bw": 100e3})
    net = ina(subckt(nl, "SPMCP_OA"))
    params = [{"name": n, "min": 1e3, "max": 100e3} for n in ("R1A", "R1B", "R2A", "R2B", "R3A", "R3B")]
    params.append({"name": "RG", "min": 20, "max": 2e3})
    limits = [{"metric": "gain_db", "min": 39.9, "max": 40.1}, {"metric": "bw", "min": 20e3},
              {"metric": "cm_db", "max": -40}, {"metric": "pm_deg", "min": 45}]
    goals = [{"metric": "bw", "direction": "max"}]
    await L(s, "check_feasibility", goals=goals, limits=limits, params=params, netlist=net, circuit_type="opamp")
    await L(s, "start_optimization", run_id="ina", netlist=net, params=params, goals=goals,
            limits=limits, circuit_type="opamp", sim_options=SO)
    await L(s, "evaluate_candidate", run_id="ina", params={k: 10e3 for k in ("R1A","R1B","R2A","R2B","R3A","R3B")} | {"RG": 200})
    await L(s, "run_optimization", run_id="ina", iterations=30)
    await L(s, "query_run", run_id="ina", what="best")
    await L(s, "finish_design", run_id="ina", tolerance_pct={"R": 0.1})
    await L(s, "generate_design_report", run_id="ina", plots=True)

async def draw_flow(s, L, name, proj):
    f = proj / f"{name}.cir"; f.write_text(UNSEEN[name])      # the LLM writes this file
    L.calls.append({"tool": "(write file, other tool)", "args": {"path": str(f), "content": UNSEEN[name]}, "answer": "ok", "secs": 0})
    await L(s, "render_schematic", path=str(f), output_path=str(proj / f"{name}.png"))

OA_TEST = """* open-loop test of the op-amp block
Vcc vcc 0 5
Vee vee 0 -5
Vin inp 0 DC 0 AC 1
X1 inp fb vcc vee out SPMCP_OA
Rf out fb 1G
Cf fb 0 1
CL out 0 10p
{SUB}.ac dec 20 1 1G
.meas AC a0 FIND mag(V(out)) AT 10
.meas AC ugf WHEN mag(V(out))=1 FALL=1
.end
"""
CMP_TEST = """* comparator + gate driver test
Vcc vcc 0 12
Vref ref 0 1.2
Vin in 0 PULSE(1.1 1.3 1u 10n 10n 4u 8u)
X1 in ref vcc 0 out SPMCP_CMP
CL out 0 1n
{SUB}.tran 10n 20u
.meas TRAN vhi MAX V(out) FROM 2u TO 5u
.meas TRAN vlo MIN V(out) FROM 6u TO 9u
.meas TRAN tr TRIG V(in) VAL=1.2 RISE=1 TARG V(out) VAL=6 RISE=1
.end
"""
CHUA = """* Chua's circuit, piecewise-linear diode, sweep of R to show the route to chaos
.param R=1.8k
C1 1 0 10n IC=0.1
C2 2 0 100n
L1 2 0 18m
Rr 1 2 {R}
B1 1 0 I = -0.757m*V(1) + 0.5*(-0.409m+0.757m)*(abs(V(1)+1)-abs(V(1)-1))
.tran 1u 20m 5m UIC
.meas TRAN vpk MAX V(1)
.meas TRAN vmn MIN V(1)
.end
"""
async def opamp_flow(s, L, proj):
    nl = await get_netlist(s, L, "oa_src", "noninverting_amp", {"gain": 10, "bw": 100e3})
    await L(s, "simulate", netlist=OA_TEST.replace("{SUB}", subckt(nl, "SPMCP_OA")))
async def cmp_flow(s, L, proj):
    nl = await get_netlist(s, L, "bk_src", "buck", {"vin": 12, "vout": 5, "iout": 1})
    await L(s, "simulate", netlist=CMP_TEST.replace("{SUB}", subckt(nl, "SPMCP_CMP")))
async def chua_flow(s, L, proj):
    await L(s, "simulate", netlist=CHUA, sweep_param="R", sweep_values=[1.6e3, 1.7e3, 1.8e3, 1.9e3, 2.0e3])
    f = proj / "chua.cir"; f.write_text(CHUA.replace("B1 1 0 I = -0.757m*V(1) + 0.5*(-0.409m+0.757m)*(abs(V(1)+1)-abs(V(1)-1))", "D1 1 0 DCHUA\n.model DCHUA D"))
    L.calls.append({"tool": "(write file, other tool)", "args": {"path": str(f), "content": f.read_text()}, "answer": "ok", "secs": 0})
    await L(s, "render_schematic", path=str(f), output_path=str(proj / "chua.png"))

CIRCUITS = [  # (no, name, flow)
    (1, "RC low-pass", lambda s, L, p: template_flow(s, L, "rc_lowpass", SPECS["rc_lowpass"], p)),
    (2, "Sallen-Key low-pass", lambda s, L, p: template_flow(s, L, "sallen_key_lowpass", SPECS["sallen_key_lowpass"], p)),
    (3, "Non-inverting amp", lambda s, L, p: template_flow(s, L, "noninverting_amp", SPECS["noninverting_amp"], p)),
    (4, "LDO", lambda s, L, p: template_flow(s, L, "ldo", SPECS["ldo"], p)),
    (5, "Buck (closed loop)", lambda s, L, p: template_flow(s, L, "buck", SPECS["buck"], p)),
    (6, "Instrumentation amp", ina_flow),
    (7, "Inverting amp", lambda s, L, p: draw_flow(s, L, "inverting", p)),
    (8, "Common-source MOSFET amp", lambda s, L, p: draw_flow(s, L, "cs_amp", p)),
    (9, "Wheatstone bridge", lambda s, L, p: draw_flow(s, L, "bridge", p)),
    (10, "Boost converter", lambda s, L, p: draw_flow(s, L, "boost", p)),
    (11, "Two-stage amp w/ feedback", lambda s, L, p: draw_flow(s, L, "two_stage", p)),
    (12, "Pi filter", lambda s, L, p: draw_flow(s, L, "pi_filter", p)),
    (13, "5-stage RC ladder", lambda s, L, p: draw_flow(s, L, "ladder5", p)),
    (14, "LC tank", lambda s, L, p: draw_flow(s, L, "lc_tank", p)),
    (15, "Two-stage CMOS op-amp", opamp_flow),
    (16, "Comparator + gate drive", cmp_flow),
    (17, "Chua's circuit", chua_flow),
]

async def main(only):
    out = {}
    async def tools(s, init):
        tl = (await s.list_tools()).tools
        return {"instructions": init.instructions,
                "tools": [{"name": t.name, "description": t.description, "input_schema": t.inputSchema} for t in tl]}
    out["surface"] = await with_server(tempfile.mkdtemp(), tools)
    for no, name, flow in CIRCUITS:
        if only and no not in only: continue
        proj = Path(tempfile.mkdtemp()); L = Log(); t = time.time()
        try:
            await with_server(proj, lambda s, i: flow(s, L, proj))
            err = None
        except Exception as e:
            err = repr(e)
        out[no] = {"name": name, "calls": L.calls, "error": err, "secs": round(time.time() - t)}
        print(no, name, len(L.calls), "calls", out[no]["secs"], "s", err or "", flush=True)
    tag = "_".join(map(str, only)) or "all"
    (HERE / f"log_{tag}.json").write_text(json.dumps(out, indent=1, default=str))

asyncio.run(main([int(x) for x in sys.argv[1:]]))
