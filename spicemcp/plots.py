"""Waveform plots as files, never as tokens.

**Token guard.** Nothing in this module returns a waveform, an array of samples or a
base64 image. A plot is written to disk and the caller gets back a path plus scalars.
A single `.tran` run here is 5018 points; inlined into a tool reply it costs ~30k
tokens and the model still cannot see the shape. `_guard()` enforces this mechanically
rather than by convention -- any list longer than `MAX_LIST` is replaced by a note, so
a future metric that starts returning its input cannot quietly turn an optimization
loop into a context fire.

matplotlib is imported lazily and forced onto the `Agg` backend. It is an optional
extra (`pip install 'spicemcp[plots]'`): the server must keep importing on a machine
that has no matplotlib, and `raw.py` does every metric with stdlib `struct` and `math`
for exactly that reason. Agg matters too -- the default backend on Windows tries to
open a window, which in a stdio MCP server means a hang with no error.

**One palette.** Traces come from `render.SERIES` and the chrome (background, ink,
typeface, grid) from `render.STYLES[STYLE]`, so a Bode plot, a step response and a
schematic in the same report are one document instead of three tools' defaults. The
colours are named by what they *mean* below; a figure that maps "the trace" and "the
limit it is judged against" to two arbitrary hexes has to be re-learned every time.
"""

from __future__ import annotations

import math
import os
from pathlib import Path

from . import raw, render

MAX_LIST = 32              # a returned list longer than this is a waveform, not a metric
FIGSIZE = (9.0, 6.0)
DPI = 110

# The schematic style whose background, ink and typeface the plots borrow. Same default
# as `render`, so nothing has to be passed through to keep a report consistent.
STYLE = render.DEFAULT_STYLE

# Five roles for the five palette entries, in ramp order. One colour, one meaning --
# reused across every figure rather than chosen per figure:
#   C_TRACE   the primary trace
#   C_TRACE2  a second trace on the same figure (phase), and histogram bars
#   C_MARK    a measured point the plot calls out (-3 dB, UGF, peak, settling, mean)
#   C_LIMIT   a limit the design is judged against (spec bounds, +-3 sigma, harmonics)
#   C_BAND    margin brackets and tolerance bands
C_TRACE, C_TRACE2, C_MARK, C_LIMIT, C_BAND = render.SERIES


def _chrome() -> dict:
    """Background, ink, grid and typeface, read off the schematic style.

    Derived rather than duplicated: changing `tech_minimal`'s ink moves the plots with it,
    and there is no second copy of the palette to drift out of step.
    """
    st = render.style(STYLE)
    r = st["roles"]
    return {"bg": st["bg"], "ink": r["wire"]["c"], "faint": r["value"]["c"],
            "grid": (st.get("grid") or {}).get("c") or "#DDDDDD",
            "sans": r["net"]["font"], "mono": r["value"]["font"]}


# A harmonic this far below the fundamental is float noise, not distortion: the 4th of
# a 3-tone fixture measures -187 dBc. Drawing it at all implies a harmonic that is not
# there. -120 dBc is 1 ppm, below any real simulated circuit and above float64 grass.
SILENT = -120.0

# Written out rather than zipped against a prefix string: an off-by-one in that string
# is invisible and mislabels whole decades (1e6 as "1k", 47 as "47m").
SI = {-24: "y", -21: "z", -18: "a", -15: "f", -12: "p", -9: "n", -6: "u", -3: "m",
      0: "", 3: "k", 6: "M", 9: "G", 12: "T", 15: "P", 18: "E", 21: "Z", 24: "Y"}


class PlotError(RuntimeError):
    pass


