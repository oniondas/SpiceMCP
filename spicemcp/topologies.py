"""Topology library: tested circuits with hand-calculated starting values.

Real design starts from a known topology sized by textbook equations, and only then
optimizes. Each template here turns a short spec ("fc = 10 kHz") into:

  * an LTspice netlist with {PARAM} placeholders and the `.meas` lines already written,
  * the parameter ranges, goals and limits for `engine.start_run`,
  * a hand-sized start point from the design equations, with the working shown,
    so the optimizer refines a design that is already close instead of searching blind.

Every template is verified in the test suite by simulating the hand-sized start (with
ngspice where LTspice is not installed) and checking it lands near the spec.

Metric names: AC magnitudes are in dB (`*_db`) because that is what LTspice reports;
`param_<NAME>` metrics price a part directly (see `engine.evaluate`).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Callable

TWO_PI = 2 * math.pi


class TemplateError(ValueError):
    pass


@dataclass
class SpecField:
    name: str
    unit: str
    help: str
    default: float | None = None          # None = required


@dataclass
class Design:
    netlist: str
    params: list[dict]                      # [{"name","min","max"} | {"name","values"}]
    goals: list[dict]
    limits: list[dict]                      # [{"metric","min","max"}]
    start: dict[str, float]
    working: list[str]                      # the hand calculation, line by line
    circuit_type: str
    tech: dict = field(default_factory=dict)


@dataclass
class Template:
    name: str
    title: str
    circuit_type: str
    summary: str
    spec: list[SpecField]
    build: Callable[[dict], Design]

    def resolve(self, spec: dict | None) -> dict:
        spec = {k.lower(): float(v) for k, v in (spec or {}).items()}
        known = {f.name for f in self.spec}
        extra = sorted(set(spec) - known)
        if extra:
            raise TemplateError(f"{self.name}: unknown spec keys {extra}; "
                                f"expected {sorted(known)}")
        out = {}
        for f in self.spec:
            if f.name in spec:
                out[f.name] = spec[f.name]
            elif f.default is not None:
                out[f.name] = f.default
            else:
                raise TemplateError(f"{self.name}: spec needs {f.name} ({f.unit}): {f.help}")
            if out[f.name] <= 0 and f.name not in ("q",):
                raise TemplateError(f"{self.name}: {f.name} must be positive")
        return out

    def design(self, spec: dict | None = None) -> Design:
        return self.build(self.resolve(spec))

    def describe(self) -> dict:
        return {"name": self.name, "title": self.title, "circuit_type": self.circuit_type,
                "summary": self.summary,
                "spec": {f.name: (f"{f.unit}, {f.help}" + (
                    f" (default {f.default:g})" if f.default is not None else " (required)"))
                         for f in self.spec}}


def _rng(v: float, lo: float = 0.1, hi: float = 10.0) -> tuple[float, float]:
    """A search range of one decade either side of the hand-sized value."""
    return v * lo, v * hi


def _eng(v: float, unit: str = "") -> str:
    if v == 0:
        return f"0 {unit}".strip()
    for f, p in ((1e9, "G"), (1e6, "M"), (1e3, "k"), (1, ""), (1e-3, "m"), (1e-6, "µ"),
                 (1e-9, "n"), (1e-12, "p")):
        if abs(v) >= f:
            return f"{v / f:.3g} {p}{unit}"
    return f"{v:.3g} {unit}"


# --- 1. RC low-pass ------------------------------------------------------------------

def _rc_lowpass(s: dict) -> Design:
    fc = s["fc"]
    r = 10e3
    c = 1 / (TWO_PI * fc * r)
    if c < 100e-12:                 # tiny cap: parasitics dominate, lower the R
        c = 100e-12
        r = 1 / (TWO_PI * fc * c)
    if c > 10e-6:                   # electrolytic territory: raise the R instead
        c = 10e-6
        r = 1 / (TWO_PI * fc * c)
    net = f"""* RC low-pass (SpiceMCP template rc_lowpass), fc target {fc:g} Hz
