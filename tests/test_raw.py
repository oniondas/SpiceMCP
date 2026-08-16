"""Checks for the `.raw` reader and the waveform metrics.

Every fixture in `tests/fixtures/` is a real LTspice 26.0.2 output whose answer is known
in closed form, so these run identically with and without LTspice installed -- the
binary layout is pinned against genuine files rather than against a writer of my own
that could be wrong in the same direction as the reader.

The closed forms are recomputed here rather than pasted as constants: a hard-coded
1591.55 tests that the number has not changed, not that it is right.
"""

from __future__ import annotations

import cmath
import math
import struct
from pathlib import Path

import pytest

from spicemcp import raw

FX = Path(__file__).parent / "fixtures"

TAU = 1e-4                     # rc_ac / rc_tran: R=1k, C=100n
FC = 1 / (2 * math.pi * TAU)   # 1591.5494309190 Hz
POLES = (628.3185307179587, 62831.85307179587, 628318.5307179587)   # ac_loop, rad/s
A0 = 100.0
ZETA, W0 = 0.2, 1e5           # rlc_tran: L=1m, C=100n, R=40


def rc(f: float) -> complex:
    return 1 / (1 + 1j * 2 * math.pi * f * TAU)


def loop(f: float) -> complex:
    s = 2j * math.pi * f
    out = A0
    for w in POLES:
        out /= 1 + s / w
    return out


def loop_phase(f: float) -> float:
    """Unwrapped by construction -- three arctans that each stay inside 90 degrees."""
    return -sum(math.degrees(math.atan(2 * math.pi * f / w)) for w in POLES)


def bisect_log(pred, lo: float, hi: float) -> float:
    for _ in range(200):
        mid = math.sqrt(lo * hi)
        if pred(mid):
            lo = mid
        else:
            hi = mid
    return math.sqrt(lo * hi)


# --------------------------------------------------------------------- binary layout

def test_every_fixture_parses_with_the_shape_its_header_claims():
    want = {"rc_ac": ("ac", 1, 251), "ac_loop": ("ac", 1, 1401),
            "rc_tran": ("tran", 1, 1022), "rlc_tran": ("tran", 1, 2527),
            "thd_tran": ("tran", 1, 5018), "op_mos": ("op", 1, 1),
            "step_ac": ("ac", 3, 81)}
    for name, (analysis, steps, npts) in want.items():
        w = raw.read_raw(FX / f"{name}.raw")
        assert len(w) == steps, name
        assert w[0].analysis == analysis, name
        assert len(w[0].x) == npts, name
        assert len(w[0].data[w[0].names[0]]) == npts, name


def test_complex_ac_columns_match_closed_form_to_float64():
    """All-complex128 layout, `nv * 16` stride. A wrong stride still yields plausible
    numbers -- interleaved real/imag from the next column looks like a phase error."""
    w = raw.read_raw(FX / "rc_ac.raw")[0]
    assert w.complex
    err = max(abs(v - rc(f)) for f, v in zip(w.x, w["out"]))
    assert err < 1e-12, f"AC column decode off by {err:.2e}"


def test_real_tran_columns_match_closed_form_to_float32():
    """float64 axis + float32 columns, so the stride is `8 + (nv-1)*4`, not `nv*8`.
    float32 stores ~7 digits, which sets the tolerance -- not the parser."""
    w = raw.read_raw(FX / "rc_tran.raw")[0]
    assert not w.complex
    err = max(abs(v - (1 - math.exp(-t / TAU))) for t, v in zip(w.x, w["out"]) if t > 1e-9)
    assert err < 1e-5, f"tran column decode off by {err:.2e}"


def test_transient_time_axis_is_monotonic_after_sign_correction():
    """LTspice sets the sign bit on some time samples: a real run reads -1.0505e-05
    between 1.01e-06 and 2e-05. Unhandled it breaks every time-domain metric."""
    for name in ("rc_tran", "rlc_tran", "thd_tran"):
        t = raw.read_raw(FX / f"{name}.raw")[0].x
        assert all(b >= a for a, b in zip(t, t[1:])), f"{name} time axis not monotonic"
        assert all(v >= 0 for v in t), f"{name} has negative time"