def _plt():
    """matplotlib on the Agg backend, styled, or a clear error saying how to install it.

    `use()` has to run before `pyplot` is imported, or the backend is already chosen.
    The rcParams go on here rather than in each plot: one place to set them means a new
    figure cannot be added that quietly uses matplotlib's blue-on-white defaults.
    """
    try:
        import matplotlib
    except ImportError as e:                        # pragma: no cover - env dependent
        raise PlotError("matplotlib is not installed; "
                        "pip install 'spicemcp[plots]'") from e
    matplotlib.use("Agg", force=True)
    import matplotlib.pyplot as plt
    c = _chrome()
    plt.rcParams.update({
        "figure.facecolor": c["bg"], "axes.facecolor": c["bg"],
        "savefig.facecolor": c["bg"],
        "font.family": "sans-serif", "font.sans-serif": c["sans"],
        "text.color": c["ink"], "axes.labelcolor": c["ink"],
        "xtick.color": c["ink"], "ytick.color": c["ink"],
        "axes.edgecolor": c["faint"], "axes.linewidth": 0.8,
        "axes.titlecolor": c["ink"],
        "grid.color": c["grid"], "grid.linewidth": 0.7,
        # Bars and traces over the grid, not under it: matplotlib's default draws
        # gridlines on top, which puts pale stripes across every filled bar and reads as
        # a bar that has been cut into segments.
        "axes.axisbelow": True,
        "axes.prop_cycle": plt.cycler(color=list(render.SERIES)),
        "legend.framealpha": 0.92, "legend.edgecolor": c["grid"],
        "legend.facecolor": c["bg"],
    })
    return plt


def plots_dir(project: str | Path | None = None) -> Path:
    """`$SPICEMCP_PROJECT/.ltspice-mcp/plots/`, created on demand."""
    root = Path(project or os.environ.get("SPICEMCP_PROJECT") or os.getcwd())
    d = root / ".ltspice-mcp" / "plots"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _guard(d: dict) -> dict:
    """Strip anything array-shaped out of a returned metric dict. See module docstring."""
    return {k: (f"<{len(v)} values omitted: the plot carries the waveform>"
                if isinstance(v, (list, tuple)) and len(v) > MAX_LIST else v)
            for k, v in d.items()}


def _resolve(source, step: int = 0) -> raw.Wave:
    """Accept a `.raw` path or an already-parsed `Wave`."""
    if isinstance(source, raw.Wave):
        return source
    waves = raw.read_raw(source)
    if step >= len(waves):
        raise PlotError(f"{Path(source).name} has {len(waves)} step(s); asked for {step}")
    return waves[step]


def _out(source, out, kind: str, project) -> Path:
    if out:
        p = Path(out)
        p.parent.mkdir(parents=True, exist_ok=True)
        return p
    stem = Path(source).stem if not isinstance(source, raw.Wave) else "wave"
    return plots_dir(project) / f"{stem}_{kind}.png"


def _save(plt, fig, path: Path) -> str:
    fig.tight_layout()
    fig.savefig(path, dpi=DPI)
    plt.close(fig)                                  # Agg figures leak until closed
    return str(path)


# ------------------------------------------------------------------------------- Bode

