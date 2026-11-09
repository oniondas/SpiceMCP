"""Benchmark report: SpiceMCP against a plain model, a model with a raw simulator, and
other open-source SPICE servers, on three circuits (RC filter, x10 amplifier, buck).

    python docs/reports/benchmark/make_report.py  ->  SpiceMCP_Benchmark.pdf + figures/

Reads results/checks.json (measure/checker.py on every arm's final netlist) and
results/effort.json (time, calls, tokens per arm). Needs matplotlib and reportlab."""
from __future__ import annotations

import json
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from reportlab.lib import colors  # noqa: E402
from reportlab.lib.pagesizes import A4  # noqa: E402
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet  # noqa: E402
from reportlab.lib.units import mm  # noqa: E402
from reportlab.platypus import (Image, PageBreak, Paragraph, SimpleDocTemplate,  # noqa: E402
                                Spacer, Table, TableStyle)

HERE = Path(__file__).resolve().parent
RES = HERE / "results"
FIG = HERE / "figures"
OUT = HERE / "SpiceMCP_Benchmark.pdf"
ARMS = ["spicemcp", "blind", "rawspice"]
ARM_NAME = {"spicemcp": "SpiceMCP 1.1", "blind": "Model alone, no tools",
            "rawspice": "Model + raw simulator"}
COL = {"spicemcp": "#2a6fdb", "blind": "#9aa0a6", "rawspice": "#e8912d"}
CIRC = ["rc", "amp", "buck"]
CIRC_NAME = {"rc": "RC low-pass 1 kHz", "amp": "x10 amplifier", "buck": "12 V to 5 V buck"}


def load():
    return (json.loads((RES / "checks.json").read_text()), json.loads((RES / "effort.json").read_text()),
            json.loads((RES / "haiku_checks.json").read_text()))


def fig_yield(checks, tag="", title=""):
    FIG.mkdir(exist_ok=True)
    fig, ax = plt.subplots(figsize=(7.2, 3.0), dpi=200)
    w = 0.26
    for i, a in enumerate(ARMS):
        ys = [checks[a][c]["yield_pct"] for c in CIRC]
        xs = [k + (i - 1) * w for k in range(len(CIRC))]
        bars = ax.bar(xs, ys, w * 0.92, color=COL[a], label=ARM_NAME[a])
        for x, y, c in zip(xs, ys, CIRC):
            note = f"{y:g}%" + ("*" if checks[a][c]["not_buyable"] or checks[a][c].get("ideal") else "")
            ax.text(x, y + 1.5, note, ha="center", fontsize=7)
    ax.set_xticks(range(len(CIRC)), [f"{CIRC_NAME[c]}\n({checks['spicemcp'][c]['boards']} boards)" for c in CIRC], fontsize=8)
    ax.set_ylabel("boards that meet every spec", fontsize=8)
    ax.set_ylim(0, 112)
    if title:
        ax.set_title(title, fontsize=9, loc="left")
    ax.legend(fontsize=7, frameon=False, ncol=3, loc="upper left")
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)
    ax.tick_params(labelsize=7)
    fig.tight_layout()
    p = FIG / f"yield{tag}.png"
    fig.savefig(p)
    plt.close(fig)
    return p


def fig_effort(effort):
    fig, axs = plt.subplots(1, 2, figsize=(7.2, 2.4), dpi=200)
    for ax, key, label in ((axs[0], "tool_tokens", "tokens sent to/from tools\n(all 3 circuits)"),
                           (axs[1], "calls", "tool calls (all 3 circuits)")):
        vals = [effort[a][key] for a in ARMS]
        ax.barh([ARM_NAME[a] for a in ARMS], vals, color=[COL[a] for a in ARMS])
        for i, v in enumerate(vals):
            ax.text(v, i, f" {v:,}", va="center", fontsize=7)
        ax.set_title(label, fontsize=8)
        ax.tick_params(labelsize=7)
        ax.set_xlim(0, max(vals) * 1.35 or 1)
        for s in ("top", "right"):
            ax.spines[s].set_visible(False)
    fig.tight_layout()
    p = FIG / "effort.png"
    fig.savefig(p)
    plt.close(fig)
    return p


