"""LTspice `.raw` reader and waveform metrics.

ponytail: stdlib only, `struct` and `math`. This module is what `.meas` cannot do --
THD, settling, phase margin -- and it has to import on a machine with no numpy, or the
whole server stops loading. numpy and matplotlib live in `plots.py`, which is optional.

Three facts about real LTspice `.raw` files, each established by running LTspice and
each capable of producing a confidently wrong number if you assume otherwise:

1. **A transient time column contains negative values.** Point 4 of a real run reads
   `-1.0505e-05` sitting between `1.01e-06` and `2e-05`. LTspice uses the sign bit as a
   flag on some time samples; the magnitude is the time. `abs()` restores monotonicity.
   Left alone it corrupts rise time, settling and any resampling -- and the waveform
   still plots, because a plotter sorts by x.

   That correction belongs on the `time` axis and on the complex AC `frequency` column,
   and *nowhere else* -- see `_axis()`. A `.dc` sweep and a `.step temp` axis carry real
   negative values, and blanket `abs()` silently read `.dc V1 -5 5` back as
   `5 4 3 2 1 0 1 2 3 4 5` and turned the -40 C corner into +40 C.

2. **Transient samples are NOT uniformly spaced**, even with compression off: a real
   1 kHz run had `dt` ranging over five orders of magnitude (1e-11 to 1e-6). Feeding
   that array straight to an FFT gives 27.5% THD on a signal whose true THD is 5.385%.
   `resample()` exists for exactly this and `thd()` always calls it.

3. **LTspice compresses transient waveforms by default**, decimating the file (a 5 ms
   run came back as 275 points). The metrics here cannot recover what was thrown away.
   Put `.options plotwinsize=0` in any netlist you intend to measure this way -- see
   `tests/fixtures/*.cir`.

`.step` runs concatenate every dataset into one file with the sweep axis restarting, so
`read_raw` always returns a *list* of `Wave`. A stepped `.op` is the exception: LTspice
stores it as one dataset whose sweep axis *is* the step variable, so 30 Monte Carlo runs
arrive as a single 30-point wave -- which is what makes Monte Carlo one LTspice launch.
"""

from __future__ import annotations

import cmath
import dataclasses
import math
import re
import struct
from pathlib import Path

# `Binary:` / `Values:` terminate the header. LTspice writes the header UTF-16LE.
_BIN = b"Binary:"
_ASCII = b"Values:"


@dataclasses.dataclass
class Wave:
    """One dataset. `data` holds complex for an AC run, float otherwise.

    `x` is the sweep axis as a positive float -- time, frequency or the swept source --
    already corrected for fact (1) above. `names[0]` is the axis's own name.
    """

    plotname: str
    flags: list[str]
    names: list[str]
    kinds: list[str]
    x: list[float]
    data: dict[str, list]
    step: int = 0
    step_params: str = ""

    @property
    def analysis(self) -> str:
        """`ac`, `tran`, `dc`, `op` or `noise` -- what the metrics dispatch on."""
        p = self.plotname.lower()
        for key, name in (("ac analysis", "ac"), ("transient", "tran"),
                          ("dc transfer", "dc"), ("operating point", "op"),
                          ("noise", "noise")):
            if key in p:
                return name
        return p

    @property
    def complex(self) -> bool:
        return "complex" in self.flags

    def _find(self, name: str) -> str:
        """Resolve a caller's name to the variable as LTspice spells it."""
        for cand in (name, f"V({name})", f"I({name})"):
            k = cand.lower()
            for n in self.names:
                if n.lower() == k:
                    return n
        raise KeyError(f"no variable {name!r} in {self.plotname}; has {self.names}")

    def __getitem__(self, name: str) -> list:
        """Case-insensitive, and `out` also finds `V(out)`.

        LTspice spells nodes `V(out)` in the `.raw` but a caller thinks in node names,
        and case survives round trips inconsistently.
        """
        return self.data[self._find(name)]

    def get(self, name: str, default=None):
        try:
            return self[name]
        except KeyError:
            return default

    def signal(self, name: str | None = None) -> tuple[str, list]:
        """The variable to measure. Defaults to `V(out)`, else the first voltage."""
        if name:
            n = self._find(name)
            return n, self.data[n]
        for n in self.names:
            if n.lower() == "v(out)":
                return n, self.data[n]
        for n, k in zip(self.names, self.kinds):
            if k == "voltage" and n != self.names[0]:
                return n, self.data[n]
        raise KeyError(f"no voltage to measure in {self.names}")


