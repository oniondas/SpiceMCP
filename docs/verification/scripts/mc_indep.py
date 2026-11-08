"""Yield re-check, separate from finish_design: own random generator and seed, more
boards, parts list taken from what finish_design returned. Supply tolerance is applied
by moving the operating point the measurement reads (for the LDO the .dc sweep would
otherwise override the source value).  Usage: mc_indep.py BACKEND BOARDS NAME..."""
import json, random, re, sys, tempfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from mcpc import OUT
from spicemcp import ir, opamp, sim, topologies as T
from ina_netlist import ina

BACKEND, N, NAMES = sys.argv[1], int(sys.argv[2]), sys.argv[3:]
SPECS = {"rc_lowpass": {"fc": 1000}, "sallen_key_lowpass": {"fc": 10000},
         "noninverting_amp": {"gain": 10, "bw": 100e3},
         "ldo": {"vin": 5, "vout": 3.3, "iload": 0.1},
         "buck": {"vin": 12, "vout": 5, "iout": 1}}
# the parts lists finish_design returned in the ngspice run (E96 1 %, E12 5 %, L 20 %)
PARTS = {"rc_lowpass": {"C1": 1.8e-9, "R1": 88.7e3},
         "sallen_key_lowpass": {"C1": 2.2e-9, "C2": 1e-9, "R1": 13.3e3, "R2": 9.31e3},
         "noninverting_amp": {"R1": 10e3, "R2": 90.9e3},
         "ldo": {"R1": 174e3, "RZ": 0.825},
         "buck": {"C": 2.2e-6, "CI": 22e-9, "L": 18e-6, "R1": 31.6e3},
         "ina_1pct": {"R1A": 9760, "R1B": 10200, "R2A": 9760, "R2B": 9760, "R3A": 10e3, "R3B": 10e3, "RG": 205},
         "ina_tool1pct": {"R1A": 9760, "R1B": 10000, "R2A": 9760, "R2B": 9760, "R3A": 10e3, "R3B": 10e3, "RG": 205},
         "ina_matched": {"R1A": 10e3, "R1B": 10e3, "R2A": 10e3, "R2B": 10e3, "R3A": 10e3, "R3B": 10e3, "RG": 202}}
TOL = {"R": 0.01, "C": 0.05, "L": 0.20}
INA_LIMITS = [{"metric": "gain_db", "min": 39.9, "max": 40.1}, {"metric": "bw", "min": 20e3},
              {"metric": "cm_db", "max": -40}, {"metric": "pm_deg", "min": 45}]


def tol(k, rtol):
    return {"C": TOL["C"], "L": TOL["L"]}.get(k[0], rtol) if k != "CI" else TOL["C"]


def run(name, rtol=0.01, seed=20261108):
    rng = random.Random(seed)
    if name.startswith("ina"):
        net, limits, fixed = ina(opamp.subckt(10e6, 10.0)), INA_LIMITS, {}
    else:
        d = T.get(name).design(SPECS[name])
        net, limits = d.netlist, [x for x in d.limits if not x["metric"].startswith("param_")]
        fixed = {k: v for k, v in d.start.items() if k not in PARTS[name]}
    rows = []
    for i in range(N):
        p = {**fixed, **{k: v * (1 + rng.uniform(-tol(k, rtol), tol(k, rtol)))
                         for k, v in PARTS[name].items()}}
        txt, _ = ir.render(net, p)
        s = 1.0
        if name in ("ldo", "buck"):
            s = 1 + rng.uniform(-0.05, 0.05)
            v0 = SPECS[name]["vin"]
            txt = re.sub(rf"^Vin in 0 {v0:g}$", f"Vin in 0 {v0 * s:.6g}", txt, flags=re.M)
            if name == "ldo":    # read the DC sweep at the drawn supply, not at 5 V
                txt = txt.replace(f"vout FIND V(out) AT {v0:g}", f"vout FIND V(out) AT {v0 * s:.4g}")
        r = sim.simulate(txt, p, {"backend": BACKEND, "timeout_s": 600},
                         Path(tempfile.mkdtemp()), f"mc{i}")
        m = r.metrics if r.status == "ok" else {}
        ok = bool(m) and all((x.get("min") is None or m.get(x["metric"], float("nan")) >= x["min"]) and
                             (x.get("max") is None or m.get(x["metric"], float("nan")) <= x["max"])
                             for x in limits)
        rows.append({"ok": ok, "supply": s, "metrics": m})
    k = sum(r["ok"] for r in rows)
    pct = 100 * k / N
    # Wilson 95 % interval
    z, ph = 1.96, k / N
    c = (ph + z * z / (2 * N)) / (1 + z * z / N)
    h = z * ((ph * (1 - ph) / N + z * z / (4 * N * N)) ** 0.5) / (1 + z * z / N)
    out = {"boards": N, "passed": k, "yield_pct": pct, "ci95": [100 * (c - h), 100 * (c + h)],
           "resistor_tol_pct": rtol * 100, "limits": limits, "rows": rows}
    print(name, rtol, f"{pct:.1f}%  95% CI {100*(c-h):.0f}-{100*(c+h):.0f}%", flush=True)
    return out


res = {}
for n in NAMES:
    res[n] = run(n)
    if n in ("ina_1pct", "ina_matched"):
        res[n + "_0.1pct"] = run(n, 0.001)
(OUT / f"mc_{BACKEND}_{N}_{'_'.join(NAMES)}.json").write_text(json.dumps(res, indent=1))
