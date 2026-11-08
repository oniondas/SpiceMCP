"""Build the final report PDF from the per-step report figures.

    python docs/final/make_pdf.py      ->  docs/final/SpiceMCP_Engineering_Report.pdf

Written for an electronics engineer who has not used SPICE internals: every figure is
from docs/reports/step1..5, and every number is copied from those reports.
"""
from __future__ import annotations

import json
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.patches import FancyArrowPatch, FancyBboxPatch  # noqa: E402
from reportlab.lib import colors  # noqa: E402
from reportlab.lib.enums import TA_LEFT  # noqa: E402
from reportlab.lib.pagesizes import A4  # noqa: E402
from reportlab.lib.styles import ParagraphStyle  # noqa: E402
from reportlab.lib.units import mm  # noqa: E402
from reportlab.pdfbase import pdfmetrics  # noqa: E402
from reportlab.pdfbase.ttfonts import TTFont  # noqa: E402
from reportlab.platypus import (Image, PageBreak, Paragraph,  # noqa: E402
                                SimpleDocTemplate, Spacer, Table, TableStyle)

HERE = Path(__file__).resolve().parent
REP = HERE.parent / "reports"
OUT = HERE / "SpiceMCP_Engineering_Report.pdf"
BEFORE, AFTER, AFTER2, BAND = "#E07A5F", "#1E56A0", "#38A3A5", "#F2CC8F"
INK, MUTED, GRID, BG = "#1F2328", "#57606A", "#E6E8EB", "#FCFCFB"

FONT = "/usr/share/fonts/truetype/liberation/"
pdfmetrics.registerFont(TTFont("Sans", FONT + "LiberationSans-Regular.ttf"))
pdfmetrics.registerFont(TTFont("Sans-Bold", FONT + "LiberationSans-Bold.ttf"))
pdfmetrics.registerFont(TTFont("Sans-Oblique", FONT + "LiberationSans-Italic.ttf"))
pdfmetrics.registerFontFamily("Sans", normal="Sans", bold="Sans-Bold", italic="Sans-Oblique")

S = {
    "title": ParagraphStyle("t", fontName="Sans-Bold", fontSize=26, leading=31,
                            textColor=colors.HexColor(INK)),
    "sub": ParagraphStyle("s", fontName="Sans", fontSize=13, leading=18,
                          textColor=colors.HexColor(MUTED)),
    "h1": ParagraphStyle("h1", fontName="Sans-Bold", fontSize=17, leading=22,
                         textColor=colors.HexColor(AFTER), spaceAfter=4),
    "kicker": ParagraphStyle("k", fontName="Sans-Bold", fontSize=9, leading=12,
                             textColor=colors.HexColor(MUTED)),
    "h2": ParagraphStyle("h2", fontName="Sans-Bold", fontSize=11.5, leading=15,
                         textColor=colors.HexColor(INK), spaceBefore=6, spaceAfter=2),
    "body": ParagraphStyle("b", fontName="Sans", fontSize=9.6, leading=13.6,
                           textColor=colors.HexColor(INK), alignment=TA_LEFT),
    "bullet": ParagraphStyle("bl", fontName="Sans", fontSize=9.6, leading=13.4,
                             textColor=colors.HexColor(INK), leftIndent=11,
                             bulletIndent=0, spaceAfter=1.5),
    "cap": ParagraphStyle("c", fontName="Sans-Oblique", fontSize=8.2, leading=11,
                          textColor=colors.HexColor(MUTED)),
    "cell": ParagraphStyle("cell", fontName="Sans", fontSize=8.6, leading=11,
                           textColor=colors.HexColor(INK)),
    "cellb": ParagraphStyle("cellb", fontName="Sans-Bold", fontSize=8.6, leading=11,
                            textColor=colors.HexColor(INK)),
}
W = A4[0] - 36 * mm
TMP = HERE / "_figs"


def P(t, s="body"):
    return Paragraph(t, S[s])


def bullets(items):
    return [Paragraph(t, S["bullet"], bulletText="•") for t in items]


def img(path, width=W, max_h=None):
    from PIL import Image as PI
    w, h = PI.open(path).size
    hh = width * h / w
    if max_h and hh > max_h:
        width, hh = max_h * w / h, max_h
    return Image(str(path), width=width, height=hh)


