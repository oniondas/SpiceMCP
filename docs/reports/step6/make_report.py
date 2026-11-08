"""Step 6 pictures: ideal stand-ins versus real devices, same circuit, same values.

    python docs/reports/step6/make_report.py

Each template is simulated twice in ngspice at its hand-sized values:
  ideal  the op-amp as a controlled source (E), the LDO error amp as a behavioural
         source (B), the buck switch as an ideal switch (S): what the templates used
  real   the transistor-level CMOS op-amp and a P-channel power MOSFET: what they use now
"""
from __future__ import annotations

import json
import math
import subprocess
import sys
import tempfile
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[2]))
from spicemcp import ir, layout, opamp, render, sim, topologies as T  # noqa: E402

IDEAL, REAL, LIMIT, BAND = "#E07A5F", "#1E56A0", "#57606A", "#F2CC8F"
INK, MUTED, GRID, BG = "#1F2328", "#57606A", "#E6E8EB", "#FCFCFB"
plt.rcParams.update({"font.family": "DejaVu Sans", "font.size": 9.5, "axes.edgecolor": GRID,
                     "axes.labelcolor": MUTED, "xtick.color": MUTED, "ytick.color": MUTED,
                     "figure.facecolor": BG, "axes.facecolor": BG, "text.color": INK})
SPECS = {
    "sallen_key_lowpass": {"fc": 10000},
    "noninverting_amp": {"gain": 10, "bw": 100e3},
    "ldo": {"vin": 5, "vout": 3.3, "iload": 0.1},
    "buck": {"vin": 12, "vout": 5, "iout": 1},
}
TITLE = {"sallen_key_lowpass": "Sallen-Key low-pass", "noninverting_amp": "Non-inverting amp",
         "ldo": "LDO regulator", "buck": "Buck converter"}
WAVE = {"sallen_key_lowpass": "vdb(out)", "noninverting_amp": "vdb(out)",
        "ldo": "v(out)", "buck": "v(out)"}


def _strip_subckt(net: str) -> str:
    out, inside = [], False
    for ln in net.splitlines():
        if ln.strip().lower().startswith(".subckt"):
            inside = True
        if not inside:
            out.append(ln)
        if ln.strip().lower().startswith(".ends"):
            inside = False
    return "\n".join(out) + "\n"


def ideal(name: str, net: str) -> str:
    """The same deck with the ideal stand-in the template used before."""
    net = _strip_subckt(net)
    gbw = T.get(name).resolve(SPECS[name]).get("gbw", 10e6)
    swap = {
        "sallen_key_lowpass": ("X1 b out vcc vee out SPMCP_OA", "E1 out 0 b out 1e6"),
        "noninverting_amp": ("X1 in inn vcc vee out SPMCP_OA",
                             f"E1 x 0 in inn 1e5\nRp x y 1k\nCp y 0 {1e5 / (2 * math.pi * gbw * 1e3):.6g}\n"
                             "E2 out 0 y 0 1"),
        "ldo": ("X1 fb ref in 0 g SPMCP_OA", "B1 g 0 V=max(0, min(V(in), 2000*(V(fb)-V(ref))))"),
    }
    if name == "buck":
        lines = []
        for ln in net.splitlines():
            if ln.startswith("Vdrv"):
                ln = "Vg g 0 PULSE" + ln.split("PULSE", 1)[1].replace("(0 10 ", "(0 5 ")
            elif ln.startswith("M1 "):
                ln = "S1 in sw g 0 SWM"
            elif ln.startswith(".model PSW"):
                ln = ".model SWM SW(Ron=0.05 Roff=1Meg Vt=2.5 Vh=0)"
            lines.append(ln)
        return "\n".join(lines) + "\n"
    a, b = swap[name]
    assert a in net, (name, a)
    return net.replace(a, b)