def _decode(b: bytes) -> str:
    """LTspice writes UTF-16LE; ngspice and hand-written `.raw` files are ASCII."""
    return b.decode("utf-16-le") if b[1:2] == b"\x00" else b.decode("latin-1")


def _split_header(blob: bytes) -> tuple[str, bytes, bool]:
    for marker, binary in ((_BIN, True), (_ASCII, False)):
        for enc in ("utf-16-le", "latin-1"):
            tag = marker.decode().encode(enc)
            i = blob.find(tag)
            if i >= 0:
                nl = blob.find("\n".encode(enc), i)
                return _decode(blob[:i]), blob[nl + len("\n".encode(enc)):], binary
    raise ValueError("not a SPICE .raw file: no 'Binary:' or 'Values:' section")


def read_raw(path) -> list[Wave]:
    """Parse a `.raw`. Always a list: one `Wave` per `.step` dataset.

    A `.step` run writes every dataset into one file with the sweep axis restarting, and
    the header reports only the total point count -- so the datasets are split on the
    axis reversing direction.
      ponytail: that heuristic breaks on a non-monotonic sweep (a `.tran` with UIC that
      backs up, a hand-built PWL sweep). Read the offsets from the `.log` if one shows
      up. Step *labels* already come from the `.log` when it is next to the `.raw`.
    """
    path = Path(path)
    head, body, binary = _split_header(path.read_bytes())
    meta = {}
    var_lines = []
    in_vars = False
    for line in head.splitlines():
        if line[:1] in ("\t", " ") and in_vars:
            var_lines.append(line)
            continue
        key, _, val = line.partition(":")
        key = key.strip().lower()
        in_vars = key == "variables"
        if not in_vars and val:
            meta[key] = val.strip()

    nv = int(meta.get("no. variables", 0))
    npt = int(meta.get("no. points", 0))
    flags = meta.get("flags", "real").lower().split()
    names, kinds = [], []
    for line in var_lines[:nv]:
        f = line.split()
        names.append(f[1])
        kinds.append(f[2].lower() if len(f) > 2 else "")
    if len(names) != nv:
        raise ValueError(f"{path.name}: header claims {nv} variables, lists {len(names)}")

    cplx = "complex" in flags
    cols = _read_binary(body, nv, npt, cplx) if binary else _read_ascii(body, nv, npt)

    plotname = meta.get("plotname", "")
    labels = _step_labels(path) if "stepped" in flags else []
    cols[0] = _axis(cols[0], kinds[0] if kinds else "", cplx)
    waves = []
    for s, (lo, hi) in enumerate(_datasets(cols[0], "stepped" in flags)):
        axis = cols[0][lo:hi]
        data = {n: c[lo:hi] for n, c in zip(names, cols)}
        waves.append(Wave(plotname=plotname, flags=flags, names=names, kinds=kinds,
                          x=axis, data=data, step=s,
                          step_params=labels[s] if s < len(labels) else ""))
    return waves


def _axis(col: list, kind: str, cplx: bool) -> list[float]:
    """The sweep axis as a real float, correcting only the axes that need correcting.

    `abs()` belongs on exactly two axes and is a bug on every other one:

    * `time` -- LTspice sets the sign bit as a flag on some samples (fact 1 above), so
      the magnitude is the time and `abs()` restores monotonicity.
    * an AC `frequency` column, which is stored complex with a zero imaginary part;
      `abs()` is how it becomes a float.

    A `.dc` sweep axis and a `.step` parameter or temperature axis carry real negative
    values. Blanket `abs()` read a `.dc -5 5` sweep back as `5 4 3 2 1 0 1 2 3 4 5` and
    turned `.step temp list -40` into `+40` -- both of which plot and measure without
    complaint, at the wrong operating point.
    """
    if kind == "time" or cplx:
        return [abs(v) for v in col]
    return [v.real if isinstance(v, complex) else v for v in col]


