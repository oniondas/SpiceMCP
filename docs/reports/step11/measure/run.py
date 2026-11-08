"""Step 11 measurement: every template designed start to finish with the defaults a user
gets, on the code at PYTHONPATH (v0.15.0 = before, v0.16.0 = after), with ngspice.

    PYTHONPATH=<repo> python run.py <label> <out.json> [runs] [only,these,ids]

Records per template: does the parts list meet the limits, the yield and what it covers,
the worst case, the ratings to buy, and seconds taken. Real-part runs (after only) use
the test library in tests/fixtures/lib."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from pathlib import Path

from spicemcp import db, server

LABEL, OUT = sys.argv[1], Path(sys.argv[2])
RUNS = int(sys.argv[3]) if len(sys.argv) > 3 else 30
ONLY = set(sys.argv[4].split(",")) if len(sys.argv) > 4 else None
NG = server.SimOptions(backend="ngspice")
CASES = [("rc", "rc_lowpass", {"fc": 1000}, None),
         ("sk", "sallen_key_lowpass", {"fc": 10000}, None),
         ("amp", "noninverting_amp", {"gain": 10, "bw": 100e3}, None),
         ("ldo", "ldo", {"vin": 5, "vout": 3.3, "iload": 0.1}, None),
         ("buck", "buck", {"vin": 12, "vout": 3.3, "iout": 1}, None)]
if LABEL != "before":
    lib = Path(__file__).resolve().parents[4] / "tests" / "fixtures" / "lib"
    os.environ["SPICEMCP_LIB_DIRS"] = str(lib)
    CASES += [("sk_toa10", "sallen_key_lowpass", {"fc": 10000}, {"opamp": "TOA10"}),
              ("buck_1n5819", "buck", {"vin": 12, "vout": 3.3, "iout": 1},
               {"switch": "Si4427BDY", "diode": "1N5819"}),
              ("buck_mbrs340", "buck", {"vin": 12, "vout": 3.3, "iout": 1},
               {"switch": "Si4427BDY", "diode": "MBRS340"})]


def finish(rid):
    out = server.finish_design(rid, yield_runs=RUNS, seed=11)
    while out.get("status") == "still running" or "still running" in str(out.get("next_step", "")):
        out = server.finish_design(rid, yield_runs=RUNS, seed=11)
    return out


res = json.loads(OUT.read_text()) if ONLY and OUT.is_file() else {}
for rid, name, spec, parts in CASES:
    if ONLY and rid not in ONLY:
        continue
    d = tempfile.mkdtemp()
    server.PROJECT = d
    server._con = db.connect(Path(d))
    t0 = time.time()
    kw = {"parts": parts} if parts else {}
    st = server.start_from_template(rid, name, spec, NG, **kw)
    if "error" in st:
        res[rid] = {"error": st["error"]}
        continue
    f = finish(rid)
    det = server.query_run(rid, "details")
    res[rid] = {"template": name, "spec": spec, "parts": parts,
                "start_metrics": st["start_result"]["metrics"],
                "seconds": round(time.time() - t0, 1), "answer": f,
                "stress": (det.get("finish") or {}).get("stress"),
                "spread": ((det.get("finish") or {}).get("yield") or {}).get("spread")}
    print(rid, res[rid]["seconds"], json.dumps(f.get("yield"))[:300], flush=True)
    OUT.write_text(json.dumps(res, indent=1))
