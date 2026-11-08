"""More ready-made circuits: the second half of the topology library.

Same contract as `topologies.py`: each builder turns a short spec into a netlist with
{PARAM} placeholders and `.meas` lines, parameter ranges, goals, limits, and a start
point sized by the textbook equations, with the working shown. Each one's hand-sized
start is simulated in the test suite and must land on its spec.
"""
from __future__ import annotations

import math

from . import opamp
from .topologies import (CMP, PM_MIN, TWO_PI, Design, SpecField, Template, TemplateError,
                         _eng, _loop_deck, _noise_deck)

SUPPLY = "Vcc vcc 0 5\nVee vee 0 -5\n"


# --- 6. Inverting amplifier ------------------------------------------------------------

def _inverting_amp(s: dict) -> Design:
    g, bw, gbw, cl, rin = s["gain"], s["bw"], s["gbw"], s["cload"], s["rin"]
    noise_gain = 1 + g
    if noise_gain * bw > gbw:
        raise TemplateError(f"inverting_amp: (1 + gain) x bw = {noise_gain * bw:.3g} Hz "
                            f"exceeds the op-amp GBW {gbw:.3g} Hz; raise gbw or lower gain "
                            "or bw")
    r1, r2 = rin, rin * g
    g_db = 20 * math.log10(g)
    oa = opamp.subckt(gbw, 10.0)
    net = f"""* Inverting amplifier (SpiceMCP template inverting_amp)
* gain -{g:g} V/V, input resistance R1; transistor-level op-amp on +-5 V
V1 in 0 AC 1
{SUPPLY}R1 in inn {{R1}}
R2 out inn {{R2}}
X1 0 inn vcc vee out {opamp.NAME}
RL out 0 10k
CL out 0 {cl:g}
{oa}.ac dec 100 10 {gbw * 10:g}
.meas AC gain_db FIND mag(V(out)) AT 10
.meas AC bw WHEN mag(V(out))={0.7071 * g:.6g} FALL=1
.end
""" + _loop_deck("Inverting amplifier stability", f"""V1 in 0 0
{SUPPLY}R1 in fbd {{R1}}
R2 out fbd {{R2}}
X1 0 inn vcc vee out {opamp.NAME}
RL out 0 10k
CL out 0 {cl:g}
Lbrk fbd inn 1k
Cinj inn inj 1k
{oa}""", "fbd", 100, gbw * 100) + _noise_deck("Inverting amplifier noise", f"""V1 in 0 AC 1
{SUPPLY}R1 in inn {{R1}}
R2 out inn {{R2}}
X1 0 inn vcc vee out {opamp.NAME}
RL out 0 10k
CL out 0 {cl:g}
{oa}""", "out", 10, 10 * bw)
    return Design(
        net, [{"name": "R1", "min": rin, "max": rin * 10},
              {"name": "R2", "min": r2 / 10, "max": r2 * 10}],
        goals=[{"metric": "bw", "direction": "max", "ref": bw}],
        limits=[{"metric": "gain_db", "min": g_db - 0.1, "max": g_db + 0.1},
                {"metric": "bw", "min": bw}, {"metric": "pm_deg", "min": PM_MIN}],
        start={"R1": r1, "R2": r2},
        working=[f"Gain = −R2/R1. The input sees R1, so R1 = rin = {_eng(r1, 'Ω')}.",
                 f"R2 = gain·R1 = {_eng(r2, 'Ω')}.",
                 f"The op-amp works at the noise gain 1 + R2/R1 = {noise_gain:g}, so "
                 f"bandwidth ≈ GBW / {noise_gain:g} = {_eng(gbw / noise_gain, 'Hz')} "
                 f"(spec {_eng(bw, 'Hz')}).",
                 f"Stability: {_eng(cl, 'F')} load, phase margin ≥ {PM_MIN:g}°; the loop is "
                 "the same as a non-inverting amplifier of gain 1 + R2/R1.",
                 "Noise: third deck, 10 Hz to 10·bw (vn_uv)."],
        circuit_type="opamp", tech={"gbw": gbw})


# --- 7. Band-pass (multiple feedback) --------------------------------------------------