def _read_binary(body: bytes, nv: int, npt: int, cplx: bool) -> list[list]:
    """Complex runs store every column as two float64. Real runs store the axis as
    float64 and every other column as float32 -- so the row stride is not `nv * 8`."""
    cols: list[list] = [[] for _ in range(nv)]
    if cplx:
        need = npt * nv * 16
        if len(body) < need:
            raise ValueError(f"truncated .raw: want {need} bytes of data, have {len(body)}")
        for k in range(npt):
            row = struct.unpack_from(f"<{2 * nv}d", body, k * nv * 16)
            for j in range(nv):
                cols[j].append(complex(row[2 * j], row[2 * j + 1]))
        return cols
    stride = 8 + (nv - 1) * 4
    need = npt * stride
    if len(body) < need:
        raise ValueError(f"truncated .raw: want {need} bytes of data, have {len(body)}")
    for k in range(npt):
        o = k * stride
        cols[0].append(struct.unpack_from("<d", body, o)[0])
        rest = struct.unpack_from(f"<{nv - 1}f", body, o + 8) if nv > 1 else ()
        for j, v in enumerate(rest, start=1):
            cols[j].append(v)
    return cols


def _read_ascii(body: bytes, nv: int, npt: int) -> list[list]:
    """`Values:` form. Point index and axis share a line, then one value per line."""
    toks = _decode(body).split() if body[1:2] == b"\x00" else body.decode("latin-1").split()
    cols: list[list] = [[] for _ in range(nv)]
    i = 0
    for _ in range(npt):
        i += 1                                       # the point index, not data
        for j in range(nv):
            t = toks[i]
            i += 1
            cols[j].append(complex(*map(float, t.split(","))) if "," in t else float(t))
    return cols


def _datasets(axis: list, stepped: bool) -> list[tuple[int, int]]:
    """Index ranges of each `.step` dataset, found where the sweep axis reverses.

    Runs on the already-corrected axis from `_axis()`, so it compares values directly.
    An `abs()` here would cut a bipolar `.dc -5 5` sweep into six datasets at the zero
    crossing, because `abs()` makes a monotonic sweep look like it reverses.
    """
    if not stepped or len(axis) < 2:
        return [(0, len(axis))]
    a = axis
    back = (lambda i: a[i] < a[i - 1]) if a[1] >= a[0] else (lambda i: a[i] > a[i - 1])
    cuts = [0] + [i for i in range(1, len(a)) if back(i)] + [len(a)]
    return list(zip(cuts, cuts[1:]))


def _step_labels(raw_path: Path) -> list[str]:
    """`.step r=1000` lines from the sibling `.log`, in dataset order.

    Without them a caller gets `step=0,1,2` and no idea which parameters those were.
    """
    log = raw_path.with_suffix(".log")
    if not log.is_file():
        return []
    text = log.read_text(encoding="utf-8", errors="replace")
    if "\x00" in text:                               # some LTspice logs are UTF-16LE
        text = log.read_bytes().decode("utf-16-le", errors="replace")
    return re.findall(r"^\.step\s+(.+?)\s*$", text, re.M)


# ---------------------------------------------------------------- interpolation helpers

def _interp_x(xs: list[float], ys: list[float], target: float,
              log_x: bool = False) -> float | None:
    """First x where y crosses `target`, linearly interpolated. None if it never does."""
    for x0, x1, y0, y1 in zip(xs, xs[1:], ys, ys[1:]):
        if (y0 - target) * (y1 - target) <= 0 and y0 != y1:
            f = (target - y0) / (y1 - y0)
            if log_x and x0 > 0 and x1 > 0:
                return x0 * (x1 / x0) ** f
            return x0 + f * (x1 - x0)
    return None


def _interp_y(xs: list[float], ys: list[float], target: float,
              log_x: bool = False) -> float:
    """y at x=`target`, linearly interpolated (in log-x if asked)."""
    if target <= xs[0]:
        return ys[0]
    if target >= xs[-1]:
        return ys[-1]
    for x0, x1, y0, y1 in zip(xs, xs[1:], ys, ys[1:]):
        if x0 <= target <= x1:
            if x1 == x0:
                return y0
            f = ((math.log(target / x0) / math.log(x1 / x0))
                 if log_x and x0 > 0 else (target - x0) / (x1 - x0))
            return y0 + f * (y1 - y0)
    return ys[-1]


