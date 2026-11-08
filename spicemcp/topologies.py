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

from . import opamp

TWO_PI = 2 * math.pi
CMP = "SPMCP_CMP"                   # the op-amp circuit, compensated fast, as a comparator


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


PM_MIN = 45.0                       # degrees: the usual floor for a feedback loop


def _loop_deck(title: str, circuit: str, ret: str, lo: float, hi: float) -> str:
    """A second deck: the circuit with its feedback broken at the amplifier input.

    `Lbrk` (1 kH) carries the DC bias, so the circuit sits at its real operating point,
    and opens the loop at every frequency swept. `Cinj` (1 kF) drives the amplifier input
    with 1 V AC. What comes back at `ret` is minus the loop gain, so its phase where its
    magnitude crosses 1 is the phase margin. `pm` is that crossing: `pm_deg` the margin,
    `ugf` the crossover frequency.
    """
    return (f"* {title}: loop gain, feedback broken at the amplifier input\n{circuit}"
            f"Vinj inj 0 AC 1\n.ac dec 50 {lo:g} {hi:g}\n"
            f".meas AC ugf WHEN mag(V({ret}))=1 FALL=1\n"
            f".meas AC pm FIND V({ret}) WHEN mag(V({ret}))=1 FALL=1\n.end\n")


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
    fc, q, cl = s["fc"], s["q"], s["cload"]
    w = TWO_PI * fc
    r = 10e3
    gbw = max(1e6, 100 * fc)        # the op-amp must be much faster than the filter
    c1 = 2 * q / (w * r)            # feedback capacitor (to the output)
    c2 = 1 / (2 * q * w * r)        # capacitor to ground
    net = f"""* Sallen-Key low-pass, unity gain (SpiceMCP template sallen_key_lowpass)
* fc target {fc:g} Hz, Q {q:g}; transistor-level op-amp on +-5 V as the follower
V1 in 0 AC 1
Vcc vcc 0 5
Vee vee 0 -5
R1 in a {{R1}}
R2 a b {{R2}}
C1 a out {{C1}}
C2 b 0 {{C2}}
X1 b out vcc vee out {opamp.NAME}
CL out 0 {cl:g}
{opamp.subckt(gbw, 10.0)}.ac dec 100 {fc / 100:g} {fc * 100:g}
.meas AC f3db WHEN mag(V(out))=0.7071 FALL=1
.meas AC peak_db MAX mag(V(out))
.meas AC atten10_db FIND mag(V(out)) AT {fc * 10:g}
.meas AC g_half_db FIND mag(V(out)) AT {fc / 2:g}
.end
""" + _loop_deck("Sallen-Key stability", f"""V1 in 0 0
Vcc vcc 0 5
Vee vee 0 -5
R1 in a {{R1}}
R2 a b {{R2}}
C1 a out {{C1}}
C2 b 0 {{C2}}
X1 b fbk vcc vee out {opamp.NAME}
CL out 0 {cl:g}
Lbrk out fbk 1k
Cinj fbk inj 1k
{opamp.subckt(gbw, 10.0)}""", "out", fc / 10, gbw * 100)
    params = [{"name": "R1", "min": 1e3, "max": 100e3}, {"name": "R2", "min": 1e3, "max": 100e3},
              {"name": "C1", "min": c1 / 10, "max": c1 * 10},
              {"name": "C2", "min": c2 / 10, "max": c2 * 10}]
    # For Q = 0.707 the -3 dB point IS f0; for other Q it moves, so only limit it then.
    limits = [{"metric": "peak_db", "max": 20 * math.log10(max(q * q / math.sqrt(q * q - 0.25)
                                                              if q > 0.7072 else 1.0, 1.0)) + 0.1},
              {"metric": "pm_deg", "min": PM_MIN}]
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
                 f"Q = {q:g} (0.707 = Butterworth, maximally flat).",
                 f"Op-amp: two-stage CMOS (MOSFETs) on ±5 V, GBW {_eng(gbw, 'Hz')} "
                 "(100× fc), wired as a unity-gain follower.",
                 f"Stability: a follower is the op-amp's hardest case. With "
                 f"{_eng(cl, 'F')} on the output, phase margin must stay above "
                 f"{PM_MIN:g}° (second deck, loop broken at the op-amp input)."],
        circuit_type="filter",
        tech={"f_pass": fc})


# --- 3. Non-inverting amplifier with a real (finite-GBW) op-amp ----------------------