def plot_bode(source, out=None, node: str | None = None, step: int = 0,
              title: str | None = None, project=None) -> dict:
    """Magnitude and phase, with the phase margin drawn where it is measured.

    The phase margin annotation is the point of this plot. A number in a table says
    "48 degrees"; the marker shows it is 48 degrees *at the unity-gain frequency*, which
    is what makes it a stability claim rather than arithmetic on a curve.
    """
    plt = _plt()
    w = _resolve(source, step)
    m = raw.ac_metrics(w, node)
    f = w.x
    label, vals = w.signal(node)
    mag = [20 * math.log10(abs(v)) if abs(v) > 0 else -400.0 for v in vals]
    ph = raw._unwrap([math.degrees(_phase(v)) for v in vals])

    fig, (a1, a2) = plt.subplots(2, 1, figsize=FIGSIZE, dpi=DPI, sharex=True,
                                 gridspec_kw={"height_ratios": [3, 2]})
    a1.semilogx(f, mag, lw=1.6, color=C_TRACE)
    a1.set_ylabel(f"|{label}|  (dB)")
    a1.grid(True, which="both", alpha=0.35)
    a2.semilogx(f, ph, lw=1.6, color=C_TRACE2)
    a2.set_ylabel("phase (deg)")
    a2.set_xlabel("frequency (Hz)")
    a2.grid(True, which="both", alpha=0.35)
    ink = _chrome()["faint"]
    a1.axhline(0, color=ink, lw=0.8, ls=":")
    a2.axhline(-180, color=ink, lw=0.8, ls=":")

    bw, ugf, pm = m["bw_3db_hz"], m["ugf_hz"], m["phase_margin_deg"]
    if bw:
        a1.axvline(bw, color=C_MARK, lw=1.0, ls="--")
        a1.annotate(f"-3 dB\n{_eng(bw)}Hz", (bw, m["dc_gain_db"] - 3),
                    textcoords="offset points", xytext=(6, -18), fontsize=8,
                    color=C_MARK)
    if ugf:
        for ax in (a1, a2):
            ax.axvline(ugf, color=C_LIMIT, lw=1.1, ls="--")
        a1.plot([ugf], [0.0], "o", ms=6, color=C_LIMIT)
        a1.annotate(f"UGF {_eng(ugf)}Hz", (ugf, 0.0), textcoords="offset points",
                    xytext=(8, 8), fontsize=8, color=C_LIMIT)
        pha = raw._interp_y(f, ph, ugf, log_x=True)
        a2.plot([ugf], [pha], "o", ms=6, color=C_LIMIT)
        # The bracket from -180 up to the phase at UGF *is* the phase margin.
        a2.annotate("", xy=(ugf, pha), xytext=(ugf, -180),
                    arrowprops=dict(arrowstyle="<->", color=C_TRACE, lw=1.4))
        a2.annotate(f"PM = {pm:.1f}°", (ugf, (pha - 180) / 2),
                    textcoords="offset points", xytext=(10, 0), fontsize=9,
                    color=C_TRACE, fontweight="bold")
    if m["f_phase_180_hz"] and m["gain_margin_db"] is not None:
        f180, gm = m["f_phase_180_hz"], m["gain_margin_db"]
        a1.annotate("", xy=(f180, 0.0), xytext=(f180, -gm),
                    arrowprops=dict(arrowstyle="<->", color=C_BAND, lw=1.2))
        a1.annotate(f"GM = {gm:.1f} dB", (f180, -gm / 2), textcoords="offset points",
                    xytext=(8, 0), fontsize=8, color=C_BAND)
    else:
        a1.annotate("phase never reaches -180°: no gain margin in this span",
                    (0.02, 0.05), xycoords="axes fraction", fontsize=7.5, color=ink)

    head = title or f"{Path(str(source)).stem if not isinstance(source, raw.Wave) else label}"
    if w.step_params:
        head += f"   [{w.step_params}]"
    a1.set_title(head, fontsize=10)
    return {"plot_path": _save(plt, fig, _out(source, out, "bode", project)),
            **_guard(m)}


def _phase(z: complex) -> float:
    import cmath
    return cmath.phase(z)


# -------------------------------------------------------------------------- transient

def plot_transient(source, out=None, node: str | None = None, step: int = 0,
                   settle_pct=(1.0, 0.1), final: float | None = None,
                   title: str | None = None, project=None) -> dict:
    """Step response with the settling bands shaded.

    The bands are why this plot is worth making: a settling time is meaningless without
    the tolerance it settled to, and a shaded band shows immediately whether the trace
    is inside it or still ringing across the edge.
    """
    plt = _plt()
    w = _resolve(source, step)
    m = raw.tran_metrics(w, node, settle_pct, final)
    label, y = w.signal(node)
    t = w.x
    fin = m["final"]
    span = abs(fin - m["initial"]) or 1.0

    fig, ax = plt.subplots(figsize=FIGSIZE, dpi=DPI)
    ax.plot([x * 1e0 for x in t], y, lw=1.5, color=C_TRACE, label=label)
    ax.axhline(fin, color=_chrome()["faint"], lw=0.9, ls=":")
    # The tighter band is drawn in the stronger colour: it is the one the design is
    # actually judged on, and the wider one is context around it.
    for pct, col in zip(sorted(settle_pct, reverse=True), (C_BAND, C_MARK)):
        b = span * pct / 100
        ax.axhspan(fin - b, fin + b, color=col, alpha=0.16, zorder=0,
                   label=f"±{pct:g}% band")
        key = f"t_settle_{raw._tag(pct)}"
        ts = m.get(key)
        if ts:
            ax.axvline(ts, color=col, lw=1.1, ls="--")
            ax.annotate(f"settle {pct:g}%\n{_eng(ts)}s", (ts, fin - b),
                        textcoords="offset points", xytext=(5, -26), fontsize=8,
                        color=col)
        elif key in m:
            ax.annotate(f"never settles to {pct:g}% in this run", (0.5, 0.06),
                        xycoords="axes fraction", fontsize=8, color=C_LIMIT)

    if m.get("overshoot_pct"):
        ax.plot([m["t_max"]], [m["v_max"]], "v", ms=7, color=C_LIMIT)
        ax.annotate(f"overshoot {m['overshoot_pct']:.2f}%", (m["t_max"], m["v_max"]),
                    textcoords="offset points", xytext=(8, 4), fontsize=9,
                    color=C_LIMIT)
    rise = m.get("t_rise_10_90") or m.get("t_fall_10_90")
    if rise:
        # Bottom-left: the peak annotation owns the top-left on a fast-rising edge.
        ax.annotate(f"10-90% = {_eng(rise)}s\nslew+ = {_eng(m['slew_pos_v_per_s'])}V/s",
                    (0.02, 0.04), xycoords="axes fraction", fontsize=8.5,
                    color=_chrome()["ink"], va="bottom",
                    bbox=dict(boxstyle="round", fc=_chrome()["bg"],
                              ec=_chrome()["grid"], alpha=0.92))
    ax.set_xlabel("time (s)")
    ax.set_ylabel(label)
    ax.grid(True, alpha=0.35)
    ax.legend(fontsize=8, loc="lower right")
    head = title or (Path(str(source)).stem if not isinstance(source, raw.Wave) else label)
    if w.step_params:
        head += f"   [{w.step_params}]"
    ax.set_title(head, fontsize=10)
    return {"plot_path": _save(plt, fig, _out(source, out, "tran", project)),
            **_guard(m)}


