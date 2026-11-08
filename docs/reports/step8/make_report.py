"""Step 8 pictures: a buck that regulates, and circuits checked for stability.

    python docs/reports/step8/make_report.py

Every number is an ngspice simulation of the template decks.
  1. Buck, open loop (the template before this step) against closed loop (now): the
     same 40 random boards (part tolerance and ±5 % input), and a step in the input.
  2. LDO with its 1 µF output capacitor: loop gain and a load step, without and with
     the zero resistor the template now sizes.
  3. Op-amp phase margin against the capacitance it drives.
"""
from __future__ import annotations

import json
import math
import random
import subprocess
import sys
import tempfile
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[2]))
from spicemcp import ir, sim, topologies as T  # noqa: E402

BEFORE, AFTER, LIMIT, BAND = "#E07A5F", "#1E56A0", "#57606A", "#F2CC8F"
INK, MUTED, GRID, BG = "#1F2328", "#57606A", "#E6E8EB", "#FCFCFB"
plt.rcParams.update({"font.family": "DejaVu Sans", "font.size": 9.5, "axes.edgecolor": GRID,
                     "axes.labelcolor": MUTED, "xtick.color": MUTED, "ytick.color": MUTED,
                     "figure.facecolor": BG, "axes.facecolor": BG, "text.color": INK})
BUCK = {"vin": 12, "vout": 5, "iout": 1}
LDO = {"vin": 5, "vout": 3.3, "iload": 0.1}
N = 40
T_STEP = 0.6e-3                      # the input step, inside both runs
TOL = {"L": 0.20, "C": 0.05, "R1": 0.01, "CI": 0.05, "D": 0.0}


def open_loop_buck(s: dict) -> tuple[str, dict]:
    """The buck template as it was up to 0.12: a fixed duty cycle, no controller."""
    vin, vout, iout, fsw, ripple = s["vin"], s["vout"], s["iout"], 500e3, 50
    d = (vout + 0.4) / (vin + 0.4)
    di = 0.3 * iout
    lval = (vin - vout) * d / (fsw * di)
    cval = di / (8 * fsw * 0.8 * ripple * 1e-3)
    rl, t = vout / iout, 1 / fsw
    f0 = 1 / (2 * math.pi * math.sqrt(lval * cval))
    tstop = max(60 / f0, 400 * t)
    win = min(100 * t, tstop / 4)
    net = f"""* Buck converter, open loop (the template up to 0.12)
.param duty={{D}}
Vin in 0 {vin:g}
Vdrv in g PULSE(0 {min(10.0, vin):g} 0 5n 5n {{duty*{t:g}}} {t:g})
M1 sw g in PSW
D1 0 sw DS
L1 sw out {{L}}
C1 out 0 {{C}}
RL out 0 {rl:g}
.model PSW VDMOS(pchan Vto=-2 Kp=20 Rd=0.04 Rs=0.01 Rb=0.01 Cgs=1n Cgdmax=1n Cgdmin=0.1n Cjo=0.5n)
.model DS D(Is=1e-6 Rs=0.02 N=1.2)
.tran {t / 200:g} {tstop:g} 0 {t / 200:g}
.meas TRAN vout AVG V(out) FROM {tstop - win:g} TO {tstop:g}
.meas TRAN vmax MAX V(out) FROM {tstop - win:g} TO {tstop:g}
.meas TRAN vmin MIN V(out) FROM {tstop - win:g} TO {tstop:g}
.meas TRAN ripple_mv PARAM (vmax-vmin)*1000
.end
"""
    return net, {"D": d, "L": lval, "C": cval}


def _sim(job):
    net, tag = job
    r = sim.simulate(net, {}, {"backend": "ngspice", "timeout_s": 600},
                     Path(tempfile.mkdtemp()), tag)
    return r.metrics if r.status == "ok" else {}


