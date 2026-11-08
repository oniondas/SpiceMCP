"""Circuit catalog: every circuit built so far, in serial order, with its schematic,
its simulated response and its key result.

    python docs/circuits/make_catalog.py   ->  docs/circuits/SpiceMCP_Circuit_Catalog.pdf

Needs ngspice, matplotlib and reportlab. Schematics 1 to 14 are drawn by
SpiceMCP's own auto-placer (`layout.draw`) from the netlist that is simulated. The two
transistor-level building blocks (15, 16) are drawn by hand (the `Sch` class), because the
auto-placer draws an op-amp as one symbol. Chua's circuit (17) uses the gallery images.
"""
from __future__ import annotations

import json
import logging
import math
import subprocess
import sys
import tempfile
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
logging.getLogger("matplotlib.font_manager").setLevel(logging.ERROR)
import matplotlib.pyplot as plt  # noqa: E402

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(ROOT))
from spicemcp import ir, layout, opamp, render, sim, topologies as T  # noqa: E402

FIG = HERE / "figures"
OUT = HERE / "SpiceMCP_Circuit_Catalog.pdf"
BLUE, ORANGE, TEAL, BAND = "#1E56A0", "#E07A5F", "#38A3A5", "#F2CC8F"
INK, MUTED, GRID, BG = "#1F2328", "#57606A", "#E6E8EB", "#FFFFFF"
plt.rcParams.update({"font.family": "DejaVu Sans", "font.size": 9.5, "axes.edgecolor": GRID,
                     "axes.labelcolor": MUTED, "xtick.color": MUTED, "ytick.color": MUTED,
                     "figure.facecolor": BG, "axes.facecolor": BG, "text.color": INK})

OA = opamp.subckt()
RAILS = "Vcc vcc 0 5\nVee vee 0 -5\n"


# --------------------------------------------------------------------------- helpers
def wave(deck: str, *exprs: str):
    """Run one deck in ngspice. Returns (x, [y per expr], metrics)."""
    work = Path(tempfile.mkdtemp())
    w = " ".join(exprs)
    if not deck.lstrip().startswith("*"):            # SPICE reads line 1 as the title
        deck = "* catalog deck\n" + deck
    deck = sim.to_ngspice(deck).replace(".endc", f"wrdata {work / 'w.txt'} {w}\n.endc")
    (work / "d.cir").write_text(deck)
    out = subprocess.run(["ngspice", "-b", str(work / "d.cir")], capture_output=True,
                         text=True, timeout=900)
    metrics, _ = sim.parse_ngspice(out.stdout)
    xs, ys = [], [[] for _ in exprs]
    for line in (work / "w.txt").read_text().splitlines():
        p = line.split()
        if len(p) >= 2 * len(exprs):
            try:
                vals = [float(v) for v in p]
            except ValueError:
                continue
            xs.append(vals[0])
            for i in range(len(exprs)):
                ys[i].append(vals[2 * i + 1])
    if not xs:
        raise RuntimeError(f"ngspice wrote no data for {exprs}:\n{out.stdout[-2000:]}")
    return xs, ys, metrics


def draw(net: str, name: str, values: dict | None = None) -> tuple[Path, int]:
    """SpiceMCP's own auto-placed schematic, rendered to PNG."""
    dr = layout.draw(net, values)
    dr.sheet.directives = [x for x in dr.sheet.directives if not x.startswith(".meas")]
    work = Path(tempfile.mkdtemp())
    p = dr.write(work / f"{name}.asc")
    score = layout.lint(p)["score"]
    png = FIG / f"{name}_schematic.png"
    render.render(str(p), str(png), "tech_minimal", 160)
    return png, score


def first_deck(net: str) -> str:
    return sim.decks(net)[0]


def clean(ax, title=None):
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)
    ax.tick_params(length=0)
    ax.grid(color=GRID, lw=0.7)
    ax.set_axisbelow(True)
    if title:
        ax.set_title(title, loc="left", fontsize=10)


def save(fig, name: str) -> Path:
    p = FIG / f"{name}_plot.png"
    fig.tight_layout()
    fig.savefig(p, dpi=150)
    plt.close(fig)
    return p


def bode(ax, f, db, ylabel="gain (dB)"):
    ax.semilogx(f, db, color=BLUE, lw=2)
    ax.set_xlabel("frequency (Hz)")
    ax.set_ylabel(ylabel)
    clean(ax)


def eng(v: float, unit: str = "") -> str:
    for k, p in ((1e9, "G"), (1e6, "M"), (1e3, "k"), (1, ""), (1e-3, "m"), (1e-6, "µ"),
                 (1e-9, "n"), (1e-12, "p")):
        if abs(v) >= k:
            return f"{v / k:.3g} {p}{unit}"
    return f"{v:.3g} {unit}"


def at(xs, ys, x0):
    return ys[min(range(len(xs)), key=lambda i: abs(xs[i] - x0))]


def template(name, spec):
    d = T.get(name).design(spec)
    return d, ir.render(d.netlist, d.start)[0]


# --------------------------------------------------------------------------- circuits
def c_rc():
    d, net = template("rc_lowpass", {"fc": 1000})
    sch, sc = draw(d.netlist, "01_rc", d.start)
    f, (y,), m = wave(first_deck(net), "vdb(out)")
    fig, ax = plt.subplots(figsize=(7.5, 3.2))
    bode(ax, f, y)
    ax.axhline(-3, color=MUTED, ls=":", lw=1)
    ax.axvline(m["fc"], color=ORANGE, ls="--", lw=1)
    ax.text(m["fc"] * 1.15, -30, f"-3 dB at {m['fc']:.1f} Hz", color=ORANGE, fontsize=9)
    clean(ax, "Frequency response")
    return sch, sc, save(fig, "01_rc"), [
        f"Cutoff (-3 dB) {m['fc']:.1f} Hz for a 1 kHz spec",
        f"{m['atten10_db']:.1f} dB one decade above cutoff (20 dB/decade)"]


