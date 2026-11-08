"""Checks for the plot artifacts.

Two things are actually worth testing here, and neither is "does it look nice":

1. **The token guard holds.** Every return is scalars and a path. A plotter that starts
   handing back its input array turns an optimization loop into a context fire, and it
   would still produce correct pictures while doing it -- so the test is on the reply.
2. **Absent means absent.** A harmonic at -187 dBc must not render as a bar. The first
   version clipped it to the axis floor, which draws full height and reads as heavy
   distortion on a spectrally clean signal.

matplotlib is an optional extra, so the rendering tests skip without it while the guard
and formatting tests still run. Fixtures are real `.raw` files, so nothing here needs
LTspice installed.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

from spicemcp import plots, raw, render

FX = Path(__file__).parent / "fixtures"
mpl = pytest.importorskip  # rendering tests call mpl("matplotlib") first


# ------------------------------------------------------------------------ token guard

def test_guard_replaces_arrays_and_keeps_scalars():
    d = {"thd_pct": 5.38, "harmonics_v": [0.1] * 9, "samples": list(range(5000)),
         "note": "text", "path": None}
    g = plots._guard(d)
    assert g["thd_pct"] == 5.38 and g["note"] == "text" and g["path"] is None
    assert g["harmonics_v"] == [0.1] * 9, "9 harmonics are a metric, not a waveform"
    assert isinstance(g["samples"], str) and "5000 values omitted" in g["samples"]


@pytest.mark.parametrize("fixture,fn", [
    ("rc_ac", "plot_bode"), ("ac_loop", "plot_bode"),
    ("rc_tran", "plot_transient"), ("rlc_tran", "plot_transient"),
    ("thd_tran", "plot_thd"),
])
def test_no_plot_reply_ever_carries_a_waveform(fixture, fn, tmp_path):
    """The module's whole reason to exist. rc_tran alone is 1022 points; thd_tran 5018."""
    mpl("matplotlib")
    r = getattr(plots, fn)(FX / f"{fixture}.raw", out=tmp_path / f"{fixture}.png")
    n = len(raw.read_raw(FX / f"{fixture}.raw")[0].x)
    for k, v in r.items():
        assert not (isinstance(v, (list, tuple)) and len(v) > plots.MAX_LIST), \
            f"{fn} returned {k} with {len(v)} entries"
    assert len(str(r)) < 1500, f"{fn} reply is {len(str(r))} chars for {n} points"


def test_the_plot_is_a_real_png_on_disk_and_the_figure_is_closed(tmp_path):
    """An unclosed Agg figure leaks per call; 30 Monte Carlo runs is 30 live figures."""
    plt = mpl("matplotlib.pyplot")
    p = Path(plots.plot_bode(FX / "ac_loop.raw", out=tmp_path / "b.png")["plot_path"])
    assert p.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"
    assert p.stat().st_size > 10_000, "a 6-inch Bode plot is not 10 kB of PNG"
    assert plt.get_fignums() == [], "figure left open"


def test_plots_dir_follows_the_project_env_var(tmp_path, monkeypatch):
    monkeypatch.setenv("SPICEMCP_PROJECT", str(tmp_path))
    assert plots.plots_dir() == tmp_path / ".ltspice-mcp" / "plots"
    assert plots.plots_dir().is_dir(), "created on demand"
    other = tmp_path / "elsewhere"
    assert plots.plots_dir(other) == other / ".ltspice-mcp" / "plots"


def test_default_output_path_is_named_after_the_source(tmp_path, monkeypatch):
    mpl("matplotlib")
    monkeypatch.setenv("SPICEMCP_PROJECT", str(tmp_path))
    p = Path(plots.plot_transient(FX / "rc_tran.raw")["plot_path"])
    assert p.name == "rc_tran_tran.png" and p.parent == plots.plots_dir()


# --------------------------------------------------------------------- what it draws

def test_absent_harmonics_are_absent_not_bars_at_the_floor(tmp_path):
    """The fixture synthesises h2 and h3 only. h4-h9 measure ~-187 dBc, which is float
    noise: clipping them to the axis floor draws nine full-height bars on a clean signal.
    """
    mpl("matplotlib")
    r = plots.plot_thd(FX / "thd_tran.raw", out=tmp_path / "t.png")
    assert r["harmonics_present"] == [2, 3]
    assert min(r["harmonic_db"][3:]) < plots.SILENT, "fixture changed; test is moot"