def boards(template: str, start: dict, seed: int = 8) -> list[tuple[str, str]]:
    """N decks, each with every part and the input drawn inside its tolerance. The same
    seed gives both buck versions the same draws for the parts they share."""
    rng = random.Random(seed)
    jobs = []
    for i in range(N):
        draw = {k: rng.uniform(-1, 1) for k in ("L", "C", "R1", "CI", "VIN")}
        p = {k: v * (1 + TOL.get(k, 0) * draw.get(k, 0)) for k, v in start.items()}
        net, _ = ir.render(template, p)
        vin = BUCK["vin"] * (1 + 0.05 * draw["VIN"])
        net = net.replace(f"Vin in 0 {BUCK['vin']:g}\n", f"Vin in 0 {vin:.4g}\n", 1)
        jobs.append((net, f"b{i:02d}"))
    return jobs


def wave(net: str, vec: str):
    work = Path(tempfile.mkdtemp())
    deck = sim.to_ngspice(net).replace(".endc", f"wrdata {work / 'w.txt'} {vec}\n.endc")
    (work / "d.cir").write_text(deck)
    subprocess.run(["ngspice", "-b", str(work / "d.cir")], capture_output=True, text=True,
                   timeout=900, cwd=work)
    cols = [ln.split() for ln in (work / "w.txt").read_text().splitlines() if ln.strip()]
    return [float(c[0]) for c in cols], [float(c[1]) for c in cols]


def _clean(ax):
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)
    ax.tick_params(length=0)
    ax.grid(color=GRID, lw=0.7)
    ax.set_axisbelow(True)


def buck_part(res: dict) -> None:
    d = T.get("buck").design(BUCK)
    old_net, old_start = open_loop_buck(BUCK)
    with ProcessPoolExecutor(4) as ex:
        old = list(ex.map(_sim, boards(old_net, old_start)))
        new = list(ex.map(_sim, boards(d.netlist, d.start)))
    lo, hi = BUCK["vout"] * 0.98, BUCK["vout"] * 1.02

    def ok(m):
        return bool(m) and lo <= m["vout"] <= hi and m["ripple_mv"] <= 50

    res["buck"] = {
        "boards": N, "tolerance_pct": {"L": 20, "C": 5, "R": 1, "CI": 5, "Vin": 5},
        "open_loop": {"yield_pct": 100 * sum(map(ok, old)) / N,
                      "vout_min": min(m["vout"] for m in old if m),
                      "vout_max": max(m["vout"] for m in old if m)},
        "closed_loop": {"yield_pct": 100 * sum(map(ok, new)) / N,
                        "vout_min": min(m["vout"] for m in new if m),
                        "vout_max": max(m["vout"] for m in new if m),
                        "ripple_max_mv": max(m["ripple_mv"] for m in new if m),
                        "failed_to_simulate": sum(1 for m in new if not m)}}
    print("buck", res["buck"])

    # input step 11.4 V -> 12.6 V half way, on the nominal board
    step = {}
    for tag, (net, start) in {"open": (old_net, old_start),
                              "closed": (d.netlist, d.start)}.items():
        n, _ = ir.render(net, start)
        n = n.replace("Vin in 0 12\n", f"Vin in 0 PWL(0 11.4 {T_STEP:g} 11.4 "
                                         f"{T_STEP + 1e-6:g} 12.6)\n", 1)
        step[tag] = wave(n, "v(out)")
        x, y = step[tag]
        after = [(t, v) for t, v in zip(x, y) if t > T_STEP]
        final = sum(v for t, v in after if t > x[-1] - 1e-4) / max(
            1, sum(1 for t, _ in after if t > x[-1] - 1e-4))
        late = [t for t, v in after if abs(v - 5) > 0.1]
        res["buck"][f"input_step_{tag}"] = {
            "peak_v": max(v for _, v in after), "settled_v": final,
            "back_in_spec_after_ms": (late[-1] - T_STEP) * 1e3 if late and abs(final - 5) <= 0.1
            else None}

    fig, axes = plt.subplots(1, 2, figsize=(13, 4.4), gridspec_kw={"width_ratios": [1, 1.25]})
    ax = axes[0]
    bins = [4.70 + 0.025 * i for i in range(25)]
    ax.axvspan(lo, hi, color=BAND, alpha=0.6, lw=0)
    ax.hist([m["vout"] for m in old if m], bins=bins, color=BEFORE, alpha=0.85,
            label=f"open loop (before): {res['buck']['open_loop']['yield_pct']:.0f}% pass")
    ax.hist([m["vout"] for m in new if m], bins=bins, color=AFTER, alpha=0.85,
            label=f"closed loop (now): {res['buck']['closed_loop']['yield_pct']:.0f}% pass")
    _clean(ax)
    ax.set_xlabel("output voltage (V)")
    ax.set_ylabel("boards")
    ax.set_title(f"{N} random boards: parts in tolerance, input 12 V ±5%", loc="left",
                 fontsize=10.5)
    ax.legend(frameon=False, fontsize=8.5, loc="upper left")
    ax.text(lo, ax.get_ylim()[1] * 0.55, " spec\n 5 V ±2%", color=MUTED, fontsize=8.5)

    ax = axes[1]
    for tag, col, lab in (("open", BEFORE, "open loop (before)"),
                          ("closed", AFTER, "closed loop (now)")):
        x, y = step[tag]
        ax.plot([v * 1e3 for v in x], y, color=col, lw=1.1, label=lab)
    _clean(ax)
    ax.axhspan(lo, hi, color=BAND, alpha=0.5, lw=0)
    ax.set_ylim(4.6, 6.0)
    ax.set_xlim(0.35, 1.1)
    ax.axvline(T_STEP * 1e3, color=MUTED, ls=":", lw=1)
    ax.set_xlabel("time (ms)")
    ax.set_ylabel("output voltage (V)")
    ax.set_title("The input steps from 11.4 V to 12.6 V at 0.6 ms", loc="left", fontsize=10.5)
    ax.legend(frameon=False, fontsize=8.5, loc="lower right")
    fig.tight_layout()
    fig.savefig(HERE / "buck_closed_loop.png", dpi=130)
    plt.close(fig)


