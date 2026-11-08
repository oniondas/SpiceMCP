"""Step 9 pictures: Phase 0 quick fixes (v0.14.0).

    python docs/reports/step9/make_report.py

Token numbers come from the same 17 flows run end to end over MCP stdio, before (v0.13.0,
`tokens_before.json`) and after (v0.14.0, `tokens_after.json`), counted with the same
tokenizer by `measure/analyze.py`. The yield picture is pure arithmetic.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[2]))
from spicemcp.buildable import yield_lower_bound  # noqa: E402

BEFORE, AFTER, BAND = "#E07A5F", "#1E56A0", "#F2CC8F"
INK, MUTED, GRID, BG = "#1F2328", "#57606A", "#E6E8EB", "#FCFCFB"
plt.rcParams.update({"font.family": "DejaVu Sans", "font.size": 9.5, "axes.edgecolor": GRID,
                     "axes.labelcolor": MUTED, "xtick.color": MUTED, "ytick.color": MUTED,
                     "figure.facecolor": BG, "axes.facecolor": BG, "text.color": INK})


def load():
    return (json.loads((HERE / "tokens_before.json").read_text()),
            json.loads((HERE / "tokens_after.json").read_text()))


def tokens_figure(b, a, path):
    fig, ax = plt.subplots(1, 2, figsize=(10.5, 3.9), gridspec_kw={"width_ratios": [1, 1.6]})
    # left: fixed cost re-sent every turn, and the three biggest answers
    names = ["tool list +\ninstructions", "start_from\n_template", "finish\n_design",
             "list\n_templates"]
    def avg(d, t):
        r = d["per_tool_resp"].get(t)
        return r["avg"] if r else 0
    bv = [b["overhead"]["total"]] + [avg(b, t) for t in
                                     ("start_from_template", "finish_design", "list_templates")]
    av = [a["overhead"]["total"]] + [avg(a, t) for t in
                                     ("start_from_template", "finish_design", "list_templates")]
    x = range(len(names))
    ax[0].bar([i - 0.2 for i in x], bv, 0.4, color=BEFORE, label="v0.13.0")
    ax[0].bar([i + 0.2 for i in x], av, 0.4, color=AFTER, label="v0.14.0")
    for i, (p, q) in enumerate(zip(bv, av)):
        ax[0].text(i - 0.2, p, f"{p:,}", ha="center", va="bottom", fontsize=8, color=MUTED)
        ax[0].text(i + 0.2, q, f"{q:,}", ha="center", va="bottom", fontsize=8, color=AFTER)
    ax[0].set_xticks(list(x), names, fontsize=8.5)
    ax[0].set_ylabel("tokens")
    ax[0].set_title("Re-sent every turn, and the biggest answers", fontsize=10, loc="left")
    ax[0].legend(frameon=False, fontsize=8.5)
    ax[0].grid(axis="y", color=GRID)
    # right: billed input per circuit, no cache
    rb = {r["no"]: r for r in b["rows"]}
    ra = {r["no"]: r for r in a["rows"]}
    nos = sorted(set(rb) & set(ra))
    y = list(range(len(nos)))[::-1]
    ax[1].barh([v + 0.2 for v in y], [rb[n]["billed_in"] / 1000 for n in nos], 0.4,
               color=BEFORE)
    ax[1].barh([v - 0.2 for v in y], [ra[n]["billed_in"] / 1000 for n in nos], 0.4,
               color=AFTER)
    ax[1].set_yticks(y, [f"{n}. {rb[n]['name']}" for n in nos], fontsize=7.5)
    ax[1].set_xlabel("input tokens billed for the whole design, thousands (no cache)")
    tb, ta = (sum(rb[n]["billed_in"] for n in nos), sum(ra[n]["billed_in"] for n in nos))
    ax[1].set_title(f"All 17 circuits: {tb / 1000:.0f}k → {ta / 1000:.0f}k tokens "
                    f"({(1 - ta / tb) * 100:.0f}% less)", fontsize=10, loc="left")
    ax[1].grid(axis="x", color=GRID)
    for s in ("top", "right"):
        ax[0].spines[s].set_visible(False)
        ax[1].spines[s].set_visible(False)
    fig.tight_layout()
    fig.savefig(path, dpi=170)
    plt.close(fig)
    return {"overhead_before": b["overhead"]["total"], "overhead_after": a["overhead"]["total"],
            "billed_before": tb, "billed_after": ta,
            "cached_before": sum(rb[n]["billed_in_cached"] for n in nos),
            "cached_after": sum(ra[n]["billed_in_cached"] for n in nos),
            "answers_before": sum(rb[n]["resp"] for n in nos),
            "answers_after": sum(ra[n]["resp"] for n in nos),
            "per_tool_before": {k: avg(b, k) for k in ("start_from_template", "finish_design",
                                                       "list_templates")},
            "per_tool_after": {k: avg(a, k) for k in ("start_from_template", "finish_design",
                                                      "list_templates")}}


def yield_figure(path):
    fig, ax = plt.subplots(figsize=(10.5, 3.6))
    ns = list(range(5, 301, 5))
    ax.axhspan(0, 100, color=BG)
    ax.plot(ns, [100 for _ in ns], color=BEFORE, lw=2,
            label='v0.13.0 reading of "n of n passed": 100% yield, ± 100/n %')
    ax.fill_between(ns, [100 - 100 / n for n in ns], [100 for _ in ns], color=BEFORE,
                    alpha=0.15)
    ax.plot(ns, [100 * yield_lower_bound(n, n) for n in ns], color=AFTER, lw=2.2,
            label="v0.14.0: yield is at least this, 95% confidence")
    for n in (30, 100, 300):
        lo = 100 * yield_lower_bound(n, n)
        ax.plot([n], [lo], "o", color=AFTER)
        ax.annotate(f"{n}/{n} pass → at least {lo:.0f}%", (n, lo),
                    (n + 6 if n < 300 else n - 75, lo - 7), fontsize=8.5, color=AFTER)
    ax.set_xlabel("boards simulated, all of them passing")
    ax.set_ylabel("yield, %")
    ax.set_ylim(60, 102)
    ax.grid(color=GRID)
    ax.legend(frameon=False, fontsize=8.5, loc="lower right")
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)
    ax.set_title("What a clean Monte Carlo run proves", fontsize=10, loc="left")
    fig.tight_layout()
    fig.savefig(path, dpi=170)
    plt.close(fig)


def main():
    b, a = load()
    res = tokens_figure(b, a, HERE / "tokens.png")
    yield_figure(HERE / "yield_bound.png")
    res["yield_30_of_30_at_least_pct"] = round(100 * yield_lower_bound(30, 30), 1)
    (HERE / "results.json").write_text(json.dumps(res, indent=1))
    print(json.dumps(res, indent=1))


if __name__ == "__main__":
    main()