def _bandpass(s: dict) -> Design:
    f0, q, h0 = s["f0"], s["q"], s["gain"]
    if h0 >= 2 * q * q:
        raise TemplateError(f"bandpass: gain {h0:g} must be below 2·Q² = {2 * q * q:g} "
                            "for the multiple-feedback circuit")
    if q > 20:
        raise TemplateError("bandpass: Q above 20 is too sensitive to part tolerance for "
                            "one stage; use two stages")
    c = 10e-9 if f0 < 20e3 else 1e-9
    w0 = TWO_PI * f0
    r1 = q / (h0 * w0 * c)
    r2 = 2 * q / (w0 * c)
    r3 = q / ((2 * q * q - h0) * w0 * c)
    gbw = max(1e6, 20 * 2 * q * q * f0)
    half = math.sqrt(1 + 1 / (4 * q * q))
    flo, fhi = f0 * (half - 1 / (2 * q)), f0 * (half + 1 / (2 * q))
    oa = opamp.subckt(gbw, 10.0)
    core = f"""{SUPPLY}R1 in a {{R1}}
R3 a 0 {{R3}}
C1 a inn {{C1}}
C2 a out {{C2}}
R2 out inn {{R2}}
"""
    g_db = 20 * math.log10(h0)
    net = f"""* Band-pass filter, multiple feedback (SpiceMCP template bandpass)
* centre {f0:g} Hz, Q {q:g}, centre gain -{h0:g} V/V; op-amp on +-5 V
V1 in 0 AC 1
{core}X1 0 inn vcc vee out {opamp.NAME}
{oa}.ac dec 200 {f0 / 100:g} {f0 * 100:g}
.meas AC g0_db FIND mag(V(out)) AT {f0:g}
.meas AC glo_db FIND mag(V(out)) AT {flo:.6g}
.meas AC ghi_db FIND mag(V(out)) AT {fhi:.6g}
.meas AC peak_db MAX mag(V(out))
.meas AC edge_lo_db PARAM glo_db-g0_db
.meas AC edge_hi_db PARAM ghi_db-g0_db
.end
""" + _loop_deck("Band-pass stability", f"""V1 in 0 0
{SUPPLY}R1 in a {{R1}}
R3 a 0 {{R3}}
C1 a inn {{C1}}
C2 a out {{C2}}
R2 out inn {{R2}}
X1 0 inn vcc vee oo {opamp.NAME}
Lbrk oo out 1k
Cinj out inj 1k
{oa}""", "oo", f0 / 100, gbw * 100)
    return Design(
        net, [{"name": "R1", "min": r1 / 5, "max": r1 * 5},
              {"name": "R2", "min": r2 / 5, "max": r2 * 5},
              {"name": "R3", "min": r3 / 5, "max": r3 * 5},
              {"name": "C1", "min": c / 5, "max": c * 5},
              {"name": "C2", "min": c / 5, "max": c * 5}],
        goals=[{"metric": "param_c1", "direction": "min", "ref": c}],
        limits=[{"metric": "g0_db", "min": g_db - 0.5, "max": g_db + 0.5},
                {"metric": "peak_db", "max": g_db + 0.6},
                {"metric": "edge_lo_db", "min": -4.0, "max": -2.0},
                {"metric": "edge_hi_db", "min": -4.0, "max": -2.0},
                {"metric": "pm_deg", "min": PM_MIN}],
        start={"R1": r1, "R2": r2, "R3": r3, "C1": c, "C2": c},
        working=[f"Equal capacitors C1 = C2 = {_eng(c, 'F')}, ω0 = 2π·{f0:g}.",
                 f"R2 = 2Q/(ω0·C) = {_eng(r2, 'Ω')} (sets Q and f0 with C).",
                 f"R1 = Q/(H0·ω0·C) = {_eng(r1, 'Ω')} (sets the centre gain {h0:g}).",
                 f"R3 = Q/((2Q² − H0)·ω0·C) = {_eng(r3, 'Ω')} (moves f0 without "
                 "changing Q).",
                 f"The −3 dB edges of a Q {q:g} band-pass are {_eng(flo, 'Hz')} and "
                 f"{_eng(fhi, 'Hz')}; the limits check the response is 2 to 4 dB down "
                 "there, so the centre and the bandwidth are both right.",
                 f"Op-amp GBW {_eng(gbw, 'Hz')}: the circuit asks for a loop gain of 2Q² "
                 "at f0, with a 20× margin."],
        circuit_type="filter", tech={"f_pass": f0})


# --- 8. Current sense (low-side shunt) -------------------------------------------------

def _current_sense(s: dict) -> Design:
    imax, vfs, pmax, bw = s["imax"], s["vout_fs"], s["shunt_w"], s["bw"]
    if vfs > 3.0:
        raise TemplateError("current_sense: vout_fs above 3 V does not fit the op-amp's "
                            "output swing on +-5 V")
    rs = min(0.5 * pmax / imax ** 2, 0.1 / imax)       # half the power rating, <= 100 mV
    g = vfs / (imax * rs)
    if g < 1:
        raise TemplateError("current_sense: the shunt alone gives more than vout_fs; "
                            "lower shunt_w")
    r1 = 1e3
    r2 = r1 * (g - 1)
    gbw = max(2e6, 10 * g * bw)
    oa = opamp.subckt(gbw, 10.0)
    core = f"""{SUPPLY}Vbus bus 0 12
RS sh 0 {{RS}}
X1 sh inn vcc vee out {opamp.NAME}
R2 out inn {{R2}}
R1 inn 0 {{R1}}
RL out 0 10k
"""
    net = f"""* Low-side current sense (SpiceMCP template current_sense)
* 0 to {imax:g} A through the shunt -> 0 to {vfs:g} V; op-amp on +-5 V
Iload bus sh 0
{core}{oa}.dc Iload 0 {imax:g} {imax / 100:g}
.meas DC vfs FIND V(out) AT {imax:g}
.meas DC v10 FIND V(out) AT {imax / 10:g}
.meas DC err_fs_pct PARAM (vfs/{vfs:g}-1)*100
.meas DC err_10_pct PARAM (v10/{vfs / 10:g}-1)*100
.end
* Current sense bandwidth
Iload bus sh DC 0 AC 1
{core}{oa}.ac dec 50 10 {gbw * 10:g}
.meas AC bw WHEN mag(V(out))={0.7071 * vfs / imax:.6g} FALL=1
.end
""" + _loop_deck("Current sense stability", f"""Iload bus sh 0
{SUPPLY}Vbus bus 0 12
RS sh 0 {{RS}}
X1 sh inn vcc vee out {opamp.NAME}
R2 out fbd {{R2}}
R1 fbd 0 {{R1}}
RL out 0 10k
Lbrk fbd inn 1k
Cinj inn inj 1k
{oa}""", "fbd", 100, gbw * 100)
    return Design(
        net, [{"name": "RS", "min": rs / 10, "max": rs * 2},
              {"name": "R1", "min": 100.0, "max": 100e3},
              {"name": "R2", "min": max(r2 / 10, 100.0), "max": max(r2 * 10, 1e4)}],
        goals=[{"metric": "param_rs", "direction": "min", "ref": rs}],
        limits=[{"metric": "err_fs_pct", "min": -1.0, "max": 1.0},
                {"metric": "err_10_pct", "min": -5.0, "max": 5.0},
                {"metric": "param_rs", "max": pmax / imax ** 2},
                {"metric": "bw", "min": bw}, {"metric": "pm_deg", "min": PM_MIN}],
        start={"RS": rs, "R1": r1, "R2": max(r2, 100.0)},
        working=[f"Shunt: at most half its {pmax:g} W rating at {imax:g} A and at most "
                 f"100 mV drop: RS = {_eng(rs, 'Ω')}, dissipating {imax ** 2 * rs:.3g} W.",
                 f"Gain = vout_fs / (imax·RS) = {g:.4g}: a non-inverting amplifier, "
                 f"R1 = {_eng(r1, 'Ω')}, R2 = R1·(G − 1) = {_eng(r2, 'Ω')}.",
                 "Accuracy is checked at full scale (±1%) and at 10% of full scale "
                 f"(±5%). At 10% the shunt drops only {imax * rs * 100:.3g} mV, so the "
                 "op-amp's offset dominates: 0.2 mV is 3% there. A zero-drift amplifier "
                 "(find_part) brings it under 0.1%.",
                 f"Bandwidth ≈ GBW / G; op-amp GBW {_eng(gbw, 'Hz')} for {_eng(bw, 'Hz')}.",
                 "The goal is the smallest shunt that still meets accuracy: less heat, "
                 "less voltage stolen from the load's ground."],
        circuit_type="opamp", tech={"gbw": gbw})


