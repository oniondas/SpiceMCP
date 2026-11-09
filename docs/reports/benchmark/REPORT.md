# Benchmark: SpiceMCP against designing without it

Three circuits (RC low-pass 1 kHz, x10 amplifier, 12 V to 5 V buck), designed three ways
and measured by one checker written separately from SpiceMCP (`measure/checker.py`).
Full write-up with charts: `SpiceMCP_Benchmark.pdf`.

| circuit | SpiceMCP 1.1 | model alone, no tools | model + raw ngspice |
|---|---|---|---|
| RC 1 kHz ±2 % (200 boards) | meets, 43 % yield | meets, 39.5 % | meets, 39.5 % |
| x10 amp ±0.1 dB (200 boards) | meets, 66.5 % | meets, 67.5 % | meets, 89 %* |
| buck 5 V ±2 %, ≤50 mV (30 boards × 3 temps) | meets, 100 % | meets, 100 %* | meets, 100 % |

\* 1.01 kΩ is not a buyable E96 value (with 1.00 kΩ: 67.5 %); the model-alone buck uses an
ideal switch.

- Same pass rate for all three: a strong model already writes the textbook answer for
  these circuits.
- SpiceMCP's edge is proof and paperwork: measured yield, buyable values, ratings to buy,
  schematic, report.
- It lost on time (14 min for the buck), tokens (197k vs 145k billed input) and amplifier
  yield (off-centre resistor pair; a centred buyable pair gives 91 %).

## Reproduce

```
apt-get install ngspice
PYTHONPATH=. python docs/reports/benchmark/measure/arm_spicemcp.py out.json   # SpiceMCP arm
python docs/reports/benchmark/measure/checker.py <arm> <rc|amp|buck> <netlist.cir> [boards]
python docs/reports/benchmark/make_report.py
```

The specs handed to every arm are in `baselines/SPECS.md`, their parts in
`baselines/parts.lib`, and each arm's final netlists in `baselines/<arm>/`. Checker output
is in `results/checks.json`; time, calls and tokens in `results/effort.json`.
