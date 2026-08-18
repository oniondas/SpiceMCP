"""SpiceMCP: LTspice MCP server with a stateful optimization layer.

The MCP process owns all optimization state. The LLM's job is topology and
strategy; identity, history, dedup, best-so-far and rollback are never its
responsibility. Every tool answer is derived from SQLite at call time.
"""

from __future__ import annotations

import os

from mcp.server.fastmcp import FastMCP

from . import db, engine, feasibility, plots, render, report, sim

mcp = FastMCP("spicemcp")

PROJECT = os.environ.get("SPICEMCP_PROJECT", os.getcwd())
_con = None


def con():
    global _con
    if _con is None:
        _con = db.connect(PROJECT)
    return _con


def _guard(fn, *a, **kw):
    try:
        return fn(*a, **kw)
    except engine.EngineError as e:
        return {"error": str(e)}


# --- circuit / simulation ------------------------------------------------------

@mcp.tool()
def simulate_netlist(netlist: str, sim_config: dict | None = None) -> dict:
    """Run one LTspice netlist immediately, outside any optimization run.

    Metrics come from `.meas` directives in the netlist. No state is recorded.
    """
    root = db.state_root(PROJECT)
    res = sim.simulate(netlist, {}, sim_config or {"backend": "ltspice"},
                       root / "simulations" / "adhoc", "adhoc")
    return {"status": res.status, "metrics": res.metrics, "warnings": res.warnings,
            "failure_type": res.failure_type, "failure_message": res.failure_message,
            "failure_detail": res.failure_detail, "log_path": res.log_path}


@mcp.tool()
def check_ltspice() -> dict:
    """Report the LTspice executable and state directory in use."""
    return {"ltspice_exe": sim.find_ltspice(), "project": PROJECT,
            "state_dir": str(db.state_root(PROJECT)),
            "analytic_models": sorted(sim.ANALYTIC_MODELS)}


@mcp.tool()
def simulate_sweep(netlist: str, param_name: str, values: list[float],
                   timeout_s: float = 120.0) -> dict:
    """Execute a multi-point LTspice parameter/corner sweep and return aggregated results.

    One LTspice launch for the whole sweep, via `.step`. `param_name` is resolved against
    the netlist, so an ordinary deck can be swept without being re-authored as a template:

      "temp"   -> `.step temp list ...`           corner / PVT sweep
      "{GAIN}" -> `.step param GAIN list ...`     the deck is already parametric
      "R1"     -> R1's literal value is replaced by a stepped parameter

    Returns `metrics_table`: one row per point, `{"step", "value", "metrics"}`. The metric
    names are whatever the netlist's own `.meas` directives are called -- none are assumed.
    Each row's `value` is read back from what LTspice itself reported, so a point the
    simulator skipped cannot shift the rest of the table onto the wrong values;
    `value_source` says where it came from (`log`, `raw`, or `requested` for a row nothing
    reported).

    A deck with no `.meas` still returns its points and `raw_path` (read it with `raw.py`)
    plus a warning saying so. `values` needs at least two distinct finite numbers.
    """
    root = db.state_root(PROJECT) / "simulations" / "sweep"
    try:
        return sim.simulate_sweep(netlist, param_name, values, root, "sweep",
                                  {"timeout_s": timeout_s})
    except sim.SweepError as e:
        return {"status": "failed", "failure_type": sim.STRUCTURAL_ERROR,
                "failure_message": str(e), "sweep_param": {"name": param_name,
                                                           "values": values},
                "metrics_table": [], "raw_path": None}


@mcp.tool()
def render_schematic(asc_path: str, output_path: str = "schematic.png",
                     style: str = render.DEFAULT_STYLE, dpi: int = 300) -> dict:
    """Render an LTspice .asc schematic into a professional PNG or SVG.

    Draws the schematic that is in the file: each symbol's geometry comes from its own
    `.asy`, transformed by that instance's orientation, so the parts are the standard
    electrical symbols LTspice draws and not generic boxes. Wires, junction dots, net
    labels, power rails, grounds, ports, pin names, designators, values and SPICE
    directives all come from the file.

    Format follows the extension: `.svg` (vector, needs nothing) or `.png` (needs
    matplotlib, honours `dpi`). Both are emitted from the same geometry, so they are the
    same drawing. The canvas is computed from the schematic's own bounding box plus a
    small margin -- nothing is cropped and there is no empty page.

    styles: `tech_minimal` (the default -- modern EDA, charcoal ink, colour only on
    input/output/feedback nets), `classic` (monochrome, print and publication),
    `sketch` (engineering notebook: graph paper and a slightly imperfect line, with the
    topology still exact). Same geometry in all three.

    Waveform plots (`generate_design_report`, the `plot_*` helpers) draw their traces
    from one five-colour data-visualisation ramp and their chrome from `tech_minimal`,
    so a schematic and its plots are one document rather than three tools' defaults.

    Deterministic: the same file and style produce the same image every time.
    """
    try:
        return render.render(asc_path, output_path, style, dpi)
    except (render.RenderError, OSError, ValueError) as e:
        return {"success": False, "error": f"{type(e).__name__}: {e}",
                "output_path": None}


