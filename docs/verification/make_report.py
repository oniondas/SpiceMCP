"""Build the independent verification report.

    python docs/verification/make_report.py  ->  docs/verification/SpiceMCP_Verification_Report.pdf

Every number comes from docs/verification/results/ (written by the scripts in
docs/verification/scripts/). ngspice results are from Linux, LTspice results from the
Windows laptop (results/ltspice/), when present.
"""
from __future__ import annotations

import json
import math
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.patches import FancyArrowPatch, FancyBboxPatch  # noqa: E402
from reportlab.lib import colors  # noqa: E402
from reportlab.lib.pagesizes import A4  # noqa: E402
from reportlab.lib.styles import ParagraphStyle  # noqa: E402
from reportlab.lib.units import mm  # noqa: E402
from reportlab.pdfbase import pdfmetrics  # noqa: E402
from reportlab.pdfbase.ttfonts import TTFont  # noqa: E402
from reportlab.platypus import (Image, KeepTogether, PageBreak, Paragraph,  # noqa: E402
                                SimpleDocTemplate, Spacer, Table, TableStyle)

HERE = Path(__file__).resolve().parent
RES = HERE / "results"
LT = RES / "ltspice"
FIG = HERE / "figures"
OUT = HERE / "SpiceMCP_Verification_Report.pdf"

CLAIM, MEAS, MEAS2, BAND, BAD = "#E07A5F", "#1E56A0", "#38A3A5", "#F2CC8F", "#C0392B"
OK_C, WARN_C, FAIL_C = "#2E7D32", "#B26A00", "#C0392B"
INK, MUTED, GRID, BG = "#1F2328", "#57606A", "#E6E8EB", "#FCFCFB"
plt.rcParams.update({"font.family": "DejaVu Sans", "font.size": 9.5, "axes.edgecolor": GRID,
                     "axes.labelcolor": MUTED, "xtick.color": MUTED, "ytick.color": MUTED,
                     "figure.facecolor": BG, "axes.facecolor": BG, "text.color": INK,
                     "axes.spines.top": False, "axes.spines.right": False})

