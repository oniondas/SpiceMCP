# SpiceMCP

LTspice MCP server where **the server owns the optimization state, not the LLM**.

The model contributes topology and strategy. Candidate identity, simulation history,
dedup, best-so-far, sensitivities and rollback all live in SQLite and are re-derived
on every call, so a long optimization can't drift into remembering a circuit that
never existed.

## Install

```bash
pip install -e ".[dev]"
pytest -q                                
```

LTspice is auto-detected (`AppData\Local\Programs\ADI\LTspice\LTspice.exe`,
`Program Files\LTC\LTspiceXVII\XVIIx64.exe`, …). Override with `LTSPICE_EXE`.

Register with your MCP client:

```json
{
  "mcpServers": {
    "spicemcp": {
      "command": "python",
      "args": ["-m", "spicemcp.server"],
      "env": { "SPICEMCP_PROJECT": "C:/path/to/your/circuit/project" }
    }
  }
}
```

State lands in `$SPICEMCP_PROJECT/.ltspice-mcp/`:

```
state.db      authoritative state
candidates/   cand_XXXX.cir, byte-exact netlists for rollback
simulations/  LTspice working dirs and logs
cache/  reports/
```

## Metrics come from `.meas`

There is no `.raw` waveform parser. Every metric you want to optimize is a `.meas`
directive in the netlist, and the server reads the values back from LTspice's `.log`.
The metric definitions then live with the circuit, versioned alongside it.

```spice
.ac dec 100 1 10Meg
.meas AC gain_db MAX mag(V(out))          ; mag(), NOT db() -- see below
.meas AC bandwidth WHEN mag(V(out))=0.707 FALL=1
.meas TRAN power AVG (-I(V1)*V(vcc))
```

**Never wrap an AC `.meas` in `db()`.** LTspice already reports AC measurement
magnitudes in dB, so `db()` converts twice — and it never errors, it just returns a
smaller plausible number. Verified on 26.0.2: a gain of 100 measures as `40dB` via
`mag()` but `32.04dB` (= 20·log10(40)) via `db()`. The server lints for this and
returns a warning alongside the metrics.

AC results are complex, so the phase is available too, as `<name>_deg`:

```
gdb: MAX(mag(V(out)))=(40.0dB,-159.417738334°)   ->  gdb = 40.0, gdb_deg = -159.42
```

One more trap, because two `.meas` forms print the same shape with opposite meanings:

```
bw:   mag(V(out))=0.7071  AT 159158.003411       -> 159158  (WHEN: the crossing)
p050: V(out) =0.140915020014 at 6.666666667e-05  -> 0.1409  (FIND AT: the value)
```

In a `WHEN` measure the number after `=` is the trigger level *you wrote* and `AT`
carries the result; in `FIND ... AT` it is the reverse. LTspice separates them only by
case — uppercase `AT` for a point it found, lowercase `at` for one you specified — so
the parser is case-sensitive here. Getting it backwards returns the sample time as the
measurement, which plots as a perfectly plausible straight line.

## Usage

`start_optimization` once, then let `run_optimization` do the iterating:

```python
start_optimization(
  run_id="amp_001",
  netlist_template=open("amp.cir").read(),   # tunables written as {R1}, {C1}
  param_space={"R1": {"min": 1e3, "max": 100e3},
               "C1": {"values": [1e-9, 4.7e-9, 1e-8]}},
  objectives=[{"metric": "gain_db",   "direction": "max"},
              {"metric": "bandwidth", "direction": "max"},
              {"metric": "power",     "direction": "min"}],
  constraints={"bandwidth": ">100000", "power": "<0.005"},
  seed=42)

run_optimization(run_id="amp_001", iterations=40)   # 40 sims, ONE compact answer
```

A two-sided bound needs the list form, since a dict can't hold two entries for one
metric: `constraints=[{"metric": "fc", "op": ">", "value": 9500},
{"metric": "fc", "op": "<", "value": 10500}]`.

`run_optimization` returns a summary, not a transcript: best candidate, constraint
status, the strongest measured sensitivity, the most promising unexplored region,
`step_frac_final` (how far the step had to shrink), and `at_param_space_bound` — which
parameters sit on a `min`/`max`/list edge. That last one matters: a bounded search
always reports *an* optimum, and if the winner is pinned to the fence you drew, the
answer is "widen `param_space`", not "converged".

The full history stays queryable but never arrives unasked.

## Schematics from LTspice's own parts

`spicemcp.asc` writes a real `.asc` using LTspice's symbol library, so a candidate opens
as a schematic you can probe and edit — not as a netlist in a text window. Pin offsets
are read from the actual `.asy`, which means `res`, `cap`, `OpAmps/opamp`, `nmos`, `npn`
and everything else in `lib/sym` work with no per-part table.

Wire by **pin name**; nothing takes a pin coordinate:

```python
from spicemcp import asc
sh = asc.Sheet()
V1 = sh.part("voltage", "V1", (0, 80), value="AC 1")
R1 = sh.part("res", "R1", (80, 112), "R270", value=1849.60938)
U1 = sh.part("OpAmps/opamp", "U1", (480, 176), "M180",
             SpiceLine="Aol=1Meg", SpiceLine2="GBW=1G")
sh.net(V1["+"], R1["A"])
sh.route(R1["B"], U1["noninvin"], "VH")     # L-shaped, no intermediate points
sh.gnd(V1["-"]);  sh.flag(U1["out"], "out")
sh.directive(".lib opamp.sub", ".ac dec 400 100 1Meg")
assert "XU1 out b out opamp" in asc.check(sh.write("f.asc"))
```