# --- 9. ADC driver (buffer + charge-bucket RC) ----------------------------------------

def _adc_driver(s: dict) -> Design:
    fs, bits, vstep = s["fs"], int(round(s["bits"])), s["vstep"]
    if not 8 <= bits <= 16:
        raise TemplateError("adc_driver: bits must be 8 to 16")
    if vstep > 3:
        raise TemplateError("adc_driver: vstep above 3 V does not fit the op-amp swing")
    tacq = 1 / (2 * fs)
    n_tau = (bits + 1) * math.log(2)                     # settle to 1/2 LSB
    tau = tacq / (n_tau * 1.5)
    cf = 1e-9 if tau > 50e-9 else 100e-12
    rf = tau / cf
    gbw = max(10e6, 10 / (TWO_PI * tau))
    if gbw > 200e6:
        raise TemplateError(f"adc_driver: {bits} bits at {fs:g} S/s needs an op-amp above "
                            "200 MHz; lower fs or bits")
    t0 = 1e-6
    oa = opamp.subckt(gbw, 10.0)
    lsb = vstep / 2 ** bits
    core = f"""{SUPPLY}X1 in out vcc vee out {opamp.NAME}
RF out adc {{RF}}
CF adc 0 {{CF}}
"""
    net = f"""* ADC driver: op-amp buffer and charge-bucket RC (SpiceMCP template adc_driver)
* {bits}-bit ADC at {fs:g} S/s: a {vstep:g} V step must settle to 1/2 LSB in {tacq:.3g} s
.options reltol=1e-5 abstol=1e-12 vntol=1e-8
V1 in 0 PULSE(0 {vstep:g} {t0:g} 1n 1n 1 2)
{core}{oa}.tran {tacq / 2000:g} {t0 + 2 * tacq:g} 0 {tacq / 2000:g}
.meas TRAN vacq FIND V(adc) AT {t0 + tacq:g}
.meas TRAN vpk MAX V(adc) FROM {t0:g} TO {t0 + 2 * tacq:g}
.meas TRAN err_lsb PARAM ({vstep:g}-vacq)/{lsb:.6g}
.meas TRAN over_lsb PARAM (vpk-{vstep:g})/{lsb:.6g}
.end
""" + _loop_deck("ADC driver stability", f"""V1 in 0 0
{SUPPLY}X1 in fbk vcc vee out {opamp.NAME}
RF out adc {{RF}}
CF adc 0 {{CF}}
Lbrk out fbk 1k
Cinj fbk inj 1k
{oa}""", "out", 1e3, gbw * 100)
    return Design(
        net, [{"name": "RF", "min": rf / 10, "max": rf * 10},
              {"name": "CF", "min": cf / 3, "max": cf * 3}],
        goals=[{"metric": "param_cf", "direction": "max", "ref": cf}],
        limits=[{"metric": "err_lsb", "min": -0.5, "max": 0.5},
                {"metric": "over_lsb", "max": 0.5},
                {"metric": "pm_deg", "min": PM_MIN}],
        start={"RF": rf, "CF": cf},
        working=[f"Acquisition time = half the sample period = {_eng(tacq, 's')}.",
                 f"Settling to ½ LSB of {bits} bits takes (bits + 1)·ln2 = {n_tau:.1f} "
                 f"time constants; with 1.5× margin τ = {_eng(tau, 's')}.",
                 f"CF = {_eng(cf, 'F')} (the charge bucket: much larger than the ADC's "
                 f"sampling capacitor), RF = τ/CF = {_eng(rf, 'Ω')}.",
                 f"RF also isolates the op-amp from CF, which would otherwise make it ring. "
                 f"Op-amp GBW {_eng(gbw, 'Hz')}: ten times faster than the RC.",
                 f"1 LSB = {_eng(lsb, 'V')}; the limits are the error at the end of "
                 "acquisition and the overshoot, each within ½ LSB.",
                 "The goal is the largest bucket capacitor that still settles: it absorbs "
                 "the ADC's sampling kick best."],
        circuit_type="opamp", tech={"gbw": gbw})