V1 in 0 AC 1
R1 in out {{R1}}
C1 out 0 {{C1}}
.ac dec 100 {fc / 1000:g} {fc * 1000:g}
.meas AC fc WHEN mag(V(out))=0.7071 FALL=1
.meas AC atten10_db FIND mag(V(out)) AT {fc * 10:g}
.end
"""
    r_lo, r_hi = max(r / 100, 10.0), min(r * 10, 1e6)
    return Design(
        net, [{"name": "R1", "min": r_lo, "max": r_hi},
              {"name": "C1", "min": 1 / (TWO_PI * fc * r_hi), "max": 1 / (TWO_PI * fc * r_lo)}],
        goals=[{"metric": "param_c1", "direction": "min", "ref": c}],
        limits=[{"metric": "fc", "min": fc * 0.98, "max": fc * 1.02}],
        start={"R1": r, "C1": c},
        working=[f"fc = 1 / (2·π·R·C). Pick R = {_eng(r, 'Ω')} (low noise, light load).",
                 f"C = 1 / (2·π·{fc:g}·{r:.4g}) = {_eng(c, 'F')}.",
                 f"Search R from {_eng(r_lo, 'Ω')} to {_eng(r_hi, 'Ω')}: higher R means "
                 "more noise and more sensitivity to the load.",
                 "Goal: the smallest capacitor that keeps fc within ±2%."],
        circuit_type="filter",
        tech={"f_pass": fc})


# --- 2. Sallen-Key low-pass (unity gain) --------------------------------------------

def _sallen_key(s: dict) -> Design:
    fc, q = s["fc"], s["q"]
    w = TWO_PI * fc
    r = 10e3
    c1 = 2 * q / (w * r)            # feedback capacitor (to the output)
    c2 = 1 / (2 * q * w * r)        # capacitor to ground
    net = f"""* Sallen-Key low-pass, unity gain (SpiceMCP template sallen_key_lowpass)
* fc target {fc:g} Hz, Q {q:g}; ideal op-amp as a high-gain follower
V1 in 0 AC 1
R1 in a {{R1}}
R2 a b {{R2}}
C1 a out {{C1}}
C2 b 0 {{C2}}
E1 out 0 b out 1e6
.ac dec 100 {fc / 100:g} {fc * 100:g}
.meas AC f3db WHEN mag(V(out))=0.7071 FALL=1
.meas AC peak_db MAX mag(V(out))
.meas AC atten10_db FIND mag(V(out)) AT {fc * 10:g}
.meas AC g_half_db FIND mag(V(out)) AT {fc / 2:g}
.end
"""
    params = [{"name": "R1", "min": 1e3, "max": 100e3}, {"name": "R2", "min": 1e3, "max": 100e3},
              {"name": "C1", "min": c1 / 10, "max": c1 * 10},
              {"name": "C2", "min": c2 / 10, "max": c2 * 10}]
    # For Q = 0.707 the -3 dB point IS f0; for other Q it moves, so only limit it then.
    limits = [{"metric": "peak_db", "max": 20 * math.log10(max(q * q / math.sqrt(q * q - 0.25)
                                                              if q > 0.7072 else 1.0, 1.0)) + 0.1}]
    if abs(q - 1 / math.sqrt(2)) < 0.01:
        # Butterworth: -3 dB at fc, flat passband (-0.26 dB at fc/2 ideally), and the
        # full 40 dB/decade beyond. Without the passband limit an over-damped (low-Q)
        # filter also passes the fc test, with a droopy response nobody asked for.
        limits += [{"metric": "f3db", "min": fc * 0.97, "max": fc * 1.03},
                   {"metric": "g_half_db", "min": -0.5},
                   {"metric": "atten10_db", "max": -39.0}]
    return Design(
        net, params,
        goals=[{"metric": "param_c1", "direction": "min", "ref": c1}],
        limits=limits,
        start={"R1": r, "R2": r, "C1": c1, "C2": c2},
        working=[f"Equal resistors R1 = R2 = {_eng(r, 'Ω')}, ω0 = 2·π·{fc:g}.",
                 f"C1 = 2·Q / (ω0·R) = {_eng(c1, 'F')} (feedback cap).",
                 f"C2 = 1 / (2·Q·ω0·R) = {_eng(c2, 'F')} (cap to ground).",
                 f"Q = {q:g} (0.707 = Butterworth, maximally flat)."],
        circuit_type="filter",
        tech={"f_pass": fc})


# --- 3. Non-inverting amplifier with a real (finite-GBW) op-amp ----------------------

def _noninv_amp(s: dict) -> Design:
    g, bw, gbw = s["gain"], s["bw"], s["gbw"]
    if g < 1:
        raise TemplateError("noninverting_amp: gain must be >= 1 V/V")
    r1 = 10e3
    r2 = r1 * (g - 1) if g > 1 else 0.0
    if g * bw > gbw:
        raise TemplateError(f"noninverting_amp: gain x bw = {g * bw:.3g} Hz exceeds the "
                            f"op-amp GBW {gbw:.3g} Hz; raise gbw or lower gain or bw")
    g_db = 20 * math.log10(g)
    a0, rp = 1e5, 1e3
    cp = a0 / (TWO_PI * gbw * rp)
    net = f"""* Non-inverting amplifier (SpiceMCP template noninverting_amp)
