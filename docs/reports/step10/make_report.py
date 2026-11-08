"""Step 10 pictures: Phase 1, usable day to day (v0.15.0).

    python docs/reports/step10/make_report.py

Inputs, all measured: `buck_call_times.json` (every tool call of the buck flow, seconds,
v0.14.0 and v0.15.0, ngspice on a 4-core machine) and the token counts of the 17 flows
(`tokens_v013.json`, `tokens_before.json` = v0.14.0, `tokens_after.json` = v0.15.0)
from `measure/run.py` + `measure/analyze.py`.
"""
from __future__ import annotations

import json
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

HERE = Path(__file__).resolve().parent
OLD, BEFORE, AFTER, LIMIT = "#C9B79C", "#E07A5F", "#1E56A0", "#57606A"
INK, MUTED, GRID, BG = "#1F2328", "#57606A", "#E6E8EB", "#FCFCFB"
plt.rcParams.update({"font.family": "DejaVu Sans", "font.size": 9.5, "axes.edgecolor": GRID,
                     "axes.labelcolor": MUTED, "xtick.color": MUTED, "ytick.color": MUTED,
                     "figure.facecolor": BG, "axes.facecolor": BG, "text.color": INK})
SHORT = {"list_templates": "list", "start_from_template": "start", "run_optimization": "optimise",
         "finish_design": "finish", "generate_design_report": "report",
         "render_schematic": "draw"}


def timeline(path):
    t = json.loads((HERE / "buck_call_times.json").read_text())
    fig, ax = plt.subplots(figsize=(10.5, 3.3))
    for row, (ver, col) in enumerate((("v0.14.0", BEFORE), ("v0.15.0", AFTER))):
        x = 0.0
        y = 1 - row
        for tool, s in t[ver]:
            ax.barh(y, max(s, 0.5), left=x, height=0.55, color=col,
                    edgecolor=BG, linewidth=1.2)
            if s > 40:
                lab = SHORT.get(tool, tool)
                ax.text(x + s / 2, y, lab if s > 100 else lab[0].upper(), ha="center",
                        va="center", fontsize=7.5, color="white")
            x += s
        ax.text(x + 8, y, f"{x / 60:.1f} min, longest call {max(s for _, s in t[ver]):.0f} s",
                va="center", fontsize=9, color=col)
    ax.set_yticks([1, 0], ["v0.14.0", "v0.15.0"])
    ax.set_xlabel("seconds from the first call (each block is one tool call)")
    ax.set_xlim(0, 1500)
    ax.axvline(60, color=LIMIT, ls="--", lw=1)
    ax.text(62, 1.45, "60 s: where many clients give up on a call", fontsize=8, color=LIMIT)
    ax.set_ylim(-0.5, 1.6)
    for s in ("top", "right", "left"):
        ax.spines[s].set_visible(False)
    ax.set_title("Buck converter, whole design flow on 4 CPU cores (O = optimise, "
                 "F = finish)", fontsize=10, loc="left")
    fig.tight_layout()
    fig.savefig(path, dpi=170)
    plt.close(fig)
    return {v: {"total_s": round(sum(s for _, s in t[v])), "longest_s": max(s for _, s in t[v]),
                "calls": len(t[v])} for v in t}


def tokens(path):
    d = {v: json.loads((HERE / f).read_text()) for v, f in
         (("v0.13.0", "tokens_v013.json"), ("v0.14.0", "tokens_before.json"),
          ("v0.15.0", "tokens_after.json"))}
    rows = {v: {r["no"]: r for r in x["rows"]} for v, x in d.items()}
    pick = [(6, "Instrumentation amp\n(custom netlist)"), (15, "Op-amp block\ntest"),
            (16, "Comparator\ntest"), (5, "Buck\n(long calls)")]
    fig, ax = plt.subplots(1, 2, figsize=(10.5, 3.6), gridspec_kw={"width_ratios": [1.6, 1]})
    for k, (v, col) in enumerate((("v0.13.0", OLD), ("v0.14.0", BEFORE), ("v0.15.0", AFTER))):
        ax[0].bar([i + (k - 1) * 0.27 for i in range(len(pick))],
                  [rows[v][n]["billed_in"] / 1000 for n, _ in pick], 0.27, color=col, label=v)
    ax[0].set_xticks(range(len(pick)), [p for _, p in pick], fontsize=8.5)
    ax[0].set_ylabel("input tokens, thousands (no cache)")
    ax[0].legend(frameon=False, fontsize=8.5)
    ax[0].grid(axis="y", color=GRID)
    ax[0].set_title("Circuits this phase changed", fontsize=10, loc="left")
    tot = {v: sum(r["billed_in"] for r in x["rows"]) for v, x in d.items()}
    cac = {v: sum(r["billed_in_cached"] for r in x["rows"]) for v, x in d.items()}
    vs = list(tot)
    ax[1].bar([i - 0.2 for i in range(3)], [tot[v] / 1000 for v in vs], 0.4,
              color=[OLD, BEFORE, AFTER])
    ax[1].bar([i + 0.2 for i in range(3)], [cac[v] / 1000 for v in vs], 0.4,
              color=[OLD, BEFORE, AFTER], alpha=0.45)
    for i, v in enumerate(vs):
        ax[1].text(i - 0.2, tot[v] / 1000, f"{tot[v] / 1000:.0f}k", ha="center",
                   va="bottom", fontsize=8)
        ax[1].text(i + 0.2, cac[v] / 1000, f"{cac[v] / 1000:.0f}k", ha="center",
                   va="bottom", fontsize=8, color=MUTED)
    ax[1].set_xticks(range(3), vs)
    ax[1].set_title("All 17 circuits (solid: no cache, light: cached)", fontsize=10,
                    loc="left")
    ax[1].grid(axis="y", color=GRID)
    for a in ax:
        for s in ("top", "right"):
            a.spines[s].set_visible(False)
    fig.tight_layout()
    fig.savefig(path, dpi=170)
    plt.close(fig)
    return {"total": tot, "cached": cac,
            "per_circuit": {n: {v: rows[v][n]["billed_in"] for v in vs} for n, _ in pick}}


def main():
    res = {"buck": timeline(HERE / "buck_timeline.png"), "tokens": tokens(HERE / "tokens.png")}
    (HERE / "results.json").write_text(json.dumps(res, indent=1))
    print(json.dumps(res, indent=1))


if __name__ == "__main__":
    main()