def c_sk():
    d, net = template("sallen_key_lowpass", {"fc": 10000})
    sch, sc = draw(d.netlist, "02_sk", d.start)
    f, (y,), m = wave(first_deck(net), "vdb(out)")
    fig, ax = plt.subplots(figsize=(7.5, 3.2))
    bode(ax, f, y)
    ax.axhline(-3, color=MUTED, ls=":", lw=1)
    ax.axvline(m["f3db"], color=ORANGE, ls="--", lw=1)
    ax.text(m["f3db"] * 1.3, -12, f"-3 dB at {m['f3db']:.0f} Hz", color=ORANGE, fontsize=9)
    clean(ax, "Frequency response")
    return sch, sc, save(fig, "02_sk"), [
        f"-3 dB at {m['f3db']:.0f} Hz for a 10 kHz spec, flat passband (peak {m['peak_db']:.3f} dB)",
        f"{m['atten10_db']:.1f} dB one decade above cutoff (40 dB/decade)"]


def c_noninv():
    d, net = template("noninverting_amp", {"gain": 10, "bw": 100e3})
    sch, sc = draw(d.netlist, "03_noninv", d.start)
    dk = sim.decks(net)
    f, (y,), m = wave(dk[0], "vdb(out)")
    m2 = wave(dk[1], "vdb(fbd)")[2] if len(dk) > 1 else {}
    fig, ax = plt.subplots(figsize=(7.5, 3.2))
    bode(ax, f, y)
    ax.axvline(m["bw"], color=ORANGE, ls="--", lw=1)
    ax.text(m["bw"] * 0.8, -20, f"bandwidth {eng(m['bw'], 'Hz')}", color=ORANGE, fontsize=9,
            ha="right")
    clean(ax, "Frequency response (gain 10)")
    r = [f"Gain {m['gain_db']:.2f} dB (x{10 ** (m['gain_db'] / 20):.2f}) for a x10 spec",
         f"Bandwidth {eng(m['bw'], 'Hz')} (spec: at least 100 kHz)"]
    if "pm_deg" in m2:
        r.append(f"Phase margin {m2['pm_deg']:.0f}° (stable)")
    return sch, sc, save(fig, "03_noninv"), r


def c_ldo():
    d, net = template("ldo", {"vin": 5, "vout": 3.3, "iload": 0.1})
    sch, sc = draw(d.netlist, "04_ldo", d.start)
    dk = sim.decks(net)
    vin, (vo,), m = wave(dk[0], "v(out)")
    m2 = wave(dk[1], "vdb(fbd)")[2]
    t, (vt,), m3 = wave(dk[2], "v(out)")
    fig, (a, b) = plt.subplots(1, 2, figsize=(9, 3.2))
    a.plot(vin, vo, color=BLUE, lw=2)
    a.axhspan(3.3 * 0.99, 3.3 * 1.01, color=BAND, alpha=0.6, lw=0)
    a.set_xlabel("input voltage (V)")
    a.set_ylabel("output voltage (V)")
    clean(a, "Output vs input")
    b.plot([x * 1e6 for x in t], vt, color=BLUE, lw=1.6)
    b.set_xlabel("time (µs)")
    b.set_ylabel("output voltage (V)")
    clean(b, "Load step 10 mA → 100 mA → 10 mA")
    return sch, sc, save(fig, "04_ldo"), [
        f"{m['vout']:.3f} V out from 5 V in, at 0.1 A",
        f"Still {m['vout_low']:.3f} V at 3.6 V in (low dropout)",
        f"Phase margin {m2['pm_deg']:.0f}°; load step dip {m3['step_mv']:.0f} mV"]


def c_buck():
    d, net = template("buck", {"vin": 12, "vout": 5, "iout": 1})
    sch, sc = draw(d.netlist, "05_buck", d.start)
    t, (v,), m = wave(first_deck(net), "v(out)")
    fig, (a, b) = plt.subplots(1, 2, figsize=(9, 3.2))
    a.plot([x * 1e3 for x in t], v, color=BLUE, lw=1)
    a.set_xlabel("time (ms)")
    a.set_ylabel("output voltage (V)")
    clean(a, "Start-up")
    end = t[-1]
    tt = [(x, y) for x, y in zip(t, v) if x > end - 10e-6]
    b.plot([(x - end) * 1e6 + 10 for x, _ in tt], [y for _, y in tt], color=BLUE, lw=1.6)
    b.axhspan(4.9, 5.1, color=BAND, alpha=0.5, lw=0)
    b.set_xlabel("time (µs), last 10 µs")
    b.set_ylabel("output voltage (V)")
    clean(b, "Ripple, settled")
    return sch, sc, save(fig, "05_buck"), [
        f"{m['vout']:.3f} V out from 12 V in, at 1 A, 500 kHz switching",
        f"Ripple {m['ripple_mv']:.1f} mV (limit 50 mV)",
        f"Efficiency {m['eff'] * 100:.1f}%"]


INA_PARTS = {"R1A": 9760, "R1B": 10200, "RG": 205, "R2A": 9760, "R2B": 9760,
             "R3A": 10000, "R3B": 10000}