* gain target {g:g} V/V, op-amp: A0 = 100 dB, GBW = {gbw:g} Hz (single pole)
V1 in 0 AC 1
E1 x 0 in inn {a0:g}
Rp x y {rp:g}
Cp y 0 {cp:.6g}
E2 out 0 y 0 1
R2 out inn {{R2}}
R1 inn 0 {{R1}}
RL out 0 10k
.ac dec 100 10 {gbw * 10:g}
.meas AC gain_db FIND mag(V(out)) AT 10
.meas AC bw WHEN mag(V(out))={0.7071 * g:.6g} FALL=1
.end
"""
    return Design(
        net, [{"name": "R1", "min": 1e3, "max": 100e3},
              {"name": "R2", "min": max(r2 / 10, 10.0), "max": max(r2 * 10, 1e4)}],
        goals=[{"metric": "bw", "direction": "max", "ref": bw}],
        limits=[{"metric": "gain_db", "min": g_db - 0.1, "max": g_db + 0.1},
                {"metric": "bw", "min": bw}],
        start={"R1": r1, "R2": max(r2, 10.0)},
        working=[f"Gain = 1 + R2/R1. Pick R1 = {_eng(r1, 'Ω')}.",
                 f"R2 = R1·(G − 1) = {_eng(r2, 'Ω')}.",
                 f"Bandwidth ≈ GBW / G = {gbw:g} / {g:g} = {_eng(gbw / g, 'Hz')} "
                 f"(spec {_eng(bw, 'Hz')}).",
                 "The 10 kΩ load and the R2+R1 divider also load the output."],
        circuit_type="opamp",
        tech={"gbw": gbw})


# --- 4. LDO regulator ----------------------------------------------------------------

def _ldo(s: dict) -> Design:
    vin, vout, iload, vdo = s["vin"], s["vout"], s["iload"], s["dropout"]
    vref = 1.2
    if vout <= vref:
        raise TemplateError(f"ldo: vout must be above the {vref} V reference")
    if vin < vout + vdo:
        raise TemplateError(f"ldo: vin {vin:g} V is below vout + dropout = {vout + vdo:g} V")
    r2 = 100e3
    r1 = r2 * (vout / vref - 1)
    kp, vt, length = 50e-6, 0.7, 1e-6
    vsg = vout + vdo                # gate pulled to 0 V at the dropout point
    w = 1.5 * iload * length / (kp * ((vsg - vt) * vdo - vdo * vdo / 2))
    vlow = vout + vdo
    net = f"""* PMOS LDO regulator (SpiceMCP template ldo)