def table(rows, widths, head=True):
    data = [[P(str(c), "cellb" if (head and i == 0) else "cell") for c in r]
            for i, r in enumerate(rows)]
    t = Table(data, colWidths=widths, hAlign="LEFT")
    st = [("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor(GRID)),
          ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
          ("TOPPADDING", (0, 0), (-1, -1), 3), ("BOTTOMPADDING", (0, 0), (-1, -1), 3)]
    if head:
        st.append(("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#F0F2F4")))
    t.setStyle(TableStyle(st))
    return t


def section(kicker, title, lead):
    return [P(kicker, "kicker"), P(title, "h1"), P(lead), Spacer(1, 6)]


# ------------------------------------------------------------ new summary figures

def scoreboard(path):
    s4 = json.load(open(REP / "step4/results.json"))["templates"]
    s5 = json.load(open(REP / "step5/results.json"))
    tiles = [
        ("Optimizer finds a working\nSallen-Key filter", "0 of 10", "10 of 10", "runs"),
        ("Ready-made circuits that\nmeet spec before tuning", "0", "5", "templates"),
        ("Auto-drawn schematics that\nare the right circuit",
         f"{sum(not v['before']['wrong_connections'] for v in s4.values())} of 5",
         f"{sum(not v['after']['wrong_connections'] for v in s4.values())} of 5", "circuits"),
        ("Standard-part designs that\nstill meet every limit",
         f"{sum(v['nearest_meets'] for v in s5.values())} of 5",
         f"{sum(v['chosen_meets'] for v in s5.values())} of 5", "circuits"),
        ("Sallen-Key boards that pass\n(5% capacitors)",
         f"{s5['sallen_key_lowpass']['yield_nearest']:.0f}%",
         f"{s5['sallen_key_lowpass']['yield_chosen']:.0f}%", "of boards"),
    ]
    fig, ax = plt.subplots(1, len(tiles), figsize=(10.5, 2.5))
    fig.patch.set_facecolor("white")
    for a, (lab, b, f, unit) in zip(ax, tiles):
        a.axis("off")
        a.add_patch(FancyBboxPatch((0.02, 0.02), 0.96, 0.96, boxstyle="round,pad=0,rounding_size=0.06",
                                   fc=BG, ec=GRID, transform=a.transAxes))
        a.text(0.5, 0.86, lab, ha="center", va="top", fontsize=9, color=INK,
               transform=a.transAxes, linespacing=1.3)
        a.text(0.5, 0.42, b, ha="center", fontsize=13, color=BEFORE,
               transform=a.transAxes)
        a.text(0.5, 0.31, "before", ha="center", fontsize=7.5, color=MUTED,
               transform=a.transAxes)
        a.text(0.5, 0.13, f, ha="center", fontsize=19, color=AFTER, weight="bold",
               transform=a.transAxes)
    fig.subplots_adjust(left=0.01, right=0.99, top=0.98, bottom=0.02, wspace=0.08)
    fig.savefig(path, dpi=200)
    plt.close(fig)


def flow(path):
    steps = [("1  Spec", "fc = 10 kHz ±3%,\ngain 20 dB, 3.3 V out ...", False),
             ("2  Known circuit,\nsized by equations", "5 ready-made circuits:\nfilters, amp, LDO, buck", True),
             ("3  Automatic tuning\nin the simulator", "finds values that meet\nevery limit", True),
             ("4  Standard parts\n+ yield check", "E12/E96 values, tolerance,\nhow many boards pass", True),
             ("5  Schematic\n+ design report", "drawn automatically,\nchecked against netlist", True)]
    fig, ax = plt.subplots(figsize=(10, 2.2))
    ax.axis("off")
    ax.set_xlim(0, 10)
    ax.set_ylim(0, 2.4)
    w = 1.74
    for i, (t, sub, new) in enumerate(steps):
        x = 0.1 + i * 2.0
        ax.add_patch(FancyBboxPatch((x, 0.55), w, 1.55, boxstyle="round,pad=0.02,rounding_size=0.12",
                                    fc="#EEF3FA" if new else BG,
                                    ec=AFTER if new else GRID, lw=1.4))
        ax.text(x + w / 2, 1.82, t, ha="center", va="top", fontsize=10, weight="bold",
                color=INK, linespacing=1.15)
        ax.text(x + w / 2, 1.05, sub, ha="center", va="top", fontsize=8, color=MUTED,
                linespacing=1.25)
        if new:
            ax.text(x + w / 2, 0.38, "new or improved",
                    ha="center", fontsize=7.5, color=AFTER)
        if i < len(steps) - 1:
            ax.add_patch(FancyArrowPatch((x + w + 0.02, 1.32), (x + 2.0 - 0.02, 1.32),
                                         arrowstyle="-|>", mutation_scale=12, color=MUTED))
    fig.subplots_adjust(left=0, right=1, top=1, bottom=0)
    fig.savefig(path, dpi=200)
    plt.close(fig)


