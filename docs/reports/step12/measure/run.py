"""Step 12 measurement: each new template designed start to finish with the defaults a
user gets (ngspice), and the parts list it writes.

    PYTHONPATH=<repo> python run.py <out.json> [only,these,ids]

Linear circuits get 30 random boards per yield, the two switchers 10 (each board is a
40 to 55 s transient at three temperatures). Same seed for all."""
from __future__ import annotations

import csv
import json
import sys
import tempfile
import time
from pathlib import Path

from spicemcp import db, server

OUT = Path(sys.argv[1])
ONLY = set(sys.argv[2].split(",")) if len(sys.argv) > 2 else None
NG = server.SimOptions(backend="ngspice")
CASES = [("inverting_amp", {"gain": 10, "bw": 100e3}, 30),
         ("bandpass", {"f0": 1000}, 30),
         ("current_sense", {"imax": 2}, 30),
         ("adc_driver", {"fs": 100e3}, 30),
         ("voltage_reference", {"vout": 2.5}, 30),
         ("protection", {"vin": 12, "vsurge": 40, "vmax": 20, "iload": 0.5}, 30),
         ("boost", {"vin": 5, "vout": 12, "iout": 0.5}, 10),
         ("flyback", {"vin": 12, "vout": 5, "iout": 1}, 10)]


def finish(rid, runs):
    out = server.finish_design(rid, yield_runs=runs, seed=12)
    while "still running" in json.dumps(out):
        time.sleep(5)
        out = server.finish_design(rid, yield_runs=runs, seed=12)
    return out


res = json.loads(OUT.read_text()) if OUT.is_file() else {}
for name, spec, runs in CASES:
    if ONLY and name not in ONLY:
        continue
    d = tempfile.mkdtemp()
    server.PROJECT = d
    server._con = db.connect(Path(d))
    t0 = time.time()
    st = server.start_from_template(name, name, spec, NG)
    if "error" in st:
        res[name] = {"error": st["error"]}
        OUT.write_text(json.dumps(res, indent=1))
        continue
    t1 = time.time()
    f = finish(name, runs)
    det = server.query_run(name, "details")
    bom = (det.get("finish") or {}).get("bom") or {}
    rows = []
    if bom.get("csv"):
        with open(bom["csv"], encoding="utf-8-sig") as fh:
            rows = list(csv.reader(fh))
    res[name] = {"spec": spec, "boards": runs,
                 "start_metrics": st["start_result"]["metrics"],
                 "start_status": st["start_result"].get("status"),
                 "seconds_start": round(t1 - t0, 1), "seconds_finish": round(time.time() - t1, 1),
                 "answer": f, "stress": (det.get("finish") or {}).get("stress"), "bom": rows}
    print(name, res[name]["seconds_finish"], json.dumps(f.get("yield"))[:300], flush=True)
    OUT.write_text(json.dumps(res, indent=1))
