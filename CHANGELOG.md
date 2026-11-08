# Changelog

## 0.15.0

- No call blocks for minutes. `run_optimization` stops at a time limit (50 s,
  `SPICEMCP_CALL_BUDGET_S`) and says to call again; the run's state is saved, so it
  continues. `finish_design` keeps working in the background: if it is not done in time
  it answers "still running", and calling it again for the same run returns the result.
  Both send MCP progress notifications while they work.
- Simulations run side by side, one per CPU core up to 8 (`SPICEMCP_WORKERS`): the
  yield boards, the standard-value combinations, the re-centring steps, a netlist's
  extra decks (stability, load step), the optimiser's first screening moves, and
  ngspice sweeps.
- `simulate` and sweeps use the configured backend (`SPICEMCP_BACKEND`) instead of
  always LTspice; on ngspice a sweep runs one simulation per value.
- Built-in blocks by name: a netlist that places `X1 in+ in- V+ V- out SPMCP_OA` (op-amp)
  or `SPMCP_CMP` (comparator) gets the subcircuit added automatically, for simulation
  and for drawing. No template run and netlist dump to borrow them.
- The suggested flow no longer calls `check_feasibility` before `start_optimization`
  (which runs the same check), so a custom netlist is sent once, not twice.
- Packaging for PyPI (`pip install spicemcp`): metadata, a build check in CI, and a
  publish workflow on version tags. Works with the `mcp` package 1.x and 2.x (2.x
  renamed FastMCP to MCPServer); CI tests both.

## 0.14.0

- MIT license (`LICENSE`, `license` in `pyproject.toml`).
- Fewer tokens per design. The tool list is 2,547 tokens instead of 4,270 (no schema
  `title`s, shorter field text, optional inputs without `null` alternatives, advanced
  inputs `sim_options`, `skip_preflight`, `seed` still accepted but not listed). Answers
  are one-line JSON with numbers to 6 digits. `start_from_template` answers in about 250
  tokens instead of 800 to 1,700, `finish_design` in about 300 instead of 850 to 1,750,
  `list_templates` in about 430 instead of 715. The hand calculation, the full
  feasibility report and the finish_design alternatives and spread are on the new
  `query_run(what="details")`.
- Yield is stated with a real confidence bound: `30/30 pass: yield is at least 90% (95%
  confidence)` (one-sided Clopper-Pearson), replacing the wrong "30 runs resolve yield to
  about 3%". The design report's Monte Carlo table says the same.
- Yield draws new boards each time: a fresh random seed per `finish_design`, reported in
  the answer; `seed=` repeats a run exactly. It was always seed 0 before.
- `SPICEMCP_BACKEND=ngspice` selects ngspice for every run without passing `sim_options`.
- The loop-break test parts (`Lbrk`, `Cinj`) no longer raise "impractical value" warnings.
- After a template run meets every limit, a design on a search bound no longer asks for a
  new run with wider ranges; the template's ranges are deliberate.
- The engineering report PDF is rebuilt (the v0.13.0 copy was two files written over each
  other and would not open), is written atomically, and a test checks it opens. Its
  schematic readability table adds today's scores next to the step 4 ones (LDO 76, buck 69).

## 0.13.0

- The buck template is closed loop: an integrating error amplifier, a comparator against
  a ramp and a push-pull gate drive (all transistor level) set the duty cycle. On 40
  random boards (parts in tolerance, input ±5%) 100% now meet 5 V ±2%, against 22% open
  loop. Its output capacitor is sized for the worst-case inductor and capacitor.
- Stability checks run with every simulation. A netlist can hold several decks; each is
  simulated and the measurements merged (`sim.decks`). The Sallen-Key, the amplifier and
  the LDO measure phase margin (`pm_deg`) and crossover (`ugf`) with the loop broken at
  the amplifier input, and must keep 45°. The LDO also measures a 10%-to-100% load step.
- The LDO has an output capacitor (`cout`, 1 µF) and a hand-sized resistor in series
  with it: with a bare ceramic capacitor the loop has 2° of phase margin and rings; with
  the resistor it has 72°.
- The op-amp's MOSFETs have gate and overlap capacitance, so its phase margin falls with
  the capacitance it drives (`cload`, 100 pF by default).
- ngspice: a complex `FIND V(x)` in AC is reported as dB and degrees, as LTspice does.
- Schematics: a capacitor behind a series resistor hangs below its node.
- LTspice: a run that reaches its operating point by Gmin stepping is no longer reported as
  failed; only a stepping that fails is.

## 0.12.0

- Checked in real LTspice 26 on Windows 11 (`tools/ltspice_check.py`). LTspice netlists all
  13 drawn schematics back to the same circuit, and all 5 templates meet their spec in
  LTspice. LTspice and ngspice agree within 2% (see `docs/reports/step7/REPORT.md`).