@mcp.tool()
def get_visual_style() -> dict:
    """The house style: the schematic styles available, and the plot colour ramp.

    Read this instead of guessing a hex when composing a figure, a diagram or an HTML
    page that has to sit beside this server's output. `series` is ordered, and each entry
    already has a job -- reusing one for something else is what makes a set of figures
    stop reading as one document.
    """
    st = render.style(render.DEFAULT_STYLE)
    return {
        "default_style": render.DEFAULT_STYLE,
        "schematic_styles": sorted(render.STYLES),
        "series": list(render.SERIES),
        "series_roles": {
            "trace": plots.C_TRACE,
            "trace_secondary_and_bars": plots.C_TRACE2,
            "measured_point": plots.C_MARK,
            "limit_or_spec": plots.C_LIMIT,
            "margin_or_band": plots.C_BAND,
        },
        "chrome": {"background": st["bg"], "ink": st["roles"]["wire"]["c"],
                   "faint": st["roles"]["value"]["c"],
                   "grid": (st.get("grid") or {}).get("c"),
                   "net_in": st["roles"]["net_in"]["c"],
                   "net_out": st["roles"]["net_out"]["c"],
                   "net_fb": st["roles"]["net_fb"]["c"]},
        "fonts": {"sans": render.SANS, "mono": render.MONO},
        "note": "The ramp is one monotonic lightness sweep, so it survives greyscale "
                "printing; the first two entries are far enough apart in hue to carry a "
                "two-trace plot alone.",
    }


@mcp.tool()
def check_feasibility(objectives: list, constraints: dict | list | None = None,
                      param_space: dict | None = None,
                      netlist_template: str | None = None,
                      circuit_type: str = "general", mode: str = "practical",
                      technology_params: dict | None = None) -> dict:
    """Pre-flight check: is this specification physically and structurally achievable,
    before any simulation or optimization budget is spent.

    Costs nothing (no LTspice launch). Three layers report separately:
      static    -- self-contradictory bounds, over-unity efficiency, bad param_space
      template  -- metrics with no `.meas`, {PARAM} vs param_space, gain with no active
                   element, reactive-element count vs required filter order
      physics   -- closed-form limits per circuit_type: filter order (Butterworth /
                   Chebyshev / elliptic), slew rate and GBW against current and C_load,
                   4kTR and kT/C noise floors, LDO dropout / headroom / Vout-over-Vin
                   efficiency ceiling, DC-DC topology and duty cycle

    circuit_type: "general" | "filter" | "opamp" | "ldo" | "dcdc"
    mode: "practical" (physics + engineering guidelines) | "theoretical" (hard limits
      only) | "concept" (no netlist needed -- check an idea before writing a deck)
    technology_params: SI units. vdd, vin, vout, cload, iload, itotal, temp_c, ft, rmin,
      vov, rds_on, lmin, f_pass, f_stop, ripple_db, atten_db, filter_kind, duty_max, fsw.
      Checks that need a value you did not give are listed in `checks_skipped`, never
      silently passed.

    `status`: "infeasible" (a hard bound is violated -- fix the spec), "impractical"
    (buildable, with flagged concerns), "feasible", "unverified" (nothing ruled it out
    but the domain physics could not be evaluated). `passed` is false only for
    "infeasible".
    """
    return _guard(lambda: feasibility.check(
        objectives, constraints, param_space, netlist_template, circuit_type, mode,
        technology_params).to_dict())


# --- run lifecycle -------------------------------------------------------------

@mcp.tool()
def start_optimization(run_id: str, netlist_template: str, param_space: dict,
                       objectives: list, constraints: dict | list | None = None,
                       sim_config: dict | None = None, seed: int = 0,
                       notes: str = "", preflight: dict | None = None) -> dict:
    """Create a persistent optimization run.

    netlist_template: an LTspice netlist where tunable values appear as {NAME}
      placeholders, and every metric you care about has a `.meas` directive.
    param_space: {"R1": {"min": 1e3, "max": 1e5}, "C1": {"values": [1e-9, 1e-8]}}
    objectives:  [{"metric": "gain_db", "direction": "max", "weight": 1.0}]
    constraints: {"bandwidth": ">100000", "power": "<0.005"}
    Scores are a pure function of metrics, so the best candidate can never be
    retroactively demoted. State survives an MCP restart.

    The spec is checked by `check_feasibility` first and the run is NOT created if a
    requirement is impossible for any topology; the answer then carries the violations
    and suggested fixes. `preflight` tunes that check:
    {"bypass": true} to skip it entirely, plus optional "circuit_type", "mode" and
    "technology_params" to get the domain physics checked here too.
    """
    return _guard(engine.start_run, con(), run_id, netlist_template, param_space,
                  objectives, constraints, sim_config, seed, notes, preflight)


