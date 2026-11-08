"""Step 12 pictures: Phase 3, more circuits and a parts list to order (v0.17.0).

    python docs/reports/step12/make_report.py

Inputs, measured with ngspice by `measure/run.py` (seed 12, the defaults a user gets,
start point straight to finish_design; yield over part tolerance, supply ±5 % and
-40/25/85 °C):
  after.json     the eight new templates: start metrics, yield, stress, parts list
  surface_*.json tokens re-sent every turn (`measure/surface.py`), v0.16.0 and v0.17.0
"""
from __future__ import annotations

import json
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

HERE = Path(__file__).resolve().parent
GOOD, MID, BAD = "#2A9D8F", "#1E56A0", "#E07A5F"
INK, MUTED, GRID, BG = "#1F2328", "#57606A", "#E6E8EB", "#FCFCFB"
plt.rcParams.update({"font.family": "DejaVu Sans", "font.size": 9.5, "axes.edgecolor": GRID,
                     "axes.labelcolor": MUTED, "xtick.color": MUTED, "ytick.color": MUTED,
                     "figure.facecolor": BG, "axes.facecolor": BG, "text.color": INK})
NAMES = {"inverting_amp": "Inverting\namp", "bandpass": "Band-pass\nfilter",
         "current_sense": "Current\nsense", "adc_driver": "ADC\ndriver",
         "voltage_reference": "Bandgap\nreference", "protection": "Input\nprotection",
         "boost": "Boost\n5→12 V", "flyback": "Flyback\n12→5 V"}


def load(name):
    return json.loads((HERE / name).read_text())


def yields(a, path):
    ids = [k for k in NAMES if k in a and "answer" in a[k]]
    fig, ax = plt.subplots(figsize=(10.5, 3.7))
    out = {}
    for i, k in enumerate(ids):
        y = a[k]["answer"]["yield"]
        pct = y["yield_pct"]
        col = GOOD if pct >= 90 else MID if pct >= 60 else BAD
        ax.bar(i, pct, 0.6, color=col)
        ax.text(i, pct + 2, f"{y['passed']}/{y['runs']}", ha="center", fontsize=8.5, color=col)
        wc = y.get("worst_case") or {}
        out[k] = {"passed": y["passed"], "runs": y["runs"], "yield_pct": pct,
                  "fails_most": wc.get("fails_most"),
                  "part": (wc.get("part_driving_failures") or {}).get("part"),
                  "minutes": round(a[k]["seconds_finish"] / 60, 1)}
    ax.set_xticks(range(len(ids)), [NAMES[k] for k in ids])
    ax.set_ylabel("boards passing every limit, %")
    ax.set_ylim(0, 110)
    for s in ("top", "right", "left"):
        ax.spines[s].set_visible(False)
    ax.yaxis.grid(True, color=GRID)
    ax.set_axisbelow(True)
    ax.set_title("Yield of each new ready-made circuit: random boards, supply ±5 %, "
                 "-40 / 25 / 85 °C", fontsize=10, loc="left")
    fig.tight_layout()
    fig.savefig(path, dpi=170)
    plt.close(fig)
    return out


def main():
    a = load("after.json")
    res = {"yield": yields(a, HERE / "yield_new.png"),
           "start": {k: v["start_metrics"] for k, v in a.items() if "start_metrics" in v},
           "bom": {k: v.get("bom") for k, v in a.items()},
           "stress": {k: v.get("stress") for k, v in a.items()}}
    sb, sa = load("surface_before.json"), load("surface_after.json")
    res["tokens_every_turn"] = {"before": sb["instructions"] + sb["tool_list"],
                                "after": sa["instructions"] + sa["tool_list"]}
    (HERE / "results.json").write_text(json.dumps(res, indent=1, ensure_ascii=False))
    print(json.dumps(res["yield"], indent=1))


if __name__ == "__main__":
    main()