# --- 10. Voltage reference (bandgap) ---------------------------------------------------

def _reference(s: dict) -> Design:
    vout, tc_max = s["vout"], s["tc_ppm"]
    if not 1.25 <= vout <= 3.5:
        raise TemplateError("voltage_reference: vout must be 1.25 to 3.5 V")
    n, vt = 8, 0.025852
    r1 = 10e3                       # both branches: equal currents from the op-amp
    # Zero-TC point with a PTAT current: Vref = EG + (XTI - 1)·Vt (the model's EG 1.11,
    # XTI 3); the textbook 1.235 V assumes the silicon bandgap 1.205 V and XTI ~ 2.
    vbg = 1.11 + 2 * vt
    i = 50e-6
    r3 = vt * math.log(n) / i       # dVbe across R3 sets the PTAT current
    vbe = vt * math.log(i / 1e-16)
    r2 = (vbg - vbe) / i            # PTAT gain: R2/R3 * Vt ln N cancels Vbe's slope
    r1 = r2
    gain = vout / vbg
    rg1 = 10e3
    rg2 = rg1 * (gain - 1)
    net = f"""* Bandgap voltage reference (SpiceMCP template voltage_reference)
* {vout:g} V, temperature coefficient under {tc_max:g} ppm/C from -40 to 85 C
{SUPPLY}R1 vbg a {{R1}}
Q1 a a 0 QN
R2 vbg b {{R2}}
R3 b c {{R3}}
Q2 c c 0 QN {n}
Rstart vcc a 10Meg
X1 a b vcc vee vbg {opamp.NAME}
X2 vbg g vcc vee out {opamp.NAME}
RG2 out g {{RG2}}
RG1 g 0 {rg1:g}
RL out 0 10k
.model QN NPN(IS=1e-16 BF=100 XTI=3 EG=1.11)
{opamp.subckt(2e6, 10.0)}.temp 25
.dc temp -40 85 5
.meas DC vroom FIND V(out) AT 25
.meas DC vmax MAX V(out)
.meas DC vmin MIN V(out)
.meas DC tc_ppm PARAM (vmax-vmin)/vroom/125*1e6
.end
"""
    return Design(
        net, [{"name": "R1", "min": r1 / 3, "max": r1 * 3},
              {"name": "R2", "min": r2 / 3, "max": r2 * 3},
              {"name": "R3", "min": r3 / 2, "max": r3 * 2},
              {"name": "RG2", "min": max(rg2 / 3, 100.0), "max": max(rg2 * 3, 1e3)}],
        goals=[{"metric": "tc_ppm", "direction": "min", "ref": tc_max}],
        limits=[{"metric": "vroom", "min": vout * 0.995, "max": vout * 1.005},
                {"metric": "tc_ppm", "max": tc_max}],
        start={"R1": r1, "R2": r2, "R3": r3, "RG2": max(rg2, 100.0)},
        working=[f"Two transistors, Q2 {n}× the area of Q1, at equal currents: their "
                 f"base-emitter voltages differ by Vt·ln{n} = {vt * math.log(n) * 1e3:.1f} mV, "
                 "which rises with temperature (PTAT).",
                 f"R3 = Vt·ln{n} / I = {_eng(r3, 'Ω')} for I = {_eng(i, 'A')} per branch.",
                 f"Vbe ≈ {vbe:.3f} V falls about 2 mV/°C. With a PTAT current the slope "
                 f"cancels when the output is EG + (XTI − 1)·Vt = {vbg:.3f} V (the "
                 f"transistor model's EG 1.11 V, XTI 3): R2 = (Vbg − Vbe)/I = "
                 f"{_eng(r2, 'Ω')}.",
                 "The op-amp forces equal voltage at a and b, so R1 = R2 gives equal "
                 "currents. Rstart (10 MΩ) kicks it out of the zero-current state.",
                 f"Second op-amp: gain 1 + RG2/RG1 = {gain:.4g} to reach {vout:g} V.",
                 "Temperature coefficient: swept from −40 to 85 °C, (max − min)/V/125 °C "
                 "(the box method datasheets use). The goal pushes it down; R3 trims it."],
        circuit_type="general", tech={})


# --- 11. Input protection --------------------------------------------------------------