@mcp.tool()
def stop_optimization(run_id: str) -> dict:
    """Stop a run. History and candidates are retained."""
    return _guard(engine.stop, con(), run_id)


@mcp.tool()
def get_optimization_status(run_id: str) -> dict:
    """Compact status: best candidate, constraint pass/fail, one useful observation,
    and the most promising unexplored region. Use this instead of replaying history."""
    return _guard(engine.summary, con(), run_id)


@mcp.tool()
def list_runs() -> list[dict]:
    """All persisted runs."""
    return [{"run_id": r["run_id"], "status": r["status"],
             "created_at": r["created_at"], "current_candidate": r["current_candidate"]}
            for r in con().execute(
                "SELECT * FROM runs ORDER BY created_at DESC").fetchall()]


# --- evaluation ----------------------------------------------------------------

@mcp.tool()
def evaluate_candidate(run_id: str, params: dict, parent_id: str | None = None,
                       sim_config: dict | None = None, force: bool = False) -> dict:
    """Simulate one parameter set, with dedup, scoring and constraint checking.

    If this exact design+config was already simulated, the cached result is returned
    with cached=true and no LTspice run happens (pass force=true to override).
    A modification never overwrites a candidate -- it creates a new one.
    """
    return _guard(engine.evaluate, con(), run_id, params, parent_id, sim_config,
                  force, "llm")


@mcp.tool()
def run_optimization(run_id: str, iterations: int = 10,
                     step_frac: float = 0.25) -> dict:
    """Run `iterations` experiments locally and return ONE compact summary.

    Preferred entry point: the engine picks each next experiment from empirical
    sensitivities, constraint violations and unexplored regions, so the LLM is not
    in the loop per simulation.
    """
    return _guard(engine.optimize, con(), run_id, iterations, step_frac)


@mcp.tool()
def select_next_experiment(run_id: str, step_frac: float = 0.25) -> dict:
    """Propose the next parameter set (without simulating) plus the reason why.

    Guaranteed not to propose an already-evaluated design or a known-failed one.
    """
    return _guard(engine.select_next, con(), run_id, step_frac)


# --- state queries -------------------------------------------------------------

@mcp.tool()
def get_best_candidate(run_id: str) -> dict:
    """Best and best-feasible candidate, derived from the database."""
    return _guard(engine.get_best, con(), run_id)


@mcp.tool()
def get_pareto_frontier(run_id: str, include_infeasible: bool = False) -> dict:
    """Non-dominated candidates across all objectives, as compact summaries."""
    r = _guard(engine.pareto, con(), run_id, include_infeasible)
    return r if isinstance(r, dict) else {"candidates": r}


@mcp.tool()
def get_optimization_history(run_id: str, limit: int = 20, offset: int = 0) -> dict:
    """Most recent iterations, newest first. Paged -- never the whole database."""
    r = _guard(engine.history, con(), run_id, limit, offset)
    return r if isinstance(r, dict) else {"iterations": r}


@mcp.tool()
def get_previous_candidates(run_id: str, limit: int = 20) -> dict:
    """Candidate lineage: id, parent, generation, params, whether evaluated."""
    rows = con().execute(
        "SELECT d.*, (SELECT COUNT(*) FROM experiments e"
        " WHERE e.candidate_id=d.candidate_id) AS n_exp"
        " FROM designs d WHERE d.run_id=? ORDER BY d.candidate_id DESC LIMIT ?",
        (run_id, limit)).fetchall()
    return {"candidates": [
        {"candidate_id": r["candidate_id"], "parent_id": r["parent_id"],
         "generation": r["generation"], "origin": r["origin"],
         "params": db.jloads(r, "params"), "evaluated": r["n_exp"] > 0}
        for r in rows]}


@mcp.tool()
def get_candidate(run_id: str, candidate_id: str, include_netlist: bool = False) -> dict:
    """Full record for one candidate, including its experiments."""
    try:
        d = engine.get_candidate(con(), candidate_id)
    except engine.EngineError as e:
        return {"error": str(e)}
    exps = con().execute(
        "SELECT * FROM experiments WHERE candidate_id=? ORDER BY iteration",
        (candidate_id,)).fetchall()
    out = {"candidate_id": candidate_id, "run_id": d["run_id"],
           "parent_id": d["parent_id"], "generation": d["generation"],
           "origin": d["origin"], "params": db.jloads(d, "params"),
           "topology_fp": d["topology_fp"], "design_fp": d["design_fp"],
           "experiments": [engine._experiment_summary(e) for e in exps]}
    if include_netlist:
        out["netlist"] = d["netlist"]
    return out


