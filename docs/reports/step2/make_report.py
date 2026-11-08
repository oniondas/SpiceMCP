"""Step 2 pictures: the optimizer before vs after, on two filter problems.

    PYTHONPATH=<old checkout> python bench.py 60 10 > bench_before.json
    PYTHONPATH=<this checkout> python bench.py 60 10 > bench_after.json
    python make_report.py
"""
from __future__ import annotations

import json
import math
import statistics
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

HERE = Path(__file__).resolve().parent
BEFORE_C, AFTER_C = "#E07A5F", "#1E56A0"
INK, MUTED, GRID, BG = "#1F2328", "#57606A", "#E6E8EB", "#FCFCFB"
plt.rcParams.update({"font.family": "DejaVu Sans", "font.size": 10, "axes.edgecolor": GRID,
                     "axes.labelcolor": MUTED, "xtick.color": MUTED, "ytick.color": MUTED,
                     "figure.facecolor": BG, "axes.facecolor": BG, "text.color": INK})
TITLES = {"rc": "RC low-pass\nfc = 1 kHz ±2%, smallest C",
          "sallen_key": "Sallen-Key low-pass\nfc = 10 kHz ±3%, Q = 0.707 ±5%, smallest C"}


def load():
    return (json.loads((HERE / "bench_before.json").read_text()),
            json.loads((HERE / "bench_after.json").read_text()))


def _clean(ax, grid="y"):
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)
    ax.tick_params(length=0)
    ax.grid(axis=grid, color=GRID, lw=0.8)
    ax.set_axisbelow(True)


def fig_success(b, a, path):
    fig, axes = plt.subplots(1, 2, figsize=(12, 4.6))
    for ax, name in zip(axes, ("rc", "sallen_key")):
        budget = len(b[name][0]["curve"])
        xs = range(1, budget + 1)
        for runs, color, label in ((b[name], BEFORE_C, "Before (v0.6)"),
                                   (a[name], AFTER_C, "After (v0.7)")):
            frac = [100 * sum(1 for r in runs if r["first_feasible"] and r["first_feasible"] <= x)
                    / len(runs) for x in xs]
            ax.step(xs, frac, where="post", color=color, lw=2.2)
            ax.text(budget + 1, frac[-1], f"{label}: {frac[-1]:.0f}%", color=INK,
                    va="center", fontsize=9.5)
        ax.set_xlim(0, budget * 1.45)
        ax.set_ylim(-3, 108)
        ax.set_xlabel("simulations run")
        ax.set_ylabel("% of runs with a working design")
        ax.set_title(TITLES[name], loc="left", fontsize=10.5)
        _clean(ax, "both")
    fig.suptitle("How often the optimizer finds a design that meets every spec (10 runs each)",
                 x=0.01, ha="left", fontsize=13, fontweight="bold")
    fig.tight_layout(rect=(0, 0, 1, 0.92))
    fig.savefig(path, dpi=150)
    plt.close(fig)


def fig_search_map(b, a, path, seed=3):
    fig, axes = plt.subplots(1, 2, figsize=(12, 5.2), sharey=True)
    rs = [10 ** (x / 10) for x in range(10, 61)]
    for ax, runs, color, title in ((axes[0], b, BEFORE_C, "Before: linear steps"),
                                   (axes[1], a, AFTER_C, "After: log steps + global sample")):
        pts = runs["rc"][seed]["points"]
        ax.plot(rs, [1e9 / (2 * math.pi * r * 1000) for r in rs], color=INK, lw=1,
                ls=(0, (4, 3)))
        ax.scatter([p["R1"] for p in pts], [p["C1"] * 1e9 for p in pts], s=28, color=color,
                   edgecolor=BG, linewidth=1.2, zorder=3)
        best = runs["rc"][seed]["best"]
        ax.set_xscale("log")
        ax.set_yscale("log")
        ax.set_xlim(10, 1e6)
        ax.set_ylim(1, 1e4)
        ax.set_xlabel("R1 (ohms)")
        ax.set_title(f"{title}\nbest working C = "
                     + (f"{best:.3g} nF" if best else "none found"), loc="left", fontsize=10.5)
        _clean(ax, "both")
    axes[0].set_ylabel("C1 (nF)")
    fig.suptitle("Where the 60 simulations went (RC problem, same seed).  Dashed line: fc = 1 kHz, where every working design lies", x=0.01, ha="left",
                 fontsize=13, fontweight="bold")
    fig.tight_layout(rect=(0, 0, 1, 0.92))
    fig.savefig(path, dpi=150)
    plt.close(fig)


def fig_best(b, a, path):
    fig, axes = plt.subplots(1, 2, figsize=(12, 4.4))
    for ax, name in zip(axes, ("rc", "sallen_key")):
        for x, runs, color in ((0, b[name], BEFORE_C), (1, a[name], AFTER_C)):
            vals = sorted(r["best"] for r in runs if r["best"] is not None)
            miss = sum(1 for r in runs if r["best"] is None)
            ax.scatter([x + (i - len(vals) / 2) * 0.03 for i in range(len(vals))], vals,
                       s=40, color=color, edgecolor=BG, linewidth=1.2, zorder=3)
            if vals:
                med = statistics.median(vals)
                ax.hlines(med, x - 0.25, x + 0.25, color=INK, lw=1.5)
                ax.text(x + 0.28, med, f"median {med:.3g} nF", va="center", fontsize=9)
            ax.text(x, 1.6e3 if name == "sallen_key" else 2e3, f"{miss} of {len(runs)} runs\nfound nothing",
                    ha="center", fontsize=8.5, color=MUTED)
        ax.set_yscale("log")
        ax.set_ylim(0.3, 1e4)
        ax.set_xlim(-0.6, 1.9)
        ax.set_xticks([0, 1], ["Before (v0.6)", "After (v0.7)"])
        ax.set_ylabel("best design: total C (nF)\nlower is better")
        ax.set_title(TITLES[name], loc="left", fontsize=10.5)
        _clean(ax)
    fig.suptitle("Quality of the final design (each dot is one run)", x=0.01, ha="left",
                 fontsize=13, fontweight="bold")
    fig.tight_layout(rect=(0, 0, 1, 0.92))
    fig.savefig(path, dpi=150)
    plt.close(fig)


def main():
    b, a = load()
    fig_success(b, a, HERE / "success_rate.png")
    fig_search_map(b, a, HERE / "search_map.png")
    fig_best(b, a, HERE / "best_design.png")
    for name in ("rc", "sallen_key"):
        for tag, d in (("before", b), ("after", a)):
            ok = [r for r in d[name] if r["best"] is not None]
            print(name, tag, f"{len(ok)}/{len(d[name])}",
                  "median first", statistics.median([r["first_feasible"] for r in ok]) if ok else None)


if __name__ == "__main__":
    main()