# ------------------------------------------------------------------------------- THD

def plot_thd(source, out=None, node: str | None = None, step: int = 0,
             f0: float | None = None, harmonics: int = 9, skip_cycles: float = 1.0,
             title: str | None = None, project=None) -> dict:
    """Harmonic bars in dBc, fundamental as the 0 dBc reference.

    dBc rather than volts: distortion is a ratio, and on a linear volt axis the 2nd
    harmonic of a clean amplifier is an invisible sliver next to the fundamental.

    Harmonics that were never synthesised are drawn as *nothing*, not as a bar clipped
    to the axis floor. A clipped bar is the full height of the plot and reads as heavy
    distortion at every harmonic -- the first version of this plot showed a spectrally
    clean signal as nine large red bars.
    """
    plt = _plt()
    w = _resolve(source, step)
    m = raw.thd(w, node, f0, harmonics, skip_cycles)
    amps = m["harmonics_v"]
    fund = amps[0]
    dbc = {k: (20 * math.log10(a / fund) if a > 0 else None)
           for k, a in enumerate(amps, start=1)}
    ks = list(range(2, len(amps) + 1))               # h1 is the reference, not a bar
    present = {k: v for k, v in dbc.items() if k in ks and v is not None and v > SILENT}
    absent = [k for k in ks if k not in present]

    fig, ax = plt.subplots(figsize=FIGSIZE, dpi=DPI)
    c = _chrome()
    floor = (min(present.values()) - 15) if present else -60.0
    ax.set_ylim(floor, 8)
    ax.set_xlim(ks[0] - 0.7, ks[-1] + 0.7)     # room for the outermost bar's labels
    if present:
        ax.bar(list(present), [v - floor for v in present.values()], bottom=floor,
               color=C_LIMIT, width=0.6)
        for k, v in present.items():
            ax.annotate(f"{v:.1f}", (k, v), textcoords="offset points", xytext=(0, 3),
                        ha="center", fontsize=8)
    for k in absent:
        ax.annotate(f"< {SILENT:.0f}", (k, floor), textcoords="offset points",
                    xytext=(0, 6), ha="center", fontsize=7, color=c["faint"],
                    rotation=90)
    ax.axhline(0.0, color=C_TRACE, lw=1.8)
    # Axes-fraction x, data y: pinned to the 0 dBc line wherever the x limits land.
    ax.annotate(f"fundamental {_eng(fund)}V  (0 dBc ref)", (0.02, 0.0),
                xycoords=("axes fraction", "data"), textcoords="offset points",
                xytext=(0, 5), fontsize=8, color=C_TRACE)

    ax.set_xticks(ks)
    ax.set_xticklabels([f"{k}\n{_eng(k * m['f0_hz'])}Hz" for k in ks], fontsize=8)
    ax.set_xlabel("harmonic")
    ax.set_ylabel("amplitude (dBc)")
    ax.grid(True, axis="y", alpha=0.35)
    ax.annotate(f"THD = {m['thd_pct']:.4f}%"
                + (f"\nSINAD = {m['sinad_db']:.2f} dB" if m["sinad_db"] else "")
                + f"\nf0 = {_eng(m['f0_hz'])}Hz, {m['cycles']} cycles",
                (0.98, 0.95), xycoords="axes fraction", ha="right", va="top",
                fontsize=9, bbox=dict(boxstyle="round", fc=c["bg"], ec=c["grid"]))
    head = title or (Path(str(source)).stem if not isinstance(source, raw.Wave) else "THD")
    ax.set_title(head, fontsize=10)
    return {"plot_path": _save(plt, fig, _out(source, out, "thd", project)),
            "harmonics_present": sorted(present), **_guard(m)}