def resample(x: list[float], y: list[float], n: int,
             t0: float | None = None, t1: float | None = None) -> tuple[float, list[float]]:
    """`n` samples of y on a uniform grid over [t0, t1). Returns (dt, samples).

    Mandatory before any FFT: see fact (2) in the module docstring. Linear interpolation
    is enough because LTspice's own solver output is piecewise-linear between samples.
    """
    t0 = x[0] if t0 is None else t0
    t1 = x[-1] if t1 is None else t1
    if n < 2 or t1 <= t0:
        raise ValueError(f"cannot resample {n} points over [{t0}, {t1}]")
    dt = (t1 - t0) / n
    out, j, m = [], 0, len(x)
    for k in range(n):
        t = t0 + k * dt
        while j + 2 < m and x[j + 1] < t:
            j += 1
        x0, x1 = x[j], x[j + 1]
        out.append(y[j] if x1 == x0 else
                   y[j] + (t - x0) * (y[j + 1] - y[j]) / (x1 - x0))
    return dt, out


# ------------------------------------------------------------------------- AC metrics

def ac_metrics(w: Wave, name: str | None = None, ref_db: float | None = None) -> dict:
    """Gain/phase metrics from an `.ac` run.

    `phase_margin_deg` and `gain_margin_db` read the trace as a *loop gain*: they are
    `180 + phase(UGF)` and the attenuation where the phase crosses -180. Handing this a
    closed-loop response gives you arithmetic, not stability -- simulate the loop.
    """
    if not w.complex:
        raise ValueError(f"ac_metrics needs a complex AC run, got {w.plotname!r}")
    label, vals = w.signal(name)
    f = w.x
    mag = [abs(v) for v in vals]
    mag_db = [20 * math.log10(m) if m > 0 else -400.0 for m in mag]
    ph = _unwrap([math.degrees(cmath.phase(v)) for v in vals])

    ipk = max(range(len(mag)), key=mag.__getitem__)
    ref = mag_db[0] if ref_db is None else ref_db
    ugf = _interp_x(f, mag_db, 0.0, log_x=True)
    f180 = _interp_x(f, ph, -180.0, log_x=True)
    out = {
        "signal": label,
        "dc_gain_db": _r(mag_db[0]),
        "peak_gain_db": _r(mag_db[ipk]),
        "f_peak_hz": _r(f[ipk]),
        "bw_3db_hz": _r(_interp_x(f, mag_db, ref - 3.0, log_x=True)),
        "ugf_hz": _r(ugf),
        "phase_margin_deg": _r(180.0 + _interp_y(f, ph, ugf, log_x=True)) if ugf else None,
        "gain_margin_db": _r(-_interp_y(f, mag_db, f180, log_x=True)) if f180 else None,
        "f_phase_180_hz": _r(f180),
        "n_points": len(f),
    }
    if ugf is None:
        out["note"] = ("gain never crosses 0 dB in the swept range, so there is no UGF "
                       "and no phase margin; widen .ac or check this is a loop gain")
    return out


def _unwrap(deg: list[float]) -> list[float]:
    """Remove the +-180 wraps `atan2` introduces.

    Without this a phase margin reads off a trace that jumps 360 degrees mid-sweep, and
    the -180 crossing lands wherever the branch cut happened to fall.
    """
    out = [deg[0]]
    off = 0.0
    for a, b in zip(deg, deg[1:]):
        d = b - a
        if d > 180:
            off -= 360
        elif d < -180:
            off += 360
        out.append(b + off)
    return out


# -------------------------------------------------------------------- transient metrics