- The LTspice reference files the waveform tests read (`tests/fixtures/*.raw`, `*.log`) are
  committed, so the whole suite runs without LTspice.
- CI: GitHub Actions runs the tests on Ubuntu (with ngspice) and Windows, Python 3.11 and 3.13.
- The version is read from `pyproject.toml`; a test checks the changelog has an entry for it.
- Fixed a stale test that still called the old `simulate_sweep` tool.

## 0.11.0

- The templates use real devices instead of ideal controlled sources. A transistor-level
  two-stage CMOS op-amp (`opamp.py`, MOSFETs on real supply rails) replaces the `E`
  sources in the Sallen-Key and non-inverting amplifier and the behavioural error
  amplifier in the LDO. The buck switches a P-channel power MOSFET from a gate driver
  instead of an ideal switch. See `docs/reports/step6/REPORT.md`.
- The schematic drawer places 5-pin op-amp subcircuits (`opamp2` symbol, rail labels on
  the supply pins) and 3-pin power MOSFETs (`VDMOS`), and writes the op-amp's `.subckt`
  beside the `.asc` as a `.lib`.

## 0.10.0

- `finish_design` replaces `stop_optimization`. It ends a run and turns the best design
  into standard part values (E6/E12/E24/E48/E96), coarse parts first. The fine-step
  values are re-centred to maximise margin, and each candidate set gets a Monte Carlo
  yield run under part tolerance (and optional supply tolerance). It returns a parts
  list with margin and yield (see `docs/reports/step5/REPORT.md`).
- Yield sampling runs per simulation, so it works on the ngspice backend as well as
  LTspice.

## 0.9.0

- Schematics drawn from a netlist (`layout.py`): `render_schematic` accepts `.cir`/`.net`
  and places, routes and labels the parts, with signal flowing left to right and ground
  at the bottom. Every drawing is read back and checked against the netlist pin by pin,
  and refused if it is not the same circuit. A readability score comes with it
  (see `docs/reports/step4/REPORT.md`).
- `start_from_template` writes the run's schematic (`schematic_asc`).
- Part values are drawn the way people write them (`15.9n`, not `1.5915494e-08`). The
  `.asc` keeps the exact number.
- A bundled fallback symbol set, so drawing works without an LTspice install.
- `render_schematic`'s first argument is now `path` (was `asc_path`).

## 0.8.0

- Topology library (`list_templates`, `start_from_template`): RC and Sallen-Key low-pass,
  non-inverting amplifier, PMOS LDO, buck converter. Each one is hand-sized from design
  equations and verified by simulation (see `docs/reports/step3/REPORT.md`).
- `param_<NAME>` metrics: goals and limits on part values.
- Optional `ngspice` backend that translates LTspice `.meas` directives.
- Fix: a second run in one project crashed (candidate ids were per run, but stored as a
  project-wide key).
- Optimizer: a trust region on Newton repairs (each part changes at most 2x per move),
  local screening probes, and dB metrics modelled linearly. The Sallen-Key benchmark is now 10/10.

## 0.7.0

Optimizer (see `docs/reports/step2/REPORT.md`): Sallen-Key fc+Q benchmark 0/10 -> 9/10,
RC fc benchmark 7/10 -> 10/10.

- Log-scale search for ranges spanning 10x or more (`scale` per param: auto/log/linear).
- Opening Latin-hypercube global sample (skipped after a user-evaluated start point).
- Joint Newton repair of all limited metrics from measured log elasticities, with an
  active set for parts pinned at a bound.
- Repairs continue from a push that improved the goal, so the search slides along a limit.

## 0.6.0

Tool surface rebuilt so small models can use it (see `docs/reports/step1/REPORT.md`).

- 26 tools down to 11. `query_run(what=...)` replaces the 15 `get_*`, `list_runs` and
  `select_next_experiment` tools. `simulate` replaces `simulate_netlist` and
  `simulate_sweep`. `check_setup` replaces `check_ltspice`. `get_visual_style` is now the
  `spicemcp://style` resource.
- `start_optimization` takes `netlist`, `params`, `goals`, `limits` (typed, strict) instead
  of `netlist_template`, `param_space`, `objectives`, `constraints`. `preflight` is now
  `circuit_type` and `skip_preflight`; `sim_config` is `sim_options`.
- `check_feasibility` takes `goals`, `limits`, `params`, `netlist`, `tech`.
- `generate_design_report`: `plot` is `plots`; `node`, `r_tol`, `c_tol` use their defaults.
- Every answer has `next_step`; every error has `fix` and `example`.
- Pin `mcp<2` (2.x renamed FastMCP) and declare `pydantic>=2`.