`check()` netlists the drawing back through LTspice and returns its element lines. Use
it — it is the only way to know the picture is the circuit you meant. A wrong
orientation or an unwired pin produces a schematic that opens and simulates happily,
and comparing against the candidate netlist is what catches it. Directives and notes
auto-stack above and below the drawn content, so text placement isn't a coordinate
either.

Use the library part, not an equivalent model: `E1 out 0 in out 1e6` is a VCVS, and
`OpAmps/opamp` is a single-pole amplifier with `Aol` and `GBW` you can dial. `GBW` is a
real knob, not a formality — on a 10 kHz Sallen-Key, going from an effectively ideal
`GBW=1G` to the block's own `GBW=10Meg` default moves the corner 1.55 Hz and the step
overshoot from 5.76 % to 5.81 %.

### Tools

| lifecycle | evaluation | state queries |
|---|---|---|
| `start_optimization` | `evaluate_candidate` | `get_best_candidate` |
| `stop_optimization` | `run_optimization` | `get_pareto_frontier` |
| `get_optimization_status` | `select_next_experiment` | `get_optimization_history` |
| `list_runs` | `simulate_netlist` | `get_candidate` / `get_previous_candidates` |
| | `check_ltspice` | `compare_candidates` / `get_similar_candidates` |
| | | `get_parameter_sensitivity` |
| | | `get_explored_parameter_range` |
| | | `get_failed_candidates` |
| | | `get_optimization_trace` |
| | | `rollback_to_candidate` |

## Guarantees worth knowing

- **A worse iteration cannot demote the incumbent.** `best_candidate` is a `MAX(score)`
  query, and `score` is a pure function of a candidate's metrics (per-objective `ref`
  scales fixed at run creation). Nothing to overwrite, so nothing can be overwritten.
- **Modification never mutates.** A changed parameter set is a new `cand_NNNN` with a
  `parent_id`; the tree reconstructs exactly how any candidate was produced.
- **Duplicates aren't re-simulated.** Lookup by `sha256(design) + sha256(sim_config)`.
  Same circuit under different conditions is a *different experiment*, not a duplicate.
- **`rollback_to_candidate` restores stored bytes**, never a reconstruction.
- **Sensitivities are measured.** `finite_difference` means it came from candidate pairs
  differing in that parameter alone; `ols_marginal` is a weaker correlational fit, and
  the method is reported so you can tell them apart.
- **Every parameter gets screened once.** A parameter nobody has varied has no measured
  sensitivity, so no amount of exploiting can pick it — the search would fixate on
  whichever knob the seed happened to move. Unmeasured therefore outranks unexploited,
  and a decade-spaced `values` list steps to the adjacent entry rather than by a
  fraction of its span (±15% of `1e-8` snaps back to `1e-8`, which would freeze every
  E-series part you list).
- **The search is scale-free, cycles coordinates, and refines its step.** Three ways a
  bounded local search reports a confident near-miss instead of an answer, all three
  found by running a real filter to completion:
  - ranking parameters by raw `d(metric)/d(param)` compares farads against ohms, so the
    capacitor always wins — leverage is scaled by each parameter's range instead;
  - greedy coordinate descent never revisits a lower-ranked coordinate while a
    higher-ranked one still has untried values, and a continuous one always does, so
    coordinates take turns;
  - a step fixed at a fraction of each range can bracket an optimum but never enter a
    tolerance window narrower than one step, so it halves on stagnation.
- **Failures are remembered and avoided**, classified as `convergence_failure`,
  `structural_error`, `constraint_violation`, `numerical_instability`, …

## Deliberate simplifications

| skipped | add when |
|---|---|
| `.raw` waveform parsing | a metric can't be expressed as `.meas` |
| Bayesian optimization / GP surrogate | a single sim is slow enough that ~30 wasted evals beats a surrogate's complexity |
| step halving on stagnation, not a line search or trust region | a sim is slow enough that the evaluations spent bracketing cost more than the bookkeeping |
| one parameter moves per iteration (coordinate descent, not a full compass poll) | the metric surface has strong parameter interactions the per-coordinate view misses |
| component-graph IR (topology edits are new templates) | a tool needs to rewrite topology programmatically |
| incremental sensitivity updates (O(n²) per iteration) | runs exceed a few hundred candidates |
| `promising_unexplored_region` is the widest untried gap, not direction-aware — it can point away from where the metric improves | you want it as a search directive rather than a coverage hint (the actual search already uses measured sensitivity) |
| `asc.Sheet` places parts at coordinates you pick; only pins, routing and text are computed | a topology arrives that nobody wants to lay out by hand — then write a placer, not more templates |

## The hallucination-loop test

`tests/test_hallucination_loop.py` drives the sequence that makes memory-based agents
panic — improve, improve, **best**, worse, worse, improve — and asserts the incumbent
survives it, that it also legitimately updates when something is genuinely better, that
duplicates aren't re-simulated, that sensitivities match closed-form derivatives, and
that a simulated process restart loses nothing.