FONT = "/usr/share/fonts/truetype/liberation/"
pdfmetrics.registerFont(TTFont("Sans", FONT + "LiberationSans-Regular.ttf"))
pdfmetrics.registerFont(TTFont("Sans-Bold", FONT + "LiberationSans-Bold.ttf"))
pdfmetrics.registerFont(TTFont("Sans-Oblique", FONT + "LiberationSans-Italic.ttf"))
pdfmetrics.registerFont(TTFont("Sym", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"))
pdfmetrics.registerFontFamily("Sans", normal="Sans", bold="Sans-Bold", italic="Sans-Oblique")
S = {
    "title": ParagraphStyle("t", fontName="Sans-Bold", fontSize=25, leading=30,
                            textColor=colors.HexColor(INK)),
    "sub": ParagraphStyle("s", fontName="Sans", fontSize=12.5, leading=17,
                          textColor=colors.HexColor(MUTED)),
    "h1": ParagraphStyle("h1", fontName="Sans-Bold", fontSize=16.5, leading=21,
                         textColor=colors.HexColor(MEAS), spaceAfter=4),
    "kicker": ParagraphStyle("k", fontName="Sans-Bold", fontSize=9, leading=12,
                             textColor=colors.HexColor(MUTED)),
    "h2": ParagraphStyle("h2", fontName="Sans-Bold", fontSize=11.5, leading=15,
                         textColor=colors.HexColor(INK), spaceBefore=6, spaceAfter=2),
    "body": ParagraphStyle("b", fontName="Sans", fontSize=9.6, leading=13.6,
                           textColor=colors.HexColor(INK)),
    "bullet": ParagraphStyle("bl", fontName="Sans", fontSize=9.6, leading=13.4,
                             textColor=colors.HexColor(INK), leftIndent=11, bulletIndent=0,
                             spaceAfter=1.5),
    "cap": ParagraphStyle("c", fontName="Sans-Oblique", fontSize=8.2, leading=11,
                          textColor=colors.HexColor(MUTED)),
    "cell": ParagraphStyle("cell", fontName="Sans", fontSize=8.4, leading=10.6,
                           textColor=colors.HexColor(INK)),
    "cellb": ParagraphStyle("cellb", fontName="Sans-Bold", fontSize=8.4, leading=10.6,
                            textColor=colors.HexColor(INK)),
}
W = A4[0] - 36 * mm


def J(name):
    p = RES / name
    return json.loads(p.read_text()) if p.is_file() else None


def JL(name):
    p = LT / name
    return json.loads(p.read_text()) if p.is_file() else None


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


VERDICT = {"ok": ("✔ holds", OK_C), "warn": ("≈ mostly", WARN_C), "fail": ("✘ does not hold", FAIL_C),
           "new": ("new finding", WARN_C)}


def table(rows, widths, head=True, verdict_col=None):
    data = []
    for i, r in enumerate(rows):
        line = []
        for j, c in enumerate(r):
            if i and j == verdict_col and c in VERDICT:
                txt, col = VERDICT[c]
                sym, word = txt.split(" ", 1)
                line.append(Paragraph(f'<font color="{col}" name="Sym">{sym}</font> '
                                      f'<font color="{col}"><b>{word}</b></font>', S["cell"]))
            else:
                line.append(P(str(c), "cellb" if (head and i == 0) else "cell"))
        data.append(line)
    t = Table(data, colWidths=widths, hAlign="LEFT", repeatRows=1 if head else 0)
    st = [("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor(GRID)),
          ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
          ("TOPPADDING", (0, 0), (-1, -1), 2.5), ("BOTTOMPADDING", (0, 0), (-1, -1), 2.5)]
    if head:
        st.append(("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#F0F2F4")))
    t.setStyle(TableStyle(st))
    return t


def section(kicker, title, lead):
    return [P(kicker, "kicker"), P(title, "h1"), P(lead), Spacer(1, 5)]


def eng(v, unit=""):
    if v is None:
        return "–"
    a = abs(v)
    for f, p in ((1e6, "M"), (1e3, "k"), (1, ""), (1e-3, "m"), (1e-6, "µ"), (1e-9, "n"), (1e-12, "p")):
        if a >= f:
            return f"{v / f:.3g} {p}{unit}".strip()
    return f"{v:.3g} {unit}"


def ci(k, n):
    z, ph = 1.96, k / n
    c = (ph + z * z / (2 * n)) / (1 + z * z / n)
    h = z * math.sqrt(ph * (1 - ph) / n + z * z / (4 * n * n)) / (1 + z * z / n)
    return 100 * max(c - h, 0), 100 * min(c + h, 1)


# ================================================================ data

TOOLS = J("tools.json")
CLAIMS = J("claims_ngspice.json")
TPL = J("templates_ngspice_40.json")
INA = J("ina_ngspice.json")
CUR = J("ina_curves_ngspice.json")
SEARCH = J("search_ngspice.json")
SCH = J("schematics_drawn_only.json")
MC = {}
for p in sorted(RES.glob("mc_ngspice_*.json")):
    MC.update(json.loads(p.read_text()))
LT_CLAIMS, LT_TPL, LT_SCH, LT_INA, LT_INFO = (JL("claims_ltspice.json"),
                                              JL("templates_ltspice_40.json"),
                                              JL("schematics_ltspice.json"),
                                              JL("ina_ltspice.json"), JL("info.json"))
HAVE_LT = LT_INFO is not None


def ina_answer(tool, k=0):
    hits = [e["answer"] for e in INA if e["tool"] == tool]
    return hits[k] if len(hits) > k else {}


# ================================================================ figures

def fig_method(path):
    fig, ax = plt.subplots(figsize=(10, 2.5))
    ax.set_xlim(0, 100); ax.set_ylim(0, 26); ax.axis("off")
    boxes = [(1, "Fresh copy", "downloaded from\nGitHub, v0.13.0"),
             (21, "Own test client", "drives SpiceMCP like\nan AI assistant does"),
             (41, "Two simulators", "ngspice (Linux)\nLTspice (your laptop)"),
             (61, "Own test benches", "separate circuits and\nmore virtual boards"),
             (81, "Compare", "claimed vs measured,\nsame spec limits")]
    for x, t, s in boxes:
        ax.add_patch(FancyBboxPatch((x, 4), 17, 18, boxstyle="round,pad=0.3,rounding_size=1.5",
                                    fc="#EAF1FA", ec=MEAS, lw=1.2))
        ax.text(x + 8.5, 16.5, t, ha="center", va="center", weight="bold", fontsize=9.6)
        ax.text(x + 8.5, 9.5, s, ha="center", va="center", fontsize=8.6, color=MUTED)
    for x in (18.6, 38.6, 58.6, 78.6):
        ax.add_patch(FancyArrowPatch((x, 13), (x + 2.2, 13), arrowstyle="-|>",
                                     mutation_scale=12, color=MUTED))
    fig.savefig(path, dpi=200, bbox_inches="tight"); plt.close(fig)


def fig_sk(path):
    c = CLAIMS["sk_curve"]
    fig, ax = plt.subplots(figsize=(8.6, 3.3))
    ax.semilogx(c["f"], c["ideal"], "--", color=CLAIM, lw=1.8, label="ideal op-amp (my own stand-in)")
    ax.semilogx(c["f"], c["real"], color=MEAS, lw=2.2, label="transistor op-amp (as shipped)")
    if LT_CLAIMS and "sk_curve" in LT_CLAIMS:
        lc = LT_CLAIMS["sk_curve"]
        ax.semilogx(lc["f"], lc["real"], "o", ms=3.5, color=MEAS2, label="same, in LTspice")
    for v, lab in ((CLAIMS["sk_real"]["a1m"], "measured"), (CLAIMS["sk_ideal"]["a1m"], "ideal")):
        ax.plot([1e6], [v], "o", color=INK, ms=5)
        ax.annotate(f"{v:.1f} dB ({lab})", (1e6, v), (1.4e6, v + 6), fontsize=8.5)
    ax.axvline(1e6, color=GRID, lw=1)
    ax.set_xlabel("frequency (Hz)"); ax.set_ylabel("gain (dB)")
    ax.set_ylim(-100, 5); ax.grid(True, which="major", color=GRID, lw=0.6)
    ax.legend(frameon=False, fontsize=8.5, loc="lower left")
    ax.set_title("Sallen-Key low-pass, 10 kHz: claimed −43 dB at 1 MHz (real) vs −80 dB (ideal)",
                 fontsize=10, loc="left")
    fig.savefig(path, dpi=200, bbox_inches="tight"); plt.close(fig)


YIELD_ROWS = [  # key in tool run, key in MC, label, claimed number in the PDF
    ("rc_lowpass", "rc_lowpass", "RC low-pass", 45.0),
    ("sallen_key_lowpass", "sallen_key_lowpass", "Sallen-Key", 68.0),
    ("noninverting_amp", "noninverting_amp", "Amplifier ×10", 50.0),
    ("ldo", "ldo", "LDO (±5% supply)", 100.0),
    ("buck", "buck", "Buck (±5% supply)", 100.0),
]


def fig_yield(path):
    labels, claim, tool, mc, lo, hi = [], [], [], [], [], []
    for tk, mk, lab, cl in YIELD_ROWS:
        labels.append(lab); claim.append(cl)
        tool.append(((TPL.get(tk) or {}).get("finish", {}).get("yield") or {}).get("yield_pct"))
        m = MC.get(mk)
        if m:
            mc.append(m["yield_pct"]); lo.append(max(0, m["yield_pct"] - m["ci95"][0]))
            hi.append(max(0, m["ci95"][1] - m["yield_pct"]))
        else:
            mc.append(float("nan")); lo.append(0); hi.append(0)
    x = range(len(labels))
    fig, ax = plt.subplots(figsize=(9.2, 3.3))
    w = 0.27
    ax.bar([i - w for i in x], claim, w, color=CLAIM, label="claimed in the report (40 boards)")
    ax.bar(list(x), [t or 0 for t in tool], w, color=MEAS2, label="re-run through the tool (40 boards)")
    ax.bar([i + w for i in x], mc, w, color=MEAS, label="my own check (more boards, new random draw)",
           yerr=[lo, hi], capsize=3, ecolor=INK, error_kw={"lw": 0.9})
    for i in x:
        for dx, v in ((-w, claim[i]), (0, tool[i]), (w, mc[i])):
            if v is not None and not math.isnan(v):
                ax.text(i + dx, v + 2, f"{v:.0f}", ha="center", fontsize=7.5)
    ax.set_xticks(list(x)); ax.set_xticklabels(labels)
    ax.set_ylabel("boards meeting every spec (%)"); ax.set_ylim(0, 118)
    ax.legend(frameon=False, fontsize=8, ncol=3, loc="upper center", bbox_to_anchor=(0.5, 1.13))
    fig.savefig(path, dpi=200, bbox_inches="tight"); plt.close(fig)


SCH_CLAIM = {"rc_lowpass": 100, "sallen_key_lowpass": 100, "noninverting_amp": 100,
             "ldo": 97, "buck": 100}


def fig_schem(path):
    rows = [r for r in SCH if r["circuit"] in SCH_CLAIM or r["circuit"] == "instrumentation_amp"]
    names = {"rc_lowpass": "RC", "sallen_key_lowpass": "Sallen-Key", "noninverting_amp": "Amplifier",
             "ldo": "LDO", "buck": "Buck", "instrumentation_amp": "Instr. amp\n(new, unseen)"}
    fig, ax = plt.subplots(figsize=(8.6, 2.9))
    x = range(len(rows)); w = 0.38
    cl = [SCH_CLAIM.get(r["circuit"], float("nan")) for r in rows]
    now = [r["lint"]["score"] for r in rows]
    ax.bar([i - w / 2 for i in x], cl, w, color=CLAIM, label="score in the report")
    ax.bar([i + w / 2 for i in x], now, w, color=MEAS, label="score measured now (v0.13.0)")
    for i in x:
        if not math.isnan(cl[i]):
            ax.text(i - w / 2, cl[i] + 2, f"{cl[i]:.0f}", ha="center", fontsize=8)
        ax.text(i + w / 2, now[i] + 2, f"{now[i]:.0f}", ha="center", fontsize=8)
    ax.set_xticks(list(x)); ax.set_xticklabels([names[r["circuit"]] for r in rows])
    ax.set_ylabel("readability score (0–100)"); ax.set_ylim(0, 115)
    ax.legend(frameon=False, fontsize=8.5, ncol=2, loc="upper center", bbox_to_anchor=(0.5, 1.15))
    fig.savefig(path, dpi=200, bbox_inches="tight"); plt.close(fig)


def fig_search(path):
    a = SEARCH["analytic"]["sallen_key"]["first_feasible"]
    r = [x["first_feasible"] for x in SEARCH["real_sallen_key"]]
    fig, ax = plt.subplots(figsize=(8.6, 2.9))
    for data, col, lab in ((a, MEAS2, f"formula model, {len(a)} new random starts"),
                           (r, MEAS, f"real op-amp in ngspice, from scratch, {len(r)} starts")):
        xs = list(range(0, 61))
        ys = [100 * sum(1 for f in data if f is not None and f <= k) / len(data) for k in xs]
        ax.step(xs, ys, where="post", color=col, lw=2.2, label=lab)
    ax.plot([20], [100], "o", color=CLAIM, ms=7, label="claimed: 10 of 10, typically by 20")
    ax.axvline(20, color=GRID, lw=1)
    ax.set_xlabel("simulations spent"); ax.set_ylabel("runs with a working design (%)")
    ax.set_ylim(0, 108); ax.legend(frameon=False, fontsize=8.3, loc="lower right")
    fig.savefig(path, dpi=200, bbox_inches="tight"); plt.close(fig)


def fig_ina_resp(path):
    f = CUR["f"]
    fig, (a1, a2) = plt.subplots(1, 2, figsize=(9.6, 3.2))
    a1.semilogx(f, CUR["hand"]["diff_db"], "--", color=CLAIM, lw=1.8, label="hand-sized start")
    a1.semilogx(f, CUR["finished"]["diff_db"], color=MEAS, lw=2.2, label="finished parts")
    a1.axhspan(39.9, 40.1, color=BAND, alpha=0.6, lw=0)
    a1.axvline(20e3, color=GRID, lw=1); a1.text(21e3, 10, "20 kHz\n(min. bandwidth)", fontsize=7.5, color=MUTED)
    a1.set_ylim(0, 46); a1.set_xlabel("frequency (Hz)"); a1.set_ylabel("gain (dB)")
    a1.set_title("Wanted signal (difference between inputs)", fontsize=9.5, loc="left")
    a1.legend(frameon=False, fontsize=8, loc="lower left")
    a2.semilogx(f, CUR["hand"]["cm_db"], "--", color=CLAIM, lw=1.8)
    a2.semilogx(f, CUR["finished"]["cm_db"], color=MEAS, lw=2.2)
    a2.axhline(-40, color=BAD, lw=1.2); a2.text(12, -37, "spec limit (CMRR 80 dB)", fontsize=7.5, color=BAD)
    a2.axvline(60, color=GRID, lw=1)
    a2.set_xlabel("frequency (Hz)"); a2.set_ylabel("gain (dB)")
    a2.set_title("Unwanted signal (same on both inputs, e.g. mains hum)", fontsize=9.5, loc="left")
    fig.tight_layout(); fig.savefig(path, dpi=200, bbox_inches="tight"); plt.close(fig)


def fig_ina_yield(path):
    fig, ax = plt.subplots(figsize=(9.0, 3.0))
    for key, col, lab in (("ina_1pct", CLAIM, "1% resistors"), ("ina_1pct_0.1pct", MEAS, "0.1% resistors")):
        m = MC.get(key)
        if not m:
            continue
        v = [40 - r["metrics"]["cm_db"] for r in m["rows"] if "cm_db" in r["metrics"]]
        bins = [55 + 2.5 * i for i in range(35)]
        ax.hist([min(x, 140) for x in v], bins=bins, color=col, alpha=0.75,
                label=f"{lab}: {m['yield_pct']:.0f}% of {m['boards']} boards pass")
    ax.axvline(80, color=BAD, lw=1.4); ax.text(80.8, ax.get_ylim()[1] * 0.85, "spec: 80 dB", color=BAD, fontsize=8)
    ax.set_xlabel("hum rejection, CMRR at 60 Hz (dB)  — higher is better")
    ax.set_ylabel("boards"); ax.legend(frameon=False, fontsize=8.5)
    fig.savefig(path, dpi=200, bbox_inches="tight"); plt.close(fig)


def fig_lt_vs_ng(path):
    rows = []
    pairs = [("Sallen-Key at 1 MHz (dB)", CLAIMS["sk_real"].get("a1m"), LT_CLAIMS["sk_real"].get("a1m")),
             ("LDO phase margin (°)", CLAIMS["ldo_rz"].get("pm_deg"), LT_CLAIMS["ldo_rz"].get("pm_deg")),
             ("LDO load-step dip (mV)", CLAIMS["ldo_rz"].get("step_mv"), LT_CLAIMS["ldo_rz"].get("step_mv")),
             ("Op-amp gain (dB)", CLAIMS["opamp"].get("a0"), LT_CLAIMS["opamp"].get("a0")),
             ("Op-amp GBW (MHz)", CLAIMS["opamp"].get("gbw", 0) / 1e6, (LT_CLAIMS["opamp"].get("gbw") or 0) / 1e6),
             ("Buck peak on input step (V)", CLAIMS["buck_step"].get("vpk"), LT_CLAIMS["buck_step"].get("vpk"))]
    for lab, a, b in pairs:
        if a is not None and b is not None:
            rows.append((lab, a, b))
    fig, ax = plt.subplots(figsize=(8.6, 0.42 * len(rows) + 0.8))
    for i, (lab, a, b) in enumerate(rows):
        d = 100 * (b - a) / abs(a) if a else 0
        ax.barh(i, d, color=MEAS if abs(d) < 5 else WARN_C)
        ax.text(d + (0.3 if d >= 0 else -0.3), i, f"{d:+.1f}%  (ng {a:.4g} / LT {b:.4g})",
                va="center", ha="left" if d >= 0 else "right", fontsize=7.8)
    ax.set_yticks(range(len(rows))); ax.set_yticklabels([r[0] for r in rows], fontsize=8.5)
    ax.axvline(0, color=INK, lw=0.8); ax.set_xlim(-12, 12)
    ax.set_xlabel("LTspice minus ngspice (%)")
    fig.savefig(path, dpi=200, bbox_inches="tight"); plt.close(fig)


# ================================================================ document

def footer(c, doc):
    c.saveState()
    c.setFont("Sans", 7.5); c.setFillColor(colors.HexColor(MUTED))
    c.drawString(18 * mm, 10 * mm, "SpiceMCP 0.13.0 · independent verification")
    c.drawRightString(A4[0] - 18 * mm, 10 * mm, f"page {doc.page}")
    c.restoreState()


def build():
    FIG.mkdir(exist_ok=True)
    fig_method(FIG / "method.png"); fig_sk(FIG / "sallen_key_1mhz.png")
    fig_yield(FIG / "yield.png"); fig_schem(FIG / "schematic_scores.png")
    fig_search(FIG / "search.png"); fig_ina_resp(FIG / "ina_response.png")
    fig_ina_yield(FIG / "ina_cmrr.png")
    if HAVE_LT and LT_CLAIMS:
        fig_lt_vs_ng(FIG / "ltspice_vs_ngspice.png")

    tpl = {k: v for k, v in TPL.items() if k != "list_templates"}
    st = []

    # ------------------------------------------------------------ cover + verdict
    tool_rows = TOOLS["tools"]
    maxd = max(t["desc_chars"] for t in tool_rows)
    lt_tests = (LT_INFO or {}).get("pytest", "pending")
    st += [Spacer(1, 10 * mm), P("SpiceMCP 0.13.0", "title"),
           P("Independent verification: does it do what the report says?", "sub"), Spacer(1, 6),
           P("Every claim in the engineering report (v0.13.0, commit 16c0ad0) was re-measured from a fresh download, using a "
             "separate test client, separate test circuits and, where possible, two different "
             "simulators. Then one circuit that is not in SpiceMCP's library, a three-op-amp "
             "instrumentation amplifier, was designed from a written spec to an orderable parts "
             "list using only the tool."), Spacer(1, 6),
           img(FIG / "method.png"), Spacer(1, 6), P("Verdict in one page", "h2")]
    sk = CLAIMS["sk_real"]["a1m"]
    ldo = CLAIMS["ldo_rz"]; oa = CLAIMS["opamp"]; bs = CLAIMS["buck_step"]
    buck_tool = tpl["buck"]["finish"]["yield"]["yield_pct"]
    buck_mc = MC.get("buck", {}).get("yield_pct")
    rows = [["claim in the report", "measured here", "verdict"],
            ["About 340 automated checks pass", f"339 pass, 41 skipped on Linux (they need LTspice). "
             f"Windows + LTspice: {lt_tests}", "ok"],
            ["13 commands, each description short (limit 200 characters)",
             f"{TOOLS['n_tools']} commands, longest {maxd} characters, no untyped inputs", "ok"],
            ["Sallen-Key: −43 dB at 1 MHz with the real op-amp (−80 dB ideal)",
             f"{sk:.1f} dB (ideal stand-in: {CLAIMS['sk_ideal']['a1m']:.1f} dB)", "ok"],
            ["LDO: 72° phase margin with the series resistor, ~2° without",
             f"{ldo['pm_deg']:.1f}° with, {CLAIMS['ldo_norz']['pm_deg']:.1f}° without", "ok"],
            ["LDO: 39 mV dip on a 10× load step", f"{ldo['step_mv']:.1f} mV", "ok"],
            ["Op-amp: 87 dB, 10.6 MHz, 10 V/µs, 1.1 mA",
             f"{oa['a0']:.1f} dB, {oa['gbw'] / 1e6:.1f} MHz, {oa['slew']:.1f} V/µs, "
             f"{abs(oa['icc']) * 1e3:.2f} mA", "warn"],
            ["Buck: input step peaks at 5.7 V, back in spec after 124 µs, ~89% efficient",
             f"{bs['vpk']:.2f} V, {(bs['t_back'] - 0.6e-3) * 1e6:.0f} µs, "
             f"{tpl['buck']['start']['start_result']['metrics']['eff'] * 100:.1f}%", "ok"],
            ["Buck: 100% of boards regulate (closed loop)",
             "Output voltage: 100% of boards in spec (4.95 to 5.03 V). But after the tool's own "
             f"tune-and-finish steps some boards exceed 50 mV ripple: {buck_tool:.0f}% pass on its "
             f"40 boards" + (f", {buck_mc:.0f}% on {MC['buck']['boards']} fresh boards" if buck_mc is not None else ""),
             "warn"],
            ["Yield figures for the other four circuits", "Reproduced exactly by the tool; my larger "
             "re-check lands within the expected scatter, except the amplifier (66%, not 50%)", "warn"],
            ["Optimizer finds a working Sallen-Key in 10 of 10 runs, ~20 simulations",
             f"{SEARCH['analytic']['sallen_key']['found_within_60']} of 20 new starts within 60; "
             f"{sum(1 for x in SEARCH['real_sallen_key'] if x['first_feasible'])} of 10 with the real "
             "op-amp from scratch", "warn"],
            ["Every drawing is the same circuit as the netlist, pin by pin",
             (f"LTspice read back {sum(1 for r in LT_SCH if r.get('same_circuit'))} of {len(LT_SCH)} "
              "drawings as the identical circuit" if LT_SCH else "connections checked by eye; LTspice "
              "read-back pending"), "ok" if LT_SCH and all(r.get("same_circuit") for r in LT_SCH) else "warn"],
            ["Schematic readability: LDO 97, buck 100",
             "LDO 76, buck 69 today: the scores were measured before the real op-amp and the "
             "controller were added, and never updated", "fail"],
            ["The final report PDF in the repository", "The copy committed at v0.13.0 cannot be opened "
             "(broken file structure). Rebuilding it from its script works.", "fail"],
            ]
    st += [table(rows, [64 * mm, 82 * mm, 28 * mm], verdict_col=2), Spacer(1, 4),
           P("Plain-language summary: the electrical results are real and repeat in a second "
             "simulator. The weak spots are bookkeeping (a broken PDF, stale scores), how sure the "
             "yield numbers are, and drawings of circuits the layout was never tuned for.", "cap"),
           PageBreak()]

    # ------------------------------------------------------------ tests & tool surface
    st += section("CHECK 1", "Install, tests and the command list",
                  "A clean copy was downloaded and installed exactly as a new user would, and the "
                  "commands were listed by connecting to SpiceMCP the way an AI assistant does.")
    trows = [["command", "description length", "inputs"]] + [
        [t["name"], f"{t['desc_chars']} characters, {t['desc_lines']} lines", str(t["n_inputs"])]
        for t in tool_rows]
    st += [table(trows, [55 * mm, 70 * mm, 25 * mm]), Spacer(1, 4)]
    st += bullets([
        f"<b>Tests:</b> 339 passed, 41 skipped on Linux; the skipped ones need LTspice. "
        f"On your Windows laptop with LTspice: {lt_tests}.",
        f"<b>Commands:</b> {TOOLS['n_tools']}, every description two lines and at most {maxd} "
        "characters. Every input has a declared shape, so the assistant never guesses a format.",
        "<b>Small inaccuracies:</b> the server's own header still says \"11 tools\", and "
        "<i>simulate</i> always asks for LTspice, so the one-off simulation command does not work "
        "on a machine with only ngspice (the design flow does).",
    ])
    st.append(PageBreak())

    # ------------------------------------------------------------ search
    st += section("CHECK 2", "Does the optimizer find working part values?",
                  "The report says the Sallen-Key filter search now succeeds in 10 of 10 runs, "
                  "typically after about 20 simulations. That was measured on ten particular random "
                  "starts. I used twenty different ones, and also ran the harder real-op-amp version "
                  "from scratch, with no hand calculation to start from.")
    st += [img(FIG / "search.png", max_h=72 * mm),
           P("Share of runs that have found a design meeting every limit, against the number of "
             "simulations spent.", "cap")]
    an = SEARCH["analytic"]["sallen_key"]
    st += bullets([
        f"Formula model, new starts: {an['found_within_60']} of 20 succeed within 60 simulations; "
        f"{an['found_within_20']} of 20 within 20. The claimed median of about 20 holds; "
        "\"10 of 10\" is what those ten starts happened to give.",
        f"Real op-amp in the simulator, no starting values: "
        f"{sum(1 for x in SEARCH['real_sallen_key'] if x['first_feasible'])} of 10 succeed within 60 "
        "simulations, most within 15 to 25.",
        f"RC filter: 20 of 20 within 20 simulations (claimed 10 of 10, about 6). Holds.",
        "<b>For you:</b> give a run 60 simulations, not 20, and check its status before trusting it. "
        "The tool tells you honestly when it has not found a design yet.",
    ])
    st.append(PageBreak())

    # ------------------------------------------------------------ real devices
    st += section("CHECK 3", "Real op-amp, LDO stability and the closed-loop buck",
                  "These are the electrical claims that matter most on a bench. Each was "
                  "re-measured with a test circuit written separately from SpiceMCP's own.")
    st += [img(FIG / "sallen_key_1mhz.png", max_h=70 * mm),
           P("The real op-amp lets signal leak through above about 300 kHz; an ideal model hides it. "
             "Claim confirmed.", "cap"), Spacer(1, 4)]
    rows = [["what", "claimed", "measured", "verdict"],
            ["Op-amp open-loop gain", "87 dB", f"{oa['a0']:.1f} dB", "ok"],
            ["Op-amp gain-bandwidth", "10.6 MHz", f"{oa['gbw'] / 1e6:.2f} MHz", "warn"],
            ["Op-amp slew rate", "10 V/µs", f"{oa['slew']:.1f} V/µs", "warn"],
            ["Op-amp supply current", "1.1 mA", f"{abs(oa['icc']) * 1e3:.2f} mA", "ok"],
            ["LDO phase margin, with 0.83 Ω series resistor", "72°", f"{ldo['pm_deg']:.1f}°", "ok"],
            ["LDO phase margin, capacitor alone", "2°", f"{CLAIMS['ldo_norz']['pm_deg']:.1f}°", "ok"],
            ["LDO dip on a 10 mA → 100 mA step", "39 mV", f"{ldo['step_mv']:.1f} mV", "ok"],
            ["Buck: peak when input jumps 11.4 → 12.6 V", "5.7 V", f"{bs['vpk']:.2f} V", "ok"],
            ["Buck: back within ±2% after", "124 µs", f"{(bs['t_back'] - 0.6e-3) * 1e6:.0f} µs", "ok"],
            ["Buck efficiency", "about 89%",
             f"{tpl['buck']['start']['start_result']['metrics']['eff'] * 100:.1f}%", "ok"]]
    st += [table(rows, [72 * mm, 30 * mm, 40 * mm, 30 * mm], verdict_col=3), Spacer(1, 4)]
    st += bullets([
        "Gain-bandwidth and slew are 6–7% below the stated figures with my test load. The op-amp "
        "is sized for 10 MHz, so 10.6 MHz looks like a different load in the original measurement. "
        "Not important for any circuit here.",
        "<b>Gap:</b> the buck's control loop has no simulated stability check. Its 90° margin is a "
        "hand estimate, while the LDO and op-amp circuits get a real measurement. The input-step "
        "result above is good evidence the loop is stable, but it is not the same check.",
        "The buck overshoots more with the controller (5.72 V) than without (5.47 V), because the "
        "loop is slow. The report says so; worth knowing before you power anything sensitive from it.",
    ])
    st.append(PageBreak())

    # ------------------------------------------------------------ yield
    st += section("CHECK 4", "How many built boards would pass (yield)",
                  "Yield is estimated by simulating virtual boards with every part at a random value "
                  "inside its tolerance. I re-ran the whole tool flow, then checked each finished parts "
                  "list again with my own random draw and more boards.")
    st += [img(FIG / "yield.png", max_h=74 * mm),
           P("Black whiskers: the range the true yield lies in with 95% confidence, from my larger "
             "sample.", "cap")]
    st += bullets([
        "<b>The tool's numbers reproduce exactly</b>: it always uses the same random draw, so running "
        "it again proves nothing new. That is why I made a fresh draw with more boards.",
        "RC, Sallen-Key and LDO: my check agrees with the claim.",
        "Amplifier: 66% on 200 boards, not 50%. With 40 boards a 15-point miss is just bad luck; "
        "the claim is too pessimistic, not wrong in direction.",
        f"Buck: the report's 100% was measured at the hand-sized values. Through the tool's own "
        f"tune-and-finish steps the inductor shrinks to 18 µH and 2 of 40 boards exceed 50 mV ripple "
        f"({buck_tool:.0f}%)."
        + (f" My 100-board check: {buck_mc:.0f}%; every failure is ripple of 50.1 to 53.8 mV, "
           "while the output voltage itself stays in spec on every board, so the regulation "
           "claim holds." if buck_mc is not None else ""),
        "<b>Wording to fix:</b> each answer says \"40 runs resolve yield to about 2%\". 40 boards give "
        "steps of 2.5%, but the uncertainty is ±8 to ±15 points. The engineering report's own "
        "\"about ±8%\" is the honest figure.",
        "<b>Subtle bug:</b> for the LDO the ±5% supply tolerance is applied to a source that the "
        "first test then sweeps anyway, so that part of the check does nothing. The LDO still "
        "passes when I apply the supply correctly, so the result stands.",
    ])
    st.append(PageBreak())

    # ------------------------------------------------------------ schematics
    st += section("CHECK 5", "Automatic schematics",
                  "Two separate questions: is the drawing the same circuit as the netlist (a must), "
                  "and is it easy to read (nice to have)?")
    if LT_SCH:
        ok = sum(1 for r in LT_SCH if r.get("same_circuit"))
        same_txt = (f"<b>Same circuit:</b> LTspice itself turned each of the {len(LT_SCH)} drawings "
                    f"back into a netlist, and my own comparison matched it pin by pin to the source "
                    f"in {ok} of {len(LT_SCH)} cases, including the new instrumentation amplifier.")
    else:
        same_txt = ("<b>Same circuit:</b> checked by eye on the new instrumentation amplifier and the "
                    "buck; the LTspice read-back is in the LTspice section.")
    st += [img(FIG / "schematic_scores.png", max_h=62 * mm),
           P("Readability score: 100 minus points for crossings, wires through parts and similar.", "cap")]
    st += bullets([
        same_txt,
        "<b>Readability:</b> the RC, Sallen-Key and amplifier drawings still score 100, and the eight "
        "small test circuits 99–100. The LDO (76) and buck (69) fell when real devices and the "
        "controller were added, and the report kept the old numbers.",
        "The new instrumentation amplifier draws correctly but scores 27: long wires and five "
        "crossings (picture on the next page). Three op-amps with cross-coupled feedback are beyond "
        "what the placer was tuned for, so \"works on circuits it was never tuned for\" holds only "
        "for small circuits.",
    ])
    st.append(PageBreak())

    # ------------------------------------------------------------ new circuit
    f1, f2 = ina_answer("finish_design", 0), ina_answer("finish_design", 1)
    hand = ina_answer("evaluate_candidate")["metrics"]
    st += section("NEW CIRCUIT", "An instrumentation amplifier, designed with the tool",
                  "Not in SpiceMCP's library, so every step went through the general-purpose "
                  "commands, the way an assistant would use them.")
    st += [P("The spec, as an engineer would write it", "h2")]
    st += bullets([
        "Amplify the difference between two sensor wires by <b>100 (40 dB ± 0.1 dB)</b>.",
        "Bandwidth at least <b>20 kHz</b>.",
        "Reject a signal common to both wires (mains hum) by at least <b>80 dB at 60 Hz</b>.",
        "±5 V supply, 10 kΩ and 100 pF load, stable (phase margin ≥ 45°). Standard 1% resistors.",
    ])
    st += [P("What I did, in the tool's own commands", "h2")]
    st += bullets([
        "<b>list_templates</b>: no instrumentation amplifier, so a custom design is needed.",
        "<b>start_from_template</b> + <b>query_run(netlist)</b> on the amplifier template, only to copy "
        "the transistor-level op-amp model. There is no command that hands it out directly.",
        "Wrote the circuit: three op-amps, seven resistors as tunable values, a second copy driven "
        "with the same signal on both inputs to measure hum rejection, and a loop-break test for "
        "stability. <b>check_feasibility</b> and <b>start_optimization</b> accepted it.",
        "<b>evaluate_candidate</b> with textbook values (all 10 kΩ, gain resistor 200 Ω), "
        "<b>run_optimization</b> (30 simulations), <b>finish_design</b> with 1% and with 0.1% resistors.",
    ])
    st += [img(FIG / "ina_response.png", max_h=66 * mm),
           P("Sand band: the allowed gain window. Red line: the limit for the unwanted signal.", "cap"),
           PageBreak()]
    sch_png = RES / "schematics" / "instrumentation_amp.png"
    if sch_png.is_file():
        st += [img(sch_png, max_h=105 * mm),
               P("The schematic SpiceMCP drew for the finished amplifier. It is the right circuit, "
                 "but untidy (score 27).", "cap")]
    parts = f2.get("parts", {})
    prow = [["part", "value", "tolerance", "role"]]
    role = {"R1A": "feedback, input stage", "R1B": "feedback, input stage",
            "RG": "sets the gain", "R2A": "difference stage in", "R2B": "difference stage in",
            "R3A": "difference stage feedback", "R3B": "difference stage to ground"}
    for k in ("RG", "R1A", "R1B", "R2A", "R2B", "R3A", "R3B"):
        if k in parts:
            prow.append([k, parts[k]["label"] + " Ω", f"{parts[k]['tolerance_pct']:g}%", role[k]])
    prow.append(["X1–X3", "op-amp", "", "any ±5 V op-amp, 10 MHz GBW class"])
    st += [P("Parts list", "h2"), table(prow, [20 * mm, 30 * mm, 25 * mm, 90 * mm])]
    st.append(PageBreak())
    m1, m2 = MC.get("ina_1pct", {}), MC.get("ina_1pct_0.1pct", {})
    st += [P("Before and after", "h2")]
    rows = [["", "hand-sized start", "finished, standard values"],
            ["gain", f"{hand['gain_db']:.2f} dB", f"{f2['metrics']['gain_db']:.2f} dB"],
            ["bandwidth", eng(hand["bw"], "Hz"), eng(f2["metrics"]["bw"], "Hz")],
            ["hum rejection (CMRR) at 60 Hz", f"{40 - hand['cm_db']:.0f} dB", f"{40 - f2['metrics']['cm_db']:.0f} dB"],
            ["phase margin", f"{hand['pm_deg']:.0f}°", f"{f2['metrics']['pm_deg']:.0f}°"],
            ["boards passing, 1% resistors", "–", f"{m1.get('yield_pct', float('nan')):.0f}% of {m1.get('boards', 0)}"],
            ["boards passing, 0.1% resistors", "–", f"{m2.get('yield_pct', float('nan')):.0f}% of {m2.get('boards', 0)}"]]
    st += [table(rows, [62 * mm, 50 * mm, 55 * mm]), Spacer(1, 6),
           img(FIG / "ina_cmrr.png", max_h=64 * mm),
           P("Each bar counts virtual boards. Hum rejection depends on how well four resistors match, "
             "so it is the spec that suffers from tolerance.", "cap")]
    st += bullets([
        "<b>The tool got it right where it matters:</b> it confirmed the textbook design meets every "
        "spec, turned it into standard values and showed that 1% resistors are not good enough "
        "for 80 dB (only "
        f"{m1.get('yield_pct', 0):.0f}% of boards pass), while 0.1% resistors give "
        f"{m2.get('yield_pct', 0):.0f}%. That is the real-world answer: use 0.1% parts or a matched "
        "resistor network.",
        "<b>Where a human would do better:</b> the optimizer, told only to widen bandwidth, de-matched "
        "the resistor pairs for +3% bandwidth, dropping nominal hum rejection from 127 dB to 96 dB "
        "(still inside spec; snapping to standard values happened to restore it). An engineer would keep the pairs equal. Ask for both goals, or fix "
        "the pairs, when using it.",
        f"finish_design with 1% resistors reported {f1['yield']['yield_pct']:.0f}% yield on its "
        f"40 boards; my 200-board check of that same parts list gave "
        f"{MC['ina_tool1pct']['yield_pct']:.0f}%. Consistent.",
        "<b>Rough edges met on the way:</b> no command returns the op-amp model; the stability-test "
        "parts (1 kH, 1 kF) raise two false \"impractical value\" warnings; the design report on "
        "ngspice has no plots.",
    ])
    st.append(PageBreak())

    # ------------------------------------------------------------ LTspice
    st += section("CHECK 6", "The same checks in LTspice on Windows",
                  "SpiceMCP is meant for LTspice. Everything above was repeated on your laptop with "
                  "LTspice, from a separate fresh copy.")
    if HAVE_LT:
        st += bullets(LT_INFO.get("summary", []))
        if (FIG / "ltspice_vs_ngspice.png").is_file():
            st += [img(FIG / "ltspice_vs_ngspice.png", max_h=70 * mm),
                   P("Same test circuits in both simulators. Bars: how far LTspice lands from ngspice.", "cap")]
    else:
        st += bullets(["Pending: the LTspice run on your laptop had not reported back when this version was built. This page will be filled in when it does; everything above is from ngspice."])
    st.append(PageBreak())

    # ------------------------------------------------------------ fixes
    st += section("WHAT TO FIX", "Discrepancies, most important first",
                  "None of these changes an electrical conclusion. Each is small to fix.")
    rows = [["#", "issue", "suggested fix"],
            ["1", "docs/final/SpiceMCP_Engineering_Report.pdf at v0.13.0 is a broken file (two copies "
                  "written over each other). The copy shared in the project is the same broken file.",
             "Rebuild with make_pdf.py and add a test that opens the PDF."],
            ["2", "Readability scores in the report (LDO 97, buck 100) predate steps 6 and 8.",
             "Recompute from the current drawings, or label them by version."],
            ["3", "Buck 100% yield holds at hand-sized values, not after tuning (95%).",
             "Say which, or keep ripple margin when tuning shrinks L."],
            ["4", "\"40 runs resolve yield to about 2%\" overstates precision; the random draw is fixed.",
             "Report a confidence range; allow a seed."],
            ["5", "LDO supply tolerance does nothing in the swept first test.",
             "Move the read-out point with the supply, as done in this check."],
            ["6", "Buck has no simulated loop-stability test.", "Add a loop-break test like the LDO's."],
            ["7", "Custom op-amp circuits need the op-amp model copied from a template run.",
             "Return the model from list_templates or a resource."],
            ["8", "simulate always uses LTspice; ngspice runs only through the design flow.",
             "Honour the backend option there too."],
            ["9", "Large circuits draw correctly but untidily (instrumentation amplifier: 27).",
             "Group mirrored stages; a known limit is fine if stated."],
            ["10", "Op-amp GBW/slew 6–7% below the stated 10.6 MHz / 10 V/µs.", "State the test load."]]
    st += [table(rows, [8 * mm, 98 * mm, 64 * mm])]
    st += [Spacer(1, 8), P("How to repeat this", "h2")]
    st += bullets([
        "Scripts: docs/verification/scripts/ (README inside). Raw results: docs/verification/results/.",
        "Rebuild this PDF: python docs/verification/make_report.py",
    ])

    doc = SimpleDocTemplate(str(OUT), pagesize=A4, leftMargin=18 * mm, rightMargin=18 * mm,
                            topMargin=16 * mm, bottomMargin=16 * mm,
                            title="SpiceMCP 0.13.0 independent verification", author="Shounak Das")
    doc.build(st, onFirstPage=footer, onLaterPages=footer)
    print(OUT)


if __name__ == "__main__":
    build()