def test_operating_point_exposes_node_voltages_but_no_device_parameters():
    """Pinning a real LTspice limitation, because it dictates how a DC audit must work:
    a `.op` .raw carries node voltages and terminal currents only. There is no `Vgs`,
    `Vds` or `Vth` here and no ngspice-style `@M1[vth]` -- those are syntax errors on
    26.0.2. `robustness.audit_dc_operating_points` therefore derives Vds/Vgs from node
    voltages plus the netlist's terminal order, and reads Vth off the `.model` card."""
    w = raw.read_raw(FX / "op_mos.raw")[0]
    assert len(w.x) == 1
    assert w["vdd"][0] == pytest.approx(5.0)
    assert w["g"][0] == pytest.approx(2.0)
    assert "Id(M1)" in w.names and "Ic(Q1)" in w.names
    assert not [n for n in w.names if "vth" in n.lower() or "vgs" in n.lower()]


def test_stepped_run_splits_into_datasets_and_keeps_its_labels():
    """A `.step` run concatenates every dataset into one file with the sweep axis
    restarting; the header reports only the total. Labels come from the sibling `.log`,
    without which `step=0,1,2` says nothing about which circuit it was."""
    waves = raw.read_raw(FX / "step_ac.raw")
    assert [w.step_params for w in waves] == ["r=1000", "r=2000", "r=4000"]
    for w, r in zip(waves, (1e3, 2e3, 4e3)):
        assert w.x == sorted(w.x), "sweep axis not restored per dataset"
        want = 1 / (2 * math.pi * r * 100e-9)
        got = raw.ac_metrics(w)["bw_3db_hz"]
        assert got == pytest.approx(want, rel=0.01), f"r={r}: fc {got} != {want}"


# ------------------------------------------------------------------------- sweep axes

def test_a_bipolar_dc_sweep_keeps_its_negative_half():
    """`abs()` belongs on a transient time axis and an AC frequency column, and is a bug
    everywhere else. It read this sweep back as `5 4 3 2 1 0 1 2 3 4 5`: still plottable,
    still measurable, and the wrong operating point at six of the eleven samples."""
    w = raw.read_raw(FX / "dc_sweep.raw")[0]
    assert w.analysis == "dc"
    assert w.x == pytest.approx([float(v) for v in range(-5, 6)])
    assert w["out"] == pytest.approx([v / 2 for v in range(-5, 6)], abs=1e-6)


def test_a_temperature_step_keeps_a_negative_corner():
    """-40 C read as +40 C is the failure this pins, and it is the cold corner of every
    PVT sweep. R1 has tc=1m, so each point has a closed form."""
    w = raw.read_raw(FX / "temp_op.raw")[0]
    assert w.kinds[0] == "temperature"
    assert w.x == pytest.approx([-40.0, 27.0, 125.0])
    want = [1000 / (1000 * (1 + 1e-3 * (t - 27)) + 1000) for t in w.x]
    assert w["out"] == pytest.approx(want, rel=1e-6)


def test_a_stepped_op_is_one_wave_whose_axis_is_the_step_variable():
    """LTspice stores a stepped `.op` as a single dataset with the step parameter as the
    sweep axis -- 30 runs is one 30-point wave, not 30 one-point waves. Monte Carlo reads
    it straight out of that array, which is why it costs one LTspice launch."""
    waves = raw.read_raw(FX / "mc_op.raw")
    assert len(waves) == 1
    w = waves[0]
    assert w.kinds[0] == "param" and w.names[0] == "run"
    assert w.x == pytest.approx([float(i) for i in range(1, 31)])
    assert w["mid"] == pytest.approx([0.5] * 30, abs=1e-6), "fixed divider must not move"