def test_plot_metrics_are_the_same_numbers_raw_computes(tmp_path):
    """The plot must not have its own second opinion about the phase margin."""
    mpl("matplotlib")
    w = raw.read_raw(FX / "ac_loop.raw")[0]
    r = plots.plot_bode(w, out=tmp_path / "b.png")
    m = raw.ac_metrics(w)
    for k in ("ugf_hz", "phase_margin_deg", "gain_margin_db", "bw_3db_hz"):
        assert r[k] == m[k], k


def test_a_circuit_with_no_unity_crossing_still_plots(tmp_path):
    """rc_ac has DC gain of exactly 1 and so no UGF, no PM, no GM. Every annotation on
    this plot is optional and the drawing code has to survive all of them being None."""
    mpl("matplotlib")
    r = plots.plot_bode(FX / "rc_ac.raw", out=tmp_path / "rc.png")
    assert r["ugf_hz"] is None and r["phase_margin_deg"] is None
    assert Path(r["plot_path"]).stat().st_size > 10_000


def test_a_transient_that_never_settles_still_plots(tmp_path):
    """settle=None is a normal outcome for a short capture, not an error."""
    mpl("matplotlib")
    w = raw.read_raw(FX / "rc_tran.raw")[0]
    n = next(i for i, t in enumerate(w.x) if t > 300e-6)
    short = raw.Wave(w.plotname, w.flags, w.names, w.kinds, w.x[:n],
                     {k: v[:n] for k, v in w.data.items()})
    r = plots.plot_transient(short, out=tmp_path / "s.png", final=1.0)
    assert r["t_settle_1pct"] is None
    assert Path(r["plot_path"]).is_file()


def test_a_stepped_run_plots_one_step_and_labels_which(tmp_path):
    mpl("matplotlib")
    for i, want in enumerate(("r=1000", "r=2000", "r=4000")):
        r = plots.plot_bode(FX / "step_ac.raw", out=tmp_path / f"s{i}.png", step=i)
        assert r["bw_3db_hz"] == pytest.approx(
            raw.ac_metrics(raw.read_raw(FX / "step_ac.raw")[i])["bw_3db_hz"])
    with pytest.raises(plots.PlotError, match="3 step"):
        plots.plot_bode(FX / "step_ac.raw", out=tmp_path / "x.png", step=3)


# --------------------------------------------------------------------------- histogram

def test_histogram_yield_counts_the_spec_not_three_sigma(tmp_path):
    """The number a design review asks for. 3-sigma fitting inside the spec and 100% of
    samples passing are different claims, and only one of them is a yield."""
    mpl("matplotlib")
    vals = [1.0] * 27 + [3.0, 3.0, 3.0]              # 3 of 30 outside a 2.0 upper limit
    r = plots.plot_histogram(vals, out=tmp_path / "h.png", label="fc", spec=(None, 2.0))
    assert r["n"] == 30 and r["yield_pct"] == pytest.approx(90.0)
    assert r["mean"] == pytest.approx(1.2) and r["max"] == 3.0
    assert r["sigma3_hi"] > 2.0, "spread is wide; the yield number is doing the work"
    assert not any(isinstance(v, list) and len(v) > plots.MAX_LIST for v in r.values())


def test_histogram_refuses_one_sample_instead_of_dividing_by_zero(tmp_path):
    mpl("matplotlib")
    with pytest.raises(plots.PlotError, match=">=2 samples"):
        plots.plot_histogram([1.0], out=tmp_path / "h.png")


# ------------------------------------------------------------------------ house palette

def test_every_plot_colour_comes_from_the_one_palette():
    """No figure may introduce a hex of its own.

    The point of a shared palette is that a schematic and its plots read as one document,
    and that breaks silently: a hardcoded `#1f77b4` still draws a correct curve. So the
    check is on the source -- every literal colour in `plots.py` must be a palette entry
    or a chrome colour derived from the schematic style.
    """
    src = Path(plots.__file__).read_text(encoding="utf-8")
    allowed = {c.lower() for c in render.SERIES}
    found = {m.lower() for m in re.findall(r'"(#[0-9a-fA-F]{3,8})"', src)}
    # `_chrome`'s own fallback grid colour is the one literal that is not a trace.
    assert found - allowed <= {"#dddddd"}, found - allowed


