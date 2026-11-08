"""Step 3 pictures: the topology library, simulated in SPICE (ngspice).

    python docs/reports/step3/make_report.py

Every curve is a real simulation of the template deck at its hand-sized values; the
second half runs the optimizer from that start point.
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[2]))
from spicemcp import db, ir, server, sim, topologies as T  # noqa: E402

TRACE, TRACE2, LIMIT, BAND = "#1E56A0", "#38A3A5", "#E07A5F", "#F2CC8F"
INK, MUTED, GRID, BG = "#1F2328", "#57606A", "#E6E8EB", "#FCFCFB"
plt.rcParams.update({"font.family": "DejaVu Sans", "font.size": 9.5, "axes.edgecolor": GRID,
                     "axes.labelcolor": MUTED, "xtick.color": MUTED, "ytick.color": MUTED,
                     "figure.facecolor": BG, "axes.facecolor": BG, "text.color": INK})
SPECS = {
    "rc_lowpass": {"fc": 1000},
    "sallen_key_lowpass": {"fc": 10000},
    "noninverting_amp": {"gain": 10, "bw": 100e3},
    "ldo": {"vin": 5, "vout": 3.3, "iload": 0.1},
    "buck": {"vin": 12, "vout": 5, "iout": 1},
}
WAVE = {"rc_lowpass": "vdb(out)", "sallen_key_lowpass": "vdb(out)",
        "noninverting_amp": "vdb(out)", "ldo": "v(out)", "buck": "v(out)"}


def waveform(name: str, params: dict) -> tuple[list[float], list[float], dict]:
    d = T.get(name).design(SPECS[name])
    net, _ = ir.render(d.netlist, params)
    work = Path(tempfile.mkdtemp())
    deck = sim.to_ngspice(net).replace(".endc", f"wrdata {work / 'w.txt'} {WAVE[name]}\n.endc")
    (work / "d.cir").write_text(deck)
    out = subprocess.run(["ngspice", "-b", str(work / "d.cir")], capture_output=True,
                         text=True, timeout=300)
    metrics, _ = sim.parse_ngspice(out.stdout)
    xs, ys = [], []
    for line in (work / "w.txt").read_text().splitlines():
        parts = line.split()
        if len(parts) >= 2:
            xs.append(float(parts[0]))
            ys.append(float(parts[1]))
    return xs, ys, metrics


def _clean(ax):
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)
    ax.tick_params(length=0)
    ax.grid(color=GRID, lw=0.7)
    ax.set_axisbelow(True)


def fig_gallery(path):
    fig, axes = plt.subplots(2, 3, figsize=(15, 8.6))
    axes = axes.ravel()
    rows = []
    for ax, name in zip(axes, SPECS):
        t, spec = T.get(name), SPECS[name]
        d = t.design(spec)
        xs, ys, m = waveform(name, d.start)
        ax.plot(xs, ys, color=TRACE, lw=2)
        _clean(ax)
        ax.set_title(t.title, loc="left", fontsize=10.5, color=INK)
        if name in ("rc_lowpass", "sallen_key_lowpass", "noninverting_amp"):
            ax.set_xscale("log")
            ax.set_xlabel("frequency (Hz)")
            ax.set_ylabel("gain (dB)")
        if name == "rc_lowpass":
            fc = spec["fc"]
            ax.axvline(fc, color=LIMIT, ls="--", lw=1.2)
            ax.axhline(-3, color=LIMIT, ls=":", lw=1)
            ax.annotate(f"−3 dB at {m['fc']:.0f} Hz\n(spec {fc:g} Hz ±2%)", (m["fc"], -3),
                        xytext=(m["fc"] * 4, -12), fontsize=8.5,
                        arrowprops={"arrowstyle": "-", "color": MUTED})
            rows.append(("RC low-pass", "fc", f"{fc:g} Hz", f"{m['fc']:.1f} Hz"))
        elif name == "sallen_key_lowpass":
            fc = spec["fc"]
            ax.plot(xs, [-10 * __import__("math").log10(1 + (x / fc) ** 2) for x in xs],
                    color=TRACE2, lw=1.4, ls="--")
            ax.axvline(fc, color=LIMIT, ls="--", lw=1.2)
            ax.text(xs[0] * 1.5, -60, "solid: Sallen-Key, 40 dB/decade\n"
                    "dashed: 1st-order RC, 20 dB/decade", fontsize=8.5)
            ax.set_ylim(-85, 5)
            rows.append(("Sallen-Key", "fc (−3 dB)", f"{fc:g} Hz", f"{m['f3db']:.0f} Hz"))
            rows.append(("Sallen-Key", "gain at fc/2", "≥ −0.5 dB", f"{m['g_half_db']:.2f} dB"))
        elif name == "noninverting_amp":
            ax.axhline(20, color=LIMIT, ls=":", lw=1)
            ax.axvline(spec["bw"], color=LIMIT, ls="--", lw=1.2)
            ax.text(spec["bw"] * 1.2, 2, f"bandwidth spec ≥ {spec['bw'] / 1e3:g} kHz",
                    fontsize=8.5)
            rows.append(("Amplifier", "gain", "20.0 dB (10×)", f"{m['gain_db']:.2f} dB"))
            rows.append(("Amplifier", "bandwidth", "≥ 100 kHz", f"{m['bw'] / 1e3:.0f} kHz"))
        elif name == "ldo":
            ax.set_xlabel("input voltage (V)")
            ax.set_ylabel("output voltage (V)")
            ax.axhspan(spec["vout"] * 0.99, spec["vout"] * 1.01, color=BAND, alpha=0.6, lw=0)
            ax.axvline(spec["vout"] + 0.3, color=LIMIT, ls="--", lw=1.2)
            ax.text(spec["vout"] + 0.38, 0.6, "dropout point\n(Vout + 0.3 V)", fontsize=8.5)
            rows.append(("LDO", "Vout at 5 V in", "3.30 V ±1%", f"{m['vout']:.3f} V"))
            rows.append(("LDO", "Vout at 3.6 V in", "≥ 3.234 V", f"{m['vout_low']:.3f} V"))
        elif name == "buck":
            ax.set_xlabel("time (ms)")
            ax.set_ylabel("output voltage (V)")
            xs_ms = [x * 1e3 for x in xs]
            ax.lines[0].set_xdata(xs_ms)
            ax.axhspan(spec["vout"] * 0.98, spec["vout"] * 1.02, color=BAND, alpha=0.6, lw=0)
            ax.set_xlim(0, max(xs_ms))
            ins = ax.inset_axes((0.45, 0.12, 0.5, 0.4))
            tail = [(x, y) for x, y in zip(xs_ms, ys) if x > max(xs_ms) - 0.01]
            ins.plot([x for x, _ in tail], [y for _, y in tail], color=TRACE, lw=1.2)
            ins.set_title("zoom: last 10 µs (ripple)", fontsize=8)
            ins.tick_params(labelsize=7, length=0)
            rows.append(("Buck", "Vout", "5.0 V ±2%", f"{m['vout']:.3f} V"))
            rows.append(("Buck", "ripple", "≤ 50 mV", f"{m['ripple_mv']:.1f} mV"))
            rows.append(("Buck", "efficiency", "(reported)", f"{m['eff'] * 100:.1f} %"))
    ax = axes[5]
    ax.axis("off")
    ax.set_title("Spec vs simulated, hand-sized values (no optimization)", loc="left",
                 fontsize=10.5)
    tab = ax.table(cellText=[list(r) for r in rows],
                   colLabels=["circuit", "metric", "spec", "simulated"],
                   loc="upper left", cellLoc="left", colLoc="left")
    tab.auto_set_font_size(False)
    tab.set_fontsize(8.5)
    tab.scale(1, 1.35)
    for (r, _c), cell in tab.get_celld().items():
        cell.set_edgecolor(GRID)
        if r == 0:
            cell.set_text_props(fontweight="bold")
    fig.suptitle("Topology library: five circuits, sized by textbook equations, then simulated",
                 x=0.01, ha="left", fontsize=13.5, fontweight="bold")
    fig.tight_layout(rect=(0, 0, 1, 0.95))
    fig.savefig(path, dpi=150)
    plt.close(fig)
    return rows


def optimize_all(iterations=40):
    tmp = Path(tempfile.mkdtemp())
    server.PROJECT, server._con = str(tmp), db.connect(tmp)
    out = {}
    for name, spec in SPECS.items():
        r = server.start_from_template(name, name, spec, server.SimOptions(backend="ngspice"))
        o = server.run_optimization(name, iterations)
        best = server.query_run(name, "candidate", candidate_id=o["best_candidate"])
        out[name] = {"start": r["start_point"], "start_metrics": r["start_result"]["metrics"],
                     "start_feasible": r["start_result"]["feasible"],
                     "best": best["params"], "best_metrics": o["best_metrics"],
                     "best_feasible": o["best_is_feasible"],
                     "sims": o["iterations_evaluated"], "working": r["hand_calculation"]}
    return out


def fig_optimized(res, path):
    show = [("rc_lowpass", lambda r: r["C1"] * 1e9, "RC: capacitor C1 (nF), smaller is better", None),
            ("ldo", lambda r: r["W"] * 1e3, "LDO: pass transistor width (mm), smaller is better", None),
            ("buck", None, "Buck: output ripple (mV), limit 50 mV", 50.0)]
    fig, axes = plt.subplots(1, 3, figsize=(13, 4.2))
    for ax, (name, f, label, limit) in zip(axes, show):
        r = res[name]
        if f:
            vals = [f(r["start"]), f(r["best"])]
        else:
            vals = [r["start_metrics"]["ripple_mv"], r["best_metrics"]["ripple_mv"]]
        ax.bar([0, 1], vals, color=[TRACE2, TRACE], width=0.55, edgecolor=BG, linewidth=2)
        for x, v in zip([0, 1], vals):
            ax.text(x, v, f"{v:.3g}", ha="center", va="bottom", fontsize=11, fontweight="bold")
        if limit:
            ax.axhline(limit, color=LIMIT, ls="--", lw=1.2)
        ok = ["meets spec" if r["start_feasible"] else "misses spec",
              "meets spec" if r["best_feasible"] else "misses spec"]
        ax.set_xticks([0, 1], [f"hand-sized\n({ok[0]})", f"optimized\n({ok[1]})"])
        ax.set_ylim(0, max(vals) * 1.25)
        ax.set_yticks([])
        ax.set_title(label, loc="left", fontsize=10)
        for s in ("top", "right", "left"):
            ax.spines[s].set_visible(False)
        ax.tick_params(length=0)
    fig.suptitle("The optimizer refines the hand-sized start (40 simulations each)",
                 x=0.01, ha="left", fontsize=13, fontweight="bold")
    fig.tight_layout(rect=(0, 0, 1, 0.9))
    fig.savefig(path, dpi=150)
    plt.close(fig)


def main():
    rows = fig_gallery(HERE / "template_gallery.png")
    res = optimize_all()
    fig_optimized(res, HERE / "optimized_from_start.png")
    (HERE / "results.json").write_text(json.dumps({"spec_vs_sim": rows, "optimized": res},
                                                  indent=1, default=float))
    for n, r in res.items():
        print(n, r["start_feasible"], "->", r["best_feasible"], r["best"], r["sims"])


if __name__ == "__main__":
    main()