def tran_metrics(w: Wave, name: str | None = None, settle_pct: tuple = (1.0, 0.1),
                 final: float | None = None) -> dict:
    """Step-response metrics from a `.tran` run.

    `final` defaults to the last sample. Settling is the *last* time the trace leaves
    the band, not the first time it enters -- a ringing waveform re-crosses, and the
    first entry reports a settling time the circuit has not actually reached.
    """
    if w.complex:
        raise ValueError(f"tran_metrics needs a real run, got {w.plotname!r}")
    label, y = w.signal(name)
    t = w.x
    if len(t) < 3:
        raise ValueError(f"{label}: {len(t)} points is not a transient")
    fin = y[-1] if final is None else final
    start = y[0]
    span = fin - start

    slopes = [(b - a) / (tb - ta) for a, b, ta, tb in zip(y, y[1:], t, t[1:]) if tb > ta]
    ipk = max(range(len(y)), key=y.__getitem__)
    itr = min(range(len(y)), key=y.__getitem__)

    out = {
        "signal": label,
        "t_start": _r(t[0]), "t_end": _r(t[-1]), "n_points": len(t),
        "initial": _r(start), "final": _r(fin),
        "v_max": _r(y[ipk]), "t_max": _r(t[ipk]),
        "v_min": _r(y[itr]), "t_min": _r(t[itr]),
        "slew_pos_v_per_s": _r(max(slopes)) if slopes else None,
        "slew_neg_v_per_s": _r(min(slopes)) if slopes else None,
    }
    if span == 0:
        out["note"] = "flat trace (final == initial): no rise, overshoot or settling"
        return out

    lo, hi = start + 0.1 * span, start + 0.9 * span
    t10, t90 = _interp_x(t, y, lo), _interp_x(t, y, hi)
    edge = "rise" if span > 0 else "fall"
    out[f"t_{edge}_10_90"] = _r(t90 - t10) if (t10 is not None and t90 is not None) else None
    out["overshoot_pct"] = _r(max(0.0, (y[ipk] - fin) / abs(span) * 100) if span > 0
                              else max(0.0, (fin - y[itr]) / abs(span) * 100))

    for pct in settle_pct:
        band = abs(span) * pct / 100
        out[f"t_settle_{_tag(pct)}"] = _r(_settle(t, y, fin, band))
        out[f"settled_{_tag(pct)}"] = _settle(t, y, fin, band) is not None
    return out


def _settle(t: list[float], y: list[float], fin: float, band: float) -> float | None:
    """Last time |y - final| exceeds `band`, interpolated. None if never inside it.

    Reported as None rather than the record end when the run is too short: a 500 us
    capture of a 100 us time constant simply has not reached 0.1%, and returning 500 us
    would be a measurement of the `.tran` statement instead of the circuit.
    """
    last = None
    for i in range(len(t) - 1, -1, -1):
        if abs(y[i] - fin) > band:
            last = i
            break
    if last is None:
        return t[0]                                  # inside the band the whole time
    if last >= len(t) - 1:
        return None                                  # still outside at the last sample
    y0, y1 = y[last], y[last + 1]
    target = fin + band if y0 > fin else fin - band
    if y1 == y0:
        return t[last + 1]
    return t[last] + (target - y0) / (y1 - y0) * (t[last + 1] - t[last])


# ------------------------------------------------------------- distortion (FFT metrics)

def thd(w: Wave, name: str | None = None, f0: float | None = None, harmonics: int = 9,
        skip_cycles: float = 1.0, n: int = 4096) -> dict:
    """THD and SINAD over an integer number of steady-state cycles.

    Only the harmonic bins are needed, so this is a direct DFT at those bins rather than
    a full FFT -- no numpy, and every bin is exact instead of interpolated. Total power
    comes from Parseval on the same window, which is what makes SINAD meaningful.

    The window is trimmed to a whole number of `f0` cycles. Leakage from a partial cycle
    puts skirts around the fundamental that read as distortion: it inflates THD without
    ever looking wrong.
    """
    label, y = w.signal(name)
    t = w.x
    if t[-1] <= t[0]:
        raise ValueError(f"{label}: transient has no duration")

    if f0 is None:
        f0 = _fundamental(t, y, n)
    if not f0 or f0 <= 0:
        raise ValueError("could not find a fundamental; pass f0=")

    t0 = min(t[0] + skip_cycles / f0, t[-1])
    cycles = math.floor((t[-1] - t0) * f0)
    if cycles < 2:
        raise ValueError(f"need >=2 whole cycles of {f0:g} Hz after skipping "
                         f"{skip_cycles}; the run holds {(t[-1] - t[0]) * f0:.2f}")
    t1 = t0 + cycles / f0
    dt, ys = resample(t, y, n, t0, t1)

    dc = sum(ys) / n
    ac = [v - dc for v in ys]
    total = sum(v * v for v in ac) / n                # mean square of everything but DC
    amps = [_bin_amp(ac, cycles * k) for k in range(1, harmonics + 1)]
    fund = amps[0]
    if fund <= 0:
        raise ValueError(f"no energy at {f0:g} Hz in {label}")
    dist = math.sqrt(sum(a * a for a in amps[1:]))
    resid = max(total - fund * fund / 2, 0.0)         # noise + distortion power

    return {
        "signal": label, "f0_hz": _r(f0), "cycles": cycles,
        "window": [_r(t0), _r(t1)], "n_fft": n,
        "thd_pct": _r(dist / fund * 100),
        "thd_db": _r(20 * math.log10(dist / fund)) if dist > 0 else None,
        "sinad_db": _r(10 * math.log10((fund * fund / 2) / resid)) if resid > 0 else None,
        "dc_offset": _r(dc),
        "fundamental_v": _r(fund),
        "harmonics_v": [_r(a) for a in amps],
        "harmonic_db": [_r(20 * math.log10(a / fund)) if a > 0 else None for a in amps],
    }


