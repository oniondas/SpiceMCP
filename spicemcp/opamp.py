"""A transistor-level op-amp for the templates, in place of an ideal controlled source.

An `E` source (VCVS) is the textbook stand-in for an op-amp: infinite input impedance,
gain set by one number, no supply, no output limit. It is handy for a first look and
wrong for sign-off: it never clips, never runs out of bandwidth unless told to, never
loads, and a design that works on it may not work on a real part.

This is a real circuit instead: a two-stage CMOS op-amp built from MOSFETs, powered
from its own supply pins, the way an op-amp IC is built.

    stage 1  PMOS differential pair (M1, M2) with an NMOS current-mirror load (M3, M4),
             biased by a PMOS tail current source (M5)
    stage 2  NMOS common-source gain stage (M6) with a PMOS current-source load (M7)
    comp.    Miller capacitor Cc with a nulling resistor Rz across stage 2
    bias     diode-connected PMOS (M8) and a resistor set the currents

What follows from the transistors, not from a number typed in:
  * finite gain (about 85 dB into 10 kΩ) and gain-bandwidth GBW = gm1 / (2·π·Cc),
  * output swing that stops short of the rails, and a load that pulls the gain down,
  * slew rate = tail current / Cc, supply current, and input offset from mismatch,
  * the input pair is PMOS, so the inputs work down to the negative rail (an LDO's
    1.2 V reference on a single 5 V supply is inside the range).

Pins follow the usual 5-pin op-amp order (LTspice `opamp2` symbol and most vendor
macromodels): in+ in- V+ V- out. Any vendor model with that pin order can replace it.
The models are SPICE level 1, so the same deck runs in LTspice and ngspice.
"""

from __future__ import annotations

import math

NAME = "SPMCP_OA"
KPN, KPP, VT = 100e-6, 40e-6, 0.6
IB = 20e-6                       # bias branch current; each input device carries IB
W8, L8 = 10e-6, 2e-6             # bias diode
W1, L1 = 40e-6, 1e-6             # input pair
W6, L6 = 400e-6, 1e-6            # second-stage NMOS
M7_RATIO = 50                    # second-stage current = 50 x IB


def _vsg(i: float, w: float, l: float) -> float:
    return VT + math.sqrt(2 * i / (KPP * w / l))


def design(gbw: float = 2e6, supply: float = 10.0) -> dict:
    """Bias resistor, compensation and the resulting figures for a total supply span
    (V+ minus V-) and a target gain-bandwidth."""
    if supply < 3.0:
        raise ValueError(f"op-amp supply span {supply:g} V is below the 3 V minimum")
    rb = (supply - _vsg(IB, W8, L8)) / IB
    gm1 = math.sqrt(2 * KPP * (W1 / L1) * IB)
    cc = gm1 / (2 * math.pi * gbw)
    i6 = M7_RATIO * IB
    gm6 = math.sqrt(2 * KPN * (W6 / L6) * i6)
    return {"rb": rb, "cc": cc, "rz": 1 / gm6, "gm1": gm1, "gbw": gbw,
            "slew_v_per_us": 2 * IB / cc * 1e-6, "supply_ma": (2 + 2 + M7_RATIO) * IB * 1e3}


def subckt(gbw: float = 2e6, supply: float = 10.0, name: str = NAME) -> str:
    """The `.subckt` text, sized for a supply span and gain-bandwidth."""
    d = design(gbw, supply)
    return f""".subckt {name} inp inn vdd vss out
* two-stage CMOS op-amp, pins in+ in- V+ V- out
* GBW {gbw:.3g} Hz, slew {d['slew_v_per_us']:.2g} V/us, {d['supply_ma']:.2g} mA supply
M8 pb pb vdd vdd {name}_P W=10u L=2u
Rb pb vss {d['rb']:.4g}
M5 tail pb vdd vdd {name}_P W=20u L=2u
M1 x1 inn tail vdd {name}_P W=40u L=1u
M2 x2 inp tail vdd {name}_P W=40u L=1u
M3 x1 x1 vss vss {name}_N W=10u L=2u
M4 x2 x1 vss vss {name}_N W=10u L=2u
M6 out x2 vss vss {name}_N W=400u L=1u
M7 out pb vdd vdd {name}_P W=500u L=2u
Cc x2 cz {d['cc']:.4g}
Rz cz out {d['rz']:.4g}
.model {name}_N NMOS(KP={KPN:g} VTO={VT:g} LAMBDA=0.02)
.model {name}_P PMOS(KP={KPP:g} VTO=-{VT:g} LAMBDA=0.02)
.ends {name}
"""