def ldo_part(res: dict) -> None:
    d = T.get("ldo").design(LDO)
    rz = d.start["RZ"]
    out = {}
    for tag, z in (("no_zero", 0.01), ("with_zero", rz)):
        net, _ = ir.render(d.netlist, {**d.start, "RZ": z})
        decks = sim.decks(net)
        m = sim.simulate(net, {}, {"backend": "ngspice"}, Path(tempfile.mkdtemp()), tag).metrics
        f, mag = wave(decks[1], "vdb(fbd)")
        _, ph = wave(decks[1], "vp(fbd)")              # degrees: see sim.to_ngspice
        out[tag] = {"metrics": m, "bode": (f, mag, ph), "step": wave(decks[2], "v(out)")}
    res["ldo"] = {"rz_ohm": rz, **{k: {x: out[k]["metrics"][x] for x in
                                        ("pm_deg", "ugf", "step_mv", "kick_mv", "vout")}
                                    for k in out}}
    print("ldo", res["ldo"])

    fig, axes = plt.subplots(1, 3, figsize=(14, 4.2))
    for tag, col, lab in (("no_zero", BEFORE, "1 µF ceramic alone"),
                          ("with_zero", AFTER, f"with {rz:.2f} Ω in series (now)")):
        f, mag, ph = out[tag]["bode"]
        pm = out[tag]["metrics"]["pm_deg"]
        axes[0].semilogx(f, mag, color=col, lw=1.8, label=lab)
        axes[1].semilogx(f, ph, color=col, lw=1.8, label=f"{lab}: margin {pm:.0f}°")
        x, y = out[tag]["step"]
        axes[2].plot([v * 1e6 for v in x], [v * 1e3 - LDO["vout"] * 1e3 for v in y],
                     color=col, lw=1.4, label=lab)
    axes[0].axhline(0, color=LIMIT, ls=":", lw=1)
    axes[0].set_ylim(-40, 110)
    axes[0].set_title("Loop gain", loc="left", fontsize=10.5)
    axes[0].set_ylabel("dB")
    axes[1].axhline(T.PM_MIN, color=LIMIT, ls=":", lw=1)
    axes[1].text(150, T.PM_MIN + 4, f"{T.PM_MIN:.0f}° minimum", color=MUTED, fontsize=8.5)
    axes[1].set_ylim(-180, 200)
    axes[1].set_title("Phase margin: the phase where the gain crosses 0 dB", loc="left",
                      fontsize=10.5)
    axes[1].set_ylabel("degrees")
    for ax in axes[:2]:
        ax.set_xlabel("frequency (Hz)")
        ax.set_xlim(100, 1e8)
    axes[2].set_title("Load step 10 mA → 100 mA → 10 mA", loc="left", fontsize=10.5)
    axes[2].set_xlabel("time (µs)")
    axes[2].set_ylabel("output change (mV)")
    axes[2].set_xlim(10, 110)
    axes[2].set_ylim(-60, 60)
    for ax in axes:
        _clean(ax)
    axes[1].legend(frameon=False, fontsize=8, loc="lower left")
    axes[2].legend(frameon=False, fontsize=8, loc="lower right")
    fig.tight_layout()
    fig.savefig(HERE / "ldo_stability.png", dpi=130)
    plt.close(fig)