def _protection(s: dict) -> Design:
    vin, vsurge, vmax, iload, drop = (s["vin"], s["vsurge"], s["vmax"], s["iload"],
                                      s["drop_max"])
    if not vin * 1.1 < vmax < vsurge:
        raise TemplateError("protection: need vin x 1.1 < vmax < vsurge")
    rl = vin / iload
    if drop <= 0.5:
        raise TemplateError("protection: drop_max must exceed 0.5 V (the diode alone "
                            "drops about 0.4 V)")
    bv = vin * 1.15
    rs = max(0.05, min((0.8 * drop - 0.4) / iload, 5.0))  # 20 % of the budget spare
    rz = 0.1                                           # a 600 W SMB TVS, roughly
    ipk = (vsurge - bv) / (rs + rz)
    clamp = bv + ipk * rz
    net = f"""* Input protection: reverse polarity and surge clamp (SpiceMCP template protection)
* {vin:g} V input, {vsurge:g} V surge for 1 ms, downstream absolute max {vmax:g} V
Vin in 0 PWL(0 {vin:g} 1m {vin:g} 1.01m {vsurge:g} 2m {vsurge:g} 2.01m {vin:g} 3m {vin:g})
D1 in a DSCH
RS a out {{RS}}
DZ 0 out DTVS
C1 out 0 10u
RL out 0 {rl:g}
.model DSCH D(Is=31.7u Rs=.051 N=1.373 Cjo=110p)
.model DTVS D(Is=1n Rs={rz:g} BV={{BV}} IBV=1m Cjo=500p)
.tran 1u 3m 0 1u
.meas TRAN vclamp MAX V(out) FROM 1m TO 2.2m
.meas TRAN vnom AVG V(out) FROM 0.5m TO 1m
.meas TRAN vdrop PARAM {vin:g}-vnom
.meas TRAN izr AVG I(DZ) FROM 0.5m TO 1m
.meas TRAN ileak_ma PARAM -izr*1000
.end
* Reverse polarity: the input connected backwards
Vin in 0 {-vin:g}
D1 in a DSCH
RS a out {{RS}}
DZ 0 out DTVS
C1 out 0 10u
RL out 0 {rl:g}
.model DSCH D(Is=31.7u Rs=.051 N=1.373 Cjo=110p)
.model DTVS D(Is=1n Rs={rz:g} BV={{BV}} IBV=1m Cjo=500p)
.dc Vin {-vin:g} {-vin + 0.001:g} 0.001
.meas DC vrev FIND V(out) AT {-vin + 0.0005:g}
.end
"""
    return Design(
        net, [{"name": "RS", "min": 0.02, "max": 5.0},
              {"name": "BV", "min": vin * 1.1, "max": vmax}],
        goals=[{"metric": "vdrop", "direction": "min", "ref": drop}],
        limits=[{"metric": "vclamp", "max": vmax},
                {"metric": "vdrop", "max": drop},
                {"metric": "vrev", "min": -0.3},
                {"metric": "ileak_ma", "max": 1.0}],
        start={"RS": rs, "BV": bv},
        working=[f"D1, a Schottky in series, blocks a reversed input (output stays near "
                 f"0 V) and costs about 0.35 V at {_eng(iload, 'A')}.",
                 f"RS = (0.8·drop_max − 0.4 V)/I = {_eng(rs, 'Ω')} limits the surge "
                 "current into the clamp; its drop is checked at the nominal load.",
                 f"Clamp (TVS) breakdown {bv:.3g} V: above vin·1.1 = {vin * 1.1:.3g} V so it "
                 f"does not conduct in normal use (leakage under 1 mA), well below the "
                 f"{vmax:g} V the load can stand.",
                 f"Surge: the input steps to {vsurge:g} V for 1 ms. The clamp voltage is "
                 "BV plus the surge current times the clamp's resistance RZ: "
                 f"{bv:.3g} V + {ipk:.3g} A × {rz:g} Ω = {clamp:.3g} V. That current, for "
                 "1 ms, is what the clamp must be rated for (peak pulse current).",
                 "The stress check reports the clamp's power: buy a TVS rated for it."],
        circuit_type="general", tech={})


# --- 12/13. Boost and flyback (closed loop) --------------------------------------------

def _controller(vin, vref, v_lo, v_pp, t, tss, k):
    """Error amplifier (integrator), soft start and the PWM comparator, low-side gate
    drive. The ramp falls, so the switch is off at t = 0; the gate is high while the
    ramp is below k·Vc. The divider k caps the duty cycle at the error amplifier's top
    swing: a boost or flyback stuck at 100 % duty shorts its input."""
    return (f"Vref ref 0 PWL(0 0 {tss:g} {vref:g})\n"
            f"Vramp ramp 0 PULSE({v_lo + v_pp:g} {v_lo:g} 0 {t - 20e-9:g} 10n 10n {t:g})\n"
            f"X2 ref fb in 0 vc {opamp.NAME}\nCI vc fb {{CI}}\n"
            f"Rk1 vc vcd 10k\nRk2 vcd 0 {10e3 * k / (1 - k):.6g}\n"
            f"X3 vcd ramp in 0 g {CMP}\n")


def _duty(v_lo, v_pp, k, dmax):
    return f"min(max(({k:.6g}*V(vc)-{v_lo:g})/{v_pp:g},0),{dmax:g})"


def _kdiv(vin, v_lo, v_pp, dmax):
    """Divider ratio so the error amplifier's top (Vin) means duty dmax."""
    return (v_lo + dmax * v_pp) / vin