def ina_stage(s, ip, inn):
    p = INA_PARTS
    return (f"X1{s} {ip} n1{s} vcc vee o1{s} SPMCP_OA\nX2{s} {inn} n2{s} vcc vee o2{s} SPMCP_OA\n"
            f"R1a{s} o1{s} n1{s} {p['R1A']}\nR1b{s} o2{s} n2{s} {p['R1B']}\n"
            f"Rg{s} n1{s} n2{s} {p['RG']}\nR2a{s} o1{s} a{s} {p['R2A']}\n"
            f"R3a{s} a{s} out{s} {p['R3A']}\nR2b{s} o2{s} b{s} {p['R2B']}\n"
            f"R3b{s} b{s} 0 {p['R3B']}\nX3{s} b{s} a{s} vcc vee out{s} SPMCP_OA\n"
            f"RL{s} out{s} 0 10k\nCL{s} out{s} 0 100p\n")


def c_ina():
    oa = opamp.subckt(10e6, 10)
    drawn = RAILS + "Vp inp 0 AC 0.5\nVn inn 0 AC 0.5 180\n" + ina_stage("", "inp", "inn") + oa
    sch, sc = draw(drawn, "06_ina")
    deck = (RAILS + "Vp inp 0 AC 0.5\nVn inn 0 AC 0.5 180\nVc cm 0 AC 1\n"
            + ina_stage("", "inp", "inn") + ina_stage("c", "cm", "cm") + oa
            + ".ac dec 50 10 10Meg\n.meas AC gain_db FIND mag(V(out)) AT 100\n"
            ".meas AC bw WHEN mag(V(out))=70.71 FALL=1\n"
            ".meas AC cm_db FIND mag(V(outc)) AT 60\n.end\n")
    f, (g, c), m = wave(deck, "vdb(out)", "vdb(outc)")
    fig, ax = plt.subplots(figsize=(7.5, 3.2))
    ax.semilogx(f, g, color=BLUE, lw=2, label="difference signal (wanted)")
    ax.semilogx(f, c, color=ORANGE, lw=2, label="common signal, e.g. mains hum (unwanted)")
    ax.set_xlabel("frequency (Hz)")
    ax.set_ylabel("gain (dB)")
    ax.legend(frameon=False, fontsize=8.5, loc="center left")
    clean(ax, "Wanted vs unwanted signal")
    return sch, sc, save(fig, "06_ina"), [
        f"Gain {m['gain_db']:.2f} dB (x{10 ** (m['gain_db'] / 20):.0f}) for a x100 spec",
        f"Bandwidth {eng(m['bw'], 'Hz')} (spec: at least 20 kHz)",
        f"Hum rejection (CMRR) at 60 Hz: {m['gain_db'] - m['cm_db']:.0f} dB (spec: 80 dB)",
        "Finished with standard 1% values; 0.1% resistors give 100% of boards in spec"]


def ac_case(name, net, ac, node, title, results, extra=None, ylabel="gain (dB)", mag=False):
    sch, sc = draw(net, name)
    expr = f"mag({node})" if mag else f"vdb({node})"
    f, (y,), m = wave(net + (extra or "") + ac + "\n.end\n", expr)
    fig, ax = plt.subplots(figsize=(7.5, 3.2))
    if mag:
        ax.semilogx(f, [v * 1000 for v in y], color=BLUE, lw=2)
        ax.set_xlabel("frequency (Hz)")
        ax.set_ylabel(ylabel)
        clean(ax)
    else:
        bode(ax, f, y, ylabel)
    clean(ax, title)
    return sch, sc, save(fig, name), results(f, y, m)


def c_inverting():
    net = "V1 in 0 AC 1\nR1 in inn 1k\nR2 inn out 10k\nX1 0 inn vcc vee out SPMCP_OA\nRL out 0 2k\n" + RAILS + OA

    def res(f, y, m):
        g = at(f, y, 100)
        bw = next(f[i] for i in range(len(y)) if f[i] > 100 and y[i] < g - 3)
        return [f"Gain {g:.2f} dB (x−{10 ** (g / 20):.2f}), set by R2/R1 = 10",
                f"Bandwidth {eng(bw, 'Hz')} (op-amp sized for 2 MHz gain-bandwidth)"]
    return ac_case("07_inverting", net, ".ac dec 50 10 100Meg", "out", "Frequency response", res)


def c_cs():
    net = ("Vdd vdd 0 5\nVin g 0 DC 1.5 AC 1\nRd vdd d 2k\nM1 d g s s NM\nRs s 0 200\n"
           "Cs s 0 10u\nRL d 0 10k\n.model NM NMOS(KP=1m VTO=1)\n")
    def res(f, y, m):
        g = at(f, y, 100e3)
        return [f"Mid-band gain {g:.1f} dB (x{10 ** (g / 20):.2f}) from one MOSFET biased at 0.11 mA",
                f"Below a few Hz the source resistor cuts the gain to {at(f, y, 1):.1f} dB; "
                "the 10 µF bypass capacitor removes that loss above it"]
    return ac_case("08_cs_amp", net, ".ac dec 50 0.01 100Meg", "d", "Frequency response", res)


