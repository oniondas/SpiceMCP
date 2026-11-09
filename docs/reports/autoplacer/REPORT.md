# Schematic auto-placer 1.1.0: before and after

PDF: `AutoPlacer_Report.pdf` (rebuild: `python docs/reports/autoplacer/make_report.py`).

Fourteen hard circuits: six templates and eight with no template. Score is the
readability lint, 0 to 100. Every drawing is checked pin by pin to be the netlist's
circuit, before and after.

| Circuit | Before (1.0.0) | After (1.1.0) | Blocks found |
|---|---:|---:|---|
| LDO regulator (template) | 76 | 100 | |
| Buck converter (template) | 69 | 88 | |
| Bandgap reference (template) | 59 | 95 | |
| Boost converter (template) | 43 | 82 | |
| Flyback converter (template) | refused | 81 | transformer |
| Band-pass filter (template) | 88 | 88 | |
| Instrumentation amp | 26 | 75 | |
| 8-MOSFET op-amp, transistor level | 4 | 51 | diff pair, mirrors |
| State-variable filter | 33 | 86 | |
| BJT diff pair + mirror + follower | 67 | 100 | diff pair, mirror |
| Wien-bridge oscillator | 96 | 93 | |
| Class-AB stage with op-amp | 0 | 88 | push-pull |
| 4th-order Sallen-Key | 100 | 100 | |
| Bare flyback | refused | 98 | transformer |

Average 55 (over the 12 the old drawer could draw) -> 88 (all 14). Drawing time 0.2 s
to 5.4 s per circuit.

## What changed

- `spicemcp/blocks.py` finds diff pairs, current mirrors, push-pull stages (with the bias
  string between the bases) and coupled windings from the wiring alone. The placer draws
  each the familiar way and the search is charged for any block not in its shape.
- Placement is a search over a few dozen candidates (supplies wired or as rail labels,
  feedback laid out forward or back, crossing-reduction row orders, seeded restarts),
  each routed, verified and scored. Deterministic.
- `lint` adds `jump_labels` (signals carried by label pairs) and `aspect` (page shape).

## Still weak

- The transistor-level op-amp scores 51: it reads as an op-amp now, but has four
  crossings around the bias and compensation network.
- The Wien oscillator scores 3 points lower than before (93 vs 96).
- Blocks not yet recognised: cascodes, H-bridges, Darlingtons, LC/crystal oscillator
  cores. These fall back to the general rules.
- Above about 40 parts a single sheet gets crowded; multi-sheet drawing is not done.