def _boost(s: dict) -> Design:
    vin, vout, iout, fsw, ripple = s["vin"], s["vout"], s["iout"], s["fsw"], s["ripple_mv"]
    if vout <= vin * 1.1:
        raise TemplateError("boost: vout must be at least 1.1 x vin")
    if not 4 <= vin <= 20 or vout > 60:
        raise TemplateError("boost: vin must be 4 to 20 V (it powers the controller and "
                            "the gate) and vout at most 60 V")
    vd, vref, v_lo, v_pp = 0.4, 1.2, 0.5, 2.0
    d = 1 - vin / (vout + vd)
    if d > 0.8:
        raise TemplateError(f"boost: duty {d:.2f} is above 0.8; use two stages or a "
                            "flyback")
    il = iout / (1 - d)
    di = 0.3 * il
    t = 1 / fsw
    lval = vin * d / (fsw * di)
    cval = iout * d / (fsw * ripple * 1e-3) / (0.8 * 0.95)
    rl = vout / iout
    r2 = 10e3
    r1 = r2 * (vout / vref - 1)
    f0 = (1 - d) / (TWO_PI * math.sqrt(lval * cval))
    frhp = rl * (1 - d) ** 2 / (TWO_PI * lval)
    fc = min(f0, frhp) / 10
    dmax = min(0.85, d + 0.15)
    k = _kdiv(vin, v_lo, v_pp, dmax)
    gvd0 = vin / ((1 - d) ** 2 * v_pp) * k
    ci = gvd0 / (r1 * TWO_PI * fc)
    tstop = max(4 / fc, 400 * t)
    tss = tstop / 3
    win = min(100 * t, tstop / 4)
    net = f"""* Boost converter, closed loop (SpiceMCP template boost)
* {vin:g} V -> {vout:g} V at {iout:g} A, {fsw / 1e3:g} kHz, ripple target {ripple:g} mV
Vin in 0 {vin:g}
{_controller(vin, vref, v_lo, v_pp, t, tss, k)}R1 out fb {{R1}}
R2 fb 0 {r2:g}
L1 in sw {{L}}
M1 sw g 0 NSW
D1 sw out DS
C1 out 0 {{C}}
RL out 0 {rl:g}
.model NSW VDMOS(Vto=2 Kp=20 Rd=0.04 Rs=0.01 Rb=0.01 Cgs=1n Cgdmax=1n Cgdmin=0.1n Cjo=0.5n)
.model DS D(Is=1e-6 Rs=0.02 N=1.2)
{opamp.subckt(2e6, vin)}{opamp.comparator_subckt(vin, CMP)}.tran {t / 1000:g} {tstop:g} 0 {t / 1000:g}
.meas TRAN vout AVG V(out) FROM {tstop - win:g} TO {tstop:g}
.meas TRAN vmax MAX V(out) FROM {tstop - win:g} TO {tstop:g}
.meas TRAN vmin MIN V(out) FROM {tstop - win:g} TO {tstop:g}
.meas TRAN iin AVG I(Vin) FROM {tstop - win:g} TO {tstop:g}
.meas TRAN ripple_mv PARAM (vmax-vmin)*1000
.meas TRAN eff PARAM vout*vout/{rl:g}/(-{vin:g}*iin)
.end
""" + _loop_deck("Boost stability (averaged switch)", f"""Vin in 0 {vin:g}
Vref ref 0 {vref:g}
X2 ref fb in 0 vc {opamp.NAME}
CI vc fb {{CI}}
R1 outx fb {{R1}}
R2 fb 0 {r2:g}
Lbrk out outx 1k
Cinj outx inj 1k
* switch and diode averaged over a cycle: duty = (k·Vc - {v_lo:g}) / {v_pp:g}
L1 in x {{L}}
Vs x sw 0
Bsw sw 0 V=(V(out)+{vd:g})*(1-{_duty(v_lo, v_pp, k, dmax)})
Bd 0 out I=I(Vs)*(1-{_duty(v_lo, v_pp, k, dmax)})
C1 out 0 {{C}}
RL out 0 {rl:g}
{opamp.subckt(2e6, vin)}""", "out", fc / 100, fsw / 2)
    return Design(
        net, [{"name": "L", "min": lval / 5, "max": lval * 5},
              {"name": "C", "min": cval / 5, "max": cval * 10},
              {"name": "R1", "min": r1 * 0.8, "max": r1 * 1.25},
              {"name": "CI", "min": ci / 20, "max": ci * 20}],
        goals=[{"metric": "param_l", "direction": "min", "ref": lval},
               {"metric": "param_c", "direction": "min", "ref": cval}],
        limits=[{"metric": "vout", "min": vout * 0.98, "max": vout * 1.02},
                {"metric": "ripple_mv", "max": ripple},
                {"metric": "pm_deg", "min": PM_MIN}],
        start={"L": lval, "C": cval, "R1": r1, "CI": ci},
        working=[f"Duty D = 1 − Vin/(Vout + Vd) = {d:.3f}; inductor current "
                 f"Iout/(1 − D) = {_eng(il, 'A')}.",
                 f"Ripple current 30%: L = Vin·D/(fsw·ΔI) = {_eng(lval, 'H')}.",
                 f"C = Iout·D/(fsw·ΔV), with 20% margin and a 5%-low part: "
                 f"{_eng(cval, 'F')}.",
                 f"Divider: Vout = 1.2 V·(1 + R1/R2), R1 = {_eng(r1, 'Ω')}.",
                 f"A boost has a right-half-plane zero at Rl·(1 − D)²/(2π·L) = "
                 f"{_eng(frhp, 'Hz')} and its LC corner at {_eng(f0, 'Hz')}; the loop "
                 f"crosses over a decade below the lower one, fc = {_eng(fc, 'Hz')}.",
                 f"Integrator: control-to-output gain Vin/((1 − D)²·Vramp) = {gvd0:.3g}, "
                 f"CI = {_eng(ci, 'F')}.",
                 "Stability is simulated: the switch and diode averaged over a cycle, "
                 "loop broken between the output and the divider; phase margin ≥ 45°.",
                 "Switch: N-channel MOSFET, gate driven from Vin by the comparator stage."],
        circuit_type="dcdc", tech={"vin": vin, "vout": vout, "iload": iout, "fsw": fsw})