def c_bridge():
    net = "V1 a 0 10\nR1 a b 1k\nR2 a c 1k\nR3 b 0 1k\nR4 c 0 1.1k\nR5 b c 10k\n"
    sch, sc = draw(net, "09_bridge")
    x, (y,), m = wave(net + ".dc V1 0 10 0.1\n.end\n", "v(c)-v(b)")
    fig, ax = plt.subplots(figsize=(7.5, 3.2))
    ax.plot(x, [v * 1000 for v in y], color=BLUE, lw=2)
    ax.set_xlabel("bridge supply (V)")
    ax.set_ylabel("output, V(c) - V(b) (mV)")
    clean(ax, "Bridge output vs supply (R4 is 10% high)")
    out10 = y[-1] * 1000
    return sch, sc, save(fig, "09_bridge"), [
        f"A 10% change in one arm gives {out10:.0f} mV across the bridge at 10 V",
        "Output is zero when all four arms match, and grows linearly with supply"]


def c_boost():
    net = ("Vin in 0 5\nL1 in sw 10u\nM1 sw g 0 NSW\nVg g 0 PULSE(0 5 0 1n 1n 1u 2u)\n"
           "D1 sw out DS\nC1 out 0 10u\nRL out 0 50\n"
           ".model NSW VDMOS(Vto=2 Kp=20 Rd=0.05)\n.model DS D\n")
    sch, sc = draw(net, "10_boost")
    t, (v,), m = wave(net + ".tran 10n 1m 0 10n\n.meas TRAN vout AVG V(out) FROM 0.8m TO 1m\n"
                      ".end\n", "v(out)")
    fig, ax = plt.subplots(figsize=(7.5, 3.2))
    ax.plot([x * 1e3 for x in t], v, color=BLUE, lw=1)
    ax.axhline(5, color=MUTED, ls=":", lw=1)
    ax.text(0.02, 5.4, "input 5 V", color=MUTED, fontsize=8.5)
    ax.set_xlabel("time (ms)")
    ax.set_ylabel("output voltage (V)")
    clean(ax, "Start-up, 500 kHz, 50% duty")
    return sch, sc, save(fig, "10_boost"), [
        f"Steps 5 V up to {m['vout']:.2f} V (ideal at 50% duty: 10 V) into 50 Ω",
        "Open loop: the output depends on the input; shows the drawer handles switchers"]


def c_two_stage():
    net = ("V1 in 0 AC 1\nX1 in fb vcc vee a SPMCP_OA\nR1 a b 1k\nC1 b 0 1n\n"
           "X2 b out vcc vee out SPMCP_OA\nR2 out fb 9k\nR3 fb 0 1k\n" + RAILS + OA)
    return ac_case("11_two_stage", net, ".ac dec 50 10 100Meg",
                   "out", "Frequency response",
                   lambda f, y, m: [f"Gain {at(f, y, 100):.2f} dB (x{10 ** (at(f, y, 100) / 20):.2f}), "
                                    "set by 1 + R2/R3 = 10",
                                    "Feedback wraps both op-amps and the RC between them"])


def c_pi():
    net = "V1 in 0 AC 1\nRs in a 50\nC1 a 0 1n\nL1 a b 1u\nC2 b 0 1n\nRL b out 1\nR9 out 0 50\n"
    def res(f, y, m):
        return [f"Passband {at(f, y, 1e3):.1f} dB (the 50 Ω source and load halve the voltage)",
                f"{at(f, y, 100e6):.0f} dB at 100 MHz: steep 3rd-order roll-off"]
    return ac_case("12_pi_filter", net, ".ac dec 50 1k 1G", "out", "Frequency response", res)


def c_ladder():
    net = "V1 in 0 AC 1\n" + "".join(
        f"R{i} {'in' if i == 0 else f'n{i}'} n{i + 1} 1k\nC{i} n{i + 1} 0 1n\n" for i in range(5))
    def res(f, y, m):
        i3 = next(i for i in range(len(y)) if y[i] < -3)
        return [f"-3 dB at {eng(f[i3], 'Hz')} (each section loads the one before it)",
                f"{at(f, y, 10e6):.0f} dB at 10 MHz: 100 dB/decade, five times one RC section"]
    return ac_case("13_rc_ladder", net, ".ac dec 50 100 100Meg", "n5", "Frequency response", res)


def c_tank():
    net = "I1 0 t AC 1m\nL1 t 0 1u\nC1 t 0 1n\nR1 t 0 1k\n"
    def res(f, y, m):
        i = max(range(len(y)), key=lambda k: y[k])
        return [f"Resonates at {eng(f[i], 'Hz')} (1/2π√LC = 5.03 MHz)",
                f"Peak impedance {y[i] * 1000:.0f} Ω, set by the 1 kΩ loss resistor"]
    return ac_case("14_lc_tank", net, ".ac dec 200 100k 100Meg", "v(t)",
                   "Impedance vs frequency", res, ylabel="impedance (Ω)", mag=True)


