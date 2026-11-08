"""SpiceMCP: LTspice MCP server with a stateful optimization layer.

The MCP process owns all optimization state. The LLM's job is topology and
strategy; identity, history, dedup, best-so-far and rollback are never its
responsibility. Every tool answer is derived from SQLite at call time.

Tool surface rules (kept small on purpose, so weaker models call it correctly):
  * 11 tools. Run inspection is one tool, `query_run(what=...)`.
  * Each description is two short lines: what it does, when to use it.
  * Structured inputs are typed models (no "<0.005" strings, no dict-or-list).
  * Every answer carries `next_step`: the call that most likely comes next.
  * Every error carries `fix` and a correct `example` call.
Long explanations live in the README, not in the tool list.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Annotated, Literal

from mcp.server.fastmcp import FastMCP
from pydantic import BaseModel, ConfigDict, Field, model_validator

from . import db, engine, feasibility, layout, plots, render, report, sim, topologies

INSTRUCTIONS = """\
SpiceMCP designs circuits with LTspice. Typical flow:
1. check_feasibility  (is the spec possible?)
2. start_from_template (filters, amplifier, LDO, buck: sized for you), or
   start_optimization (your own netlist with {PARAM} placeholders and .meas lines)
3. run_optimization   (the server searches; you do not loop per simulation)
4. query_run          (status, best, pareto, history ...)
5. generate_design_report
Use simulate for a one-off netlist or a sweep. Follow `next_step` in each answer.
Never rebuild a past circuit from memory: use query_run(what="netlist") or
rollback_to_candidate."""

mcp = FastMCP("spicemcp", instructions=INSTRUCTIONS)

PROJECT = os.environ.get("SPICEMCP_PROJECT", os.getcwd())
_con = None


def con():
    global _con
    if _con is None:
        _con = db.connect(PROJECT)
    return _con


# --- input models ----------------------------------------------------------------

class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Param(_Strict):
    """A tunable value. Give min+max for a range, or values for a fixed list."""
    name: str = Field(description="Placeholder name in the netlist, e.g. R1 for {R1}")
    min: float | None = Field(None, description="Lowest value, SI units")
    max: float | None = Field(None, description="Highest value, SI units")
    values: list[float] | None = Field(None, description="Allowed values instead of a range")
    scale: Literal["auto", "log", "linear"] = Field(
        "auto", description="auto = log steps when max/min >= 10 (R, C, L, I)")

    @model_validator(mode="after")
    def _shape(self):
        if self.values is not None:
            if self.min is not None or self.max is not None:
                raise ValueError(f"{self.name}: give values OR min+max, not both")
            if not self.values:
                raise ValueError(f"{self.name}: values is empty")
        elif self.min is None or self.max is None:
            raise ValueError(f"{self.name}: needs min and max (or values)")
        elif not self.min < self.max:
            raise ValueError(f"{self.name}: min must be below max")
        return self

    def spec(self) -> dict:
        if self.values is not None:
            return {"values": self.values}
        out = {"min": self.min, "max": self.max}
        if self.scale != "auto":
            out["scale"] = self.scale
        return out


class Goal(_Strict):
    """Something to maximize or minimize."""
    metric: str = Field(description="Name of a .meas result, e.g. gain_db")
    direction: Literal["max", "min"] = "max"
    weight: float = Field(1.0, gt=0, description="Relative importance")


class Limit(_Strict):
    """A hard requirement on a metric. Give min, max, or both."""
    metric: str = Field(description="Name of a .meas result, e.g. power")
    min: float | None = Field(None, description="Metric must be >= this")
    max: float | None = Field(None, description="Metric must be <= this")

    @model_validator(mode="after")
    def _shape(self):
        if self.min is None and self.max is None:
            raise ValueError(f"{self.metric}: give min or max")
        if self.min is not None and self.max is not None and self.min > self.max:
            raise ValueError(f"{self.metric}: min is above max")
        return self


class SimOptions(_Strict):
    """Advanced simulator settings. Leave out for normal use."""
    timeout_s: float = Field(120.0, gt=0)
    ltspice_exe: str | None = Field(None, description="Path, if LTspice is not found")
    backend: Literal["ltspice", "ngspice", "analytic"] = Field(
        "ltspice", description="ngspice only where LTspice is not installed")
    model: str | None = Field(None, description="Analytic model name (tests only)")


def _space(params: list[Param]) -> dict:
    space = {}
    for p in params:
        if p.name in space:
            raise engine.EngineError(f"param {p.name!r} is listed twice")
        space[p.name] = p.spec()
    if not space:
        raise engine.EngineError("give at least one param")
    return space


def _constraints(limits: list[Limit] | None) -> list[dict]:
    out = []
    for lim in limits or []:
        if lim.min is not None:
            out.append({"metric": lim.metric, "op": ">=", "value": lim.min})
        if lim.max is not None:
            out.append({"metric": lim.metric, "op": "<=", "value": lim.max})
    return out


def _objectives(goals: list[Goal]) -> list[dict]:
    return [g.model_dump() for g in goals]


def _sim_config(opts: SimOptions | None) -> dict:
    o = (opts or SimOptions()).model_dump(exclude_none=True)
    if o.get("backend") != "analytic":
        o.pop("model", None)
    return o


# --- answers: next_step on success, fix + example on failure --------------------

EXAMPLES = {
    "check_setup": {},
    "simulate": {"netlist": "V1 in 0 AC 1\nR1 in out 1k\nC1 out 0 100n\n.ac dec 20 10 1Meg\n"
                            ".meas AC fc WHEN mag(V(out))=0.7071 FALL=1\n.end"},
    "check_feasibility": {"goals": [{"metric": "gain_db", "direction": "max"}],
                          "limits": [{"metric": "power", "max": 0.005}],
                          "circuit_type": "opamp", "mode": "concept",
                          "tech": {"vdd": 3.3, "cload": 1e-11}},
    "start_optimization": {"run_id": "rc_001",
                           "netlist": "V1 in 0 AC 1\nR1 in out {R1}\nC1 out 0 {C1}\n"
                                      ".ac dec 20 10 1Meg\n"
                                      ".meas AC fc WHEN mag(V(out))=0.7071 FALL=1\n.end",
                           "params": [{"name": "R1", "min": 100, "max": 100000},
                                      {"name": "C1", "values": [1e-9, 1e-8, 1e-7]}],
                           "goals": [{"metric": "fc", "direction": "max"}],
                           "limits": [{"metric": "fc", "max": 20000}]},
    "run_optimization": {"run_id": "rc_001", "iterations": 20},
    "evaluate_candidate": {"run_id": "rc_001", "params": {"R1": 1000, "C1": 1e-8}},
    "query_run": {"run_id": "rc_001", "what": "best"},
    "rollback_to_candidate": {"run_id": "rc_001", "candidate_id": "cand_0003"},
    "stop_optimization": {"run_id": "rc_001"},
    "generate_design_report": {"run_id": "rc_001", "pvt": True, "monte_carlo": 100},
    "render_schematic": {"path": "amp.cir", "output_path": "amp.png"},
    "list_templates": {},
    "start_from_template": {"run_id": "lpf_001", "template": "sallen_key_lowpass",
                            "spec": {"fc": 10000}},
}


def _fix_for(msg: str) -> str:
    m = msg.lower()
    if "needs candidate_ids" in m:
        return "Add candidate_ids with 2+ ids from query_run(run_id, what='candidates')."
    if "needs candidate_id" in m:
        return "Add candidate_id. Get one from query_run(run_id, what='candidates')."
    if "needs run_id" in m:
        return "Add run_id. query_run(what='runs') lists every run."
    if "unknown run" in m:
        return "Check the run_id. query_run(what='runs') lists every run."
    if "already exists" in m:
        return "Pick a new run_id, or keep using the existing run."
    if "unknown candidate" in m or "belongs to run" in m:
        return "Use a candidate_id from query_run(run_id, what='candidates')."
    if "no successful candidate" in m:
        return "Call run_optimization first, then retry."
    if "cannot be met" in m:
        return "Relax the limits listed under feasibility.violations, then retry."
    if "is stopped" in m or "nothing to do" in m:
        return "The run is stopped. Start a new run with a new run_id."
    if "unknown template" in m or "spec needs" in m or "unknown spec keys" in m:
        return "Call list_templates for the template names and their spec keys."
    if "mode must be" in m or "circuit_type must be" in m:
        return "Use one of the allowed values named in the error."
    return "Correct the input named in the error and call again."


def _fail(tool: str, msg: str, **extra) -> dict:
    return {"error": msg, "fix": _fix_for(msg), "example": EXAMPLES[tool], **extra}


def _call(tool: str, fn, *a, **kw) -> dict:
    try:
        out = fn(*a, **kw)
    except (engine.EngineError, feasibility.FeasibilityError, topologies.TemplateError,
            ValueError, KeyError) as e:
        return _fail(tool, str(e).strip("'\""))
    if isinstance(out, dict) and "error" in out:
        return {**_fail(tool, out.pop("error")), **out}
    return out


def _after_run(rid: str, s: dict) -> str:
    """What to do after a batch of iterations, read off the compact summary."""
    if not s.get("best_candidate"):
        return f"No candidate simulated cleanly. Call query_run(run_id='{rid}', what='failed')."
    if s.get("at_param_space_bound"):
        return ("Best design sits on a param bound: " + ", ".join(s["at_param_space_bound"])
                + ". Start a new run with a wider range for those params.")
    if not s.get("best_is_feasible"):
        return (f"No design meets every limit yet. Call run_optimization(run_id='{rid}', "
                f"iterations=20) again, or relax a limit.")
    return f"Design meets all limits. Call generate_design_report(run_id='{rid}')."


# --- tools: setup and one-off simulation ----------------------------------------

@mcp.tool()
def check_setup() -> dict:
    """Show the LTspice path and project folder in use.
    Call first if anything fails to simulate."""
    exe = sim.find_ltspice()
    return {"ltspice_exe": exe, "ready": bool(exe), "project": PROJECT,
            "state_dir": str(db.state_root(PROJECT)),
            "next_step": "Call simulate with a small netlist to confirm it works." if exe
            else "LTspice not found. Install LTspice, or set the LTSPICE_EXE "
                 "environment variable to its full path."}


@mcp.tool()
def simulate(netlist: str, sweep_param: str = "", sweep_values: list[float] | None = None,
             timeout_s: float = 120.0) -> dict:
    """Run one LTspice netlist now and return its .meas results. Nothing is saved.
    To sweep, set sweep_param ("R1", "{GAIN}" or "temp") and sweep_values."""
    if sweep_param or sweep_values:
        if not (sweep_param and sweep_values):
            return _fail("simulate", "sweep needs both sweep_param and sweep_values")
        root = db.state_root(PROJECT) / "simulations" / "sweep"
        try:
            out = sim.simulate_sweep(netlist, sweep_param, sweep_values, root, "sweep",
                                     {"timeout_s": timeout_s})
        except sim.SweepError as e:
            return _fail("simulate", str(e))
        out["next_step"] = ("Read metrics_table: one row per sweep value."
                            if out.get("status") == "ok"
                            else "Fix the netlist using failure_message, then retry.")
        return out
    root = db.state_root(PROJECT)
    res = sim.simulate(netlist, {}, {"backend": "ltspice", "timeout_s": timeout_s},
                       root / "simulations" / "adhoc", "adhoc")
    out = {"status": res.status, "metrics": res.metrics, "warnings": res.warnings,
           "failure_type": res.failure_type, "failure_message": res.failure_message,
           "failure_detail": res.failure_detail, "log_path": res.log_path}
    if res.status == "ok":
        out["next_step"] = ("To tune values, replace them with {NAME} placeholders and "
                            "call start_optimization.")
    else:
        out["next_step"] = "Fix the netlist using failure_message, then retry."
        out["fix"] = res.failure_message or "Check the netlist syntax and .meas lines."
    return out


@mcp.tool()
def check_feasibility(goals: list[Goal], limits: list[Limit] | None = None,
                      params: list[Param] | None = None, netlist: str = "",
                      circuit_type: Literal["general", "filter", "opamp", "ldo",
                                            "dcdc"] = "general",
                      mode: Literal["practical", "theoretical", "concept"] = "practical",
                      tech: dict[str, float | str] | None = None) -> dict:
    """Check if a spec is physically possible before simulating. Free, no LTspice.
    Use mode="concept" without a netlist; tech holds SI values like vdd, cload, vin."""
    def go():
        space = _space(params) if params else None
        v = feasibility.check(_objectives(goals), _constraints(limits), space,
                              netlist or None, circuit_type, mode, tech).to_dict()
        if v["status"] == "infeasible":
            v["next_step"] = "Spec is impossible. Relax the limits in violations, then recheck."
        elif netlist and params:
            v["next_step"] = "Call start_optimization with the same netlist, params and limits."
        else:
            v["next_step"] = ("Write a netlist with {PARAM} placeholders and .meas lines, "
                              "then call start_optimization.")
        return v
    return _call("check_feasibility", go)


# --- tools: topology library ----------------------------------------------------

@mcp.tool()
def list_templates() -> dict:
    """List ready-made circuits (filters, amplifier, LDO, buck) and the spec each needs.
    Start here when the user describes a circuit instead of giving a netlist."""
    return {"templates": [t.describe() for t in topologies.TEMPLATES.values()],
            "next_step": "Call start_from_template(run_id, template, spec)."}


@mcp.tool()
def start_from_template(run_id: str, template: str, spec: dict[str, float],
                        sim_options: SimOptions | None = None) -> dict:
    """Build a run from a ready-made circuit, size it by hand equations and simulate
    that start point. spec keys come from list_templates, in SI units."""
    def go():
        t = topologies.get(template)
        d = t.design(spec)
        space = {p.pop("name"): p for p in (dict(x) for x in d.params)}
        out = engine.start_run(con(), run_id, d.netlist, space, d.goals,
                               _constraints([Limit(**x) for x in d.limits]),
                               _sim_config(sim_options), 0, f"template {template}",
                               {"circuit_type": d.circuit_type,
                                "technology_params": {k: v for k, v in d.tech.items()
                                                      if k != "gbw"}})
        if "error" in out:
            return out
        first = engine.evaluate(con(), run_id, d.start, None, None, False, "template")
        try:                                   # a drawing is a convenience, never a gate
            sch = layout.draw(d.netlist, d.start).write(
                db.state_root(PROJECT) / "schematics" / f"{run_id}.asc")
            out["schematic_asc"] = str(sch.resolve())
        except (layout.LayoutError, OSError, ValueError):
            pass
        out.update({"template": template, "hand_calculation": d.working,
                    "start_point": d.start,
                    "start_result": {k: first.get(k) for k in
                                     ("candidate_id", "status", "metrics", "feasible",
                                      "failure_message")}})
        out["next_step"] = (
            f"Start point already meets every limit. Call run_optimization(run_id="
            f"'{run_id}', iterations=20) to improve the goal, or generate_design_report."
            if first.get("feasible") else
            f"Start point is close but misses a limit. Call run_optimization(run_id="
            f"'{run_id}', iterations=20).")
        return out
    return _call("start_from_template", go)


# --- tools: optimization run ----------------------------------------------------

@mcp.tool()
def start_optimization(run_id: str, netlist: str, params: list[Param], goals: list[Goal],
                       limits: list[Limit] | None = None,
                       circuit_type: Literal["general", "filter", "opamp", "ldo",
                                             "dcdc"] = "general",
                       skip_preflight: bool = False, seed: int = 0,
                       sim_options: SimOptions | None = None) -> dict:
    """Create a saved optimization run. netlist uses {NAME} for each param and a
    .meas line per metric. Refused if the spec is impossible."""
    def go():
        out = engine.start_run(
            con(), run_id, netlist, _space(params), _objectives(goals),
            _constraints(limits), _sim_config(sim_options), seed, "",
            {"bypass": skip_preflight, "circuit_type": circuit_type})
        if "error" not in out:
            out["next_step"] = f"Call run_optimization(run_id='{run_id}', iterations=20)."
        return out
    return _call("start_optimization", go)


@mcp.tool()
def run_optimization(run_id: str, iterations: Annotated[int, Field(ge=1, le=500)] = 20) -> dict:
    """Let the server search for `iterations` simulations and return one summary.
    Preferred way to optimize: do not loop evaluate_candidate yourself."""
    def go():
        out = engine.optimize(con(), run_id, iterations)
        out["next_step"] = _after_run(run_id, out)
        return out
    return _call("run_optimization", go)


@mcp.tool()
def evaluate_candidate(run_id: str, params: dict[str, float], force: bool = False) -> dict:
    """Simulate one exact set of param values inside a run (cached if repeated).
    Use to test your own idea; use run_optimization for searching."""
    def go():
        out = engine.evaluate(con(), run_id, params, None, None, force, "llm")
        out["next_step"] = (
            f"Call query_run(run_id='{run_id}', what='status') or run_optimization."
            if out.get("status", "ok") == "ok"
            else "Simulation failed: read failure_message and fix the params or netlist.")
        return out
    return _call("evaluate_candidate", go)


QUERIES = ("status", "best", "pareto", "history", "candidates", "candidate", "netlist",
           "compare", "similar", "sensitivity", "explored", "failed", "trace", "next",
           "runs")


@mcp.tool()
def query_run(run_id: str = "",
              what: Literal["status", "best", "pareto", "history", "candidates",
                            "candidate", "netlist", "compare", "similar", "sensitivity",
                            "explored", "failed", "trace", "next", "runs"] = "status",
              candidate_id: str = "", candidate_ids: list[str] | None = None,
              parameter: str = "", metric: str = "",
              limit: Annotated[int, Field(ge=1, le=200)] = 20) -> dict:
    """Read saved run data. Default what="status". "runs" lists all runs.
    candidate, netlist, similar need candidate_id; compare needs candidate_ids."""
    out = _call("query_run", _query, run_id, what, candidate_id, candidate_ids or [],
                parameter or None, metric or None, limit)
    if "error" in out:                     # show a correct call for THIS `what`
        ex = {"run_id": run_id or "rc_001", "what": what}
        if what in ("candidate", "netlist", "similar"):
            ex["candidate_id"] = candidate_id or "cand_0001"
        elif what == "compare":
            ex["candidate_ids"] = ["cand_0001", "cand_0002"]
        elif what == "runs":
            ex = {"what": "runs"}
        out["example"] = ex
    return out


def _query(rid, what, cid, cids, parameter, metric, limit) -> dict:
    c = con()
    if what == "runs":
        runs = [{"run_id": r["run_id"], "status": r["status"],
                 "created_at": r["created_at"],
                 "current_candidate": r["current_candidate"]}
                for r in c.execute("SELECT * FROM runs ORDER BY created_at DESC")]
        return {"runs": runs, "next_step": "Call query_run(run_id=..., what='status')."
                if runs else "No runs yet. Call start_optimization."}
    if not rid:
        raise engine.EngineError(f"what={what!r} needs run_id")
    if what in ("candidate", "netlist", "similar") and not cid:
        raise engine.EngineError(f"what={what!r} needs candidate_id")
    if what == "compare" and len(cids) < 2:
        raise engine.EngineError("what='compare' needs candidate_ids with 2 or more ids")
    engine.get_run(c, rid)                     # unknown run -> one clear error

    nxt = f"Call generate_design_report(run_id='{rid}') when the design is good."
    if what == "status":
        out = engine.summary(c, rid)
        return {**out, "next_step": _after_run(rid, out)}
    if what == "best":
        out = engine.get_best(c, rid)
    elif what == "pareto":
        out = {"candidates": engine.pareto(c, rid)}
    elif what == "history":
        out = {"iterations": engine.history(c, rid, limit, 0)}
    elif what == "candidates":
        rows = c.execute(
            "SELECT d.*, (SELECT COUNT(*) FROM experiments e"
            " WHERE e.candidate_id=d.candidate_id) AS n_exp"
            " FROM designs d WHERE d.run_id=? ORDER BY d.candidate_id DESC LIMIT ?",
            (rid, limit)).fetchall()
        out = {"candidates": [
            {"candidate_id": r["candidate_id"], "parent_id": r["parent_id"],
             "generation": r["generation"], "origin": r["origin"],
             "params": db.jloads(r, "params"), "evaluated": r["n_exp"] > 0}
            for r in rows]}
    elif what in ("candidate", "netlist"):
        d = engine.get_candidate(c, cid)
        if d["run_id"] != rid:
            raise engine.EngineError(f"{cid} belongs to run {d['run_id']!r}")
        if what == "netlist":
            return {"candidate_id": cid, "params": db.jloads(d, "params"),
                    "netlist": d["netlist"],
                    "next_step": "Use this netlist as-is; do not retype it from memory."}
        exps = c.execute("SELECT * FROM experiments WHERE candidate_id=? ORDER BY iteration",
                         (cid,)).fetchall()
        out = {"candidate_id": cid, "parent_id": d["parent_id"],
               "generation": d["generation"], "origin": d["origin"],
               "params": db.jloads(d, "params"),
               "experiments": [engine._experiment_summary(e) for e in exps]}
    elif what == "compare":
        out = engine.compare(c, rid, cids)
    elif what == "similar":
        out = {"candidates": engine.similar_candidates(c, rid, cid, limit)}
    elif what == "sensitivity":
        out = {"sensitivities": engine.sensitivity(c, rid, parameter, metric)}
        nxt = ("method=finite_difference is causal; ols_marginal is weaker. "
               f"Call run_optimization(run_id='{rid}') to keep improving.")
    elif what == "explored":
        out = engine.explored(c, rid, parameter)
    elif what == "failed":
        out = {"failures": engine.failed_candidates(c, rid, limit)}
        nxt = "Fix the netlist or narrow the param ranges that fail, then continue."
    elif what == "trace":
        out = {"trace": engine.trace(c, rid)}
    else:  # next
        out = engine.select_next(c, rid)
        nxt = (f"Call evaluate_candidate(run_id='{rid}', params=<params above>) to try it."
               if out.get("params") else "Search space is exhausted. Generate the report.")
    return {**out, "next_step": nxt}


@mcp.tool()
def rollback_to_candidate(run_id: str, candidate_id: str) -> dict:
    """Restore a past candidate's exact netlist from storage and return its file path.
    Use this instead of rebuilding an old circuit from memory."""
    def go():
        out = engine.rollback(con(), run_id, candidate_id)
        out["next_step"] = "The netlist at netlist_path is the exact stored design."
        return out
    return _call("rollback_to_candidate", go)


@mcp.tool()
def stop_optimization(run_id: str) -> dict:
    """Stop a run. All history and candidates are kept."""
    def go():
        out = engine.stop(con(), run_id)
        out["next_step"] = f"Call generate_design_report(run_id='{run_id}')."
        return out
    return _call("stop_optimization", go)


@mcp.tool()
def generate_design_report(run_id: str, plots: bool = True, dc_audit: bool = False,
                           pvt: bool = False, monte_carlo: Annotated[int, Field(ge=0, le=10000)] = 0,
                           supply: str = "", schematic: str = "", out: str = "") -> dict:
    """Write a Markdown design report for a run's best design and return its path.
    Optional checks cost LTspice runs: dc_audit, pvt (supply = source name), monte_carlo=N."""
    def go():
        res = report.generate(con(), run_id, out=out or None, schematic=schematic or None,
                              plot=plots, dc_audit=dc_audit, pvt=pvt,
                              monte_carlo=monte_carlo, supply=supply or None)
        res["next_step"] = "Open the report at the returned path and review it."
        return res
    return _call("generate_design_report", go)


@mcp.tool()
def render_schematic(path: str, output_path: str = "schematic.png",
                     style: Literal["tech_minimal", "classic", "sketch"] = "tech_minimal"
                     ) -> dict:
    """Draw a schematic as PNG or SVG. path: an LTspice .asc, or a SPICE netlist
    (.cir/.net/.sp), which is auto-placed, routed and checked first."""
    try:
        src, drawn = Path(path), None
        if src.suffix.lower() != ".asc":
            if not src.is_file():
                raise OSError(f"no such file: {src}")
            d = layout.draw(src.read_text(encoding="utf-8", errors="replace"))
            src = d.write(Path(output_path).with_suffix(".asc"))
            drawn = src
        out = render.render(str(src), output_path, style, 300)
        out["readability"] = layout.lint(src)
        if drawn:
            out["asc_path"] = str(drawn.resolve())
            out["matches_netlist"] = True       # draw() refuses a drawing that doesn't
    except (render.RenderError, layout.LayoutError, OSError, ValueError) as e:
        return {"success": False, "output_path": None,
                **_fail("render_schematic", f"{type(e).__name__}: {e}")}
    out["next_step"] = "Show the image at output_path to the user."
    return out


# --- resources: reference material that does not need to be in the tool list ----

def visual_style() -> dict:
    """The house style: schematic styles and the plot colour ramp."""
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


@mcp.resource("spicemcp://style", mime_type="application/json")
def style_resource() -> dict:
    """Colours and fonts used by SpiceMCP schematics and plots."""
    return visual_style()


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
