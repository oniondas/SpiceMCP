"""Count tokens in a run.py log:  TOKENIZER_JSON=... python analyze.py log_all.json out.json"""
import json, sys, collections
from tokenizers import Tokenizer
import os
# A tokenizers-format tokenizer.json; use the same file for every comparison.
T = Tokenizer.from_file(os.environ["TOKENIZER_JSON"])
tk = lambda s: len(T.encode(s).ids)
d = json.load(open(sys.argv[1]))
S = d["surface"]
o_instr = tk(S["instructions"] or "")
o_tools = tk(json.dumps(S["tools"]))
o_tools_min = tk(json.dumps(S["tools"], separators=(",", ":")))
per_tool = sorted(((t["name"], tk(json.dumps(t))) for t in S["tools"]), key=lambda x: -x[1])
O = o_instr + o_tools
U = 40  # the user's one-line request
def mini(a):
    try: return json.dumps(json.loads(a), separators=(",", ":"))
    except ValueError: return a
rows, agg = [], collections.defaultdict(lambda: [0, 0, 0])
for k, v in d.items():
    if k == "surface": continue
    ctx, inp, cached_eff, out_t, resp_t, resp_min, req_t = O + U, 0, 0, 0, 0, 0, 0
    inp += ctx; cached_eff += ctx; prev = ctx
    big = ("", 0)
    for c in v["calls"]:
        name = c["tool"]
        rq = tk(json.dumps({"name": name, "input": c["args"]}))
        rs = tk(c["answer"]); rsm = tk(mini(c["answer"]))
        req_t += rq; resp_t += rs; resp_min += rsm
        ctx += rq + rs
        inp += ctx; cached_eff += (ctx - prev) + 0.1 * prev; prev = ctx
        agg[name][0] += 1; agg[name][1] += rs; agg[name][2] += rq
        if rs > big[1]: big = (name, rs)
    n = len(v["calls"])
    rows.append(dict(no=int(k), name=v["name"], calls=n, req=req_t, resp=resp_t,
                     resp_min=resp_min, transcript=req_t + resp_t, final_ctx=ctx,
                     billed_in=inp, billed_in_cached=round(cached_eff), out=req_t,
                     biggest=f"{big[0]} {big[1]}", secs=v["secs"], err=v["error"]))
rows.sort(key=lambda r: r["no"])
res = dict(overhead=dict(instructions=o_instr, tools=o_tools, tools_minified=o_tools_min,
                         per_tool=per_tool, total=O), rows=rows,
           per_tool_resp={k: dict(calls=v[0], resp=v[1], avg=round(v[1] / v[0]), req=v[2]) for k, v in agg.items()})
json.dump(res, open(sys.argv[2], "w"), indent=1)
print(json.dumps(res["overhead"], indent=0))
print(f"{'#':>2} {'circuit':28} {'calls':>5} {'req':>6} {'resp':>6} {'respMin':>7} {'finalCtx':>8} {'billedIn':>8} {'cached':>7}  biggest")
for r in rows:
    print(f"{r['no']:>2} {r['name']:28} {r['calls']:>5} {r['req']:>6} {r['resp']:>6} {r['resp_min']:>7} {r['final_ctx']:>8} {r['billed_in']:>8} {r['billed_in_cached']:>7}  {r['biggest']} {r['err'] or ''}")
tot = {k: sum(r[k] for r in rows) for k in ("calls", "req", "resp", "resp_min", "billed_in", "billed_in_cached")}
print("TOTAL", tot)
for k, v in sorted(res["per_tool_resp"].items(), key=lambda x: -x[1]["resp"]): print(k, v)