SS = getSampleStyleSheet()
ST = {"title": ParagraphStyle("t", parent=SS["Title"], fontSize=20, spaceAfter=4),
      "h2": ParagraphStyle("h2", parent=SS["Heading2"], fontSize=13, spaceBefore=8, spaceAfter=4),
      "body": ParagraphStyle("b", parent=SS["BodyText"], fontSize=9.5, leading=13),
      "small": ParagraphStyle("s", parent=SS["BodyText"], fontSize=8, leading=10.5),
      "cell": ParagraphStyle("c", parent=SS["BodyText"], fontSize=7.8, leading=9.8)}


def P(t, s="body"):
    return Paragraph(t, ST[s])


def bullets(items):
    return [P("• " + t) for t in items]


def table(rows, widths, head=True):
    t = Table([[P(str(c), "cell") for c in r] for r in rows], colWidths=widths, repeatRows=1)
    style = [("GRID", (0, 0), (-1, -1), 0.3, colors.HexColor("#c8ccd2")),
             ("VALIGN", (0, 0), (-1, -1), "TOP"),
             ("TOPPADDING", (0, 0), (-1, -1), 2), ("BOTTOMPADDING", (0, 0), (-1, -1), 2)]
    if head:
        style.append(("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#eef1f5")))
    t.setStyle(TableStyle(style))
    return t


def yes(b):
    return "yes" if b else "<font color='#b3261e'>no</font>"


def nominal_text(c, r):
    n = r["nominal"]
    if c == "rc":
        m = n["nominal"]
        return f"fc {m.get('fc', 0):.0f} Hz"
    if c == "amp":
        m = n["nominal"]
        return f"{m.get('gain_db', 0):.3f} dB, BW {m.get('bw', 0) / 1e6:.2f} MHz, overshoot {m.get('overshoot_pct', 0):.0f}%"
    return ", ".join(f"{k}: {v.get('vout', 0):.3f} V / {v.get('ripple_mv', 0):.0f} mV" for k, v in n.items())


def results_table(checks):
    rows = [["circuit", "arm", "values", "meets spec (nominal)", "measured", "yield", "all values buyable"]]
    for c in CIRC:
        for a in ARMS:
            r = checks[a][c]
            parts = ", ".join(f"{k} {eng(v)}" for k, v in r["parts"].items()) if c != "buck" else \
                f"{len(r['parts'])} R/C/L"
            buy = yes(r["all_buyable"]) + (f" ({', '.join(r['not_buyable'])})" if r["not_buyable"] else "")
            if r.get("ideal"):
                buy += f"; {r['ideal']}"
            rows.append([CIRC_NAME[c], ARM_NAME[a], parts, yes(r["nominal_pass"]),
                         nominal_text(c, r), f"{r['yield_pct']:g}% of {r['boards']}", buy])
    return table(rows, [24 * mm, 26 * mm, 30 * mm, 17 * mm, 40 * mm, 16 * mm, 21 * mm])


def effort_table(e):
    rows = [["arm", "wall-clock (3 circuits)", "tool calls", "simulator runs",
             "tokens to/from tools", "model output tokens", "what you get back"]]
    for a in ARMS:
        x = e[a]
        rows.append([ARM_NAME[a], x["time"], f"{x['calls']}", x["sims"], f"{x['tool_tokens']:,}",
                     x["model_out"], x["deliverables"]])
    return table(rows, [26 * mm, 26 * mm, 14 * mm, 20 * mm, 20 * mm, 26 * mm, 42 * mm])