def schematics(path):
    """Before/after schematics without the simulator directives, large enough to read."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("s4", REP / "step4/make_report.py")
    s4 = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(s4)
    from spicemcp import ir, layout, topologies as T
    import matplotlib.image as mpimg
    show = [("sallen_key_lowpass", "Sallen-Key low-pass filter"),
            ("noninverting_amp", "Non-inverting amplifier")]
    fig, ax = plt.subplots(2 * len(show), 1, figsize=(8.5, 13),
                           gridspec_kw={"hspace": 0.32})
    for i, (n, lab) in enumerate(show):
        d = T.get(n).design(s4.SPECS[n])
        net, _ = ir.render(d.netlist, d.start)
        for j, sh in enumerate((s4.naive(net), layout.draw(net).sheet)):
            bad = len(layout.verify(sh, net))
            sh.directives, sh.notes = [], []
            pic = s4.png(sh, f"pdf_{n}_{j}")
            a = ax[2 * i + j]
            a.imshow(mpimg.imread(pic))
            a.axis("off")
            head = "BEFORE: parts in a row" if j == 0 else "AFTER: drawn automatically"
            verdict = (f"wrong circuit: {bad} node(s) shorted or broken" if bad
                       else "same circuit as the netlist")
            a.set_title(f"{lab}. {head}. {verdict}", loc="left", fontsize=10.5,
                        color=BEFORE if j == 0 else AFTER)
    fig.savefig(path, dpi=170, bbox_inches="tight")
    plt.close(fig)


# ------------------------------------------------------------ document

def footer(c, doc):
    c.saveState()
    c.setFont("Sans", 7.5)
    c.setFillColor(colors.HexColor(MUTED))
    c.drawString(18 * mm, 10 * mm, "SpiceMCP 0.10 · engineering report")
    c.drawRightString(A4[0] - 18 * mm, 10 * mm, f"page {doc.page}")
    c.restoreState()


def build():
    TMP.mkdir(exist_ok=True)
    scoreboard(TMP / "scoreboard.png")
    flow(TMP / "flow.png")
    schematics(TMP / "schematics.png")
    s4 = json.load(open(REP / "step4/results.json"))
    s5 = json.load(open(REP / "step5/results.json"))
    st = []

    # ---- cover
    st += [Spacer(1, 18 * mm), P("SpiceMCP", "title"),
           P("From a circuit-simulation prototype to an engineering design tool", "sub"),
           Spacer(1, 4), P("What changed in versions 0.6 to 0.10, shown with simulations, "
                          "and what it means for someone designing circuits.", "body"),
           Spacer(1, 10), img(TMP / "scoreboard.png"),
           P("Five headline results. Orange: before this work. Blue: now. Every number is from a "
             "simulation and is explained on the page for that step.", "cap"),
           Spacer(1, 10), P("The design flow, end to end", "h2"), img(TMP / "flow.png"),
           P("Blue boxes are new or substantially improved. You give a spec; SpiceMCP picks a "
             "known circuit, sizes it, tunes it in the simulator, turns it into orderable parts, "
             "checks how many built boards would pass, and draws the schematic.", "cap"),
           Spacer(1, 10), P("How to read this report", "h2")]
    st += bullets([
        "One section per improvement. Each starts with the problem in one sentence, then a "
        "<b>before / after</b> picture, then what it means for your design.",
        "<b>Orange</b> is always the old behaviour, <b>blue</b> the new one, a sand-coloured "
        "band is the range the spec allows.",
        "No simulator knowledge is needed. The few terms that matter (standard values, "
        "tolerance, yield, margin) are explained on the last page.",
    ])
    st.append(PageBreak())

    # ---- step 1
    st += section("STEP 1 OF 5", "Easier to drive: fewer, clearer controls",
                  "SpiceMCP is operated through an AI assistant, which picks from a list of "
                  "commands. The old list was long and loosely described, so the assistant "
                  "guessed. Guessing is where wrong circuits come from.")
    st += [img(REP / "step1/tool_surface_summary.png"),
           P("Left to right: number of commands, size of the command list, longest "
             "description, inputs the assistant had to guess the shape of.", "cap"),
           P("What changed", "h2")]
    st += bullets([
        "26 commands became 11 (13 today, after steps 3 and 5 added two). Each has a "
        "short, strict description and typed inputs.",
        "Every answer ends with <b>the next step to take</b>; every error says how to fix "
        "it and shows a correct example.",
        "The simulator's memory is the record: a past design is always restored from what "
        "was simulated, never retyped from memory.",
    ])
    st += [P("What it means for you", "h2"),
           P("Fewer wrong turns and fewer silently wrong circuits, also with smaller, cheaper "
             "AI models.")]
    st.append(PageBreak())

    # ---- step 2
    st += section("STEP 2 OF 5", "Finds working part values reliably",
                  "Part values span decades (100 Ω to 1 MΩ, 1 pF to 1 µF), and several specs "
                  "pull on the same parts. The old search stepped in straight lines and adjusted "
                  "one spec at a time, so it often got stuck.")
    st += [img(REP / "step2/success_rate.png", max_h=72 * mm),
           P("Share of 10 independent runs that had found a design meeting every spec, against "
             "the number of simulations spent.", "cap"),
           table([["problem", "runs that met every spec", "simulations to first working design"],
                  ["RC low-pass, fc = 1 kHz ±2%", "7 of 10 → <b>10 of 10</b>", "31 → <b>6</b>"],
                  ["Sallen-Key, fc = 10 kHz ±3% and Q = 0.707 ±5%", "0 of 10 → <b>10 of 10</b>",
                   "never → <b>20</b>"]], [70 * mm, 50 * mm, 54 * mm]),
           P("What changed", "h2")]
    st += bullets([
        "Steps are taken in ratios (×1.2, ÷1.5), the way an engineer thinks about part values.",
        "It first samples the whole range evenly, so it does not start in a corner.",
        "When several specs pull on the same parts (a filter's corner and its Q), it solves "
        "for all of them together, from measured sensitivities, instead of one at a time.",
    ])
    st.append(PageBreak())

    # ---- step 3
    st += section("STEP 3 OF 5", "Start from a known circuit, sized by textbook equations",
                  "Engineers rarely start from a blank page. Now you can name a circuit and a "
                  "spec, and get a netlist sized by the standard design equations, already "
                  "checked in simulation.")
    st += [img(REP / "step3/template_gallery.png", max_h=118 * mm),
           P("All five ready-made circuits at their hand-calculated values, simulated. The table "
             "(bottom right) compares spec and simulation.", "cap"),
           img(REP / "step3/optimized_from_start.png", max_h=58 * mm),
           P("Tuning then improves the hand-sized start: smaller capacitor, 44% smaller pass "
             "transistor, ripple brought under its limit.", "cap")]
    st.append(PageBreak())
    st += [P("What the five circuits are", "h2"),
           table([["circuit", "you give", "sized by"],
                  ["RC low-pass", "corner frequency", "fc = 1 / (2πRC)"],
                  ["Sallen-Key low-pass (2nd order)", "corner frequency, Q",
                   "equal-R design: C1 = 2Q/(ωR), C2 = 1/(2QωR)"],
                  ["Non-inverting amplifier", "gain, bandwidth, op-amp GBW",
                   "R2 = R1·(G−1); checks GBW ≥ G·bandwidth"],
                  ["PMOS LDO regulator", "Vin, Vout, load current, dropout",
                   "divider from Vref; pass device from current and dropout"],
                  ["Buck converter", "Vin, Vout, Iout, switching frequency, ripple",
                   "duty = Vout/Vin; L from ripple current; C from ripple voltage"]],
                 [52 * mm, 55 * mm, 67 * mm]),
           Spacer(1, 6), P("What it means for you", "h2")]
    st += bullets([
        "A working start in one step, with the hand calculation shown line by line, so you "
        "can check the reasoning.",
        "Impossible specs are refused up front with the reason (for example, a gain-bandwidth "
        "product the op-amp cannot reach, or a dropout below what the pass device allows).",
        "Tuning starts close to the answer, so it spends its simulations improving the design "
        "rather than searching for one.",
    ])
    st.append(PageBreak())

    # ---- step 4
    st += section("STEP 4 OF 5", "Schematics drawn automatically, and proven correct",
                  "A netlist is a list of connections, not a picture. Before, without someone "
                  "placing every part by hand, parts landed in a row and straight wires joined "
                  "them. In the simulator, a wire that crosses a pin connects to it, so those "
                  "drawings were quietly a <b>different circuit</b>.")
    st += [img(TMP / "schematics.png", max_h=205 * mm),
           P("Orange title: the old way. Blue title: drawn automatically. Each title says "
             "whether the drawing is still the circuit in the netlist. In the old drawings, "
             "look for wires running straight across resistor and capacitor pins.", "cap")]
    st.append(PageBreak())
    rows = [["circuit", "before: score", "before: nodes shorted or broken", "after: score",
             "after: nodes shorted or broken"]]
    title = {"rc_lowpass": "RC low-pass", "sallen_key_lowpass": "Sallen-Key low-pass",
             "noninverting_amp": "Non-inverting amp", "ldo": "LDO regulator",
             "buck": "Buck converter"}
    for n, v in s4["templates"].items():
        rows.append([title[n], v["before"]["score"], v["before"]["wrong_connections"],
                     f"<b>{v['after']['score']}</b>", f"<b>{v['after']['wrong_connections']}</b>"])
    st += [img(REP / "step4/scores.png"), table(rows, [42 * mm, 25 * mm, 41 * mm, 25 * mm, 41 * mm]),
           P("Readability score: 100 minus points for each wire crossing, wire through a part, "
             "overlapping part, hidden connection and dangling wire.", "cap"),
           P("How the drawing is laid out", "h2")]
    st += bullets([
        "Signal flows left to right; an amplifier's inputs are always left of its output.",
        "Ground at the bottom: every grounded part hangs down and gets its own ground symbol.",
        "Feedback parts (a feedback resistor, a Sallen-Key's C1) get their own lane above or "
        "below the main path. Transistor amplifiers stand upright, drain above source.",
        "Wires are right-angled, never pass through a part, and never touch another net. "
        "Measured nodes (out, fb, ref) are labelled.",
        "<b>Every drawing is read back and compared with the netlist, pin by pin.</b> A drawing "
        "that is not the same circuit is refused, never shown.",
    ])
    st.append(PageBreak())
    st += [P("It works on circuits it was never tuned for", "h2"),
           P("The layout rules were written against the five circuits above. These were tried "
             "only afterwards (scores 96 to 100, no crossings):"),
           Spacer(1, 4), img(REP / "step4/unseen_circuits.png", max_h=200 * mm)]
    st.append(PageBreak())

    # ---- step 5
    st += section("STEP 5 OF 5", "From ideal values to a parts list you can order",
                  "Tuning produces values like C1 = 1.5915 nF. Nobody sells that. Rounding "
                  "every part to the nearest standard value moves all of them at once, and on "
                  "a real board every part is also off by its tolerance. A design that sits on "
                  "the edge of its spec passes on paper and fails on many boards.")
    st += [img(REP / "step5/parts_lists.png", max_h=105 * mm),
           P("Rounding by hand breaks the two filters: the corner frequency leaves its window. "
             "The new finish step finds standard-value sets that meet every limit for all five.",
             "cap"), P("How the finish step works", "h2")]
    st += bullets([
        "Pick the coarse-step parts first (capacitors come in E12: 1.0, 1.2, 1.5, 1.8, 2.2 ...), "
        "trying the standard value just below and just above.",
        "Re-centre the fine-step parts (1% resistors in E96) so the design sits in the middle "
        "of its limits, the way you would recompute R after choosing C.",
        "Build 30–40 virtual boards with every part at a random value inside its tolerance, "
        "plus the supply within ±5% where it matters, and keep the set that passes most often.",
    ])
    st.append(PageBreak())
    yrows = [["circuit", "rounded by hand", "finish step", "finish step, precision parts"]]
    for n, v in s5.items():
        yrows.append([title[n] + (" (+ ±5% supply)" if v["supply_tol_pct"] else ""),
                      f"{v['yield_nearest']:.0f}%", f"<b>{v['yield_chosen']:.0f}%</b>",
                      f"{v['yield_tight']:.0f}%"])
    st += [P("Yield: how many built boards would meet the spec", "h2"),
           img(REP / "step5/yield.png"),
           table(yrows, [58 * mm, 36 * mm, 36 * mm, 44 * mm]),
           P("Standard tolerance: 1% resistors, 5% capacitors, 20% inductors. Precision parts: "
             "0.1% resistors, 1% capacitors, 10% inductors. Both columns use the same virtual "
             "boards, so the comparison is fair; 40 boards resolve yield to about ±8%.", "cap"),
           Spacer(1, 4), img(REP / "step5/where_boards_land.png"),
           P("Where the boards land. Shaded band: allowed window. Filled: standard tolerance. "
             "Outline: precision parts.", "cap")]
    st.append(PageBreak())
    st += [P("What the yield numbers tell a designer", "h2")]
    st += bullets([
        "<b>Filters.</b> A ±2–3% corner frequency cannot be held with 5% capacitors, whatever "
        "values you choose. The finish step gets the most out of them (Sallen-Key 10% → 65% of "
        "boards), and 1% capacitors take it to 100%.",
        "<b>Amplifier.</b> ±0.1 dB of gain is ±1.2%. Two 1% resistors can be 2% off in ratio. "
        "Use 0.1% resistors (100%) or loosen the spec.",
        "<b>LDO.</b> Its feedback loop holds 3.3 V against ±5% supply and 1% resistors: 100%.",
        "<b>Buck.</b> This ready-made buck runs without feedback, so its output follows its input: "
        "±5% in gives ±5% out against a ±2% spec. Better parts barely help (38%). The fix is a "
        "control loop, and the yield check shows that before a board is built.",
    ])

    # ---- closing
    st += [Spacer(1, 8), P("Where it stands, honestly", "h2")]
    st += bullets([
        "Everything in this report was simulated with ngspice, a second simulator, so the "
        "results do not depend on one tool. LTspice remains the main target on Windows and "
        "reads the same circuit files.",
        "The automatic schematic handles flat circuits. A circuit built from sub-blocks "
        "(subcircuits) is refused with a clear message rather than drawn wrongly.",
        "Yield covers part tolerance and supply. Temperature and silicon process corners need "
        "real device models; the design report can run them where models are supplied.",
        "Test suite: 272 automated checks pass. Another 41 depend on reference files produced by "
        "LTspice on Windows, which are not stored in the repository.",
    ])
    st += [Spacer(1, 6), P("Five terms, in plain words", "h2"),
           table([["term", "meaning"],
                  ["Standard values (E-series)", "The values parts are actually sold in. E12 has 12 "
                   "per decade (1.0, 1.2, 1.5 ... 8.2), E96 has 96 (1.00, 1.02, 1.05 ...)."],
                  ["Tolerance", "How far a real part may be from its marked value: a 5% 10 nF "
                   "capacitor is somewhere between 9.5 and 10.5 nF."],
                  ["Yield", "The share of built boards that meet every spec, with every part "
                   "somewhere inside its tolerance."],
                  ["Margin", "How far the design sits inside its nearest limit. Zero means on the "
                   "edge: half the boards would fail."],
                  ["Netlist", "The text list of which part connects to which node: what the "
                   "simulator reads, and what the schematic must match."]],
                 [42 * mm, 132 * mm])]

    doc = SimpleDocTemplate(str(OUT), pagesize=A4, leftMargin=18 * mm, rightMargin=18 * mm,
                            topMargin=16 * mm, bottomMargin=18 * mm,
                            title="SpiceMCP engineering report",
                            author="Shounak Das", subject="SpiceMCP 0.6 to 0.10")
    doc.build(st, onFirstPage=footer, onLaterPages=footer)
    print(OUT)


if __name__ == "__main__":
    build()