@mcp.tool()
def compare_candidates(run_id: str, candidate_ids: list[str]) -> dict:
    """Side-by-side params and metrics; with exactly two ids, also the deltas."""
    return _guard(engine.compare, con(), run_id, candidate_ids)


@mcp.tool()
def get_similar_candidates(run_id: str, candidate_id: str, k: int = 5) -> dict:
    """Nearest already-evaluated designs in normalized parameter distance."""
    r = _guard(engine.similar_candidates, con(), run_id, candidate_id, k)
    return r if isinstance(r, dict) else {"candidates": r}


@mcp.tool()
def get_parameter_sensitivity(run_id: str, parameter: str | None = None,
                              metric: str | None = None) -> dict:
    """Empirical parameter->metric sensitivities computed from real simulations.

    method=finite_difference means it came from pairs differing in that parameter
    alone (causal); ols_marginal is a correlational fit, so treat it as weaker.
    """
    r = _guard(engine.sensitivity, con(), run_id, parameter, metric)
    return r if isinstance(r, dict) else {"sensitivities": r}


@mcp.tool()
def get_explored_parameter_range(run_id: str, parameter: str | None = None) -> dict:
    """Which parameter values have been tried, and the widest untried interval."""
    return _guard(engine.explored, con(), run_id, parameter)


@mcp.tool()
def get_failed_candidates(run_id: str, limit: int = 20) -> dict:
    """Failures with classified causes: simulation, convergence, structural,
    constraint_violation, component_limit, numerical_instability."""
    r = _guard(engine.failed_candidates, con(), run_id, limit)
    return r if isinstance(r, dict) else {"failures": r}


@mcp.tool()
def get_optimization_trace(run_id: str, save: bool = False) -> dict:
    """Human-readable per-iteration trace, rendered from the database."""
    try:
        text = engine.trace(con(), run_id)
    except engine.EngineError as e:
        return {"error": str(e)}
    out = {"trace": text}
    if save:
        p = db.state_root(PROJECT) / "reports" / f"{run_id}_trace.txt"
        p.write_text(text, encoding="utf-8")
        out["path"] = str(p)
    return out


@mcp.tool()
def rollback_to_candidate(run_id: str, candidate_id: str) -> dict:
    """Restore a previously evaluated candidate exactly, from stored state.

    Returns the stored netlist path and its recorded results. Never reconstruct a
    past circuit from conversation memory -- use this.
    """
    return _guard(engine.rollback, con(), run_id, candidate_id)


@mcp.tool()
def generate_design_report(run_id: str, out: str | None = None,
                           schematic: str | None = None, plot: bool = True,
                           dc_audit: bool = False, pvt: bool = False,
                           monte_carlo: int = 0, supply: str | None = None,
                           node: str | None = None, r_tol: float = 0.01,
                           c_tol: float = 0.05) -> dict:
    """Write the final design report for a run as a Markdown document, and return its path.

    Six sections: executive summary and spec scorecard, schematic and BOM, optimization
    summary with baseline-vs-final metrics and measured trade-offs, waveform analysis with
    plots, robustness (DC bias / PVT / Monte Carlo yield), and an LTspice reproduction
    guide. Every number is read from the database or from a simulation launched while
    writing the file; nothing is hand-entered, so the report cannot disagree with the run.

    Sections 1-3 and 6 come from stored state and cost nothing. The rest each cost LTspice
    launches, so they are opt-in and a section that did not run says so in the document
    rather than being omitted:
      plot=True      Bode / transient / THD plots. Any flag below also triggers the one
                     shared re-run of the winner, whose fresh metrics are compared against
                     the stored ones in section 3.
      dc_audit=True  1 launch -- per-device Vds/Vgs/Vth, terminal current, region
      pvt=True       5 launches -- TT/SS/FF/SF/FS at -40/27/125 C and Vdd +-10%
                     (`supply` names the DC source to scale, e.g. "V1")
      monte_carlo=N  1 stepped launch -- N samples on passive tolerances; yield is counted
                     against this run's own constraints

    `schematic` embeds an existing image; none is generated, because a netlist carries no
    coordinates and an invented layout would be a picture of a different circuit.
    """
    return _guard(report.generate, con(), run_id, out=out, schematic=schematic, plot=plot,
                  dc_audit=dc_audit, pvt=pvt, monte_carlo=monte_carlo, supply=supply,
                  node=node, r_tol=r_tol, c_tol=c_tol)


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
