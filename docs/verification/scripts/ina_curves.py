"""Frequency response of the instrumentation amplifier: hand-sized start and the
finished parts list, differential and common-mode.  Usage: ina_curves.py BACKEND"""
import json, sys, tempfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
import numpy as np
from mcpc import OUT
from spicemcp import ir, opamp, sim
from ina_netlist import ina
BACKEND = sys.argv[1] if len(sys.argv) > 1 else "ngspice"
SETS = {"hand": {"R1A": 10e3, "R1B": 10e3, "RG": 200, "R2A": 10e3, "R2B": 10e3, "R3A": 10e3, "R3B": 10e3},
        "finished": {"R1A": 9760, "R1B": 10200, "R2A": 9760, "R2B": 9760, "R3A": 10e3, "R3B": 10e3, "RG": 205}}
f = np.logspace(1, 7, 61)
deck = sim.decks(ina(opamp.subckt(10e6, 10.0)))[0]
meas = "\n".join(f".meas AC d{i} FIND mag(V(out)) AT {x:.6g}\n.meas AC c{i} FIND mag(V(outc)) AT {x:.6g}"
                 for i, x in enumerate(f))
i = deck.rstrip().rfind("\n.end")
deck = deck[:i] + "\n" + meas + deck[i:]
res = {"f": f.tolist()}
for k, p in SETS.items():
    net, _ = ir.render(deck, p)
    m = sim.simulate(net, p, {"backend": BACKEND}, Path(tempfile.mkdtemp()), k).metrics
    res[k] = {"diff_db": [m.get(f"d{i}") for i in range(len(f))],
              "cm_db": [m.get(f"c{i}") for i in range(len(f))], "params": p,
              "gain_db": m.get("gain_db"), "bw": m.get("bw"), "cm60_db": m.get("cm_db")}
    print(k, m.get("gain_db"), m.get("bw"), m.get("cm_db"))
(OUT / f"ina_curves_{BACKEND}.json").write_text(json.dumps(res))