def _noninv_amp(s: dict) -> Design:
    g, bw, gbw, cl = s["gain"], s["bw"], s["gbw"], s["cload"]
    if g < 1:
        raise TemplateError("noninverting_amp: gain must be >= 1 V/V")
    r1 = 10e3
    r2 = r1 * (g - 1) if g > 1 else 0.0
    if g * bw > gbw:
        raise TemplateError(f"noninverting_amp: gain x bw = {g * bw:.3g} Hz exceeds the "
                            f"op-amp GBW {gbw:.3g} Hz; raise gbw or lower gain or bw")
    g_db = 20 * math.log10(g)
    oa = opamp.design(gbw, 10.0)
    net = f"""* Non-inverting amplifier (SpiceMCP template noninverting_amp)
* gain target {g:g} V/V; transistor-level op-amp on +-5 V, GBW {gbw:g} Hz
V1 in 0 AC 1
Vcc vcc 0 5
Vee vee 0 -5
X1 in inn vcc vee out {opamp.NAME}
R2 out inn {{R2}}
R1 inn 0 {{R1}}
RL out 0 10k
CL out 0 {cl:g}
{opamp.subckt(gbw, 10.0)}.ac dec 100 10 {gbw * 10:g}
.meas AC gain_db FIND mag(V(out)) AT 10
.meas AC bw WHEN mag(V(out))={0.7071 * g:.6g} FALL=1
.end
""" + _loop_deck("Amplifier stability", f"""V1 in 0 0
Vcc vcc 0 5
Vee vee 0 -5
X1 in inn vcc vee out {opamp.NAME}
R2 out fbd {{R2}}
R1 fbd 0 {{R1}}
RL out 0 10k
CL out 0 {cl:g}
Lbrk fbd inn 1k
Cinj inn inj 1k
{opamp.subckt(gbw, 10.0)}""", "fbd", 100, gbw * 100)
    return Design(
        net, [{"name": "R1", "min": 1e3, "max": 100e3},
              {"name": "R2", "min": max(r2 / 10, 10.0), "max": max(r2 * 10, 1e4)}],
        goals=[{"metric": "bw", "direction": "max", "ref": bw}],
        limits=[{"metric": "gain_db", "min": g_db - 0.1, "max": g_db + 0.1},
                {"metric": "bw", "min": bw},
                {"metric": "pm_deg", "min": PM_MIN}],
        start={"R1": r1, "R2": max(r2, 10.0)},
        working=[f"Gain = 1 + R2/R1. Pick R1 = {_eng(r1, 'Ω')}.",
                 f"R2 = R1·(G − 1) = {_eng(r2, 'Ω')}.",
                 f"Bandwidth ≈ GBW / G = {gbw:g} / {g:g} = {_eng(gbw / g, 'Hz')} "
                 f"(spec {_eng(bw, 'Hz')}).",
                 "The 10 kΩ load and the R2+R1 divider also load the output.",
                 f"Op-amp: two-stage CMOS (MOSFETs) on ±5 V. Compensation "
                 f"Cc = gm1 / (2·π·GBW) = {_eng(oa['cc'], 'F')}; slew "
                 f"{oa['slew_v_per_us']:.2g} V/µs.",
                 f"Stability: {_eng(cl, 'F')} load. Phase margin falls as the load "
                 f"capacitance rises and as the gain falls; it must stay above {PM_MIN:g}°. "
                 "If it does not, lower gbw or isolate the load with a series resistor."],
        circuit_type="opamp",
        tech={"gbw": gbw})


# --- 4. LDO regulator ----------------------------------------------------------------