def _bin_amp(y: list[float], k: int) -> float:
    """Amplitude at exactly bin `k` of `y` -- a one-bin DFT (Goertzel by hand).

    O(n) per harmonic and there are ~9 of them, so the whole spectrum is never built.
    """
    n = len(y)
    if k <= 0 or k >= n // 2:
        return 0.0
    w = 2 * math.pi * k / n
    re = im = 0.0
    for i, v in enumerate(y):
        re += v * math.cos(w * i)
        im -= v * math.sin(w * i)
    return 2 * math.hypot(re, im) / n


def _fundamental(t: list[float], y: list[float], n: int) -> float | None:
    """Largest non-DC spectral peak, found by scanning bins of one uniform resample.

    ponytail: O(n * bins) brute force over the low half of the spectrum. It is ~1e6
    multiplies and runs in well under a second; pass `f0=` if you already know it.
    """
    _, ys = resample(t, y, n)
    dc = sum(ys) / n
    ac = [v - dc for v in ys]
    span = t[-1] - t[0]
    best, bestk = 0.0, 0
    for k in range(1, n // 4):
        a = _bin_amp(ac, k)
        if a > best:
            best, bestk = a, k
    return bestk / span if bestk else None


def _tag(pct: float) -> str:
    return f"{pct:g}".replace(".", "p") + "pct"


def _r(v):
    """Round to 12 significant figures. float64 noise in the 16th digit is not a
    measurement, and it makes every returned metric unreadable in a tool response."""
    if v is None or isinstance(v, bool):
        return v
    if not isinstance(v, (int, float)) or not math.isfinite(v):
        return None if isinstance(v, float) else v
    return 0.0 if v == 0 else round(v, 11 - int(math.floor(math.log10(abs(v)))))


def summarize(path, name: str | None = None) -> dict:
    """Every metric that applies to whatever analysis this `.raw` holds.

    The tool-facing entry point: scalars only, no arrays beyond the ~9 harmonic
    amplitudes, so a full answer costs a few hundred tokens instead of a waveform.
    """
    waves = read_raw(path)
    out = {"path": str(path), "analysis": waves[0].analysis, "steps": len(waves),
            "variables": waves[0].names, "results": []}
    for w in waves:
        r: dict = {"step": w.step}
        if w.step_params:
            r["step_params"] = w.step_params
        try:
            if w.analysis == "ac":
                r.update(ac_metrics(w, name))
            elif w.analysis == "tran":
                r.update(tran_metrics(w, name))
            elif w.analysis == "op":
                r["operating_point"] = {n: _r(w.data[n][0]) for n in w.names}
            else:
                lab, v = w.signal(name)
                r.update({"signal": lab, "n_points": len(w.x),
                          "x_range": [_r(w.x[0]), _r(w.x[-1])],
                          "y_range": [_r(min(v)), _r(max(v))]})
        except (ValueError, KeyError) as e:
            r["error"] = str(e)
        out["results"].append(r)
    return out


if __name__ == "__main__":                            # smallest useful self-check
    import sys
    for p in sys.argv[1:]:
        import json
        print(json.dumps(summarize(p), indent=2))