* vout target {vout:g} V from {vin:g} V at {iload:g} A, dropout spec {vdo:g} V
* behavioural error amplifier (gain 2000, output clamped to the rails); resistive load
Vin in 0 {vin:g}
Vref ref 0 {vref:g}
B1 g 0 V=max(0, min(V(in), 2000*(V(fb)-V(ref))))
M1 out g in in PMOD W={{W}} L={length:g}
R1 out fb {{R1}}
R2 fb 0 {r2:g}
RL out 0 {vout / iload:.6g}
.model PMOD PMOS(KP={kp:g} VTO=-{vt:g} LAMBDA=0.02)
.dc Vin 0 {vin * 1.2:g} 0.01
.meas DC vout FIND V(out) AT {vin:g}
.meas DC vout_low FIND V(out) AT {vlow:g}
.meas DC vout_high FIND V(out) AT {vin * 1.15:g}
.end
"""
    return Design(
        net, [{"name": "R1", "min": r1 * 0.5, "max": r1 * 2},
              {"name": "W", "min": w / 10, "max": w * 10}],
        goals=[{"metric": "param_w", "direction": "min", "ref": w}],
        limits=[{"metric": "vout", "min": vout * 0.99, "max": vout * 1.01},
                {"metric": "vout_low", "min": vout * 0.98}],
        start={"R1": r1, "W": w},
        working=[f"Vout = Vref·(1 + R1/R2), Vref = {vref} V. R2 = {_eng(r2, 'Ω')}, "
                 f"R1 = {_eng(r1, 'Ω')}.",
                 f"At dropout ({_eng(vlow, 'V')} in) the gate is at 0 V, so Vsg = {vsg:.3g} V "
                 f"and the PMOS is in triode with Vsd = {vdo:g} V.",
                 f"I = KP·(W/L)·((Vsg−|Vt|)·Vsd − Vsd²/2) → W = {_eng(w, 'm')} "
                 f"(1.5× margin on {_eng(iload, 'A')}).",
                 "Goal: the smallest pass device that still meets dropout."],
        circuit_type="ldo",
        tech={"vin": vin, "vout": vout, "iload": iload})


# --- 5. Buck converter (open loop) ----------------------------------------------------

def _buck(s: dict) -> Design:
    vin, vout, iout, fsw, ripple = s["vin"], s["vout"], s["iout"], s["fsw"], s["ripple_mv"]
    if vout >= vin:
        raise TemplateError("buck: vout must be below vin")
    vd = 0.4
    d = (vout + vd) / (vin + vd)
    di = 0.3 * iout
    lval = (vin - vout) * d / (fsw * di)
    cval = di / (8 * fsw * ripple * 1e-3)
    rl = vout / iout
    t = 1 / fsw
    f0 = 1 / (TWO_PI * math.sqrt(lval * cval))
    tstop = max(60 / f0, 400 * t)
    win = min(100 * t, tstop / 4)
    net = f"""* Buck converter, open loop (SpiceMCP template buck)