def test_monte_carlo_draws_are_independent_and_inside_the_tolerance_asked_for():
    """`mc(1k,0.01)` is uniform over +-1%, redrawn per run and per component. Measured
    over 400 runs: sd = 0.572% = 1/sqrt(3), and nothing outside +-1%. A gaussian
    sigma=1% would put a third of the samples outside the tolerance the caller asked for.
    """
    w = raw.read_raw(FX / "mc_op.raw")[0]
    v = w["out"]
    assert len(set(v)) == 30, "draws repeat; mc() is not redrawing per run"
    # V(out) = RB/(RA+RB) with both legs 1k +-1%, so the ratio is bounded by (1-t)/(1+t).
    lo, hi = 0.99 / (0.99 + 1.01), 1.01 / (1.01 + 0.99)
    assert min(v) >= lo - 1e-9 and max(v) <= hi + 1e-9, f"{min(v)}..{max(v)} outside {lo}..{hi}"
    assert min(v) < 0.5 < max(v), "a 30-run sweep should straddle nominal"


# ------------------------------------------------------------------------- AC metrics

def test_loop_gain_ugf_phase_margin_and_gain_margin_match_closed_form():
    m = raw.ac_metrics(raw.read_raw(FX / "ac_loop.raw")[0])
    ugf = bisect_log(lambda f: abs(loop(f)) > 1, 1e2, 1e6)
    f180 = bisect_log(lambda f: loop_phase(f) > -180, 1e2, 1e7)
    assert m["dc_gain_db"] == pytest.approx(20 * math.log10(A0), abs=1e-3)
    assert m["ugf_hz"] == pytest.approx(ugf, rel=1e-4)
    assert m["phase_margin_deg"] == pytest.approx(180 + loop_phase(ugf), abs=1e-3)
    assert m["f_phase_180_hz"] == pytest.approx(f180, rel=1e-4)
    assert m["gain_margin_db"] == pytest.approx(-20 * math.log10(abs(loop(f180))), abs=1e-3)


def test_bandwidth_is_exact_to_the_sweep_grid_not_better():
    """The residual is the `.ac dec 50` grid, not the interpolator: interpolating the
    closed form between the same two sample frequencies gives the same answer to 8
    digits. A tighter tolerance here would be testing the fixture's resolution."""
    w = raw.read_raw(FX / "rc_ac.raw")[0]
    db = [20 * math.log10(abs(v)) for v in w["out"]]
    tgt = db[0] - 3.0
    i = next(k for k in range(len(db) - 1) if db[k] >= tgt >= db[k + 1])
    ex = [20 * math.log10(abs(rc(w.x[k]))) for k in (i, i + 1)]
    frac = (tgt - ex[0]) / (ex[1] - ex[0])
    same_way = w.x[i] * (w.x[i + 1] / w.x[i]) ** frac
    assert raw.ac_metrics(w)["bw_3db_hz"] == pytest.approx(same_way, rel=1e-8)
    assert same_way == pytest.approx(FC, rel=0.005)


def test_no_unity_crossing_says_so_instead_of_inventing_a_margin():
    """The RC lowpass has DC gain of exactly 1, so it has no UGF. Returning the first
    sample frequency and a phase margin computed there would be a stability number for
    a circuit that has no loop."""
    m = raw.ac_metrics(raw.read_raw(FX / "rc_ac.raw")[0])
    assert m["ugf_hz"] is None and m["phase_margin_deg"] is None
    assert "0 dB" in m["note"]


def test_phase_unwrap_removes_the_atan2_branch_cut():
    assert raw._unwrap([170.0, -170.0, -150.0]) == pytest.approx([170.0, 190.0, 210.0])
    assert raw._unwrap([-170.0, 170.0]) == pytest.approx([-170.0, -190.0])
    w = raw.read_raw(FX / "ac_loop.raw")[0]
    ph = raw._unwrap([math.degrees(cmath.phase(v)) for v in w["out"]])
    assert all(b <= a + 1e-9 for a, b in zip(ph, ph[1:])), "3-pole phase should fall"
    assert ph[-1] == pytest.approx(loop_phase(w.x[-1]), abs=0.1)


