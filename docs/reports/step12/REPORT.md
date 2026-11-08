# Step 12: eight more ready-made circuits and a parts list to order (v0.17.0)

Phase 3 of the industry-readiness roadmap. Picture: `yield_new.png`; numbers:
`results.json` (built by `make_report.py` from `after.json`, measured by `measure/run.py`
with ngspice, seed 12, defaults a user gets, start point straight to `finish_design`).

## Before and after

| | v0.16.0 | v0.17.0 |
|---|---|---|
| Ready-made circuits | 5 | 13 |
| Circuits in the catalog | 17 | 25 |
| Parts list file | none | bom.csv + bom.xlsx |
| Tokens re-sent every turn | 3,004 | 3,004 |

## Yield of each new circuit (part tolerance, supply ±5%, -40/25/85 °C)

| template | boards passing | fails most | part driving it | finish minutes |
|---|---|---|---|---|
| inverting_amp | 24/30 | gain_db | R2 | 0.0 |
| bandpass | 22/30 | edge_hi_db | R3 | 0.0 |
| current_sense | 18/30 | err_fs_pct | R2 | 0.1 |
| adc_driver | 30/30 | - | - | 0.1 |
| voltage_reference | 29/30 | vroom | R1 | 0.0 |
| protection | 30/30 | - | - | 0.0 |
| boost | 8/10 | vout | CI | 34.1 |
| flyback | 10/10 | - | - | 31.3 |

Boost and flyback: 10 boards each (a board is a 40 to 55 s transient per temperature).

## Found and fixed while measuring

- ADC driver 1/30: the settling limit included the op-amp's fixed offset (0.4 LSB).
  Settling is now measured against the final value (0.06 LSB); offset is reported apart.
  30/30.
- Bandgap 21/30: ±0.5% initial accuracy is not reachable untrimmed with 1% resistors. New
  `acc_pct` spec field, default 1%. 29/30.
- Protection: the stress check rated the series resistor for 370 W, from the 1 ms surge.
  Surge decks are now left out (`* stress: skip`); pulse ratings are in the working.
- Parts list: the flyback's windings were two inductors, one with an unevaluated value;
  now one transformer line. Fixed values off the standard series are flagged.

## Still weak, stated honestly

- Inverting amp, band-pass, current sense: 18 to 24 of 30. Their limits are tighter than
  1% resistors / 5% capacitors allow; the answer names the part to tighten.
- Parts the optimiser does not tune are held exact in the yield run (marked in the list).
- Flyback not drawn (no transformer symbol); boost and bandgap drawings score 43 and 59.
- The new drawings are not yet re-read in LTspice on Windows.
- `finish_design` on the boost and flyback takes about 30 minutes on 4 cores (each board
  is three long transients). It runs in the background with progress, but it is slow.
