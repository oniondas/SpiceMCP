# Changelog

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