def test_ac_metrics_refuses_a_real_run():
    with pytest.raises(ValueError, match="complex"):
        raw.ac_metrics(raw.read_raw(FX / "rc_tran.raw")[0])


# ------------------------------------------------------------------ transient metrics

def test_step_response_metrics_match_closed_form():
    """`final=1.0` is the asymptote. The default -- the last sample, 0.999955 here --
    shifts 0.1% settling by 4.4 us, which is correct behaviour and why it is settable."""
    m = raw.tran_metrics(raw.read_raw(FX / "rc_tran.raw")[0], final=1.0)
    assert m["t_rise_10_90"] == pytest.approx(TAU * math.log(9), rel=1e-4)
    assert m["t_settle_1pct"] == pytest.approx(TAU * math.log(100), rel=1e-4)
    assert m["t_settle_0p1pct"] == pytest.approx(TAU * math.log(1000), rel=1e-4)
    assert m["slew_pos_v_per_s"] == pytest.approx(1 / TAU, rel=1e-4)
    assert m["overshoot_pct"] == 0.0, "first-order step cannot overshoot"
    assert m["settled_1pct"] and m["settled_0p1pct"]


def test_second_order_overshoot_matches_the_damping_ratio():
    m = raw.tran_metrics(raw.read_raw(FX / "rlc_tran.raw")[0], final=1.0)
    want = math.exp(-math.pi * ZETA / math.sqrt(1 - ZETA ** 2)) * 100
    t_peak = math.pi / (W0 * math.sqrt(1 - ZETA ** 2))
    assert m["overshoot_pct"] == pytest.approx(want, rel=1e-3)
    assert m["t_max"] == pytest.approx(t_peak, rel=0.01)
    assert m["slew_neg_v_per_s"] < 0, "a ringing response must have a falling edge"


def test_settling_uses_the_last_exit_from_the_band_not_the_first_entry():
    """A ringing waveform enters the band and leaves again. The first entry reports a
    settling time the circuit has not reached, and it always looks better."""
    w = raw.read_raw(FX / "rlc_tran.raw")[0]
    m = raw.tran_metrics(w, final=1.0)
    t, y = w.x, w["out"]
    band = 0.01
    first = next(tt for tt, v in zip(t, y) if abs(v - 1.0) <= band)
    assert m["t_settle_1pct"] > first, "took the first entry into the band"
    assert all(abs(v - 1.0) <= band * 1.001
               for tt, v in zip(t, y) if tt > m["t_settle_1pct"])


def test_a_run_too_short_to_settle_reports_none_not_the_record_end():
    """Returning `t[-1]` would be a measurement of the `.tran` statement."""
    w = raw.read_raw(FX / "rc_tran.raw")[0]
    n = next(i for i, t in enumerate(w.x) if t > 300e-6)         # cut at 300us < 460us
    short = raw.Wave(w.plotname, w.flags, w.names, w.kinds, w.x[:n],
                     {k: v[:n] for k, v in w.data.items()})
    m = raw.tran_metrics(short, final=1.0)
    assert m["t_settle_1pct"] is None and m["settled_1pct"] is False
    assert m["t_settle_0p1pct"] is None


def test_flat_trace_says_so_instead_of_dividing_by_zero():
    w = raw.Wave("Transient Analysis", ["real"], ["time", "V(out)"], ["time", "voltage"],
                 [0.0, 1e-6, 2e-6], {"time": [0.0, 1e-6, 2e-6], "V(out)": [1.0, 1.0, 1.0]})
    m = raw.tran_metrics(w)
    assert "flat" in m["note"] and "overshoot_pct" not in m


# --------------------------------------------------------------------- FFT / distortion