def _ldo(s: dict) -> Design:
    vin, vout, iload, vdo = s["vin"], s["vout"], s["iload"], s["dropout"]
    cout, step_pct = s["cout"], s["step_pct"]
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
    gbw = 10e6                      # error amplifier
    beta = vref / vout
    gm = math.sqrt(2 * kp * (w / length) * iload)
    # Loop: amplifier pole and output pole both sit far below crossover, so the loop
    # needs a zero: a small resistor in series with Cout (or the capacitor's own ESR).
    # Crossover fc = GBW·gm·β·Rz; zero fz = 1/(2π·Rz·Cout). fc = 3·fz gives ~70°.
    rz = math.sqrt(3 / (TWO_PI * cout * gbw * gm * beta))
    fc = gbw * gm * beta * rz
    rl, rl_light = vout / iload, vout / (0.1 * iload)
    step_mv = step_pct / 100 * vout * 1e3
    oa = opamp.subckt(gbw, vin)
    model = f".model PMOD PMOS(KP={kp:g} VTO=-{vt:g} LAMBDA=0.02{opamp.CAPS})\n"
    core = f"""Vin in 0 {vin:g}
Vref ref 0 {vref:g}
M1 out g in in PMOD W={{W}} L={length:g}
"""
    cap = f"Rz out co {{RZ}}\nCo co 0 {cout:g}\n"
    net = f"""* PMOS LDO regulator (SpiceMCP template ldo)
* vout target {vout:g} V from {vin:g} V at {iload:g} A, dropout spec {vdo:g} V
* error amplifier: transistor-level op-amp powered from the input; resistive load
{core}X1 fb ref in 0 g {opamp.NAME}
R1 out fb {{R1}}
R2 fb 0 {r2:g}
RL out 0 {rl:.6g}
{cap}{model}{oa}.dc Vin 0 {vin * 1.2:g} 0.01
.meas DC vout FIND V(out) AT {vin:g}
.meas DC vout_low FIND V(out) AT {vlow:g}
.meas DC vout_high FIND V(out) AT {vin * 1.15:g}
.end
""" + _loop_deck("LDO stability", f"""{core}X1 fb ref in 0 g {opamp.NAME}
R1 out fbd {{R1}}
R2 fbd 0 {r2:g}
RL out 0 {rl:.6g}
{cap}Lbrk fbd fb 1k
Cinj fb inj 1k
{model}{oa}""", "fbd", 100, gbw * 100) + f"""* LDO load step: {0.1 * iload:g} A to {iload:g} A in 1 us, and back
{core}X1 fb ref in 0 g {opamp.NAME}
R1 out fb {{R1}}
R2 fb 0 {r2:g}
RL out 0 {rl_light:.6g}
{cap}Iload out 0 PULSE(0 {0.9 * iload:g} 20u 1u 1u 60u 1)
{model}{oa}.tran 20n 120u 0 20n
.meas TRAN v_light FIND V(out) AT 19u
.meas TRAN v_dip MIN V(out) FROM 20u TO 80u
.meas TRAN v_kick MAX V(out) FROM 81u TO 120u
.meas TRAN step_mv PARAM (v_light-v_dip)*1000
.meas TRAN kick_mv PARAM (v_kick-v_light)*1000
.end
"""
    return Design(
        net, [{"name": "R1", "min": r1 * 0.5, "max": r1 * 2},
              {"name": "W", "min": w / 10, "max": w * 10},
              {"name": "RZ", "min": rz / 3, "max": rz * 3}],
        goals=[{"metric": "param_w", "direction": "min", "ref": w}],
        limits=[{"metric": "vout", "min": vout * 0.99, "max": vout * 1.01},
                {"metric": "vout_low", "min": vout * 0.98},
                {"metric": "pm_deg", "min": PM_MIN},
                {"metric": "step_mv", "max": step_mv},
                {"metric": "kick_mv", "max": step_mv}],
        start={"R1": r1, "W": w, "RZ": rz},
        working=[f"Vout = Vref·(1 + R1/R2), Vref = {vref} V. R2 = {_eng(r2, 'Ω')}, "
                 f"R1 = {_eng(r1, 'Ω')}.",
                 f"At dropout ({_eng(vlow, 'V')} in) the gate is at 0 V, so Vsg = {vsg:.3g} V "
                 f"and the PMOS is in triode with Vsd = {vdo:g} V.",
                 f"I = KP·(W/L)·((Vsg−|Vt|)·Vsd − Vsd²/2) → W = {_eng(w, 'm')} "
                 f"(1.5× margin on {_eng(iload, 'A')}).",
                 "Goal: the smallest pass device that still meets dropout.",
                 "Error amplifier: two-stage CMOS op-amp (MOSFETs) running from the input, "
                 f"GBW {_eng(gbw, 'Hz')}, so it loses headroom as the input falls.",
                 f"Stability: the amplifier's pole and the {_eng(cout, 'F')} output pole are "
                 f"both below crossover, which alone leaves almost no phase margin. A zero "
                 f"fixes it: Rz in series with Cout. Pass-device gm = √(2·KP·W/L·I) = "
                 f"{gm:.3g} S, β = Vref/Vout = {beta:.3g}.",
                 f"Crossover fc = GBW·gm·β·Rz and zero fz = 1/(2π·Rz·Cout). Put fc = 3·fz: "
                 f"Rz = √(3 / (2π·Cout·GBW·gm·β)) = {_eng(rz, 'Ω')}, fc ≈ {_eng(fc, 'Hz')}. "
                 "A capacitor whose ESR is this value does the same job.",
                 f"Load step {_eng(0.1 * iload, 'A')} → {_eng(iload, 'A')}: the output may "
                 f"move at most {step_pct:g}% ({step_mv:.0f} mV)."],
        circuit_type="ldo",
        tech={"vin": vin, "vout": vout, "iload": iload})


