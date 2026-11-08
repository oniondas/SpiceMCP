"""Targeted re-measurements of single claims with separate testbenches.
Usage: ck_claims.py BACKEND   (ngspice or ltspice)"""
import json, re, sys, tempfile, math
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from mcpc import OUT
BACKEND = sys.argv[1] if len(sys.argv) > 1 else "ngspice"
from spicemcp import opamp, sim, topologies as T
OUT_ = {}
def run(net, params=None, tag="x"):
    params = params or {}
    for k, v in params.items(): net = net.replace("{%s}" % k, f"{v:.6g}")
    r = sim.simulate(net, params, {"backend": BACKEND, "timeout_s": 600}, Path(tempfile.mkdtemp()), tag)
    if r.status != "ok": print("FAIL", tag, r.failure_message, r.warnings[:3])
    return r.metrics
def first(net): return sim.decks(net)[0]
def add_meas(net, lines):
    """Insert .meas lines before the deck's final .end (not inside a .ends)."""
    i = net.rstrip().rfind("\n.end")
    return net[:i] + "\n" + lines.strip("\n") + net[i:]

# (a) Sallen-Key at 1 MHz: real op-amp vs an ideal stand-in (my own E-source, gain 1e5)
d = T.get("sallen_key_lowpass").design({"fc": 10000, "q": 1/math.sqrt(2), "cload": 100e-12})
sk = first(d.netlist).replace(".ac dec 100 100 1e+06", ".ac dec 100 100 1e7")
sk = add_meas(sk, ".meas AC a1m FIND mag(V(out)) AT 1Meg\n.meas AC a100k FIND mag(V(out)) AT 100k\n.meas AC a300k FIND mag(V(out)) AT 300k")
ideal = re.sub(r"^X1 b out vcc vee out SPMCP_OA$", "E1 out 0 b out 1e5", sk, flags=re.M)
OUT_["sk_real"] = run(sk, d.start, "skr"); OUT_["sk_ideal"] = run(ideal, d.start, "ski")
# frequency sweep data for the plot
import numpy as np
fr = np.logspace(2, 7, 41)
def sweep(net):
    m = add_meas(net, "\n".join(f".meas AC p{i} FIND mag(V(out)) AT {f:.6g}" for i, f in enumerate(fr)))
    r = run(m, d.start, "sw"); return [r.get(f"p{i}") for i in range(len(fr))]
OUT_["sk_curve"] = {"f": fr.tolist(), "real": sweep(sk), "ideal": sweep(ideal)}
print("SK", OUT_["sk_real"].get("a1m"), OUT_["sk_ideal"].get("a1m"))

# (b) LDO phase margin and load step, with and without the series resistor
d = T.get("ldo").design({"vin": 5, "vout": 3.3, "iload": 0.1, "dropout": 0.3, "cout": 1e-6, "step_pct": 3})
OUT_["ldo_rz"] = run(d.netlist, d.start, "ldo1")
OUT_["ldo_norz"] = run(d.netlist, {**d.start, "RZ": 1e-3}, "ldo0")
OUT_["ldo_start"] = d.start
print("LDO", {k: OUT_["ldo_rz"].get(k) for k in ("pm_deg", "step_mv", "vout")}, {k: OUT_["ldo_norz"].get(k) for k in ("pm_deg", "step_mv")})

# (c) the op-amp itself: open-loop gain, GBW, slew, supply current (10 V span, GBW 10 MHz)
oa = opamp.subckt(10e6, 10.0)
ol = f"""* open loop: follower with loop broken at in-
Vcc vcc 0 5
Vee vee 0 -5
Vin inp 0 0
X1 inp fbk vcc vee out SPMCP_OA
RL out 0 10k
Lbrk out fbk 1k
Cinj fbk inj 1k
Vinj inj 0 AC 1
{oa}.ac dec 50 1 1G
.meas AC a0 FIND mag(V(out)) AT 10
.meas AC gbw WHEN mag(V(out))=1 FALL=1
.end
* slew and supply current: follower, 0 to 2 V step
Vcc vcc 0 5
Vee vee 0 -5
Vin inp 0 PULSE(-1 1 1u 1n 1n 5u 10u)
X1 inp out vcc vee out SPMCP_OA
RL out 0 10k
CL out 0 10p
{oa}.tran 1n 4u
.meas TRAN t1 WHEN V(out)=-0.6 RISE=1
.meas TRAN t2 WHEN V(out)=0.6 RISE=1
.meas TRAN slew PARAM 1.2/(t2-t1)/1e6
.meas TRAN icc AVG I(Vcc) FROM 0.2u TO 0.9u
.end
"""
OUT_["opamp"] = run(ol, {}, "oa")
print("OPAMP", OUT_["opamp"])

# (d) buck: input step 11.4 -> 12.6 V at 0.6 ms, closed loop, hand-sized values
d = T.get("buck").design({"vin": 12, "vout": 5, "iout": 1, "fsw": 500e3, "ripple_mv": 50})
b = first(d.netlist)
tstop = 1.4e-3
b = b.replace("Vin in 0 12", "Vin in 0 PWL(0 11.4 0.6m 11.4 0.601m 12.6)")
b = re.sub(r"^\.tran .*$", f".tran 2n {tstop:g} 0 2n", b, flags=re.M)
b = re.sub(r"^\.meas.*\n", "", b, flags=re.M)
b = add_meas(b, ".meas TRAN v_before AVG V(out) FROM 0.45m TO 0.6m\n.meas TRAN vpk MAX V(out) FROM 0.6m TO 1.4m\n.meas TRAN v_after AVG V(out) FROM 1.25m TO 1.4m\n.meas TRAN t_back WHEN V(out)=5.1 FALL=LAST")
OUT_["buck_step"] = run(b, d.start, "bstep"); OUT_["buck_start"] = d.start
print("BUCK", OUT_["buck_step"])
RES = OUT_
(OUT / f"claims_{BACKEND}.json").write_text(json.dumps(RES, indent=1))