* {vin:g} V -> {vout:g} V at {iout:g} A, {fsw / 1e3:g} kHz, ripple target {ripple:g} mV
.param duty={{D}}
Vin in 0 {vin:g}
Vg g 0 PULSE(0 5 0 5n 5n {{duty*{t:g}}} {t:g})
S1 in sw g 0 SWM
D1 0 sw DS
L1 sw out {{L}}
C1 out 0 {{C}}
RL out 0 {rl:g}
.model SWM SW(Ron=0.05 Roff=1Meg Vt=2.5 Vh=0)
.model DS D(Is=1e-6 Rs=0.02 N=1.2)
.tran {t / 200:g} {tstop:g} 0 {t / 200:g}
.meas TRAN vout AVG V(out) FROM {tstop - win:g} TO {tstop:g}
.meas TRAN vmax MAX V(out) FROM {tstop - win:g} TO {tstop:g}
.meas TRAN vmin MIN V(out) FROM {tstop - win:g} TO {tstop:g}
.meas TRAN iin AVG I(Vin) FROM {tstop - win:g} TO {tstop:g}
.meas TRAN ripple_mv PARAM (vmax-vmin)*1000
.meas TRAN eff PARAM vout*vout/{rl:g}/(-{vin:g}*iin)
.end
"""
    return Design(
        net, [{"name": "D", "min": max(d - 0.15, 0.05), "max": min(d + 0.15, 0.95)},
              {"name": "L", "min": lval / 10, "max": lval * 10},
              {"name": "C", "min": cval / 10, "max": cval * 10}],
        goals=[{"metric": "param_l", "direction": "min", "ref": lval, "weight": 1.0},
               {"metric": "param_c", "direction": "min", "ref": cval, "weight": 1.0}],
        limits=[{"metric": "vout", "min": vout * 0.98, "max": vout * 1.02},
                {"metric": "ripple_mv", "max": ripple}],
        start={"D": d, "L": lval, "C": cval},
        working=[f"Duty D = (Vout + Vd) / (Vin + Vd) = {d:.3f} (Vd = {vd} V diode).",
                 f"Ripple current ΔI = 30% of Iout = {_eng(di, 'A')}.",
                 f"L = (Vin − Vout)·D / (fsw·ΔI) = {_eng(lval, 'H')}.",
                 f"C = ΔI / (8·fsw·ΔV) = {_eng(cval, 'F')} for {ripple:g} mV ripple.",
                 f"LC corner {_eng(f0, 'Hz')}; simulate {_eng(tstop, 's')} and measure "
                 f"the last {_eng(win, 's')}.",
                 "Open loop: no feedback controller, so Vout depends on D and the load."],
        circuit_type="dcdc",
        tech={"vin": vin, "vout": vout, "iload": iout, "fsw": fsw})


TEMPLATES: dict[str, Template] = {t.name: t for t in [
    Template("rc_lowpass", "RC low-pass filter", "filter",
             "First-order passive low-pass. 20 dB/decade roll-off.",
             [SpecField("fc", "Hz", "-3 dB cutoff frequency")], _rc_lowpass),
    Template("sallen_key_lowpass", "Sallen-Key low-pass filter (2nd order)", "filter",
             "Unity-gain active low-pass, 40 dB/decade. Q 0.707 = Butterworth.",
             [SpecField("fc", "Hz", "cutoff frequency"),
              SpecField("q", "", "quality factor", 1 / math.sqrt(2))], _sallen_key),
    Template("noninverting_amp", "Non-inverting op-amp amplifier", "opamp",
             "Gain 1 + R2/R1 with a single-pole op-amp model.",
             [SpecField("gain", "V/V", "closed-loop gain (>= 1)"),
              SpecField("bw", "Hz", "minimum -3 dB bandwidth"),
              SpecField("gbw", "Hz", "op-amp gain-bandwidth product", 10e6)], _noninv_amp),
    Template("ldo", "PMOS LDO voltage regulator", "ldo",
             "Linear regulator: PMOS pass device, 1.2 V reference, resistor divider.",
             [SpecField("vin", "V", "nominal input voltage"),
              SpecField("vout", "V", "output voltage (> 1.2 V)"),
              SpecField("iload", "A", "load current"),
              SpecField("dropout", "V", "max input-output difference at full load", 0.3)],
             _ldo),
    Template("buck", "Buck DC-DC converter (open loop)", "dcdc",
             "Step-down switching converter: switch, diode, L, C.",
             [SpecField("vin", "V", "input voltage"), SpecField("vout", "V", "output voltage"),
              SpecField("iout", "A", "load current"),
              SpecField("fsw", "Hz", "switching frequency", 500e3),
              SpecField("ripple_mv", "mV", "max output ripple, peak to peak", 50)], _buck),
]}


def get(name: str) -> Template:
    t = TEMPLATES.get(name)
    if t is None:
        raise TemplateError(f"unknown template {name!r}; choose from {sorted(TEMPLATES)}")
    return t
