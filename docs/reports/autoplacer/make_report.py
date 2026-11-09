"""Auto-placer report: 14 hard circuits drawn before (1.0.0) and after (1.1.0).

    python docs/reports/autoplacer/make_report.py   ->  AutoPlacer_Report.pdf + REPORT.md

Six templates and eight circuits with no template (transistor-level op-amp, BJT diff
pair, class-AB stage, state-variable filter, instrumentation amp, Wien oscillator,
4th-order Sallen-Key, bare flyback). The "before" pictures and scores were captured from
the 1.0.0 drawer and are kept in figures/ and before.json; "after" is drawn fresh.
Needs matplotlib and reportlab.
"""
from __future__ import annotations

import json
import logging
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
sys.path[:0] = [str(ROOT), str(ROOT / "tests")]
logging.getLogger("matplotlib.font_manager").setLevel(logging.ERROR)

from spicemcp import layout, render, topologies as T  # noqa: E402
from test_blocks import HARD  # noqa: E402
from test_topologies import SPECS  # noqa: E402

FIG = HERE / "figures"
TITLE = {
    "ldo": "LDO regulator (template)", "buck": "Buck converter (template)",
    "voltage_reference": "Bandgap reference (template)",
    "boost": "Boost converter, closed loop (template)",
    "flyback": "Flyback converter, closed loop (template)",
    "bandpass": "Band-pass filter (template)",
    "instrumentation": "Instrumentation amplifier (no template)",
    "cmos_opamp": "8-MOSFET op-amp, transistor level (no template)",
    "state_variable": "State-variable filter, 3 op-amps (no template)",
    "bjt_diff_pair": "BJT diff pair, mirror load, follower (no template)",
    "wien_oscillator": "Wien-bridge oscillator (no template)",
    "class_ab": "Class-AB power stage with op-amp (no template)",
    "sallen_key_4th": "4th-order Sallen-Key low-pass (no template)",
    "bare_flyback": "Bare flyback with transformer (no template)",
}
SHOW = ["cmos_opamp", "class_ab", "bjt_diff_pair", "instrumentation", "flyback",
        "state_variable", "boost", "ldo"]


def netlists() -> dict:
    out = {}
    for n in ("ldo", "buck", "voltage_reference", "boost", "flyback", "bandpass"):
        d = T.get(n).design(SPECS[n])
        out[n] = (d.netlist, d.start)
    for n in ("instrumentation", "cmos_opamp", "state_variable", "bjt_diff_pair",
              "wien_oscillator", "class_ab", "sallen_key_4th"):
        out[n] = (HARD[n], None)
    out["bare_flyback"] = (HARD["flyback"], None)
    return out


def draw_after() -> dict:
    res = {}
    work = Path(tempfile.mkdtemp())
    for name, (net, vals) in netlists().items():
        t = time.time()
        d = layout.draw(net, vals)
        d.sheet.directives = [x for x in d.sheet.directives if not x.startswith(".meas")]
        p = d.write(work / f"{name}.asc")
        s = layout.lint(p)
        render.render(str(p), str(FIG / f"after_{name}.png"), "tech_minimal", 110)
        res[name] = {"score": s["score"], "crossings": s["crossings"],
                     "jump_labels": s["jump_labels"], "sec": round(time.time() - t, 1),
                     "blocks": sorted({b.kind for b in d.blocks})}
        print(name, res[name], flush=True)
    (HERE / "after.json").write_text(json.dumps(res, indent=1))
    return res


def build_pdf(before: dict, after: dict) -> None:
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import landscape, A4
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.platypus import (Image, PageBreak, Paragraph, SimpleDocTemplate,
                                    Spacer, Table, TableStyle)
    from PIL import Image as PIL

    ss = getSampleStyleSheet()
    doc = SimpleDocTemplate(str(HERE / "AutoPlacer_Report.pdf"), pagesize=landscape(A4),
                            leftMargin=14 * mm, rightMargin=14 * mm, topMargin=12 * mm,
                            bottomMargin=12 * mm)
    W = landscape(A4)[0] - 28 * mm
    story = [Paragraph("Schematic auto-placer: before and after", ss["Title"]),
             Paragraph("Readability score 0 to 100 (higher is easier to read). Every "
                       "drawing, before and after, is checked pin by pin to be the "
                       "netlist's circuit.", ss["Normal"]), Spacer(1, 6)]

    def sc(r):
        return "refused" if "error" in r else str(r["score"])
    rows = [["circuit", "before", "after", "crossings", "seconds", "blocks found"]]
    for n in TITLE:
        b, a = before[n], after[n]
        rows.append([TITLE[n], sc(b), sc(a),
                     f"{b.get('crossings', '-')} -> {a['crossings']}",
                     f"{b['sec']} -> {a['sec']}", ", ".join(a["blocks"]) or "-"])
    t = Table(rows, colWidths=[W * x for x in (0.36, 0.08, 0.08, 0.12, 0.13, 0.23)])
    t.setStyle(TableStyle([("FONTSIZE", (0, 0), (-1, -1), 9),
                           ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#E6E8EB")),
                           ("GRID", (0, 0), (-1, -1), 0.3, colors.HexColor("#C8CCD0"))]))
    story += [t, Spacer(1, 8)]
    nb = [before[n]["score"] for n in TITLE if "error" not in before[n]]
    story.append(Paragraph(
        f"Average after: {sum(after[n]['score'] for n in TITLE) / len(TITLE):.0f}. "
        f"Average before: {sum(nb) / len(nb):.0f} over the {len(nb)} it could draw "
        f"(it refused the {len(TITLE) - len(nb)} with a transformer).", ss["Normal"]))
    story.append(Paragraph(
        "What changed: familiar blocks (diff pair, current mirror, push-pull, transformer) "
        "are recognised from the wiring and drawn the familiar way; placement tries a few "
        "dozen layouts and keeps the best correct one; busy supplies can be rail labels; "
        "the score also counts label jumps and page shape. The score still cannot judge "
        "everything a person sees, so the pictures follow.", ss["Normal"]))
    for n in SHOW:
        story.append(PageBreak())
        story.append(Paragraph(f"{TITLE[n]}: {sc(before[n])} -> {sc(after[n])}",
                               ss["Heading2"]))
        cells = []
        for tag in ("before", "after"):
            f = FIG / f"{tag}_{n}.png"
            if f.exists():
                w, h = PIL.open(f).size
                k = min((W / 2 - 6) / w, 150 * mm / h)
                cells.append(Image(str(f), w * k, h * k))
            else:
                cells.append(Paragraph("not drawn: " + before[n].get("error", ""),
                                       ss["Normal"]))
        tb = Table([["before (1.0.0)", "after (1.1.0)"], cells], colWidths=[W / 2, W / 2])
        tb.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"),
                                ("FONTSIZE", (0, 0), (-1, 0), 10)]))
        story.append(tb)
    doc.build(story)


if __name__ == "__main__":
    before = json.loads((HERE / "before.json").read_text())
    build_pdf(before, draw_after())