def opamp_part(res: dict) -> None:
    loads = [10e-12, 33e-12, 100e-12, 330e-12, 1e-9, 3.3e-9]
    curves = {}
    for g, lab in ((1, "gain 1 (follower)"), (10, "gain 10")):
        jobs = []
        for cl in loads:
            d = T.get("noninverting_amp").design({"gain": g, "bw": 100e3, "gbw": 10e6,
                                                  "cload": cl})
            jobs.append((ir.render(d.netlist, d.start)[0], f"a{g}"))
        with ProcessPoolExecutor(4) as ex:
            curves[lab] = [m.get("pm_deg") for m in ex.map(_sim, jobs)]
    res["opamp_pm_vs_load"] = {"load_pf": [x * 1e12 for x in loads], **curves}
    print("opamp", res["opamp_pm_vs_load"])
    fig, ax = plt.subplots(figsize=(6.6, 3.8))
    for (lab, ys), col in zip(curves.items(), (AFTER, "#38A3A5")):
        ax.semilogx([x * 1e12 for x in loads], ys, "o-", color=col, lw=1.8, label=lab)
    ax.axhspan(0, T.PM_MIN, color=BEFORE, alpha=0.08, lw=0)
    ax.axhline(T.PM_MIN, color=LIMIT, ls=":", lw=1)
    ax.text(11, T.PM_MIN - 9, "below 45°: rings, may oscillate", color=MUTED, fontsize=8.5)
    _clean(ax)
    ax.set_ylim(0, 95)
    ax.set_xlabel("capacitance on the output (pF)")
    ax.set_ylabel("phase margin (°)")
    ax.set_title("Op-amp (10 MHz) driving a capacitive load", loc="left", fontsize=10.5)
    ax.legend(frameon=False, fontsize=8.5, loc="lower left")
    fig.tight_layout()
    fig.savefig(HERE / "opamp_phase_margin.png", dpi=130)
    plt.close(fig)


def main() -> None:
    res: dict = {}
    opamp_part(res)
    ldo_part(res)
    buck_part(res)
    for name, spec in (("sallen_key_lowpass", {"fc": 10000}),
                       ("noninverting_amp", {"gain": 10, "bw": 100e3})):
        d = T.get(name).design(spec)
        m = sim.simulate(ir.render(d.netlist, d.start)[0], {}, {"backend": "ngspice"},
                         Path(tempfile.mkdtemp()), name).metrics
        res[name] = {"pm_deg": m["pm_deg"], "ugf": m["ugf"]}
    json.dump(res, open(HERE / "results.json", "w"), indent=1, default=float)


if __name__ == "__main__":
    main()