# ------------------------------------------------------- transistor-level blocks (hand drawn)
class Sch:
    """A tiny schematic canvas: MOSFETs, resistors, capacitors, wires, dots, labels."""

    def __init__(self, w, h):
        self.fig, self.ax = plt.subplots(figsize=(w, h))
        self.ax.set_aspect("equal")
        self.ax.axis("off")

    def w(self, *pts):
        xs, ys = zip(*pts)
        self.ax.plot(xs, ys, color=INK, lw=1.5, solid_capstyle="round")

    def dot(self, x, y):
        self.ax.plot([x], [y], "o", color=INK, ms=4.5)

    def t(self, x, y, s, **k):
        k.setdefault("fontsize", 9)
        self.ax.text(x, y, s, color=k.pop("color", INK), **k)

    def mos(self, cx, cy, kind, gate="left", name=""):
        """Vertical MOSFET. Returns top, bottom and gate-lead points. P: source on top."""
        sgn = -1 if gate == "left" else 1
        gx, tx, lx = cx + sgn * 0.18, cx - sgn * 0.5, cx + sgn * 0.75
        self.w((cx, cy - 0.5), (cx, cy + 0.5))                        # channel
        self.w((gx, cy - 0.38), (gx, cy + 0.38))                      # gate plate
        if kind == "p":
            self.ax.add_patch(plt.Circle((gx + sgn * 0.11, cy), 0.11, fill=False, color=INK,
                                         lw=1.3))
            self.w((gx + sgn * 0.22, cy), (lx, cy))
        else:
            self.w((gx, cy), (lx, cy))
        self.w((cx, cy + 0.38), (tx, cy + 0.38), (tx, cy + 1.2))
        self.w((cx, cy - 0.38), (tx, cy - 0.38), (tx, cy - 1.2))
        ay = cy - 0.38 if kind == "n" else cy + 0.38                  # source arrow
        self.ax.annotate("", (cx if kind == "n" else tx, ay), (tx if kind == "n" else cx, ay),
                         arrowprops={"arrowstyle": "-|>", "color": INK, "lw": 1.2,
                                     "shrinkA": 0, "shrinkB": 0, "mutation_scale": 8})
        self.t(tx + (-sgn) * 0.12, cy + 0.6, name, fontsize=9, fontweight="bold",
               ha="left" if sgn < 0 else "right")
        return {"top": (tx, cy + 1.2), "bot": (tx, cy - 1.2), "gate": (lx, cy)}

    def res(self, x, y0, y1, name):
        """Vertical resistor between y0 (bottom) and y1."""
        m, h = (y0 + y1) / 2, 0.5
        zig = [(x, m - h)] + [(x + (0.15 if i % 2 else -0.15), m - h + (i + 0.5) * 2 * h / 6)
                              for i in range(6)] + [(x, m + h)]
        self.w((x, y0), (x, m - h))
        self.w(*zig)
        self.w((x, m + h), (x, y1))
        self.t(x + 0.25, m, name, va="center")

    def hres(self, x0, x1, y, name):
        m, h = (x0 + x1) / 2, 0.45
        zig = [(m - h, y)] + [(m - h + (i + 0.5) * 2 * h / 6, y + (0.15 if i % 2 else -0.15))
                              for i in range(6)] + [(m + h, y)]
        self.w((x0, y), (m - h, y))
        self.w(*zig)
        self.w((m + h, y), (x1, y))
        self.t(m, y - 0.45, name, ha="center", va="top", fontsize=8.5)

    def hcap(self, x0, x1, y, name):
        m = (x0 + x1) / 2
        self.w((x0, y), (m - 0.08, y))
        self.w((m + 0.08, y), (x1, y))
        self.w((m - 0.08, y - 0.3), (m - 0.08, y + 0.3))
        self.w((m + 0.08, y - 0.3), (m + 0.08, y + 0.3))
        self.t(m, y - 0.45, name, ha="center", va="top", fontsize=8.5)

    def save(self, path):
        self.fig.tight_layout()
        self.fig.savefig(path, dpi=170, facecolor="white")
        plt.close(self.fig)


def _opamp_core(s: Sch, cc: str):
    """The 8-MOSFET two-stage op-amp. Rails V+ at y 12, V- at y 1.2. Returns the out node."""
    VP, VM = 12.0, 1.2
    m8 = s.mos(1.0, 10.6, "p", "left", "M8")
    m5 = s.mos(4.5, 10.6, "p", "left", "M5")
    m7 = s.mos(10.5, 10.6, "p", "left", "M7")
    for m in (m8, m5, m7):
        s.w(m["top"], (m["top"][0], VP))
    # bias line pb: M8 gate tied to its drain, feeds M5 and M7 gates
    s.w(m8["gate"], (m8["gate"][0], 9.0), (m7["gate"][0], 9.0))
    s.w(m5["gate"], (m5["gate"][0], 9.0))
    s.w(m7["gate"], (m7["gate"][0], 9.0))
    s.w(m8["bot"], (1.5, 9.0))
    for x in (1.5, m5["gate"][0]):
        s.dot(x, 9.0)
    s.res(1.5, VM, 9.0 - 0.0, "Rb")
    s.t(1.65, 9.15, "pb", fontsize=7.5, color=MUTED)
    # input pair and tail
    m1 = s.mos(3.0, 7.4, "p", "left", "M1")
    m2 = s.mos(7.0, 7.4, "p", "right", "M2")
    s.w(m5["bot"], (5.0, 8.6))
    s.w((m1["top"][0], 8.6), (m2["top"][0], 8.6))
    s.dot(5.0, 8.6)
    s.t(m1["gate"][0] - 0.1, 7.4, "in−", ha="right", va="center", fontsize=10, color=BLUE)
    s.t(m2["gate"][0] + 0.1, 7.4, "in+", ha="left", va="center", fontsize=10, color=BLUE)
    # mirror load
    m3 = s.mos(4.0, 3.6, "n", "right", "M3")
    m4 = s.mos(6.0, 3.6, "n", "left", "M4")
    s.w(m1["bot"], m3["top"])
    s.w(m2["bot"], m4["top"])
    s.w(m3["gate"], m4["gate"])
    s.w((5.0, 3.6), (5.0, 5.4), (3.5, 5.4))
    s.dot(5.0, 3.6)
    s.dot(3.5, 5.4)
    for m in (m3, m4):
        s.w(m["bot"], (m["bot"][0], VM))
    # second stage
    m6 = s.mos(10.5, 3.6, "n", "left", "M6")
    s.w(m6["bot"], (11.0, VM))
    s.w((6.5, 5.4), (9.0, 5.4), (9.0, 3.6), m6["gate"])
    s.dot(6.5, 5.4)
    s.w(m7["bot"], m6["top"])
    # compensation
    s.w((8.0, 5.4), (8.0, 6.6))
    s.dot(8.0, 5.4)
    s.hcap(8.0, 9.2, 6.6, "Cc " + cc)
    s.hres(9.2, 11.0, 6.6, "Rz")
    s.dot(11.0, 6.6)
    s.dot(11.0, 7.6)
    return VP, VM, (11.0, 7.6)