def test_thd_and_sinad_match_the_harmonics_that_were_synthesised():
    """The fixture is 1.0*sin(2pi 1k t) + 0.05*sin(2pi 2k t) + 0.02*sin(2pi 3k t)."""
    m = raw.thd(raw.read_raw(FX / "thd_tran.raw")[0])
    a = (1.0, 0.05, 0.02)
    want = math.sqrt(a[1] ** 2 + a[2] ** 2) / a[0]
    assert m["f0_hz"] == pytest.approx(1000.0, rel=1e-6)
    assert m["thd_pct"] == pytest.approx(want * 100, rel=1e-3)
    assert m["harmonics_v"][:3] == pytest.approx(a, rel=1e-4)
    assert m["harmonics_v"][3] < 1e-6, "4th harmonic was never synthesised"
    sig, dist = a[0] ** 2 / 2, (a[1] ** 2 + a[2] ** 2) / 2
    assert m["sinad_db"] == pytest.approx(10 * math.log10(sig / dist), abs=0.01)


def test_thd_without_resampling_would_be_badly_wrong():
    """The reason `thd()` always resamples. LTspice's transient timesteps span five
    orders of magnitude even with compression off, and both obvious shortcuts return a
    plausible number: truncating to a power of two then FFT-ing reads 27.5% on a 5.385%
    signal, and treating the whole array as uniformly sampled reads 4.83%."""
    w = raw.read_raw(FX / "thd_tran.raw")[0]
    t, y = w.x, w["out"]
    dt = [b - a for a, b in zip(t, t[1:])]
    assert max(dt) / min(dt) > 1000, "fixture is no longer non-uniform; test is moot"

    def naive(arr, cycles=5):                        # the record is exactly 5ms of 1kHz
        dc = sum(arr) / len(arr)
        h = [raw._bin_amp([v - dc for v in arr], cycles * k) for k in (1, 2, 3)]
        return math.sqrt(h[1] ** 2 + h[2] ** 2) / h[0] * 100

    assert naive(y[:4096]) == pytest.approx(27.47, abs=0.1)
    assert naive(y) == pytest.approx(4.83, abs=0.05)
    assert raw.thd(w)["thd_pct"] == pytest.approx(5.3851648071, rel=1e-3)


def test_thd_needs_whole_cycles_and_says_so_when_it_cannot_get_them():
    w = raw.read_raw(FX / "thd_tran.raw")[0]
    with pytest.raises(ValueError, match="whole cycles"):
        raw.thd(w, f0=1000.0, skip_cycles=4.5)       # 5ms record, 0.5 cycles left
    with pytest.raises(ValueError, match="fundamental"):
        raw.thd(w, f0=0.0)


def test_resample_lands_on_a_uniform_grid_and_interpolates_linearly():
    x = [0.0, 1.0, 3.0, 4.0]
    y = [0.0, 10.0, 30.0, 40.0]                      # y = 10x, non-uniform samples
    dt, out = raw.resample(x, y, 4, 0.0, 4.0)
    assert dt == pytest.approx(1.0)
    assert out == pytest.approx([0.0, 10.0, 20.0, 30.0])
    with pytest.raises(ValueError, match="resample"):
        raw.resample(x, y, 4, 2.0, 1.0)


def test_single_bin_dft_finds_the_amplitude_of_a_pure_tone():
    n = 1024
    y = [0.7 * math.sin(2 * math.pi * 8 * i / n) for i in range(n)]
    assert raw._bin_amp(y, 8) == pytest.approx(0.7, rel=1e-9)
    assert raw._bin_amp(y, 9) < 1e-9
    assert raw._bin_amp(y, 0) == 0.0 and raw._bin_amp(y, n) == 0.0


# ------------------------------------------------------------------- reader robustness

