"""Build the final report PDF from the per-step report figures.

    python docs/final/make_pdf.py      ->  docs/final/SpiceMCP_Engineering_Report.pdf

Written for an electronics engineer who has not used SPICE internals: every figure is
from docs/reports/step1..5, and every number is copied from those reports.
"""
from __future__ import annotations

import json
import re
import sys
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
sys.path.insert(0, str(HERE.parents[1]))
NSTEPS = 10                                # one section per roadmap step
VERSION = re.search(r'^version\s*=\s*"([^"]+)"',
                    (HERE.parents[1] / "pyproject.toml").read_text(), re.M).group(1)


def current_scores() -> dict:
    """Readability of each template's drawing as the code draws it today."""
    import tempfile
    from spicemcp import layout, topologies as T
    specs = {"rc_lowpass": {"fc": 1000}, "sallen_key_lowpass": {"fc": 10000},
             "noninverting_amp": {"gain": 10, "bw": 100e3},
             "ldo": {"vin": 5, "vout": 3.3, "iload": 0.1},
             "buck": {"vin": 12, "vout": 5, "iout": 1}}
    w, out = Path(tempfile.mkdtemp()), {}
    for n, sp in specs.items():
        d = T.get(n).design(sp)
        out[n] = layout.lint(layout.draw(d.netlist, d.start).write(w / f"{n}.asc"))["score"]
    return out
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
    s8 = json.load(open(REP / "step8/results.json"))
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
        ("Op-amps and switches built\nfrom real transistors", "0 of 4", "4 of 4", "circuits"),
        ("Ready-made circuits checked\nin LTspice on Windows", "0 of 5", "5 of 5", "circuits"),
        ("Buck boards that hold\n5 V ±2% (input ±5%)",
         f"{s8['buck']['open_loop']['yield_pct']:.0f}%",
         f"{s8['buck']['closed_loop']['yield_pct']:.0f}%", "of boards"),
    ]
    fig, ax = plt.subplots(2, len(tiles) // 2, figsize=(12.6, 5.0))
    fig.patch.set_facecolor("white")
    for a, (lab, b, f, unit) in zip(ax.ravel(), tiles):
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
    fig.subplots_adjust(left=0.01, right=0.99, top=0.98, bottom=0.02, wspace=0.08,
                        hspace=0.1)
    fig.savefig(path, dpi=200)
    plt.close(fig)


def flow(path):
    steps = [("1  Spec", "fc = 10 kHz ±3%,\ngain 20 dB, 3.3 V out ...", False),
             ("2  Known circuit,\nsized by equations", "5 ready-made circuits:\nfilters, amp, LDO, buck", True),
             ("3  Automatic tuning\nin the simulator", "meets every limit,\nphase margin included", True),
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
    from spicemcp import ir, layout, sim, topologies as T
    import matplotlib.image as mpimg
    show = [("sallen_key_lowpass", "Sallen-Key low-pass filter"),
            ("noninverting_amp", "Non-inverting amplifier")]
    fig, ax = plt.subplots(2 * len(show), 1, figsize=(8.5, 13),
                           gridspec_kw={"hspace": 0.32})
    for i, (n, lab) in enumerate(show):
        d = T.get(n).design(s4.SPECS[n])
        net = sim.decks(ir.render(d.netlist, d.start)[0])[0]      # the circuit deck
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
    c.drawString(18 * mm, 10 * mm, f"SpiceMCP {VERSION[:-2]} · engineering report")
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
           Spacer(1, 4), P(f"What changed in versions 0.6 to {VERSION[:-2]}, shown with simulations, "
                          "and what it means for someone designing circuits.", "body"),
           Spacer(1, 10), img(TMP / "scoreboard.png"),
           P("Eight headline results. Orange: before this work. Blue: now. Every number is from a "
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
    st += section(f"STEP 1 OF {NSTEPS}", "Easier to drive: fewer, clearer controls",
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
    st += section(f"STEP 2 OF {NSTEPS}", "Finds working part values reliably",
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
    st += section(f"STEP 3 OF {NSTEPS}", "Start from a known circuit, sized by textbook equations",
                  "Engineers rarely start from a blank page. Now you can name a circuit and a "
                  "spec, and get a netlist sized by the standard design equations, already "
                  "checked in simulation.")
    st += [img(REP / "step3/template_gallery.png", max_h=118 * mm),
           P("All five ready-made circuits at their hand-calculated values, simulated. The table "
             "(bottom right) compares spec and simulation.", "cap"),
           img(REP / "step3/optimized_from_start.png", max_h=58 * mm),
           P("Tuning then improves the hand-sized start: a 10× smaller capacitor in the RC "
             "filter, 14% smaller C1 in the Sallen-Key, a 44% smaller pass transistor in the "
             "LDO.", "cap")]
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
    st += section(f"STEP 4 OF {NSTEPS}", "Schematics drawn automatically, and proven correct",
                  "A netlist is a list of connections, not a picture. Before, without someone "
                  "placing every part by hand, parts landed in a row and straight wires joined "
                  "them. In the simulator, a wire that crosses a pin connects to it, so those "
                  "drawings were quietly a <b>different circuit</b>.")
    st += [img(TMP / "schematics.png", max_h=205 * mm),
           P("Orange title: the old way. Blue title: drawn automatically. Each title says "
             "whether the drawing is still the circuit in the netlist. In the old drawings, "
             "look for wires running straight across resistor and capacitor pins.", "cap")]
    st.append(PageBreak())
    rows = [["circuit", "before: score", "before: shorted or broken", "step 4: score",
             "step 4: shorted or broken", f"today (v{VERSION}): score"]]
    title = {"rc_lowpass": "RC low-pass", "sallen_key_lowpass": "Sallen-Key low-pass",
             "noninverting_amp": "Non-inverting amp", "ldo": "LDO regulator",
             "buck": "Buck converter"}
    now = current_scores()
    for n, v in s4["templates"].items():
        rows.append([title[n], v["before"]["score"], v["before"]["wrong_connections"],
                     f"<b>{v['after']['score']}</b>", f"<b>{v['after']['wrong_connections']}</b>",
                     f"<b>{now.get(n, '-')}</b>"])
    st += [img(REP / "step4/scores.png"),
           table(rows, [34 * mm, 21 * mm, 30 * mm, 21 * mm, 30 * mm, 28 * mm]),
           P("Readability score: 100 minus points for each wire crossing, wire through a part, "
             "overlapping part, hidden connection and dangling wire. The step 4 columns were "
             "measured when step 4 shipped. The last column is measured from today's drawings "
             "every time this PDF is built: the LDO and buck gained a real op-amp (step 6) and "
             "a controller (step 8), which added parts and lowered their scores.", "cap"),
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
             "only afterwards (scores "
             f"{min(v['score'] for v in s4['unseen'].values())} to "
             f"{max(v['score'] for v in s4['unseen'].values())}, no crossings):"),
           Spacer(1, 4), img(REP / "step4/unseen_circuits.png", max_h=200 * mm)]
    st.append(PageBreak())

    # ---- step 5
    st += section(f"STEP 5 OF {NSTEPS}", "From ideal values to a parts list you can order",
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
    sk = s5["sallen_key_lowpass"]
    st += [P("What the yield numbers tell a designer", "h2")]
    st += bullets([
        "<b>Filters.</b> A ±2–3% corner frequency cannot be held with 5% capacitors, whatever "
        "values you choose. The finish step gets the most out of them (Sallen-Key "
        f"{sk['yield_nearest']:.0f}% → {sk['yield_chosen']:.0f}% of boards), and 1% capacitors "
        f"take it to {sk['yield_tight']:.0f}%.",
        "<b>Amplifier.</b> ±0.1 dB of gain is ±1.2%. Two 1% resistors can be 2% off in ratio. "
        f"Use 0.1% resistors ({s5['noninverting_amp']['yield_tight']:.0f}%) or loosen the spec.",
        "<b>LDO.</b> Its feedback loop holds 3.3 V against ±5% supply and 1% resistors: "
        f"{s5['ldo']['yield_chosen']:.0f}%.",
        "<b>Buck.</b> This ready-made buck runs without feedback, so its output follows its input: "
        "±5% in gives ±5% out against a ±2% spec. Better parts do not help "
        f"({s5['buck']['yield_tight']:.0f}%). The fix is a "
        "control loop, and the yield check shows that before a board is built.",
    ])

    # ---- step 6
    s6 = json.load(open(REP / "step6/results.json"))
    st.append(PageBreak())
    st += section(f"STEP 6 OF {NSTEPS}", "Real op-amps and switches, not ideal stand-ins",
                  "The amplifier circuits used to model the op-amp as a dependent voltage "
                  "source: one number for its gain, no supply, no limits. The buck used an "
                  "ideal switch. Quick for a first look, but it hides what a real part does.")
    st += [img(REP / "step6/ideal_vs_real.png", max_h=120 * mm),
           P("Same circuits, same values. Dashed orange: the ideal stand-in used before. Solid "
             "blue: real devices, used now.", "cap"),
           P("What replaced the stand-ins", "h2")]
    oa = s6["opamp"]["1e+07"]
    st += bullets([
        "<b>Op-amp</b> (Sallen-Key, amplifier, LDO error amplifier): a two-stage CMOS op-amp "
        "made of eight MOSFETs, running from its own supply pins. Measured: "
        f"{oa['a0_db']:.0f} dB open-loop gain, {oa['gbw_hz'] / 1e6:.1f} MHz gain-bandwidth, "
        f"{oa['slew_v_per_us']:.0f} V/µs slew, {oa['supply_ma']:.1f} mA supply current. "
        "It has the standard 5-pin layout, so a vendor model can be swapped in.",
        "<b>Switch</b> (buck): a P-channel power MOSFET (50 mΩ, 1 nF gate) with a gate driver.",
    ])
    st.append(PageBreak())
    sk6 = s6["sallen_key_lowpass"]["at_1MHz"]
    st += [P("What the real parts show that the stand-ins hid", "h2")]
    st += bullets([
        f"<b>Sallen-Key.</b> At 1 MHz the ideal model claims {sk6['ideal']:.0f} dB. The real "
        f"op-amp gives {sk6['real']:.0f} dB, because signal leaks through the feedback capacitor "
        "into the op-amp's output. If the filter must block a clock or switching noise, only "
        "the real model gives the right answer.",
        "<b>Amplifier.</b> Gain and bandwidth agree within a few percent. The real op-amp adds "
        "what the stand-in left out: supply current, output swing and slew rate.",
        "<b>LDO.</b> Both regulate. The real amplifier has more gain, so the output is closer "
        f"to 3.3 V ({s6['ldo']['real']['vout']:.4f} V vs {s6['ldo']['ideal']['vout']:.4f} V).",
        "<b>Buck.</b> The MOSFET's gate charge stretches every on-time slightly: "
        f"{s6['buck']['ideal']['vout']:.2f} V becomes {s6['buck']['real']['vout']:.2f} V. The "
        "sizing equations now keep 20% ripple margin for it.",
        "All five ready-made circuits still meet their spec at the hand-sized start.",
    ])
    st += [img(REP / "step6/real_schematics.png", max_h=130 * mm),
           P("The schematics now show the real parts: the standard op-amp symbol with supply "
             "labels, and the power MOSFET. The op-amp's circuit is saved next to the drawing, "
             "so it opens and simulates in LTspice as it is.", "cap")]

    # ---- step 7
    s7 = json.load(open(REP / "step7/results.json"))
    st.append(PageBreak())
    st += section(f"STEP 7 OF {NSTEPS}", "Proven in LTspice on Windows, and checked on every change",
                  "Until now every result came from ngspice on Linux. The tool is meant for "
                  "LTspice on Windows, so it had to be shown working there, on a real install.")
    st += [img(REP / "step7/ltspice_vs_ngspice.png", max_h=110 * mm),
           P(f"The five ready-made circuits, simulated in LTspice ({s7['platform'].split('-')[0]} "
             "11, LTspice 26) and in ngspice from the same files. They agree within "
             f"{s7['max_difference_pct']:.0f}%; the largest gap is the buck's ripple, "
             "under 1 mV on a 42 mV figure.", "cap"),
           P("What was checked in LTspice", "h2")]
    st += bullets([
        f"<b>Schematics.</b> {s7['schematics_same_circuit']} of {s7['schematics_total']} drawn "
        "schematics were read back by LTspice itself and connect exactly the same pins as the "
        "circuit they were drawn from. That covers the symbols, the op-amp library file and "
        "every wire.",
        f"<b>Circuits.</b> All {len(s7['templates'])} ready-made circuits run in LTspice and "
        "meet their spec at the hand-sized start.",
        f"<b>Reference files.</b> {s7['fixtures_made']} of {s7['fixtures_total']} LTspice "
        "result files were produced and are now stored with the project, so the waveform "
        "readers are tested against real LTspice output.",
        "<b>Automatic checks.</b> Every change now runs the full test suite on Windows and on "
        "Linux, with two Python versions each, before it can be merged.",
        "<b>Version numbers.</b> Semantic versioning: 0.12.0 now. 1.0 will mean the tool "
        "controls are frozen, so scripts written against it keep working.",
    ])

    # ---- step 8
    s8 = json.load(open(REP / "step8/results.json"))
    b8, l8 = s8["buck"], s8["ldo"]
    st.append(PageBreak())
    st += section(f"STEP 8 OF {NSTEPS}", "A buck that regulates, and every feedback loop checked "
                  "for stability",
                  "The buck ran at a fixed duty cycle, so its output followed the input. And "
                  "a circuit with feedback can meet every spec on paper and still oscillate "
                  "on the bench.")
    st += [img(REP / "step8/buck_closed_loop.png", max_h=78 * mm),
           P(f"Left: the same {b8['boards']} random boards (parts in tolerance, input "
             "12 V ±5%). Right: the input jumps by 1.2 V at 0.6 ms.", "cap"),
           P("The buck now has a controller", "h2")]
    st += bullets([
        "An error amplifier, a comparator against a ramp and a gate driver, all built from "
        "transistors, set the duty cycle. The output no longer follows the input.",
        f"<b>Yield {b8['open_loop']['yield_pct']:.0f}% → {b8['closed_loop']['yield_pct']:.0f}%."
        f"</b> Open loop, the boards spread from {b8['open_loop']['vout_min']:.2f} to "
        f"{b8['open_loop']['vout_max']:.2f} V. Closed loop, from "
        f"{b8['closed_loop']['vout_min']:.2f} to {b8['closed_loop']['vout_max']:.2f} V, "
        "set by the 1% divider resistors.",
        "When the input jumps, the output peaks at "
        f"{b8['input_step_closed']['peak_v']:.1f} V and is back in spec after "
        f"{b8['input_step_closed']['back_in_spec_after_ms'] * 1000:.0f} µs. The loop is "
        "deliberately slow and simple; a faster compensator would cut that peak.",
        "Efficiency now counts the real gate drive and controller current: about 89%.",
    ])
    st.append(PageBreak())
    st += [P("Stability, checked every time a circuit is simulated", "h2"),
           P("Each feedback circuit carries a second simulation with its loop cut open at "
             "the amplifier input. It measures the <b>phase margin</b>: how far the loop is "
             "from oscillating. 45° is the usual minimum; below it a circuit rings, near "
             "0° it oscillates. The optimiser and the yield run see it like any other "
             "spec.", "body"),
           img(REP / "step8/ldo_stability.png", max_h=62 * mm),
           P("LDO with the 1 µF output capacitor every regulator needs. Orange: the "
             "capacitor alone. Blue: with the small series resistor the design now sizes.",
             "cap")]
    st += bullets([
        f"<b>LDO.</b> With a ceramic capacitor alone the phase margin is "
        f"{l8['no_zero']['pm_deg']:.0f}°: a load step rings for the whole pulse, and a board "
        f"would oscillate. A {l8['rz_ohm']:.2f} Ω resistor in series with the capacitor "
        f"(or a capacitor with that ESR) gives {l8['with_zero']['pm_deg']:.0f}°, and a "
        f"{l8['with_zero']['step_mv']:.0f} mV dip on a 10× load step that settles in "
        "about 1.5 µs. The resistor is now in the parts list.",
        "<b>Op-amp circuits.</b> The op-amp's transistors now have their capacitances, so "
        "its phase margin falls as the capacitance it drives rises, as on a real part. A "
        "follower drops below 45° at about 200 pF; at a gain of 10 it holds to about 2 nF. "
        f"The filter and the amplifier, at 100 pF, have "
        f"{s8['sallen_key_lowpass']['pm_deg']:.0f}° and "
        f"{s8['noninverting_amp']['pm_deg']:.0f}°.",
    ])
    st += [img(REP / "step8/opamp_phase_margin.png", max_h=62 * mm)]

    # ---- step 9 (phase 0 of the industry roadmap)
    s9 = json.load(open(REP / "step9/results.json"))
    st.append(PageBreak())
    st += section(f"STEP 9 OF {NSTEPS}", "Cheaper to run, nothing misleading, and licensed",
                  "Everything SpiceMCP says is read by an AI assistant, and every word costs "
                  "money on every turn. The command list alone was re-sent on each turn. And "
                  "one sentence in the yield answer claimed more certainty than the "
                  "simulation gave.")
    st += [img(REP / "step9/tokens.png", max_h=82 * mm),
           P("Left: the part re-sent on every turn, and the three biggest answers, in tokens "
             "(the unit an assistant is billed in). Right: the whole design of each of the "
             "17 circuits in the project, the same flows before and after.", "cap"),
           P("What changed", "h2")]
    st += bullets([
        f"<b>Command list {s9['overhead_before']:,} → {s9['overhead_after']:,} tokens</b>, "
        "re-sent on every turn: automatic labels removed, field text shortened, expert "
        "settings still accepted but no longer listed.",
        "<b>Answers are one line, numbers to 6 digits.</b> Indentation alone was a fifth of "
        "every answer.",
        "<b>Short answers, details on request.</b> Starting a ready-made circuit answered in "
        f"{s9['per_tool_before']['start_from_template']:,} tokens on average, now "
        f"{s9['per_tool_after']['start_from_template']:,}; the parts list in "
        f"{s9['per_tool_before']['finish_design']:,}, now "
        f"{s9['per_tool_after']['finish_design']:,}. The hand calculation, warnings and "
        "alternative part sets are one call away.",
        f"<b>All 17 circuits: {s9['billed_before'] / 1000:.0f}k → "
        f"{s9['billed_after'] / 1000:.0f}k input tokens</b> without caching "
        f"({(1 - s9['billed_after'] / s9['billed_before']) * 100:.0f}% less), "
        f"{s9['cached_before'] / 1000:.0f}k → {s9['cached_after'] / 1000:.0f}k with caching.",
        "<b>MIT license.</b> Without one, nobody could legally use the code at work.",
    ])
    st.append(PageBreak())
    st += [P("An honest yield", "h2"),
           img(REP / "step9/yield_bound.png", max_h=70 * mm),
           P("Orange: how a clean run used to read. Blue: what it actually proves.", "cap")]
    st += bullets([
        "Before: \"30 runs resolve yield to about 3%\". That is wrong. 30 boards out of 30 "
        "passing does not show the yield is 97 to 100%; a design with 92% yield passes all "
        "30 about one time in ten.",
        f"Now: \"30/30 pass: yield is at least {s9['yield_30_of_30_at_least_pct']:.0f}% (95% "
        "confidence)\". This is the standard lower bound (Clopper-Pearson). 100 clean boards "
        "prove 97%, 300 prove 99%.",
        "Each yield run now draws new boards. It used the same random boards every time, so "
        "a re-run could never find a failure the first run missed. The seed is reported, so "
        "any run can be repeated exactly.",
        "This report itself is rebuilt: the copy shipped with 0.13 was two files written over "
        "each other and would not open. It is now written in one piece, and a test opens "
        "every shipped PDF.",
    ])

    # ---- step 10 (phase 1)
    s10 = json.load(open(REP / "step10/results.json"))
    b10, t10 = s10["buck"], s10["tokens"]
    st.append(PageBreak())
    st += section(f"STEP 10 OF {NSTEPS}", "No more waiting minutes for one answer",
                  "Designing the buck converter took 20 minutes, 17 of them inside one "
                  "call. Most assistant apps give up on a call after about a minute, so the "
                  "design was lost even though the work was done.")
    st += [img(REP / "step10/buck_timeline.png", max_h=62 * mm),
           P("Every tool call of one buck design, end to end, on a 4-core machine with "
             "ngspice. Each block is one call.", "cap"),
           P("What changed", "h2")]
    st += bullets([
        f"<b>No call runs longer than about a minute</b> (longest {b10['v0.15.0']['longest_s']:.0f} s, "
        f"was {b10['v0.14.0']['longest_s']:.0f} s). Tuning stops at the limit with its progress "
        "saved and continues on the next call. Turning the design into parts keeps working in "
        "the background; asking again picks up the result. Both send progress updates.",
        f"<b>{b10['v0.14.0']['total_s'] / 60:.0f} → {b10['v0.15.0']['total_s'] / 60:.0f} minutes "
        "for the buck.</b> Simulations that do not depend on each other now run side by side, "
        "one per processor core: the yield boards, the standard-value combinations, a "
        "circuit's stability and load-step checks. On an 8-core laptop it is faster still.",
        "<b>Built-in op-amp and comparator by name.</b> A circuit can use them without copying "
        "their transistor model in: the instrumentation amplifier design dropped from "
        f"{t10['per_circuit']['6']['v0.14.0'] / 1000:.0f}k to "
        f"{t10['per_circuit']['6']['v0.15.0'] / 1000:.0f}k tokens.",
        "<b>Works without LTspice.</b> One-off simulations and sweeps follow the chosen "
        "simulator, so macOS and Linux users with ngspice can use every command.",
        "<b>Ready for pip install.</b> Packaged for the Python package index, checked in "
        "every build, and tested with both the current and the new version of the MCP "
        "library.",
    ])
    st += [img(REP / "step10/tokens.png", max_h=66 * mm),
           P(f"All 17 circuits: {t10['total']['v0.13.0'] / 1000:.0f}k → "
             f"{t10['total']['v0.15.0'] / 1000:.0f}k input tokens without caching. The one "
             "that went up is the buck: shorter calls mean more of them, and each re-reads "
             "the conversation. With caching that costs little (light bars); clients that "
             "tolerate long calls can raise the time limit.", "cap")]

    # ---- closing
    st += [PageBreak(), P("Where it stands, honestly", "h2")]
    st += bullets([
        "Results were simulated in ngspice and confirmed in LTspice on Windows: the two agree "
        "within 2%, so they do not depend on one tool.",
        "The automatic schematic handles flat circuits and 5-pin op-amps. Other sub-blocks "
        "(subcircuits) are refused with a clear message rather than drawn wrongly.",
        "The transistor-level op-amp uses simple MOSFET models. Its phase margin is now "
        "measured, but for a stability sign-off use the vendor model of the part you will buy.",
        "Yield covers part tolerance and supply. Temperature and silicon process corners need "
        "real device models; the design report can run them where models are supplied.",
        "Test suite: about 340 automated checks, run on Windows and Linux for every change.",
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

    tmp = OUT.with_suffix(".part")         # written whole, then moved: never two builds
    doc = SimpleDocTemplate(str(tmp), pagesize=A4, leftMargin=18 * mm, rightMargin=18 * mm,
                            topMargin=16 * mm, bottomMargin=18 * mm,
                            title="SpiceMCP engineering report",
                            author="Shounak Das", subject=f"SpiceMCP 0.6 to {VERSION}")
    doc.build(st, onFirstPage=footer, onLaterPages=footer)
    tmp.replace(OUT)                       # atomic on one filesystem
    print(OUT)


if __name__ == "__main__":
    build()