def _flyback(s: dict) -> Design:
    vin, vout, iout, fsw, ripple = s["vin"], s["vout"], s["iout"], s["fsw"], s["ripple_mv"]
    if not 8 <= vin <= 20:
        raise TemplateError("flyback: vin must be 8 to 20 V (it powers the controller and "
                            "the gate)")
    vd, vref, v_lo, v_pp, dnom = 0.5, 1.2, 0.5, 2.0, 0.4
    n = (vout + vd) * (1 - dnom) / (vin * dnom)        # Ns/Np for 40 % duty
    t = 1 / fsw
    isec = iout / (1 - dnom)
    ipri = isec * n
    lp = vin * dnom / (fsw * 0.4 * ipri)               # 40 % ripple on the primary
    ls = lp * n * n
    cval = iout * dnom / (fsw * ripple * 1e-3) / (0.8 * 0.95)
    rl = vout / iout
    r2 = 10e3
    r1 = r2 * (vout / vref - 1)
    f0 = (1 - dnom) / (TWO_PI * math.sqrt(ls * cval))
    frhp = rl * (1 - dnom) ** 2 / (TWO_PI * dnom * ls)
    fc = min(f0, frhp) / 10
    dmax = 0.6
    k = _kdiv(vin, v_lo, v_pp, dmax)
    gvd0 = vin * n / ((1 - dnom) ** 2 * v_pp) * k
    ci = gvd0 / (r1 * TWO_PI * fc)
    vor = (vout + vd) / n                              # reflected output voltage
    vclamp = vin + 2 * vor                             # clamp set at twice Vor
    llk = (1 - 0.98 ** 2) * lp                         # leakage of a k = 0.98 transformer
    ipk = ipri * 1.2
    pclamp = 0.5 * llk * ipk ** 2 * fsw * 2            # x Vc/(Vc - Vor) = 2
    rcl = (2 * vor) ** 2 / pclamp
    ccl = 20 / (rcl * fsw)
    tstop = max(4 / fc, 400 * t)
    tss = tstop / 3
    win = min(100 * t, tstop / 4)
    net = f"""* Flyback converter, closed loop (SpiceMCP template flyback)
* {vin:g} V -> {vout:g} V at {iout:g} A, {fsw / 1e3:g} kHz; feedback taken directly from
* the output (an isolated design adds an optocoupler; the power stage is the same)
Vin in 0 {vin:g}
{_controller(vin, vref, v_lo, v_pp, t, tss, k)}R1 out fb {{R1}}
R2 fb 0 {r2:g}
LP in sw {{LP}}
LS 0 sx {{{{LP}}*{n * n:.6g}}}
K1 LP LS 0.98
M1 sw g 0 NSW
DC1 sw cl DS
CC1 cl in {ccl:.3g}
RC1 cl in {{RCL}}
D2 sx out DS
C1 out 0 {{C}}
RL out 0 {rl:g}
.model NSW VDMOS(Vto=2 Kp=20 Rd=0.04 Rs=0.01 Rb=0.01 Cgs=1n Cgdmax=1n Cgdmin=0.1n Cjo=0.5n)
.model DS D(Is=1e-6 Rs=0.02 N=1.2)
{opamp.subckt(2e6, vin)}{opamp.comparator_subckt(vin, CMP)}.tran {t / 1000:g} {tstop:g} 0 {t / 1000:g}
.meas TRAN vout AVG V(out) FROM {tstop - win:g} TO {tstop:g}
.meas TRAN vmax MAX V(out) FROM {tstop - win:g} TO {tstop:g}
.meas TRAN vmin MIN V(out) FROM {tstop - win:g} TO {tstop:g}
.meas TRAN vds_max MAX V(sw) FROM {tstop - win:g} TO {tstop:g}
.meas TRAN ripple_mv PARAM (vmax-vmin)*1000
.end
""" + _loop_deck("Flyback stability (averaged switch)", f"""Vin in 0 {vin:g}
Vref ref 0 {vref:g}
X2 ref fb in 0 vc {opamp.NAME}
CI vc fb {{CI}}
R1 outx fb {{R1}}
R2 fb 0 {r2:g}
Lbrk out outx 1k
Cinj outx inj 1k
* the transformer and switches averaged over a cycle, referred to the secondary
Bl x 0 V={_duty(v_lo, v_pp, k, dmax)}*V(in)*{n:.6g}-(1-{_duty(v_lo, v_pp, k, dmax)})*(V(out)+{vd:g})
LM x y {{{{LP}}*{n * n:.6g}}}
Vs y 0 0
Bd 0 out I=I(Vs)*(1-{_duty(v_lo, v_pp, k, dmax)})
C1 out 0 {{C}}
RL out 0 {rl:g}
{opamp.subckt(2e6, vin)}""", "out", fc / 100, fsw / 2)
    return Design(
        net, [{"name": "LP", "min": lp / 5, "max": lp * 5},
              {"name": "C", "min": cval / 5, "max": cval * 10},
              {"name": "R1", "min": r1 * 0.8, "max": r1 * 1.25},
              {"name": "CI", "min": ci / 20, "max": ci * 20},
              {"name": "RCL", "min": rcl / 5, "max": rcl * 5}],
        goals=[{"metric": "param_lp", "direction": "min", "ref": lp},
               {"metric": "param_c", "direction": "min", "ref": cval}],
        limits=[{"metric": "vout", "min": vout * 0.97, "max": vout * 1.03},
                {"metric": "ripple_mv", "max": ripple},
                {"metric": "vds_max", "max": s["vds_max"]},
                {"metric": "pm_deg", "min": PM_MIN}],
        start={"LP": lp, "C": cval, "R1": r1, "CI": ci, "RCL": rcl},
        working=[f"Turns ratio Ns/Np = (Vout + Vd)·(1 − D)/(Vin·D) = {n:.3g} for D = "
                 f"{dnom:g} at {vin:g} V in.",
                 f"Primary peak region current ≈ n·Iout/(1 − D) = {_eng(ipri, 'A')}; "
                 f"Lp = Vin·D/(fsw·0.4·Ip) = {_eng(lp, 'H')} (40% ripple, continuous mode). "
                 f"Ls = n²·Lp; coupling 0.98 (2% leakage).",
                 f"Off-state switch voltage = Vin + (Vout + Vd)/n = "
                 f"{vin + vor:.3g} V plus the leakage spike. The RCD clamp (DC1, CC1, RC1) "
                 f"holds it near Vin + 2·Vor = {vclamp:.3g} V: the leakage "
                 f"({_eng(llk, 'H')}) dumps ½·Llk·Ip²·fsw·2 = {pclamp:.2g} W into it, so "
                 f"RC1 = (2·Vor)²/P = {_eng(rcl, 'Ω')}, CC1 = {_eng(ccl, 'F')}. "
                 f"Limit: vds_max ≤ {s['vds_max']:g} V.",
                 f"C = Iout·D/(fsw·ΔV) with margin: {_eng(cval, 'F')}.",
                 f"Right-half-plane zero {_eng(frhp, 'Hz')}, LC corner {_eng(f0, 'Hz')}: "
                 f"crossover fc = {_eng(fc, 'Hz')}; CI = {_eng(ci, 'F')}.",
                 "Stability is simulated with the averaged circuit, loop broken between "
                 "the output and the divider; phase margin ≥ 45°.",
                 "Feedback here is direct; an isolated supply needs an optocoupler or "
                 "primary-side sensing, which changes only the feedback path."],
        circuit_type="dcdc", tech={"vin": vin, "vout": vout, "iload": iout, "fsw": fsw})