def _ascii_raw(nv: int, npt: int, cplx: bool) -> bytes:
    """Minimal ASCII `.raw`. ngspice and several tools emit this form."""
    names = ["time"] + [f"V(n{j})" for j in range(1, nv)]
    head = (f"Title: t\nPlotname: Transient Analysis\n"
            f"Flags: {'complex' if cplx else 'real'}\nNo. Variables: {nv}\n"
            f"No. Points: {npt}\nVariables:\n"
            + "".join(f"\t{j}\t{n}\t{'voltage' if j else 'time'}\n"
                      for j, n in enumerate(names))
            + "Values:\n")
    body = "".join(f"{k}\t" + "\t".join(f"{k + j}" for j in range(nv)) + "\n"
                   for k in range(npt))
    return (head + body).encode("latin-1")


def test_ascii_values_form_reads_the_same_as_binary(tmp_path):
    p = tmp_path / "a.raw"
    p.write_bytes(_ascii_raw(3, 4, cplx=False))
    w = raw.read_raw(p)[0]
    assert len(w.x) == 4
    assert w.names == ["time", "V(n1)", "V(n2)"]
    assert w.x == [0.0, 1.0, 2.0, 3.0]
    assert w["n2"] == [2.0, 3.0, 4.0, 5.0]


def test_utf16_and_latin1_headers_both_decode(tmp_path):
    """LTspice writes UTF-16LE; other tools write ASCII. Guessing wrong turns the whole
    header into mojibake and `No. Points` never parses."""
    text = _ascii_raw(2, 2, cplx=False).decode("latin-1")
    p = tmp_path / "u.raw"
    p.write_bytes(text.encode("utf-16-le"))
    assert raw.read_raw(p)[0].x == [0.0, 1.0]


def test_a_truncated_binary_file_raises_instead_of_returning_short_data(tmp_path):
    """Silently returning the points that survived is the dangerous failure: the metrics
    all compute happily on a waveform that stops early."""
    good = (FX / "rc_tran.raw").read_bytes()
    p = tmp_path / "cut.raw"
    p.write_bytes(good[:len(good) // 2])
    with pytest.raises(ValueError, match="truncated"):
        raw.read_raw(p)

    p.write_bytes(b"not a raw file at all")
    with pytest.raises(ValueError, match="Binary|Values"):
        raw.read_raw(p)


def test_variable_lookup_is_by_node_name_and_fails_loudly():
    w = raw.read_raw(FX / "rc_tran.raw")[0]
    assert w["out"] is w["V(out)"] is w["v(OUT)"]
    with pytest.raises(KeyError, match="no variable"):
        w["nope"]
    assert w.get("nope", 7) == 7


def test_signal_defaults_to_v_out():
    w = raw.read_raw(FX / "rc_tran.raw")[0]
    assert w.signal()[0] == "V(out)"
    assert w.signal("in")[0] == "V(in)"


# ------------------------------------------------------------------------ token guard

def test_summarize_returns_scalars_not_waveforms():
    """The whole point of this module: a 5018-point run must come back as numbers. If a
    metric ever returns its input array, an optimization loop quietly costs 1000x."""
    for name in ("rc_ac", "ac_loop", "rc_tran", "rlc_tran", "op_mos", "step_ac"):
        s = raw.summarize(FX / f"{name}.raw")
        for r in s["results"]:
            for key, v in r.items():
                if isinstance(v, list):
                    assert len(v) <= 32, f"{name}.{key} returned {len(v)} values"
        assert len(str(s)) < 4000, f"{name} summary is {len(str(s))} chars"


def test_summarize_reports_a_bad_signal_per_step_instead_of_failing_the_lot():
    s = raw.summarize(FX / "step_ac.raw", name="nosuchnode")
    assert len(s["results"]) == 3
    assert all("error" in r for r in s["results"])


def test_rounding_keeps_twelve_figures_and_survives_specials():
    assert raw._r(1 / 3) == 0.333333333333
    assert raw._r(1234567.89012345) == 1234567.89012
    assert raw._r(0.0) == 0.0 and raw._r(None) is None
    assert raw._r(float("nan")) is None and raw._r(float("inf")) is None
    assert raw._r(True) is True