def run(net: str, wave: str):
    work = Path(tempfile.mkdtemp())
    deck = sim.to_ngspice(net).replace(".endc", f"wrdata {work / 'w.txt'} {wave}\n.endc")
    (work / "d.cir").write_text(deck)
    out = subprocess.run(["ngspice", "-b", str(work / "d.cir")], capture_output=True,
                         text=True, timeout=300)
    metrics, _ = sim.parse_ngspice(out.stdout)
    xs, ys = [], []
    for line in (work / "w.txt").read_text().splitlines():
        p = line.split()
        if len(p) >= 2:
            xs.append(float(p[0]))
            ys.append(float(p[1]))
    return xs, ys, metrics


def _clean(ax):
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)
    ax.tick_params(length=0)
    ax.grid(color=GRID, lw=0.7)
    ax.set_axisbelow(True)


def main() -> None:
    res = {}
    curves = {}
    for name, spec in SPECS.items():
        d = T.get(name).design(spec)
        real_net, _ = ir.render(d.netlist, d.start)
        ideal_net = ideal(name, real_net)
        ri = run(ideal_net, WAVE[name])
        rr = run(real_net, WAVE[name])
        if name == "sallen_key_lowpass":             # a wider sweep shows the far stopband
            wide = lambda n: n.replace(".ac dec 100 100 1e+06", ".ac dec 100 100 1e+08")
            ri = run(wide(ideal_net), WAVE[name])
            rr = run(wide(real_net), WAVE[name])
        curves[name] = (ri, rr)
        res[name] = {"ideal": ri[2], "real": rr[2]}
        print(name, {k: (round(ri[2].get(k, float("nan")), 4), round(v, 4))
                     for k, v in rr[2].items()})

    # 1. the four comparisons
    fig, axes = plt.subplots(2, 2, figsize=(13, 8.2))
    axes = axes.ravel()
    for ax, name in zip(axes, SPECS):
        (xi, yi, mi), (xr, yr, mr) = curves[name]
        if name == "buck":                           # the last 10 us: the ripple
            end = max(xr)
            ti = [(x, y) for x, y in zip(xi, yi) if x > end - 10e-6]
            tr = [(x, y) for x, y in zip(xr, yr) if x > end - 10e-6]
            xi, yi = [(x - end) * 1e6 + 10 for x, _ in ti], [y for _, y in ti]
            xr, yr = [(x - end) * 1e6 + 10 for x, _ in tr], [y for _, y in tr]
        ax.plot(xi, yi, color=IDEAL, lw=1.6, ls="--", label="ideal stand-in (before)")
        ax.plot(xr, yr, color=REAL, lw=2, label="real devices (now)")
        _clean(ax)
        ax.set_title(TITLE[name], loc="left", fontsize=10.5)
        if name in ("sallen_key_lowpass", "noninverting_amp"):
            ax.set_xscale("log")
            ax.set_xlabel("frequency (Hz)")
            ax.set_ylabel("gain (dB)")
        if name == "sallen_key_lowpass":
            ax.set_ylim(-125, 5)
            f = 1e6
            yi_1m = yi[min(range(len(xi)), key=lambda i: abs(xi[i] - f))]
            yr_1m = yr[min(range(len(xr)), key=lambda i: abs(xr[i] - f))]
            res[name]["at_1MHz"] = {"ideal": yi_1m, "real": yr_1m}
            ax.annotate(f"at 1 MHz: ideal {yi_1m:.0f} dB,\nreal op-amp {yr_1m:.0f} dB "
                        "(signal leaks\nthrough the op-amp's output)", (f, yr_1m),
                        xytext=(1.2e2, -100), fontsize=8.5,
                        arrowprops={"arrowstyle": "-", "color": MUTED})
        elif name == "noninverting_amp":
            ax.axhline(20, color=LIMIT, ls=":", lw=1)
            ax.set_ylim(-10, 25)
            ax.text(12, 3, f"gain at 10 Hz: ideal {mi['gain_db']:.3f} dB, "
                    f"real {mr['gain_db']:.3f} dB\nbandwidth: ideal {mi['bw'] / 1e3:.0f} kHz, "
                    f"real {mr['bw'] / 1e3:.0f} kHz", fontsize=8.5)
        elif name == "ldo":
            ax.set_xlabel("input voltage (V)")
            ax.set_ylabel("output voltage (V)")
            v = SPECS[name]["vout"]
            ax.axhspan(v * 0.99, v * 1.01, color=BAND, alpha=0.6, lw=0)
            ax.text(0.2, 3.65, f"output at 5 V in: ideal {mi['vout']:.4f} V, real "
                    f"{mr['vout']:.4f} V\n(the CMOS amplifier has more gain than the "
                    "stand-in's 2000)", fontsize=8.5)
            ax.set_ylim(0, 4.2)
        elif name == "buck":
            ax.set_xlabel("time (µs), last 10 µs of the run")
            ax.set_ylabel("output voltage (V)")
            ax.axhspan(4.9, 5.1, color=BAND, alpha=0.5, lw=0)
            ax.set_ylim(4.88, 5.12)
            ax.text(0.02, 0.06, f"settled output: ideal switch {mi['vout']:.3f} V, "
                    f"MOSFET {mr['vout']:.3f} V\nefficiency: ideal {mi['eff'] * 100:.1f}%, "
                    f"MOSFET {mr['eff'] * 100:.1f}%", fontsize=8.5,
                    transform=ax.transAxes)
    axes[0].legend(frameon=False, loc="lower left", fontsize=8.5)
    fig.tight_layout()
    fig.savefig(HERE / "ideal_vs_real.png", dpi=130)
    plt.close(fig)

    # 2. the templates as drawn now
    work = Path(tempfile.mkdtemp())
    pics = []
    for name, spec in SPECS.items():
        d = T.get(name).design(spec)
        dr = layout.draw(d.netlist, d.start)
        dr.sheet.directives = [x for x in dr.sheet.directives if not x.startswith(".meas")]
        p = dr.write(work / f"{name}.asc")
        res[name]["readability"] = layout.lint(p)["score"]
        png = work / f"{name}.png"
        render.render(str(p), str(png), "tech_minimal", 130)
        pics.append((name, png))
    fig, axes = plt.subplots(2, 2, figsize=(13, 9.5))
    for ax, (name, png) in zip(axes.ravel(), pics):
        ax.imshow(plt.imread(png))
        ax.axis("off")
        ax.set_title(f"{TITLE[name]}  ·  readability {res[name]['readability']}/100",
                     loc="left", fontsize=10.5, color=REAL)
    fig.tight_layout()
    fig.savefig(HERE / "real_schematics.png", dpi=130)
    plt.close(fig)

    # 3. the op-amp's own figures, measured
    oa = {}
    for gbw in (1e6, 10e6):
        deck = (f"* open-loop op-amp\nVcc vcc 0 5\nVee vee 0 -5\nV1 in 0 DC 0 AC 1\n"
                f"X1 in fb vcc vee out {opamp.NAME}\nRf out fb 1G\nCf fb 0 1\nRL out 0 10k\n"
                + opamp.subckt(gbw) + ".ac dec 50 1 1e9\n"
                ".meas AC a0 FIND mag(V(out)) AT 10\n"
                ".meas AC ugf WHEN mag(V(out))=1 FALL=1\n.end\n")
        _, _, m = run(deck, "vdb(out)")
        dd = opamp.design(gbw)
        oa[f"{gbw:g}"] = {"a0_db": m["a0"], "gbw_hz": m["ugf"],
                          "slew_v_per_us": dd["slew_v_per_us"], "supply_ma": dd["supply_ma"]}
    res["opamp"] = oa
    print(oa)
    json.dump(res, open(HERE / "results.json", "w"), indent=1, default=float)


if __name__ == "__main__":
    main()