def draw_opamp_blocks():
    s = Sch(7.5, 6.2)
    VP, VM, out = _opamp_core(s, "4 pF")
    s.w(out, (12.5, 7.6))
    s.t(12.6, 7.6, "out", va="center", fontsize=10, color=BLUE)
    s.w((0.2, VP), (12.5, VP))
    s.w((0.2, VM), (12.5, VM))
    s.t(0.2, VP + 0.25, "V+", fontsize=10)
    s.t(0.2, VM - 0.6, "V−", fontsize=10)
    s.t(6.2, 12.6, "first stage: differential pair M1/M2, mirror M3/M4, tail M5", ha="center",
        fontsize=8, color=MUTED)
    s.t(11.3, 9.2, "second stage\nM6, M7", ha="left", fontsize=8, color=MUTED)
    s.save(FIG / "15_opamp_schematic.png")

    s = Sch(9.5, 6.2)
    VP, VM, out = _opamp_core(s, "0.4 pF")
    node = out
    for i, (pn, nn, lab) in enumerate((("M9", "M10", "small inverter"),
                                       ("M11", "M12", "large inverter"))):
        cx = 13.0 + i * 2.6
        mp = s.mos(cx, 10.6, "p", "left", pn)
        mn = s.mos(cx, 3.6, "n", "left", nn)
        s.w(mp["top"], (mp["top"][0], VP))
        s.w(mn["bot"], (mn["bot"][0], VM))
        s.w(mp["bot"], mn["top"])
        gx = mp["gate"][0]
        s.w(mp["gate"], (gx, mn["gate"][1]), mn["gate"])
        s.w(node, (gx, 7.6))
        s.dot(gx, 7.6)
        node = (cx + 0.5, 7.6)
        s.dot(*node)
        s.t(cx, 12.6, lab, ha="center", fontsize=8, color=MUTED)
    s.w(node, (node[0] + 1.2, 7.6))
    s.t(node[0] + 1.3, 7.6, "gate\ndrive\nout", va="center", fontsize=9.5, color=BLUE)
    s.w((0.2, VP), (node[0] + 1.2, VP))
    s.w((0.2, VM), (node[0] + 1.2, VM))
    s.t(0.2, VP + 0.25, "V+", fontsize=10)
    s.t(0.2, VM - 0.6, "V−", fontsize=10)
    s.t(6.2, 12.6, "same two stages as the op-amp, compensated fast", ha="center", fontsize=8,
        color=MUTED)
    s.save(FIG / "16_comparator_schematic.png")


def c_opamp():
    deck = (opamp.subckt(10e6, 10) + "Vdd vdd 0 5\nVss vss 0 -5\nVp inp 0 AC 1\n"
            "X1 inp inn vdd vss out SPMCP_OA\nLb out inn 1G\nCb inn 0 1G\nRL out 0 10k\n"
            ".ac dec 50 0.1 1G\n.meas AC ugf WHEN mag(V(out))=1 FALL=1\n.end\n")
    f, (g,), m = wave(deck, "vdb(out)")
    m["a0"] = 10 ** (at(f, g, 1) / 20)
    fig, ax = plt.subplots(figsize=(7.5, 3.2))
    bode(ax, f, g, "open-loop gain (dB)")
    ax.axhline(0, color=MUTED, ls=":", lw=1)
    ax.axvline(m["ugf"], color=ORANGE, ls="--", lw=1)
    ax.text(m["ugf"] * 1.2, 40, f"unity gain\n{eng(m['ugf'], 'Hz')}", color=ORANGE, fontsize=9)
    clean(ax, "Open-loop gain (10 MHz design, ±5 V)")
    return FIG / "15_opamp_schematic.png", None, save(fig, "15_opamp"), [
        f"Open-loop gain {20 * math.log10(m['a0']):.1f} dB",
        f"Unity-gain frequency {eng(m['ugf'], 'Hz')} for a 10 MHz spec",
        "Sized from a target gain-bandwidth; used inside circuits 2, 3, 4, 5, 6, 7, 11"]


def c_comparator():
    cmp = opamp.comparator_subckt()
    deck = (cmp + "Vdd vdd 0 12\nVp inp 0 SIN(6 1 20k)\nVn inn 0 PULSE(5 7 0 24.99u 10n 10n 25u)\n"
            "X1 inp inn vdd 0 out SPMCP_CMP\nCL out 0 1n\n.tran 10n 100u\n.end\n")
    t, (a, b, o), m = wave(deck, "v(inp)", "v(inn)", "v(out)")
    fig, ax = plt.subplots(figsize=(7.5, 3.2))
    tu = [x * 1e6 for x in t]
    ax.plot(tu, o, color=BLUE, lw=1.6, label="output (drives 1 nF gate)")
    ax.plot(tu, a, color=ORANGE, lw=1.2, label="in+ (signal)")
    ax.plot(tu, b, color=TEAL, lw=1.2, label="in− (ramp)")
    ax.set_xlabel("time (µs)")
    ax.set_ylabel("voltage (V)")
    ax.legend(frameon=False, fontsize=8.5, loc="upper right", ncol=3)
    ax.set_ylim(-1, 15)
    clean(ax, "Signal vs ramp → PWM output, 12 V supply")
    return FIG / "16_comparator_schematic.png", None, save(fig, "16_comparator"), [
        "Output swings rail to rail (0 to 12 V) whenever in+ crosses in−",
        "Tapered buffer (small then large inverter) drives a power MOSFET gate directly",
        "This is the PWM block of the closed-loop buck (circuit 5)"]


