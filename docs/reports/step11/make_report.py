"""Step 11 pictures: Phase 2, real parts and real conditions (v0.16.0).

    python docs/reports/step11/make_report.py

Inputs, all measured with ngspice by `measure/run.py` (30 boards per yield, seed 11, the
defaults a user gets, start point straight to finish_design):
  before.json     v0.15.0: part tolerance only, at 27 C
  after.json      v0.16.0: part tolerance, supply +-5 %, -40/25/85 C, stress, real parts
  ldo_fixed.json  v0.16.0 after the LDO fix the corners called for (dropout sized hot)
  surface_*.json  tokens re-sent every turn (`measure/surface.py`)
"""
from __future__ import annotations

import json
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

HERE = Path(__file__).resolve().parent
BEFORE, AFTER, FIXED, BAD = "#C9B79C", "#1E56A0", "#2A9D8F", "#E07A5F"
INK, MUTED, GRID, BG = "#1F2328", "#57606A", "#E6E8EB", "#FCFCFB"
plt.rcParams.update({"font.family": "DejaVu Sans", "font.size": 9.5, "axes.edgecolor": GRID,
                     "axes.labelcolor": MUTED, "xtick.color": MUTED, "ytick.color": MUTED,
                     "figure.facecolor": BG, "axes.facecolor": BG, "text.color": INK})
NAMES = {"rc": "RC low-pass", "sk": "Sallen-Key filter", "amp": "Amplifier x10",
         "ldo": "LDO 5 V to 3.3 V", "buck": "Buck 12 V to 3.3 V"}


def load(name):
    return json.loads((HERE / name).read_text())


def yields(path):
    b, a, f = load("before.json"), load("after.json"), load("ldo_fixed.json")
    ids = list(NAMES)
    fig, ax = plt.subplots(figsize=(10.5, 3.9))
    w = 0.27
    out = {}
    for i, k in enumerate(ids):
        yb = b[k]["answer"]["yield"]
        ya = a[k]["answer"]["yield"]
        out[k] = {"before": [yb["passed"], yb["runs"]], "after": [ya["passed"], ya["runs"]],
                  "after_covers": ya.get("covers"), "worst_case": ya.get("worst_case")}
        ax.bar(i - w, yb["yield_pct"], w, color=BEFORE)
        ax.bar(i, ya["yield_pct"], w, color=AFTER if ya["yield_pct"] else BAD)
        ax.text(i - w, yb["yield_pct"] + 2, f"{yb['passed']}/{yb['runs']}", ha="center",
                fontsize=8, color=MUTED)
        ax.text(i, ya["yield_pct"] + 2, f"{ya['passed']}/{ya['runs']}", ha="center",
                fontsize=8, color=AFTER if ya["yield_pct"] else BAD)
        if k == "ldo":
            yf = f["ldo"]["answer"]["yield"]
            out[k]["fixed"] = [yf["passed"], yf["runs"]]
            ax.bar(i + w, yf["yield_pct"], w, color=FIXED)
            ax.text(i + w, yf["yield_pct"] + 2, f"{yf['passed']}/{yf['runs']}", ha="center",
                    fontsize=8, color=FIXED)
    ax.set_xticks(range(len(ids)), [NAMES[k] for k in ids])
    ax.set_ylabel("boards passing every limit, %")
    ax.set_ylim(0, 108)
    for s in ("top", "right", "left"):
        ax.spines[s].set_visible(False)
    ax.yaxis.grid(True, color=GRID)
    ax.set_axisbelow(True)
    ax.bar(0, 0, color=BEFORE, label="v0.15.0: part tolerance, 27 °C")
    ax.bar(0, 0, color=AFTER, label="v0.16.0: + supply ±5 %, -40 / 25 / 85 °C")
    ax.bar(0, 0, color=FIXED, label="LDO after the fix the corners called for")
    ax.legend(loc="upper center", bbox_to_anchor=(0.5, -0.12), fontsize=8, frameon=False,
              ncol=3)
    ax.set_title("Yield of each ready-made circuit, 30 random boards, same seed", fontsize=10,
                 loc="left")
    fig.tight_layout()
    fig.savefig(path, dpi=170)
    plt.close(fig)
    return out


LABEL = {"dissipates_w": "dissipates {} W", "peak_v": "peak {} V", "reverse_v": "reverse {} V",
         "vds_v": "Vds {} V", "vgs_v": "Vgs {} V", "rms_a": "RMS {} A", "avg_a": "average {} A",
         "peak_a": "peak {} A"}


def _g(v):
    return f"{v * 1000:.2g} m" if abs(v) < 0.1 else f"{v:.3g} "


def stress_table(a):
    rows = []
    for k in ("buck", "ldo"):
        st = a[k]["stress"] or {}
        for part, p in (st.get("parts") or {}).items():
            meas = ", ".join(LABEL[q].format(_g(v)) for q, v in p.items() if q in LABEL)
            rows.append([NAMES[k], part, meas, p["buy"]])
    return rows


def main():
    a = load("after.json")
    res = {"yield": yields(HERE / "yield_corners.png"), "stress": stress_table(a)}
    res["rating_check"] = {k: a[k]["answer"].get("rating_check") for k in
                           ("buck_1n5819", "buck_mbrs340") if k in a}
    res["real_opamp"] = {"sk_toa10": a.get("sk_toa10", {}).get("start_metrics")}
    res["noise_uv"] = {k: a[k]["start_metrics"].get("vn_uv") for k in ("sk", "amp")
                       if "vn_uv" in a[k]["start_metrics"]}
    sb, sa = load("surface_before.json"), load("surface_after.json")
    res["tokens_every_turn"] = {"before": sb["instructions"] + sb["tool_list"],
                                "after": sa["instructions"] + sa["tool_list"]}
    (HERE / "results.json").write_text(json.dumps(res, indent=1, ensure_ascii=False))
    print(json.dumps(res, indent=1, ensure_ascii=False)[:3000])


if __name__ == "__main__":
    main()