def build():
    checks, effort, hk = load()
    yfig = fig_yield(hk, "_haiku", "Haiku 5.5 (a smaller model) as the designer")
    yfig_o = fig_yield(checks, "", "Opus 5.5 (a strong model) as the designer")
    efig = fig_effort(effort)
    st = [P("How SpiceMCP compares", "title"),
          P(f"Three circuits, three ways of designing them, one independent checker. "
            f"SpiceMCP {effort['version']}, ngspice 42, {effort['date']}.", "small"),
          Spacer(1, 6), P("The short answer", "h2")]
    st += bullets(effort["headline"])
    st += [Spacer(1, 4), Image(str(yfig), width=140 * mm, height=58.3 * mm),
           Image(str(yfig_o), width=140 * mm, height=58.3 * mm),
           P("Yield: share of randomly built boards (resistors 1%, capacitors 5%, inductors 20%, "
             "supply ±5%) that meet every spec at −40, 25 and 85 °C. Same random boards for "
             "every arm. * = uses a value you cannot buy or an ideal switch.", "small")]

    st += [PageBreak(), P("What was compared", "h2")]
    st += bullets([
        "<b>Two designers:</b> Opus 5.5 (strong) and Haiku 5.5 (smaller). Opus checked every "
        "result and wrote the report.",
        "<b>SpiceMCP 1.1:</b> with Opus, a script made the calls a model would make (list "
        "templates, start, optimise 20 rounds, finish, report, draw) over the real MCP "
        "connection. With Haiku, the model chose and made every call itself.",
        "<b>Model alone, no tools:</b> a fresh model given only the spec text; it wrote the "
        "three netlists from memory, without simulating.",
        "<b>Model + raw simulator:</b> a fresh model allowed only to write netlists and run "
        "ngspice, with no templates, optimiser, value snapping or yield. Budget about 25 runs.",
        "<b>Textbook reference:</b> for the RC filter and the amplifier the textbook answer "
        "(15.8 kΩ with 10 nF; 9.09 kΩ over 1 kΩ) is exactly what the model alone wrote, so it "
        "is not a separate column.",
        "<b>Other SPICE servers:</b> installing third-party packages was blocked in this "
        "sandbox, so they were compared on features from their public pages, not run.",
        "<b>The checker</b> (measure/checker.py) is written separately from SpiceMCP, strips "
        "every arm's own analyses and measures all of them with the same decks.",
    ])
    st += [P("Results, circuit by circuit (Opus 5.5 as the designer)", "h2")]
    st.append(results_table(checks))

    st += [PageBreak(), P("Effort with Opus 5.5: time, calls and tokens", "h2"),
           Image(str(efig), width=174 * mm, height=58 * mm)]
    st.append(effort_table(effort))
    st.append(P(effort["effort_note"], "small"))

    st += [P("Against other open-source SPICE servers (features, from their public pages)", "h2")]
    feats = effort["features"]
    st.append(table([feats["head"]] + feats["rows"],
                    [40 * mm] + [134 * mm / (len(feats["head"]) - 1)] * (len(feats["head"]) - 1)))
    st.append(P(feats["note"], "small"))

    he = effort["haiku"]
    st += [PageBreak(), P("Same test with a smaller model: Haiku 5.5", "h2")]
    st += bullets(he["about"])
    st += [P("Results, circuit by circuit (Haiku 5.5 as the designer)", "h2"), results_table(hk),
           P("Effort with Haiku 5.5", "h2"), effort_table(he), P(he["note"], "small")]

    st += [PageBreak(), P("Brutally honest reading", "h2")]
    st += bullets(effort["honest"])
    st += [P("What to fix next", "h2")]
    st += bullets(effort["fixes"])
    st += [P("Limits of this benchmark", "h2")]
    st += bullets(effort["limits"])

    doc = SimpleDocTemplate(str(OUT), pagesize=A4, leftMargin=18 * mm, rightMargin=18 * mm,
                            topMargin=16 * mm, bottomMargin=16 * mm, title="SpiceMCP benchmark",
                            author="Shounak Das")
    doc.build(st)
    print(OUT)


def eng(v):
    for s, f in (("M", 1e6), ("k", 1e3), ("", 1), ("m", 1e-3), ("µ", 1e-6), ("n", 1e-9), ("p", 1e-12)):
        if abs(v) >= f * 0.999:
            return f"{v / f:.3g}{s}"
    return f"{v:.3g}"


if __name__ == "__main__":
    build()