def c_chua():
    g = ROOT / "gallery" / "chua"
    return g / "chua_circuit_classic.png", None, g / "chua_phase_portraits_2d.png", [
        "Chaotic oscillator: L, two capacitors and a negative-resistance (Chua) diode",
        "Simulated in LTspice; the output never repeats and traces a double scroll",
        "More plots (bifurcation, butterfly effect) in the README gallery"]


CATALOG = [
    (1, "RC low-pass filter", "First-order passive filter: lets low frequencies through, "
     "cuts high ones at 20 dB per decade.", "ready-made circuit", c_rc),
    (2, "Sallen-Key low-pass filter", "Second-order active filter (Butterworth, Q = 0.707) "
     "around the transistor-level op-amp: 40 dB per decade.", "ready-made circuit", c_sk),
    (3, "Non-inverting amplifier", "Amplifies by 1 + R2/R1 (here x10) with a high input "
     "impedance.", "ready-made circuit", c_noninv),
    (4, "LDO voltage regulator", "Linear regulator: 5 V in, 3.3 V out at 0.1 A, using a "
     "PMOS pass transistor and an error amplifier.", "ready-made circuit", c_ldo),
    (5, "Buck DC-DC converter (closed loop)", "Switching step-down converter: 12 V in, 5 V out "
     "at 1 A, with a PWM controller that holds the output.", "ready-made circuit", c_buck),
    (6, "Instrumentation amplifier", "Three op-amps: amplifies the difference between two "
     "sensor wires by 100 and rejects what is common to both (mains hum).",
     "designed from scratch", c_ina),
    (7, "Inverting amplifier", "Amplifies by −R2/R1 (here −10).", "drawing test", c_inverting),
    (8, "Common-source MOSFET amplifier", "Single-transistor voltage amplifier.",
     "drawing test", c_cs),
    (9, "Wheatstone bridge", "Four-resistor bridge that turns a small resistance change "
     "(e.g. a strain gauge) into a voltage.", "drawing test", c_bridge),
    (10, "Boost converter", "Switching step-up converter: 5 V in, about 10 V out.",
     "drawing test", c_boost),
    (11, "Two-stage amplifier with feedback", "Two op-amps with an RC filter between them, "
     "one feedback loop around both, gain 10.", "drawing test", c_two_stage),
    (12, "Pi filter", "C-L-C low-pass, as used on supply and RF lines.", "drawing test", c_pi),
    (13, "5-stage RC ladder", "Five RC sections in a row: a steep passive low-pass.",
     "drawing test", c_ladder),
    (14, "LC tank", "Inductor and capacitor in parallel: resonates at one frequency.",
     "drawing test", c_tank),
    (15, "Two-stage CMOS op-amp", "The 8-MOSFET op-amp inside circuits 2, 3, 4, 6, 7 and 11: "
     "differential pair, current mirror, gain stage, Miller compensation.",
     "building block", c_opamp),
    (16, "Comparator with gate drive", "The op-amp's two stages, fast, then two CMOS inverters "
     "(12 MOSFETs) that can switch a power MOSFET.", "building block", c_comparator),
    (17, "Chua's chaotic circuit", "The simplest electronic circuit that behaves chaotically.",
     "demo", c_chua),
]


def make_figures() -> list[dict]:
    FIG.mkdir(exist_ok=True)
    draw_opamp_blocks()
    rows = []
    for no, title, what, kind, fn in CATALOG:
        sch, score, plot, result = fn()
        rows.append({"no": no, "title": title, "what": what, "kind": kind,
                     "schematic": str(Path(sch).relative_to(ROOT)), "readability": score,
                     "plot": str(Path(plot).relative_to(ROOT)), "result": result})
        print(no, title, score, result)
    (HERE / "results.json").write_text(json.dumps(rows, indent=1, ensure_ascii=False))
    return rows


