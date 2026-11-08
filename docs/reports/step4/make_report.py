"""Step 4 pictures: schematics drawn from a netlist, before and after the auto-placer.

    python docs/reports/step4/make_report.py

"Before" is what the previous version could do without a person choosing coordinates:
parts dropped in a row in netlist order, each net joined pin to pin with right-angle
wires (the old `Sheet.connect`). "After" is `layout.draw`. Both are scored by the same
readability lint and both are read back and compared with the netlist.
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.image as mpimg  # noqa: E402
import matplotlib.pyplot as plt  # noqa: E402

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[2]))
sys.path.insert(0, str(HERE.parents[2] / "tests"))
from spicemcp import asc, ir, layout, render, topologies as T  # noqa: E402
from test_layout import UNSEEN  # noqa: E402

BEFORE, AFTER, BAND = "#E07A5F", "#1E56A0", "#F2CC8F"
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
WORK = Path(tempfile.mkdtemp())


def naive(netlist: str) -> asc.Sheet:
    """The old way: a row of parts, every net chained pin to pin with L-shaped wires."""
    els, dirs, notes = layout.parse(netlist)
    sh = asc.Sheet()
    nets: dict[str, list] = {}
    for i, e in enumerate(els):
        p = sh.part(e.sym, e.name, (i * 160, 0), "R0", value=e.rest or None)
        for k, n in enumerate(e.pin_nodes):
            pt = p[p.meta["order"][k]]
            if n.lower() in layout.GND:
                sh.flag(pt, "0")
            else:
                nets.setdefault(n, []).append(pt)
    for pts in nets.values():
        for a, b in zip(pts, pts[1:]):
            sh.connect(a, b)
    sh.directive(*dirs)
    return sh


def png(sheet: asc.Sheet, name: str) -> Path:
    a = sheet.write(WORK / f"{name}.asc")
    out = WORK / f"{name}.png"
    render.render(str(a), str(out), "tech_minimal", 160)
    return out


def main() -> None:
    rows = {}
    for name, spec in SPECS.items():
        d = T.get(name).design(spec)
        net, _ = ir.render(d.netlist, d.start)
        old, new = naive(net), layout.draw(net).sheet
        rows[name] = {
            "before": {**layout.lint_sheet(old), "wrong_connections": layout.verify(old, net)},
            "after": {**layout.lint_sheet(new), "wrong_connections": layout.verify(new, net)},
            "png_before": png(old, f"{name}_before"), "png_after": png(new, f"{name}_after")}
    unseen = {}
    for name, net in UNSEEN.items():
        sh = layout.draw(net).sheet
        unseen[name] = {**layout.lint_sheet(sh), "png": png(sh, f"u_{name}")}

    # 1. before / after side by side, the three that show it best
    show = ["sallen_key_lowpass", "noninverting_amp", "buck"]
    fig, ax = plt.subplots(len(show), 2, figsize=(13, 4.1 * len(show)),
                           gridspec_kw={"wspace": 0.04, "hspace": 0.22})
    for i, name in enumerate(show):
        r = rows[name]
        for j, side in enumerate(("before", "after")):
            a = ax[i, j]
            a.imshow(mpimg.imread(r[f"png_{side}"]))
            a.axis("off")
            s = r[side]
            bad = len(s["wrong_connections"])
            status = (f"WRONG CIRCUIT: {bad} node(s) shorted or broken" if bad
                      else "same circuit as the netlist")
            a.set_title(f"{TITLE[name]}: {'BEFORE (parts in a row)' if side == 'before' else 'AFTER (auto-placed)'}\n"
                        f"{status}\nscore {s['score']}/100 · {s['through_parts']} wires through parts"
                        f" · {s['crossings']} crossings",
                        fontsize=9.5, color=BEFORE if side == "before" else AFTER, loc="left")
    fig.savefig(HERE / "before_after.png", dpi=110, bbox_inches="tight")
    plt.close(fig)

    # 2. the numbers for all five templates
    names = list(SPECS)
    fig, ax = plt.subplots(1, 3, figsize=(13, 3.6))
    x = range(len(names))
    for a, key, label in ((ax[0], "score", "readability score (higher is better)"),
                          (ax[1], "wrong_connections", "nodes shorted or broken (must be 0)"),
                          (ax[2], "through_parts", "wires drawn through a part (lower is better)")):
        val = (lambda r: len(r[key]) if isinstance(r[key], list) else r[key])
        b = [val(rows[n]["before"]) for n in names]
        f = [val(rows[n]["after"]) for n in names]
        a.bar([i - 0.2 for i in x], b, 0.38, color=BEFORE, label="before")
        a.bar([i + 0.2 for i in x], f, 0.38, color=AFTER, label="after")
        for i in x:
            a.text(i - 0.2, b[i], str(b[i]), ha="center", va="bottom", fontsize=8, color=MUTED)
            a.text(i + 0.2, f[i], str(f[i]), ha="center", va="bottom", fontsize=8, color=INK)
        a.set_xticks(list(x), [TITLE[n].replace(" ", "\n", 1) for n in names], fontsize=8)
        a.set_title(label, fontsize=10, loc="left")
        for s in ("top", "right"):
            a.spines[s].set_visible(False)
        a.yaxis.grid(True, color=GRID)
        a.set_axisbelow(True)
    fig.legend(*ax[0].get_legend_handles_labels(), frameon=False, ncol=2,
               loc="upper right")
    fig.tight_layout(rect=(0, 0, 1, 0.93))
    fig.savefig(HERE / "scores.png", dpi=130)
    plt.close(fig)

    # 3. circuits the placer was never tuned on
    order = ["inverting", "cs_amp", "bridge", "boost", "two_stage", "pi_filter"]
    fig, ax = plt.subplots(3, 2, figsize=(13, 12), gridspec_kw={"wspace": 0.04, "hspace": 0.15})
    label = {"inverting": "Inverting amplifier", "cs_amp": "Common-source MOSFET amp",
             "bridge": "Wheatstone bridge", "boost": "Boost converter",
             "two_stage": "Two-stage amp with feedback", "pi_filter": "Pi filter"}
    for a, n in zip(ax.flat, order):
        a.imshow(mpimg.imread(unseen[n]["png"]))
        a.axis("off")
        a.set_title(f"{label[n]}  ·  score {unseen[n]['score']}/100  ·  "
                    f"{unseen[n]['crossings']} crossings", fontsize=9.5, loc="left", color=AFTER)
    fig.savefig(HERE / "unseen_circuits.png", dpi=100, bbox_inches="tight")
    plt.close(fig)

    def strip(r):
        return {k: (len(v) if k == "wrong_connections" else v) for k, v in r.items()}
    json.dump({"templates": {n: {"before": strip(r["before"]), "after": strip(r["after"])}
                             for n, r in rows.items()},
               "unseen": {n: {k: v for k, v in u.items() if k != "png"}
                          for n, u in unseen.items()}},
              open(HERE / "results.json", "w"), indent=1)
    for n, r in rows.items():
        print(n, "before", strip(r["before"]), "\n   after", strip(r["after"]))


if __name__ == "__main__":
    main()