# --- 5. Buck converter (closed loop) --------------------------------------------------

def _buck(s: dict) -> Design:
    vin, vout, iout, fsw, ripple = s["vin"], s["vout"], s["iout"], s["fsw"], s["ripple_mv"]
    if vout >= vin:
        raise TemplateError("buck: vout must be below vin")
    if not 4 <= vin <= 20:
        raise TemplateError("buck: vin must be 4 to 20 V: the controller runs from it "
                            "and drives the switch gate with the full input voltage")
    vd, vref, v_lo, v_pp = 0.4, 1.2, 0.5, 2.0
    if vout <= vref:
        raise TemplateError(f"buck: vout must be above the {vref} V reference")
    d = (vout + vd) / (vin + vd)
    di = 0.3 * iout
    lval = (vin - vout) * d / (fsw * di)
    # 20 % margin for the switch's real edges, and sized for the worst-case parts: an
    # inductor 20 % low raises the ripple current, a capacitor 5 % low the ripple.
    cval = di / (8 * fsw * 0.8 * ripple * 1e-3) / (0.8 * 0.95)
    rl = vout / iout
    t = 1 / fsw
    f0 = 1 / (TWO_PI * math.sqrt(lval * cval))
    q = rl * math.sqrt(cval / lval)
    r2 = 10e3
    r1 = r2 * (vout / vref - 1)
    fc = f0 / 10                                    # crossover a decade below the LC
    ci = vin / (v_pp * r1 * TWO_PI * fc)
    gm_db = -20 * math.log10(fc / f0 * max(q, 1.0))
    tstop = max(25 / f0, 400 * t)
    win = min(100 * t, tstop / 4)
    net = f"""* Buck converter, closed loop (SpiceMCP template buck)
* {vin:g} V -> {vout:g} V at {iout:g} A, {fsw / 1e3:g} kHz, ripple target {ripple:g} mV
* voltage-mode PWM: error amplifier (integrator) -> comparator against a ramp -> switch
Vin in 0 {vin:g}
Vref ref 0 {vref:g}
Vramp ramp 0 PULSE({v_lo:g} {v_lo + v_pp:g} 0 {t - 20e-9:g} 10n 10n {t:g})
X2 ref fb in 0 vc {opamp.NAME}
CI vc fb {{CI}}
R1 out fb {{R1}}
R2 fb 0 {r2:g}
X3 ramp vc in 0 g {CMP}
M1 sw g in PSW
D1 0 sw DS
L1 sw out {{L}}
C1 out 0 {{C}}
RL out 0 {rl:g}
.model PSW VDMOS(pchan Vto=-2 Kp=20 Rd=0.04 Rs=0.01 Rb=0.01 Cgs=1n Cgdmax=1n Cgdmin=0.1n Cjo=0.5n)
.model DS D(Is=1e-6 Rs=0.02 N=1.2)
{opamp.subckt(2e6, vin)}{opamp.comparator_subckt(vin, CMP)}.tran {t / 1000:g} {tstop:g} 0 {t / 1000:g}
.meas TRAN vout AVG V(out) FROM {tstop - win:g} TO {tstop:g}
.meas TRAN vmax MAX V(out) FROM {tstop - win:g} TO {tstop:g}
.meas TRAN vmin MIN V(out) FROM {tstop - win:g} TO {tstop:g}
.meas TRAN iin AVG I(Vin) FROM {tstop - win:g} TO {tstop:g}
.meas TRAN ripple_mv PARAM (vmax-vmin)*1000
.meas TRAN eff PARAM vout*vout/{rl:g}/(-{vin:g}*iin)
.end
"""
    return Design(
        net, [{"name": "L", "min": lval / 10, "max": lval * 10},
              {"name": "C", "min": cval / 10, "max": cval * 10},
              {"name": "R1", "min": r1 * 0.8, "max": r1 * 1.25},
              {"name": "CI", "min": ci / 10, "max": ci * 10}],
        goals=[{"metric": "param_l", "direction": "min", "ref": lval, "weight": 1.0},
               {"metric": "param_c", "direction": "min", "ref": cval, "weight": 1.0}],
        limits=[{"metric": "vout", "min": vout * 0.98, "max": vout * 1.02},
                {"metric": "ripple_mv", "max": ripple}],
        start={"L": lval, "C": cval, "R1": r1, "CI": ci},
        working=[f"Duty D = (Vout + Vd) / (Vin + Vd) = {d:.3f} (Vd = {vd} V diode); the "
                 "controller finds it, this only sizes L.",
                 f"Ripple current ΔI = 30% of Iout = {_eng(di, 'A')}.",
                 f"L = (Vin − Vout)·D / (fsw·ΔI) = {_eng(lval, 'H')}.",
                 f"C = ΔI / (8·fsw·0.8·ΔV) / (0.8·0.95) = {_eng(cval, 'F')}: {ripple:g} mV "
                 "ripple with 20% margin for the switch's real edges, still met with the "
                 "inductor 20% and the capacitor 5% low.",
                 f"Feedback divider: Vout = Vref·(1 + R1/R2), Vref = {vref} V, "
                 f"R2 = {_eng(r2, 'Ω')}, R1 = {_eng(r1, 'Ω')}.",
                 f"Controller: integrator (R1, CI) → comparator against a {v_lo:g}–"
                 f"{v_lo + v_pp:g} V ramp → switch gate. Modulator gain Vin / Vramp = "
                 f"{vin / v_pp:.3g}.",
                 f"LC corner f0 = {_eng(f0, 'Hz')}, Q = {q:.2g}. Crossover fc = f0/10 = "
                 f"{_eng(fc, 'Hz')}: CI = Vin / (Vramp·R1·2π·fc) = {_eng(ci, 'F')}. Phase "
                 f"margin ≈ 90°; gain margin at the LC peak ≈ {gm_db:.0f} dB.",
                 f"Simulate {_eng(tstop, 's')} and measure the last {_eng(win, 's')}.",
                 "Switch: P-channel power MOSFET (50 mΩ, 1 nF gate), driven straight "
                 "from the comparator's push-pull output stage (0 V = on, Vin = off)."],
        circuit_type="dcdc",
        tech={"vin": vin, "vout": vout, "iload": iout, "fsw": fsw})


