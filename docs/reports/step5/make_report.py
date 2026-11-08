"""Step 5 pictures: from optimised values to a parts list you can order, and its yield.

    python docs/reports/step5/make_report.py

Every number is a real ngspice simulation of the template deck. Per template:
  1. start_from_template + run_optimization(20)       -> the optimiser's best design
  2. "by hand": round every part to its nearest standard value
  3. finish_design                                    -> SpiceMCP's buildable design
Yield is a Monte Carlo over part tolerance (and +-5 % supply for the LDO and buck).
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[2]))
from spicemcp import buildable as B, db, engine, server  # noqa: E402

BEFORE, AFTER, AFTER2, BAND = "#E07A5F", "#1E56A0", "#38A3A5", "#F2CC8F"
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
TITLE = {"rc_lowpass": "RC low-pass", "sallen_key_lowpass": "Sallen-Key low-pass",
         "noninverting_amp": "Non-inverting amp", "ldo": "LDO regulator",
         "buck": "Buck converter"}
SUPPLY = {"ldo": "Vin", "buck": "Vin"}
TIGHT = {"R": 0.001, "C": 0.01, "L": 0.10}
N = 40


def main() -> None:
    tmp = Path(tempfile.mkdtemp())
    server.PROJECT = str(tmp)
    server._con = db.connect(tmp)
    con = server.con()
    res = {}
    for name, spec in SPECS.items():
        rid = name
        server.start_from_template(rid, name, spec, server.SimOptions(backend="ngspice"))
        server.run_optimization(rid, 20)
        sup = SUPPLY.get(name)
        fin = server.finish_design(rid, yield_runs=N, supply=sup or "",
                                   supply_tol_pct=5 if sup else 0)
        cons = db.jloads(engine.get_run(con, rid), "constraints", [])
        opt = db.jloads(engine.get_candidate(con, fin["from_candidate"]), "params")
        series = {k: p["series"] for k, p in fin["parts"].items()}
        tols = {k: p["tolerance_pct"] / 100 for k, p in fin["parts"].items()}
        # "by hand": every part rounded to its nearest standard value
        near = {**opt, **{k: B.snap(opt[k], s) for k, s in series.items()}}
        r = engine.evaluate(con, rid, near, None, None, False, "llm")
        near_d = {"params": near, "candidate_id": r["candidate_id"]}
        near_y = B.monte_carlo(con, rid, near_d, tols, cons, N, sup, 0.05 if sup else 0, 0)
        chosen = {"params": {**fin["other_values"],
                             **{k: p["value"] for k, p in fin["parts"].items()}},
                  "candidate_id": fin["buildable_candidate"]}
        tight = {k: TIGHT[B.part_kind(k)] for k in fin["parts"]}
        tight_y = B.monte_carlo(con, rid, chosen, tight, cons, N, sup,
                                0.05 if sup else 0, 0)
        res[name] = {
            "limits": [f"{c['metric']} {c['op']} {c['value']:g}" for c in cons
                       if not c["metric"].startswith("param_")],
            "optimised": {k: opt[k] for k in fin["parts"]},
            "nearest": {k: near[k] for k in fin["parts"]},
            "chosen": {k: p["value"] for k, p in fin["parts"].items()},
            "series": series, "tolerance_pct": {k: p["tolerance_pct"]
                                                for k, p in fin["parts"].items()},
            "nearest_meets": bool(r["feasible"]), "chosen_meets": fin["meets_all_limits"],
            "chosen_margin_pct": fin["worst_margin_pct"],
            "yield_nearest": near_y["yield_pct"],
            "yield_chosen": fin["yield"]["yield_pct"],
            "yield_tight": tight_y["yield_pct"],
            "supply_tol_pct": 5 if sup else 0,
            "spread_chosen": fin["yield"]["spread"],
            "samples_chosen": json.loads(Path(fin["yield"]["samples_path"]).read_text()),
            "samples_tight": tight_y["samples"],
            "combinations": fin["combinations_simulated"],
        }
        print(name, {k: v for k, v in res[name].items() if not k.startswith("samples")})

    names = list(SPECS)
    # 1. parts lists: optimiser value -> nearest -> chosen
    rows = []
    for n in names:
        r = res[n]
        for k in r["chosen"]:
            rows.append([TITLE[n] if k == next(iter(r["chosen"])) else "", k,
                         B.eng_value(r["optimised"][k]) if r["optimised"][k] < 1e-3 or
                         r["optimised"][k] >= 1 else f"{r['optimised'][k]:.4g}",
                         f"{B.eng_value(r['nearest'][k])}",
                         f"{B.eng_value(r['chosen'][k])}  ({r['series'][k]}, ±{r['tolerance_pct'][k]:g}%)"])
        rows.append([" ", "", "limits met?", "YES" if r["nearest_meets"] else "NO",
                     "YES" if r["chosen_meets"] else "NO"])
    fig, ax = plt.subplots(figsize=(11, 0.32 * len(rows) + 0.8))
    ax.axis("off")
    opt_txt = "optimiser value\n(not a real part)"
    t = ax.table(cellText=rows, colLabels=["circuit", "part", opt_txt,
                                          "rounded by hand\n(nearest standard)",
                                          "SpiceMCP finish_design"],
                 loc="center", cellLoc="left", colWidths=[0.2, 0.07, 0.2, 0.2, 0.33])
    t.auto_set_font_size(False)
    t.set_fontsize(9)
    t.scale(1, 1.35)
    for (i, j), c in t.get_celld().items():
        c.set_edgecolor(GRID)
        if i == 0:
            c.set_text_props(weight="bold", color=INK)
            c.set_facecolor("#F0F2F4")
            c.set_height(c.get_height() * 1.9)
        txt = c.get_text().get_text()
        if txt == "NO":
            c.set_text_props(color=BEFORE, weight="bold")
        elif txt == "YES":
            c.set_text_props(color=AFTER, weight="bold")
    fig.savefig(HERE / "parts_lists.png", dpi=130, bbox_inches="tight")
    plt.close(fig)

    # 2. yield
    fig, ax = plt.subplots(figsize=(11.5, 4))
    x = list(range(len(names)))
    sets = [("yield_nearest", BEFORE, "rounded by hand, standard tolerance"),
            ("yield_chosen", AFTER, "finish_design, standard tolerance"),
            ("yield_tight", AFTER2, "finish_design, precision parts")]
    for i, (key, col, lab) in enumerate(sets):
        vals = [res[n][key] for n in names]
        xs = [v + (i - 1) * 0.27 for v in x]
        ax.bar(xs, vals, 0.25, color=col, label=lab)
        for xx, v in zip(xs, vals):
            ax.text(xx, v + 1.5, f"{v:.0f}%", ha="center", fontsize=8, color=INK)
    ax.set_xticks(x, [TITLE[n] + ("\n(+ ±5% supply)" if n in SUPPLY else "") for n in names])
    ax.set_ylim(0, 112)
    ax.set_ylabel("boards that meet every limit (%)")
    ax.set_title(f"Yield: {N} simulated boards each, every part drawn inside its tolerance",
                 loc="left", fontsize=10.5)
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)
    ax.yaxis.grid(True, color=GRID)
    ax.set_axisbelow(True)
    ax.legend(frameon=False, ncol=3, loc="upper left", bbox_to_anchor=(0, -0.2), fontsize=8.5)
    fig.tight_layout()
    fig.savefig(HERE / "yield.png", dpi=130)
    plt.close(fig)

    # 3. where the boards land, for the two most instructive limits
    pick = [("sallen_key_lowpass", "f3db", "corner frequency (Hz)"),
            ("ldo", "vout", "output voltage (V)"),
            ("buck", "vout", "output voltage (V)")]
    fig, ax = plt.subplots(1, 3, figsize=(13, 3.6))
    for a, (n, m, lab) in zip(ax, pick):
        lims = [c for c in res[n]["limits"] if c.split()[0] == m]
        lo = [float(c.split()[2]) for c in lims if c.split()[1] == ">="]
        hi = [float(c.split()[2]) for c in lims if c.split()[1] == "<="]
        sv = [r["metrics"][m] for r in res[n]["samples_chosen"] if m in r["metrics"]]
        tv = [r["metrics"][m] for r in res[n]["samples_tight"] if m in r["metrics"]]
        allv = sv + tv + lo + hi
        span = max(allv) - min(allv)
        bins = [min(allv) - 0.05 * span + i * span * 1.1 / 24 for i in range(25)]
        if lo and hi:
            a.axvspan(lo[0], hi[0], color=BAND, alpha=0.45, lw=0, label="allowed window")
        a.hist(sv, bins=bins, color=AFTER, alpha=0.55, label="standard tolerance")
        a.hist(tv, bins=bins, histtype="step", color=AFTER2, lw=2.2, label="precision parts")
        for v in lo + hi:
            a.axvline(v, color=BEFORE, lw=1.2)
        a.set_title(f"{TITLE[n]}: {lab}", loc="left", fontsize=9.5)
        a.set_yticks([])
        for s in ("top", "right", "left"):
            a.spines[s].set_visible(False)
    ax[0].legend(frameon=False, fontsize=8, loc="upper left")
    fig.tight_layout()
    fig.savefig(HERE / "where_boards_land.png", dpi=130)
    plt.close(fig)

    json.dump({n: {k: v for k, v in r.items() if not k.startswith("samples")}
               for n, r in res.items()}, open(HERE / "results.json", "w"), indent=1)


if __name__ == "__main__":
    main()