# --------------------------------------------------------------------------- PDF
def build_pdf(rows: list[dict]) -> None:
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.lib.utils import ImageReader
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    from reportlab.platypus import (Image, KeepTogether, PageBreak, Paragraph,
                                    SimpleDocTemplate, Spacer, Table, TableStyle)

    font = Path("/usr/share/fonts/truetype/dejavu/")
    pdfmetrics.registerFont(TTFont("Sans", str(font / "DejaVuSans.ttf")))
    pdfmetrics.registerFont(TTFont("Sans-Bold", str(font / "DejaVuSans-Bold.ttf")))
    mpl_fonts = Path(matplotlib.__file__).parent / "mpl-data" / "fonts" / "ttf"
    pdfmetrics.registerFont(TTFont("Sans-Oblique", str(mpl_fonts / "DejaVuSans-Oblique.ttf")))
    pdfmetrics.registerFontFamily("Sans", normal="Sans", bold="Sans-Bold", italic="Sans-Oblique")
    C = colors.HexColor
    S = {
        "title": ParagraphStyle("t", fontName="Sans-Bold", fontSize=24, leading=29, textColor=C(INK)),
        "sub": ParagraphStyle("s", fontName="Sans", fontSize=12, leading=17, textColor=C(MUTED)),
        "h1": ParagraphStyle("h1", fontName="Sans-Bold", fontSize=16, leading=21, textColor=C(BLUE)),
        "kick": ParagraphStyle("k", fontName="Sans-Bold", fontSize=8.5, leading=11, textColor=C(MUTED)),
        "body": ParagraphStyle("b", fontName="Sans", fontSize=9.6, leading=13.5, textColor=C(INK)),
        "bul": ParagraphStyle("bl", fontName="Sans", fontSize=9.6, leading=13.4, textColor=C(INK),
                              leftIndent=11, bulletIndent=0, spaceAfter=1.5),
        "cap": ParagraphStyle("c", fontName="Sans-Oblique", fontSize=8, leading=10.5,
                              textColor=C(MUTED)),
        "cell": ParagraphStyle("cell", fontName="Sans", fontSize=8.4, leading=10.6, textColor=C(INK)),
        "cellb": ParagraphStyle("cb", fontName="Sans-Bold", fontSize=8.4, leading=10.6,
                                textColor=C(INK)),
    }
    W = A4[0] - 36 * mm

    def img(path, max_h):
        p = str(ROOT / path)
        iw, ih = ImageReader(p).getSize()
        s = min(W / iw, max_h / ih)
        return Image(p, iw * s, ih * s)

    st = [Spacer(1, 8 * mm), Paragraph("SpiceMCP circuit catalog", S["title"]), Spacer(1, 4),
          Paragraph(f"All {len(rows)} circuits built so far, in serial order: schematic, "
                    "simulated response and key result.", S["sub"]), Spacer(1, 10 * mm)]
    data = [[Paragraph(h, S["cellb"]) for h in ("No.", "Circuit", "Type", "Key result")]]
    for r in rows:
        data.append([Paragraph(str(r["no"]), S["cell"]), Paragraph(r["title"], S["cellb"]),
                     Paragraph(r["kind"], S["cell"]), Paragraph(r["result"][0], S["cell"])])
    t = Table(data, colWidths=[11 * mm, 52 * mm, 30 * mm, W - 93 * mm], repeatRows=1)
    t.setStyle(TableStyle([("LINEBELOW", (0, 0), (-1, 0), 0.8, C(INK)),
                           ("LINEBELOW", (0, 1), (-1, -1), 0.3, C(GRID)),
                           ("VALIGN", (0, 0), (-1, -1), "TOP"),
                           ("TOPPADDING", (0, 0), (-1, -1), 3),
                           ("BOTTOMPADDING", (0, 0), (-1, -1), 3)]))
    st += [t, Spacer(1, 6 * mm)]
    st += [Paragraph("How to read the pages", S["kick"]), Spacer(1, 2)]
    for b in ["<b>Ready-made circuits (1–5)</b>: you give a spec, SpiceMCP sizes the parts.",
              "<b>Designed from scratch (6)</b>: built with the general tools, not the library.",
              "<b>Drawing tests (7–14)</b>: circuits the schematic drawer was never tuned on.",
              "<b>Building blocks (15–16)</b>: the transistor-level parts inside the others.",
              "Schematics 1–14 were drawn by SpiceMCP from the simulated netlist and read back "
              "by LTspice 26 as the same circuit. Plots are ngspice simulations of that netlist."]:
        st.append(Paragraph(b, S["bul"], bulletText="•"))
    st.append(PageBreak())

    for r in rows:
        head = [Paragraph(f"CIRCUIT {r['no']} OF {len(rows)} · {r['kind'].upper()}", S["kick"]),
                Paragraph(f"{r['no']}. {r['title']}", S["h1"]), Spacer(1, 2),
                Paragraph(r["what"], S["body"]), Spacer(1, 4 * mm)]
        cap = ("Schematic drawn by SpiceMCP" + (f" · readability {r['readability']}/100"
                                                if r["readability"] is not None else "")
               if r["no"] <= 14 else
               "Transistor-level schematic" if r["no"] <= 16 else "Schematic (gallery)")
        st += head + [img(r["schematic"], 105 * mm), Paragraph(cap, S["cap"]), Spacer(1, 4 * mm),
                      img(r["plot"], 70 * mm),
                      Paragraph("Simulated response" if r["no"] < 17 else
                                "Simulated phase portraits: the double scroll", S["cap"]),
                      Spacer(1, 4 * mm), Paragraph("KEY RESULT", S["kick"])]
        st += [Paragraph(x, S["bul"], bulletText="•") for x in r["result"]]
        st.append(PageBreak())
    st.pop()

    def footer(c, doc):
        c.saveState()
        c.setFont("Sans", 7.5)
        c.setFillColor(C(MUTED))
        c.drawString(18 * mm, 10 * mm, "SpiceMCP circuit catalog")
        c.drawRightString(A4[0] - 18 * mm, 10 * mm, f"page {doc.page}")
        c.restoreState()

    doc = SimpleDocTemplate(str(OUT), pagesize=A4, leftMargin=18 * mm, rightMargin=18 * mm,
                            topMargin=15 * mm, bottomMargin=16 * mm,
                            title="SpiceMCP circuit catalog", author="Shounak Das")
    doc.build(st, onFirstPage=footer, onLaterPages=footer)


if __name__ == "__main__":
    if "--pdf-only" in sys.argv:
        build_pdf(json.loads((HERE / "results.json").read_text()))
    else:
        build_pdf(make_figures())
    print("wrote", OUT)