TEMPLATES: dict[str, Template] = {t.name: t for t in [
    Template("rc_lowpass", "RC low-pass filter", "filter",
             "First-order passive low-pass. 20 dB/decade roll-off.",
             [SpecField("fc", "Hz", "-3 dB cutoff frequency")], _rc_lowpass),
    Template("sallen_key_lowpass", "Sallen-Key low-pass filter (2nd order)", "filter",
             "Unity-gain active low-pass, 40 dB/decade. Q 0.707 = Butterworth.",
             [SpecField("fc", "Hz", "cutoff frequency"),
              SpecField("q", "", "quality factor", 1 / math.sqrt(2)),
              SpecField("cload", "F", "capacitance on the output", 100e-12)], _sallen_key),
    Template("noninverting_amp", "Non-inverting op-amp amplifier", "opamp",
             "Gain 1 + R2/R1 around a transistor-level CMOS op-amp.",
             [SpecField("gain", "V/V", "closed-loop gain (>= 1)"),
              SpecField("bw", "Hz", "minimum -3 dB bandwidth"),
              SpecField("gbw", "Hz", "op-amp gain-bandwidth product", 10e6),
              SpecField("cload", "F", "capacitance on the output", 100e-12)], _noninv_amp),
    Template("ldo", "PMOS LDO voltage regulator", "ldo",
             "Linear regulator: PMOS pass device, 1.2 V reference, resistor divider, "
             "output capacitor; checked for phase margin and load step.",
             [SpecField("vin", "V", "nominal input voltage"),
              SpecField("vout", "V", "output voltage (> 1.2 V)"),
              SpecField("iload", "A", "load current"),
              SpecField("dropout", "V", "max input-output difference at full load", 0.3),
              SpecField("cout", "F", "output capacitor", 1e-6),
              SpecField("step_pct", "%", "max output change on a 10%-to-100% load step", 3)],
             _ldo),
    Template("buck", "Buck DC-DC converter (closed loop)", "dcdc",
             "Step-down switching converter: P-MOSFET switch, diode, L, C, and a "
             "voltage-mode PWM controller that holds the output.",
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