def test_the_palette_is_the_requested_ramp_and_each_entry_has_one_job():
    assert render.SERIES == ("#003d5c", "#594e90", "#bc4c96", "#ff5f66", "#ffa600")
    roles = (plots.C_TRACE, plots.C_TRACE2, plots.C_MARK, plots.C_LIMIT, plots.C_BAND)
    assert roles == render.SERIES, "roles must map to the ramp in order"
    assert len(set(roles)) == 5, "two roles sharing a colour makes the figure ambiguous"


def test_plot_chrome_tracks_the_default_schematic_style():
    """Derived, not duplicated: move the style's ink and the plots move with it."""
    assert plots.STYLE == render.DEFAULT_STYLE == "tech_minimal"
    st = render.style(render.DEFAULT_STYLE)
    c = plots._chrome()
    assert c["bg"] == st["bg"] and c["ink"] == st["roles"]["wire"]["c"]
    assert c["sans"] == render.SANS


def test_the_style_tool_reports_the_palette_it_actually_draws_with():
    from spicemcp import server                          # noqa: PLC0415
    s = server.visual_style()
    assert s["default_style"] == "tech_minimal"
    assert s["series"] == list(render.SERIES)
    assert set(s["series_roles"].values()) == set(render.SERIES)
    assert s["schematic_styles"] == sorted(render.STYLES)
    assert s["chrome"]["background"] == render.style("tech_minimal")["bg"]


# ------------------------------------------------------------------- engineering units

@pytest.mark.parametrize("v,want", [
    (1591.549, "1.59k"), (1e6, "1M"), (0.047, "47m"), (1e-9, "1n"),
    (4.45e-10, "445p"), (-2200, "-2.2k"), (0.0, "0"), (999.4, "999"),
    (0.999996710154, "1"),      # `%.3g` alone gives "1e+03m" -- the mantissa must carry
    (999.6, "1k"),
])
def test_engineering_notation_labels_the_right_decade(v, want):
    """An off-by-one in a prefix table mislabels whole decades invisibly: an earlier
    version rendered 1e6 as `1k` and 47 as `47m`, and every axis still looked plausible.
    """
    assert plots._eng(v) == want


# --------------------------------------------------------------- optional dependency

def test_the_server_still_imports_with_no_matplotlib_and_says_how_to_get_it():
    """A subprocess, because matplotlib is already imported in this one. The core must
    load on a machine without it -- `raw.py` is stdlib `struct`+`math` for this reason --
    and asking for a plot must name the fix, not raise ImportError from three frames in.
    """
    code = (
        "import sys\n"
        "BLOCKED = ('matplotlib', 'numpy')\n"
        "class Block:\n"
        # find_spec, not find_module: the old finder protocol was removed in 3.12 and a
        # blocker using it silently does nothing, so the test passes for the wrong reason.
        "    def find_spec(self, name, path=None, target=None):\n"
        "        if name.split('.')[0] in BLOCKED:\n"
        "            raise ImportError(f'blocked: {name}')\n"
        "        return None\n"
        "sys.meta_path.insert(0, Block())\n"
        "from spicemcp import plots, raw, server\n"
        "assert not [m for m in sys.modules if m.split('.')[0] in BLOCKED]\n"
        "m = raw.summarize(r'" + str(FX / "rc_tran.raw") + "')\n"
        "assert m['results'][0]['t_rise_10_90'] > 0, 'metrics need no numpy'\n"
        "try:\n"
        "    plots.plot_bode(r'" + str(FX / "rc_ac.raw") + "')\n"
        "except plots.PlotError as e:\n"
        "    assert 'pip install' in str(e), e\n"
        "else:\n"
        "    raise AssertionError('expected PlotError')\n"
        "print('ok')\n"
    )
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                       cwd=Path(__file__).parent.parent)
    assert r.returncode == 0, r.stderr
    assert "ok" in r.stdout