MORE: list[Template] = [
    Template("inverting_amp", "Inverting op-amp amplifier", "opamp",
             "Gain -R2/R1, input resistance R1, transistor-level op-amp.",
             [SpecField("gain", "V/V", "gain magnitude (output is inverted)"),
              SpecField("bw", "Hz", "minimum -3 dB bandwidth"),
              SpecField("rin", "ohm", "minimum input resistance", 10e3),
              SpecField("gbw", "Hz", "op-amp gain-bandwidth product", 10e6),
              SpecField("cload", "F", "capacitance on the output", 100e-12)],
             _inverting_amp, ("opamp",)),
    Template("bandpass", "Band-pass filter (multiple feedback)", "filter",
             "Second-order band-pass around one op-amp; centre, Q and centre gain.",
             [SpecField("f0", "Hz", "centre frequency"),
              SpecField("q", "", "quality factor (f0 / bandwidth)", 5),
              SpecField("gain", "V/V", "centre gain magnitude", 1)], _bandpass, ("opamp",)),
    Template("current_sense", "Low-side current sense amplifier", "opamp",
             "Shunt resistor and amplifier: load current to a voltage for an ADC.",
             [SpecField("imax", "A", "full-scale current"),
              SpecField("vout_fs", "V", "output at full scale", 2.5),
              SpecField("shunt_w", "W", "shunt power rating", 0.25),
              SpecField("bw", "Hz", "minimum bandwidth", 10e3)], _current_sense, ("opamp",)),
    Template("adc_driver", "ADC driver (buffer + charge-bucket RC)", "opamp",
             "Op-amp buffer and RC that settle a step to 1/2 LSB within acquisition.",
             [SpecField("fs", "S/s", "ADC sample rate"),
              SpecField("bits", "", "ADC resolution", 12),
              SpecField("vstep", "V", "largest input step", 2)], _adc_driver, ("opamp",)),
    Template("voltage_reference", "Bandgap voltage reference", "general",
             "Two-transistor bandgap and a gain stage; low drift from -40 to 85 C.",
             [SpecField("vout", "V", "output voltage (1.25 to 3.5)"),
              SpecField("tc_ppm", "ppm/C", "max temperature coefficient", 50)], _reference),
    Template("protection", "Input protection (reverse polarity + surge clamp)", "general",
             "Series Schottky, series resistor and TVS clamp for a DC input.",
             [SpecField("vin", "V", "nominal input"),
              SpecField("vsurge", "V", "surge voltage (1 ms)"),
              SpecField("vmax", "V", "max voltage the load can stand"),
              SpecField("iload", "A", "load current"),
              SpecField("drop_max", "V", "max drop at the load current", 1.0)], _protection),
    Template("boost", "Boost DC-DC converter (closed loop)", "dcdc",
             "Step-up switching converter: N-MOSFET, diode, L, C, voltage-mode PWM.",
             [SpecField("vin", "V", "input voltage"), SpecField("vout", "V", "output voltage"),
              SpecField("iout", "A", "load current"),
              SpecField("fsw", "Hz", "switching frequency", 300e3),
              SpecField("ripple_mv", "mV", "max output ripple, peak to peak", 100)],
             _boost, ("diode",)),
    Template("flyback", "Flyback DC-DC converter (closed loop)", "dcdc",
             "Coupled-inductor converter with RCD clamp; output any voltage from the input.",
             [SpecField("vin", "V", "input voltage"), SpecField("vout", "V", "output voltage"),
              SpecField("iout", "A", "load current"),
              SpecField("fsw", "Hz", "switching frequency", 200e3),
              SpecField("ripple_mv", "mV", "max output ripple, peak to peak", 100),
              SpecField("vds_max", "V", "max switch voltage", 60)], _flyback),
]
