# Every circuit built with SpiceMCP

**Total: 17 circuits.**

**Picture book:** [circuits/SpiceMCP_Circuit_Catalog.pdf](circuits/SpiceMCP_Circuit_Catalog.pdf) has one page per circuit with its schematic, simulated plot and key result. Rebuild it with `python docs/circuits/make_catalog.py` (needs ngspice).

- **1 to 5**: ready-made circuits. You give a spec, SpiceMCP sizes the parts and simulates them.
- **6**: a new circuit designed from scratch with the general tools (not in the library).
- **7 to 14**: circuits used to test the schematic drawer. It was never tuned on them.
- **15 to 16**: transistor-level building blocks used inside the circuits above.
- **17**: the Chua's chaotic circuit demo at the top of the README.

Circuits 1 to 14 were all drawn by SpiceMCP and read back by LTspice 26 on Windows as the
same circuit, pin by pin. Simulated numbers are from ngspice on the current version (0.13).

| No. | Circuit | What it does | Key result |
|---:|---|---|---|
| 1 | RC low-pass filter | First-order passive filter, 20 dB/decade roll-off | Cutoff 1000.0 Hz for a 1 kHz spec; -20.0 dB one decade up |
| 2 | Sallen-Key low-pass filter | Second-order active filter (Butterworth, Q 0.707), 40 dB/decade | -3 dB at 9998 Hz for a 10 kHz spec; -40.3 dB one decade up |
| 3 | Non-inverting amplifier | Gain 1 + R2/R1 around the transistor-level op-amp | Gain 20.00 dB (x10); bandwidth 1.12 MHz; phase margin 85° |
| 4 | LDO voltage regulator | 5 V in, 3.3 V out at 0.1 A, PMOS pass device | 3.301 V out; phase margin 2° → 72° after adding a series resistor to the output cap |
| 5 | Buck DC-DC converter (closed loop) | 12 V in, 5 V out at 1 A, 500 kHz, PWM controller | 5.00 V, 33 mV ripple, 89.5% efficient; boards in spec 22% → 100% with the closed loop |
| 6 | Instrumentation amplifier | Three op-amps, gain 100, rejects hum common to both input wires | 40.0 dB gain, 104 kHz bandwidth, 127 dB hum rejection, 70° phase margin; 100% of boards pass with 0.1% resistors |
| 7 | Inverting amplifier | Gain −10 with the transistor-level op-amp | 19.98 dB (x−9.98); drawing score 100/100 |
| 8 | Common-source MOSFET amplifier | Single-transistor voltage amplifier | Gain x0.80 (−2 dB) at 0.11 mA bias; score 99/100 |
| 9 | Wheatstone bridge | Resistor bridge for sensing small changes | 216 mV out for a 10% arm change at 10 V; score 99/100 |
| 10 | Boost converter | 5 V step-up switching converter | 9.28 V out at 50% duty (open loop); score 100/100 |
| 11 | Two-stage amplifier with feedback | Two op-amps with an RC between them, gain 10 overall | Gain 20.00 dB (x10.00); score 100/100 |
| 12 | Pi filter | C-L-C low-pass for RF / supply filtering | −82 dB at 100 MHz; score 100/100 |
| 13 | 5-stage RC ladder | Five cascaded RC sections | −3 dB at 13.2 kHz, 100 dB/decade roll-off; score 100/100 |
| 14 | LC tank | Inductor-capacitor resonator | Resonates at 5.01 MHz (theory 5.03 MHz); score 100/100 |
| 15 | Two-stage CMOS op-amp | 8 MOSFETs, sized from a target gain-bandwidth | 87.5 dB open-loop gain, 10 MHz unity-gain for a 10 MHz spec, 1.1 mA |
| 16 | Comparator with gate drive | Op-amp stages plus a tapered inverter buffer (12 MOSFETs) | 0 to 12 V PWM output from a signal-vs-ramp input; drives the buck's power MOSFET directly |
| 17 | Chua's chaotic circuit | Nonlinear oscillator with a negative-resistance diode | Double-scroll attractor and period-doubling route to chaos (see README gallery) |

## Where each result comes from

- 1 to 5: `docs/reports/step3/`, `step7/` (LTspice numbers) and `step8/` (closed loop, stability).
- 6: the independent verification report (`docs/verification/`).
- 7 to 14: `docs/reports/step4/` (drawings), `tests/test_layout.py` and `docs/circuits/` (simulations).
- 15 to 16: `docs/reports/step6/` and `spicemcp/opamp.py`.
- 17: `gallery/chua/`.
