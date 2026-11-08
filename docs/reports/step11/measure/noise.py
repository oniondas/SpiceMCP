"""Output noise of the amplifier and the filter, as start_from_template reports it.

    PYTHONPATH=<repo> python noise.py <out.json>"""
import json
import sys
import tempfile
from pathlib import Path

from spicemcp import db, engine, server

out = {}
for rid, name, spec in (("sk", "sallen_key_lowpass", {"fc": 10000}),
                        ("amp", "noninverting_amp", {"gain": 10, "bw": 100e3}),
                        ("amp_low_r", "noninverting_amp", {"gain": 10, "bw": 100e3,
                                                           "noise_uv": 120})):
    d = tempfile.mkdtemp()
    server.PROJECT, server._con = d, db.connect(Path(d))
    st = server.start_from_template(rid, name, spec, server.SimOptions(backend="ngspice"))
    m = st["start_result"]["metrics"]
    out[rid] = {"vn_uv": m.get("vn_uv"), "start": st["start_point"],
                "limits": st["limits"]}
    if spec.get("noise_uv"):
        r = server.run_optimization(rid, iterations=20)
        b = server.query_run(rid, "best")
        out[rid]["after_optimisation"] = {
            "feasible": r.get("best_is_feasible"),
            "best": (b.get("best_feasible") or b.get("best") or {}).get("metrics", {}).get("vn_uv"),
            "params": db.jloads(engine.get_candidate(
                server.con(), (b.get("best_feasible") or b["best"])["candidate_id"]), "params")}
Path(sys.argv[1]).write_text(json.dumps(out, indent=1))
print(json.dumps(out, indent=1))