# ------------------------------------------------------------------------- histogram

def plot_histogram(values: list[float], out=None, label: str = "metric",
                   spec: tuple | None = None, bins: int = 12,
                   title: str | None = None, project=None) -> dict:
    """Monte Carlo distribution with mean, +-3 sigma and the spec limits drawn.

    Returns the statistics, not the samples. `spec=(lo, hi)` draws the limits and the
    reported `yield_pct` counts samples inside them -- which is the number a design
    review asks for, and it is not the same as "3 sigma fits".
    """
    plt = _plt()
    if len(values) < 2:
        raise PlotError(f"need >=2 samples to plot a distribution, got {len(values)}")
    n = len(values)
    mu = sum(values) / n
    sd = math.sqrt(sum((v - mu) ** 2 for v in values) / (n - 1))

    fig, ax = plt.subplots(figsize=FIGSIZE, dpi=DPI)
    ax.hist(values, bins=min(bins, n), color=C_TRACE2, edgecolor=C_TRACE, alpha=0.9)
    ax.axvline(mu, color=C_TRACE, lw=1.6, label=f"mean {mu:.6g}")
    for s in (-3, 3):
        ax.axvline(mu + s * sd, color=C_MARK, lw=1.0, ls="--",
                   label=f"{s:+d}σ" if s > 0 else None)
    inside = n
    if spec:
        lo, hi = spec
        for v, name in ((lo, "spec lo"), (hi, "spec hi")):
            if v is not None:
                ax.axvline(v, color=C_LIMIT, lw=1.6, label=name)
        inside = sum(1 for v in values
                     if (lo is None or v >= lo) and (hi is None or v <= hi))
    ax.set_xlabel(label)
    ax.set_ylabel("count")
    ax.grid(True, axis="y", alpha=0.35)
    ax.legend(fontsize=8)
    # The yield belongs in the title: the plot is what a human reads, and "27/30 pass"
    # is the sentence the histogram exists to say.
    head = title or f"Monte Carlo: {label} (n={n})"
    if spec:
        head += f"  --  {inside}/{n} inside spec ({inside / n * 100:.1f}%)"
    ax.set_title(head, fontsize=10)
    return {"plot_path": _save(plt, fig, _out("mc", out, f"hist_{label}", project)),
            "n": n, "mean": raw._r(mu), "std": raw._r(sd),
            "min": raw._r(min(values)), "max": raw._r(max(values)),
            "sigma3_lo": raw._r(mu - 3 * sd), "sigma3_hi": raw._r(mu + 3 * sd),
            "cv_pct": raw._r(sd / mu * 100) if mu else None,
            "yield_pct": raw._r(inside / n * 100),
            "spec": list(spec) if spec else None}


def _eng(v: float) -> str:
    """Engineering notation, so an axis label reads `1.59k` instead of `1591.549`."""
    if v is None or v == 0 or not math.isfinite(v):
        return "0"
    sign = "-" if v < 0 else ""
    v = abs(v)
    e = max(-24, min(24, 3 * math.floor(math.log10(v) / 3)))
    m = v / 10 ** e
    if m >= 999.5 and e < 24:
        # `%.3g` renders 999.9967 as "1e+03", so 0.99999 V printed as "1e+03mV". Carry.
        m, e = m / 1000, e + 3
    return f"{sign}{m:.3g}{SI[e]}"


if __name__ == "__main__":                            # smallest useful self-check
    import sys
    for p in sys.argv[1:]:
        w = raw.read_raw(p)[0]
        fn = {"ac": plot_bode, "tran": plot_transient}.get(w.analysis)
        print(fn(p)["plot_path"] if fn else f"{p}: no plot for {w.analysis}")
