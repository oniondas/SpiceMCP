"""SpiceMCP arm: each benchmark circuit designed over real MCP stdio with the calls a
model would make (list, start, optimise, finish, report, draw). Every request and answer
is logged for token counting, with wall-clock time and the final netlist.

    PYTHONPATH=<repo> python arm_spicemcp.py <out.json> [rc,amp,buck]"""
import asyncio, json, os, sys, tempfile, time
from pathlib import Path
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

HERE = Path(__file__).parent
REPO = Path(__file__).resolve().parents[4]
SO = {"backend": "ngspice"}
CASES = {"rc": ("rc_lowpass", {"fc": 1000}),
         "amp": ("noninverting_amp", {"gain": 10, "bw": 100e3}),
         "buck": ("buck", {"vin": 12, "vout": 5, "iout": 1})}


class Log:
    def __init__(s): s.calls = []
    async def __call__(s, ses, name, **a):
        t = time.time()
        r = await ses.call_tool(name, a)
        txt = "".join(c.text for c in r.content if hasattr(c, "text"))
        s.calls.append({"tool": name, "args": a, "answer": txt, "secs": round(time.time() - t, 1)})
        try: return json.loads(txt)
        except ValueError: return {"raw": txt}


async def flow(s, L, rid, template, spec, proj):
    await L(s, "list_templates")
    st = await L(s, "start_from_template", run_id=rid, template=template, spec=spec, sim_options=SO)
    await L(s, "run_optimization", run_id=rid, iterations=20)
    f = await L(s, "finish_design", run_id=rid, seed=12)
    while "still running" in json.dumps(f):          # a model polls the same call
        await asyncio.sleep(10)
        f = await L(s, "finish_design", run_id=rid, seed=12)
    cid = f.get("buildable_candidate")
    nl = await L(s, "query_run", run_id=rid, what="netlist", candidate_id=cid)
    await L(s, "generate_design_report", run_id=rid, plots=True)
    if st.get("schematic_asc"):
        await L(s, "render_schematic", path=st["schematic_asc"], output_path=str(proj / f"{rid}.png"))
    return f, nl.get("netlist"), st.get("schematic_asc")


async def main(out, only):
    res = json.loads(out.read_text()) if out.is_file() else {}
    for key, (template, spec) in CASES.items():
        if only and key not in only: continue
        proj = Path(tempfile.mkdtemp()); L = Log(); t = time.time()
        p = StdioServerParameters(command=sys.executable, args=[str(HERE / "srv.py")],
                                  env={**os.environ, "SPICEMCP_PROJECT": str(proj),
                                       "PYTHONPATH": str(REPO)}, cwd=str(proj))
        async with stdio_client(p) as (r, w):
            async with ClientSession(r, w) as s:
                init = await s.initialize()
                tools = [{"name": x.name, "description": x.description, "input_schema": getattr(x, "input_schema", None) or getattr(x, "inputSchema", None)}
                         for x in (await s.list_tools()).tools]
                f, nl, asc = await flow(s, L, key, template, spec, proj)
        res[key] = {"template": template, "spec": spec, "secs": round(time.time() - t, 1),
                    "calls": L.calls, "finish": f, "netlist": nl, "png": str(proj / f"{key}.png"),
                    "surface": {"instructions": init.instructions, "tools": tools}}
        print(key, res[key]["secs"], "s", len(L.calls), "calls",
              json.dumps(f.get("yield"))[:200], f.get("meets_all_limits"), flush=True)
        out.write_text(json.dumps(res, indent=1, default=str))

asyncio.run(main(Path(sys.argv[1]), set(sys.argv[2].split(",")) if len(sys.argv) > 2 else None))
