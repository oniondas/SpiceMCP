"""Step 7 picture: LTspice (run on Windows) against ngspice, same decks.

    python docs/reports/step7/make_report.py

Reads ltspice_check.json (written by tools/ltspice_check.py on a Windows machine with
LTspice) and simulates the same template decks in ngspice here.
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
sys.path.insert(0, str(HERE.parents[2] / "tools"))
from spicemcp import ir, sim, topologies as T  # noqa: E402
from ltspice_check import SPECS  # noqa: E402

INK, MUTED, GRID, BG, GOOD = "#1F2328", "#57606A", "#E6E8EB", "#FCFCFB", "#1E56A0"
TITLE = {"rc_lowpass": "RC low-pass", "sallen_key_lowpass": "Sallen-Key low-pass",
         "noninverting_amp": "Non-inverting amp", "ldo": "LDO regulator",
         "buck": "Buck converter"}
SHOW = {"rc_lowpass": [("fc", "corner frequency", "Hz")],
        "sallen_key_lowpass": [("f3db", "corner frequency", "Hz"),
                               ("atten10_db", "gain at 10× fc", "dB")],
        "noninverting_amp": [("gain_db", "gain", "dB"), ("bw", "bandwidth", "Hz")],
        "ldo": [("vout", "output at 5 V in", "V"), ("vout_low", "output at dropout", "V")],
        "buck": [("vout", "output voltage", "V"), ("ripple_mv", "ripple", "mV"),
                 ("eff", "efficiency", "")]}


def fmt(v, unit):
    if unit == "Hz":
        return f"{v / 1e3:.4f} kHz" if v < 1e6 else f"{v / 1e6:.4f} MHz"
    if unit == "":
        return f"{v * 100:.1f}%"
    return f"{v:.4f} {unit}"


def main() -> None:
    lt = json.load(open(HERE / "ltspice_check.json"))
    rows, out = [], {}
    for t in lt["simulation"]:
        n = t["template"]
        d = T.get(n).design(SPECS[n])
        net, _ = ir.render(d.netlist, d.start)
        ng = sim.simulate(net, d.start, {"backend": "ngspice"},
                          Path(tempfile.mkdtemp()), n).metrics
        out[n] = {"ltspice": t["ltspice"]["metrics"], "ngspice": ng,
                  "meets_limits": t["ltspice_meets_limits"]}
        for i, (m, lab, unit) in enumerate(SHOW[n]):
            a, b = t["ltspice"]["metrics"][m], ng[m]
            diff = abs(a - b) / abs(b) * 100
            rows.append([TITLE[n] if i == 0 else "", lab, fmt(a, unit), fmt(b, unit),
                         f"{diff:.2f}%",
                         ("YES" if t["ltspice_meets_limits"] else "NO") if i == 0 else ""])
    sch = lt["schematics"]
    fig, ax = plt.subplots(figsize=(11, 0.34 * len(rows) + 1.0))
    fig.patch.set_facecolor(BG)
    ax.axis("off")
    tab = ax.table(cellText=rows, colLabels=["circuit", "measurement", "LTspice 26 (Windows)",
                                             "ngspice 42", "difference", "meets spec\nin LTspice"],
                   loc="center", cellLoc="left", colWidths=[0.2, 0.2, 0.19, 0.17, 0.11, 0.13])
    tab.auto_set_font_size(False)
    tab.set_fontsize(9)
    tab.scale(1, 1.4)
    for (i, j), c in tab.get_celld().items():
        c.set_edgecolor(GRID)
        if i == 0:
            c.set_text_props(weight="bold", color=INK)
            c.set_facecolor("#F0F2F4")
            c.set_height(c.get_height() * 1.6)
        if c.get_text().get_text() == "YES":
            c.set_text_props(color=GOOD, weight="bold")
    fig.savefig(HERE / "ltspice_vs_ngspice.png", dpi=140, bbox_inches="tight",
                facecolor=BG)
    plt.close(fig)
    summary = {
        "ltspice": lt["ltspice"], "platform": lt["platform"],
        "fixtures_made": sum(f["raw"] for f in lt["fixtures"]),
        "fixtures_total": len(lt["fixtures"]),
        "schematics_same_circuit": sum(s["same_circuit"] for s in sch),
        "schematics_total": len(sch),
        "templates": out,
        "max_difference_pct": max(float(r[4].rstrip("%")) for r in rows),
    }
    json.dump(summary, open(HERE / "results.json", "w"), indent=1)
    print(json.dumps({k: v for k, v in summary.items() if k != "templates"}, indent=1))


if __name__ == "__main__":
    main()
