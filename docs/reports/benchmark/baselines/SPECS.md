# Benchmark specs (identical text handed to every arm)

Parts available: `parts.lib` (include it with `.include parts.lib`). It holds
`OPAMP10M` (in+ in- V+ V- out, 10 MHz GBW, for +-5 V), `OPAMP2M` (in+ in- V+ V- out,
2 MHz GBW, for a 0..12 V supply) and `CMP` (comparator, in+ in- V+ V- out, push-pull
output rail to rail, 0..12 V supply). Any ngspice primitive (R, C, L, D, VDMOS/MOS
models you write, sources, B-sources for references only) may be used.

Parts the board is built from: resistors E96 1 %, capacitors E12 5 %, inductors E12
20 %. Give every R, C and L a value from those series. The board must work from
-40 C to 85 C and with the supply +-5 % off nominal.

Netlist conventions (a common checker replaces the source and adds its own analyses):
input source named `V1` (circuits 1, 2) or `Vin` (circuit 3), output node `out`.
Leave out analyses and `.meas` lines, or they are ignored.

1. **RC low-pass filter.** First order. -3 dB cutoff 1 kHz +-2 %. Driven by an ideal
   source `V1 in 0`, output unloaded.
2. **Non-inverting amplifier.** Gain 20 dB +-0.1 dB (x10) at 1 kHz, -3 dB bandwidth at
   least 100 kHz, stable (10 mV step: overshoot 25 % or less). Supplies +-5 V on nodes
   `vcc`/`vee` (sources `Vcc`, `Vee`), input source `V1 in 0`, load 10 kOhm parallel
   100 pF on `out` (include `RL out 0 10k` and `CL out 0 100p`). Use `OPAMP10M`.
3. **Buck converter, closed loop.** 12 V in (`Vin vin 0 12`) to 5.0 V +-2 %, 1 A load
   (include `RL out 0 5`), switching 500 kHz, output ripple 50 mV peak to peak or less.
   The output must settle within 1.5 ms of power-up and hold 5 V +-2 % at 11.4 V and
   12.6 V input. Use `OPAMP2M`/`CMP` for the controller; switch and diode models are
   yours to write.
