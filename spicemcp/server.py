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

import functools
import json
import os
import threading
import time
from pathlib import Path
from typing import Annotated, Literal

try:                                     # mcp 1.x
    from mcp.server.fastmcp import FastMCP as _Server
    MCP2 = False
except ImportError:                      # mcp 2.x renamed FastMCP to MCPServer
    from mcp.server.mcpserver import MCPServer as _Server
    MCP2 = True
from mcp.types import CallToolResult, TextContent
import contextvars
from pydantic import BaseModel, ConfigDict, Field, model_validator

from . import (buildable, db, engine, feasibility, layout, opamp, plots, progress, render,
               report, sim, topologies)
from . import parts as partlib
from . import stress

INSTRUCTIONS = """\
SpiceMCP designs circuits with LTspice. Typical flow:
1. start_from_template (filters, amplifier, LDO, buck: sized for you; see
   list_templates), or start_optimization (your netlist with {PARAM} placeholders
   and .meas lines; it checks the spec is possible first)
2. run_optimization   (the server searches; you do not loop per simulation)
3. finish_design      (standard values; yield over tolerance, supply, temperature;
   part stress and the rating to buy)
4. generate_design_report
Real parts: find_part, then start_from_template(parts={"opamp": NAME}).
Built-in blocks need no .subckt: X1 in+ in- V+ V- out SPMCP_OA (op-amp) or SPMCP_CMP
(comparator). Long calls return within a minute; if an answer says "still running",
call the same tool again. query_run reads saved state at any time. Follow next_step.
Never rebuild a past circuit from memory: use query_run(what="netlist")."""

# Inputs that work when passed but are left out of the published tool list: a model
# never needs them, and every byte of the list is re-sent on every turn.
HIDDEN_INPUTS = {"sim_options", "skip_preflight", "seed"}


def _slim_schema(node):
    """Drop what a model does not read: pydantic's auto `title`s (a property *named*
    title would survive: only the keyword is removed) and hidden inputs."""
    if isinstance(node, dict):
        out = {}
        for k, v in node.items():
            if k == "title" and isinstance(v, str):
                continue
            if k == "properties":
                out[k] = {n: _slim_schema(x) for n, x in v.items() if n not in HIDDEN_INPUTS}
            elif k == "required":
                out[k] = [n for n in v if n not in HIDDEN_INPUTS]
            else:
                out[k] = _slim_schema(v)
        alts = out.get("anyOf")                 # Optional[X] = None  ->  X, not required
        if (isinstance(alts, list) and len(alts) == 2 and {"type": "null"} in alts
                and out.get("default", 0) is None):
            out.pop("anyOf"), out.pop("default")
            out = {**next(a for a in alts if a != {"type": "null"}), **out}
        if "$defs" in out:                      # a hidden input's type is now unused
            used = json.dumps({k: v for k, v in out.items() if k != "$defs"})
            out["$defs"] = {n: d for n, d in out["$defs"].items()
                            if f"#/$defs/{n}" in used}
            if not out["$defs"]:
                del out["$defs"]
        return out
    if isinstance(node, list):
        return [_slim_schema(x) for x in node]
    return node


def _round(obj):
    """6 significant digits: 3.3329999999999997 -> 3.333. Far below any part tolerance."""
    if isinstance(obj, float):
        return float(f"{obj:.6g}") if obj == obj and abs(obj) != float("inf") else obj
    if isinstance(obj, dict):
        return {k: _round(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_round(v) for v in obj]
    return obj


def compact(obj) -> str:
    """One-line JSON, numbers to 6 digits. Indentation alone was 21% of answer tokens."""
    return json.dumps(_round(obj), separators=(",", ":"), ensure_ascii=False, default=str)


_CTX: contextvars.ContextVar = contextvars.ContextVar("spicemcp_ctx", default=None)


def tool_schema(tool) -> dict:
    """A listed tool's input schema, on mcp 1.x (`inputSchema`) or 2.x (`input_schema`)."""
    return tool.input_schema if MCP2 else tool.inputSchema


def answer_text(res) -> str:
    """The text of a call_tool answer, on mcp 1.x (content list) or 2.x (a result)."""
    res = res[0] if isinstance(res, tuple) else res
    return getattr(res, "content", res)[0].text


class CompactMCP(_Server):
    """The MCP server with a slimmer tool list and one-line JSON answers. Runs on the
    mcp package 1.x (FastMCP) and 2.x (MCPServer)."""

    async def list_tools(self):
        tools = await super().list_tools()
        for t in tools:
            if MCP2:
                t.input_schema = _slim_schema(t.input_schema)
            else:
                t.inputSchema = _slim_schema(t.inputSchema)
        return tools

    async def call_tool(self, name, arguments, context=None):
        tool = self._tool_manager.get_tool(name)
        if tool is None:                         # the base class raises a clear error
            return await (super().call_tool(name, arguments, context) if MCP2
                          else super().call_tool(name, arguments))
        if context is None:
            if MCP2:
                from mcp.server.mcpserver import Context
                context = Context(mcp_server=self, subscriptions=self._subscriptions)
            else:
                context = self.get_context()
        token = _CTX.set(context)
        try:
            out = await tool.run(arguments, context=context)
        finally:
            _CTX.reset(token)
        content = [TextContent(type="text", text=out if isinstance(out, str) else compact(out))]
        return CallToolResult(content=content) if MCP2 else content


mcp = CompactMCP("spicemcp", instructions=INSTRUCTIONS)

PROJECT = os.environ.get("SPICEMCP_PROJECT", os.getcwd())
_con = None


_tl = threading.local()


def con():
    """The main thread's connection, or this worker thread's own (SQLite connections
    are not shared across threads; WAL lets them read and write side by side)."""
    global _con
    if threading.current_thread() is not threading.main_thread():
        if getattr(_tl, "con", None) is None or _tl.project != PROJECT:
            _tl.con, _tl.project = db.connect(PROJECT), PROJECT
        return _tl.con
    if _con is None:
        _con = db.connect(PROJECT)
    return _con


def _close_thread_con():
    c = getattr(_tl, "con", None)
    if c is not None:
        c.close()
        _tl.con = None


# --- long calls: progress, a time limit, and work that outlives one call ------------
# MCP clients drop calls that run for minutes. A long tool runs on a worker thread; the
# call sends progress while it waits, and returns within CALL_BUDGET_S. run_optimization
# stops itself at the limit (state is saved, so the next call continues). finish_design
# keeps running in the background; calling it again for the same run picks up the
# result.

def call_budget() -> float:
    try:
        return max(0.5, float(os.environ.get("SPICEMCP_CALL_BUDGET_S", "50")))
    except ValueError:
        return 50.0


class Job:
    def __init__(self, tool: str, run_id: str, key: str, fn, budget: float | None):
        self.tool, self.run_id, self.key = tool, run_id, key
        self.tracker = progress.Tracker(budget)
        self.result: dict | None = None
        self.started = time.monotonic()
        self.thread = threading.Thread(target=self._run, args=(fn,), daemon=True,
                                       name=f"{tool}:{run_id}")
        self.thread.start()

    def _run(self, fn):
        progress.install(self.tracker)
        try:
            self.result = fn()
        except Exception as e:                  # never lose a job's outcome silently
            self.result = _fail(self.tool, f"{type(e).__name__}: {e}")
        finally:
            progress.install(None)
            _close_thread_con()

    @property
    def finished(self) -> bool:
        return self.result is not None


JOBS: dict[tuple[str, str], Job] = {}
_JOBS_LOCK = threading.Lock()


async def _report(ctx, snap: dict) -> None:
    if ctx is None:
        return
    try:
        await ctx.report_progress(snap["done"], snap["total"], snap["message"] or None)
    except TypeError:                            # mcp versions without a message field
        try:
            await ctx.report_progress(snap["done"], snap["total"])
        except Exception:
            pass
    except Exception:                            # no progress token, no session: fine
        pass


async def _wait(job: Job, limit: float | None) -> None:
    import anyio
    ctx = _CTX.get()
    last, t0 = None, time.monotonic()
    while not job.finished and (limit is None or time.monotonic() - t0 < limit):
        await anyio.sleep(0.2)
        snap = job.tracker.snapshot()
        if snap != last:
            await _report(ctx, snap)
            last = snap


def long_tool(background: bool):
    """Register `fn` as an MCP tool that runs on a worker thread with progress and a
    time limit. The Python function itself stays plain and synchronous, for scripts."""
    def deco(fn):
        name = fn.__name__

        @functools.wraps(fn)
        async def tool(**kw):
            rid = kw.get("run_id", "")
            key = json.dumps(kw, sort_keys=True, default=str)
            with _JOBS_LOCK:
                job = JOBS.get((name, rid))
                attached = job is not None and not job.finished
                if job is None or (job.finished and job.key != key):
                    job = JOBS[(name, rid)] = Job(
                        name, rid, key, lambda: fn(**kw),
                        None if background else call_budget())
            await _wait(job, call_budget() if background else None)
            if not job.finished:
                snap = job.tracker.snapshot()
                return {"run_id": rid, "status": "still running",
                        "progress": snap["message"] or f"{snap['done']} simulations",
                        "seconds_so_far": round(time.monotonic() - job.started),
                        **({"note": "Already running from an earlier call; its inputs "
                                    "apply."} if attached and job.key != key else {}),
                        "next_step": f"Call {name}(run_id='{rid}') again to wait for "
                                     f"the result."}
            with _JOBS_LOCK:
                if JOBS.get((name, rid)) is job:
                    del JOBS[(name, rid)]
            return job.result

        mcp.tool()(tool)
        return fn
    return deco


# --- input models ----------------------------------------------------------------

class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Param(_Strict):
    """Tunable value: min+max, or values."""
    name: str = Field(description="R1 for {R1}")
    min: float | None = None
    max: float | None = None
    values: list[float] | None = None
    scale: Literal["auto", "log", "linear"] = "auto"

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
    metric: str = Field(description=".meas name")
    direction: Literal["max", "min"] = "max"
    weight: float = Field(1.0, gt=0)


class Limit(_Strict):
    """Hard limit: min and/or max."""
    metric: str = Field(description=".meas name")
    min: float | None = None
    max: float | None = None

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


def default_backend() -> str:
    """LTspice unless SPICEMCP_BACKEND=ngspice is set (Linux/macOS without LTspice)."""
    b = os.environ.get("SPICEMCP_BACKEND", "ltspice").strip().lower()
    return b if b in ("ltspice", "ngspice") else "ltspice"


def _sim_config(opts: SimOptions | None) -> dict:
    o = (opts or SimOptions(backend=default_backend())).model_dump(exclude_none=True)
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
    "finish_design": {"run_id": "rc_001", "resistors": "E96", "capacitors": "E12"},
    "generate_design_report": {"run_id": "rc_001", "pvt": True, "monte_carlo": 100},
    "render_schematic": {"path": "amp.cir", "output_path": "amp.png"},
    "list_templates": {},
    "find_part": {"query": "1N5819", "kind": "diode"},
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
    if "supply source" in m:
        return "Set supply to the name of the DC source that feeds the circuit, e.g. Vin."
    if "no successful candidate" in m or "no design has simulated" in m:
        return "Call run_optimization first, then retry."
    if "cannot be met" in m:
        return "Relax the limits listed under feasibility.violations, then retry."
    if "is stopped" in m or "nothing to do" in m:
        return "The run is stopped. Start a new run with a new run_id."
    if "not found in the spice libraries" in m:
        return ("Call find_part with part of the name. If LTspice is not installed here, "
                "set SPICEMCP_LIB_DIRS to the folder holding your .lib/.sub/.model files.")
    if "slot" in m and ("needs a" in m or "has no" in m):
        return "Pick a part of the kind named in the error (find_part kind=...)."
    if "unknown template" in m or "spec needs" or "unknown spec keys" in m:
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
    template = (engine.get_run(con(), rid)["notes"] or "").startswith("template")
    if s.get("at_param_space_bound") and not (template and s.get("best_is_feasible")):
        return ("Best design sits on a param bound: " + ", ".join(s["at_param_space_bound"])
                + ". Start a new run with a wider range for those params.")
    if not s.get("best_is_feasible"):
        return (f"No design meets every limit yet. Call run_optimization(run_id='{rid}', "
                f"iterations=20) again, or relax a limit.")
    return (f"Design meets all limits. Call finish_design(run_id='{rid}') for standard "
            f"part values and yield, then generate_design_report.")


# --- details kept out of answers, readable with query_run(what="details") ---------

def _details_path(rid: str) -> Path:
    return db.state_root(PROJECT) / "simulations" / rid / "details.json"


def _load_details(rid: str) -> dict:
    try:
        return json.loads(_details_path(rid).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save_details(rid: str, **parts) -> None:
    """Long material (hand calculation, full feasibility report, every finish_design
    alternative) is stored here instead of being sent in the answer."""
    f = _details_path(rid)
    try:
        old = json.loads(f.read_text(encoding="utf-8")) if f.is_file() else {}
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(compact({**old, **parts}), encoding="utf-8")
    except OSError:
        pass


def _short_feasibility(v: dict | None) -> dict | None:
    """status + the warning messages, nothing else. Full report: query_run details."""
    if not v:
        return None
    msgs = [x.get("message") for x in v.get("violations") or [] if x.get("message")]
    if not msgs:
        return None                              # nothing to say: leave it out
    return {"status": v.get("status"), "warnings": msgs[:3]}


def _limits_text(cons: list[dict]) -> list[str]:
    return [f"{c['metric']}{c['op']}{float(c['value']):.6g}" for c in cons]


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
    """Run one netlist now and return its .meas results. Nothing is saved.
    To sweep, set sweep_param ("R1", "{GAIN}" or "temp") and sweep_values."""
    if sweep_param or sweep_values:
        if not (sweep_param and sweep_values):
            return _fail("simulate", "sweep needs both sweep_param and sweep_values")
        root = db.state_root(PROJECT) / "simulations" / "sweep"
        try:
            out = sim.simulate_sweep(netlist, sweep_param, sweep_values, root, "sweep",
                                     {"timeout_s": timeout_s, "backend": default_backend()})
        except sim.SweepError as e:
            return _fail("simulate", str(e))
        out["next_step"] = ("Read metrics_table: one row per sweep value."
                            if out.get("status") == "ok"
                            else "Fix the netlist using failure_message, then retry.")
        return out
    root = db.state_root(PROJECT)
    res = sim.simulate(netlist, {}, {"backend": default_backend(), "timeout_s": timeout_s},
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
def find_part(query: str, kind: Literal["any", "opamp", "nmos", "pmos", "diode", "npn",
                                        "pnp"] = "any") -> dict:
    """Find a real part by part number or maker in the SPICE libraries on this computer
    (LTspice's, plus SPICEMCP_LIB_DIRS). Use the name in start_from_template parts."""
    def go():
        dirs = partlib.lib_dirs()
        if not dirs:
            raise partlib.PartError(
                "part not found in the SPICE libraries: no library folder found")
        hits = partlib.find(query, kind)
        return {"matches": [p.short() for p in hits],
                "searched": [str(d) for d in dirs],
                "next_step": ("Pass a name as start_from_template(..., parts={'opamp': "
                              "NAME}); each template lists its part slots under parts."
                              if hits else "No match: try fewer characters or kind='any'.")}
    return _call("find_part", go)


@mcp.tool()
def start_from_template(run_id: str, template: str, spec: dict[str, float],
                        sim_options: SimOptions | None = None,
                        parts: dict[Literal["opamp", "switch", "diode"], str] | None = None
                        ) -> dict:
    """Build a run from a ready-made circuit, size it by hand equations and simulate
    that start point. spec keys from list_templates (SI); parts: names from find_part."""
    def go():
        t = topologies.get(template)
        d = t.design(spec)
        chosen = {}
        for slot, name in (parts or {}).items():
            if slot not in t.parts:
                raise partlib.PartError(
                    f"{template} has no {slot} slot; it takes {list(t.parts) or 'no parts'}")
            part = partlib.get(name)
            d.netlist = partlib.apply(d.netlist, slot, part)
            chosen[slot] = part.short()
            d.working.append(f"{slot}: {part.name} ({part.short().get('rated', part.role)}) "
                             f"from {part.file}, wired {partlib.SLOTS[slot][2]}.")
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
        _save_details(run_id, template=template, spec=spec, parts=chosen,
                      part_ratings={s: partlib.get(n).ratings for s, n in (parts or {}).items()},
                      hand_calculation=d.working, feasibility=out.get("feasibility"))
        watched = {c["metric"] for c in out["constraints"]} | \
            {o["metric"] for o in out["objectives"]}
        m = first.get("metrics") or {}
        out = {"run_id": run_id, "template": template,
               "limits": _limits_text(out["constraints"]),
               "goal": [f"{o['direction']} {o['metric']}" for o in out["objectives"]],
               "feasibility": _short_feasibility(out.get("feasibility")),
               **({"parts": chosen} if chosen else {}),
               "start_point": d.start,
               "start_result": {"candidate_id": first.get("candidate_id"),
                                "status": first.get("status", "ok"),
                                "feasible": first.get("feasible"),
                                "metrics": {k: v for k, v in m.items() if k in watched},
                                **({"failure_message": first["failure_message"]}
                                   if first.get("failure_message") else {})},
               **({"schematic_asc": out["schematic_asc"]} if "schematic_asc" in out
                  else {}),
               "hand_calculation": f"query_run(run_id='{run_id}', what='details')"}
        if out["feasibility"] is None:
            del out["feasibility"]
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
            if out.get("feasibility"):
                _save_details(run_id, feasibility=out["feasibility"])
                out["feasibility"] = _short_feasibility(out["feasibility"])
                if out["feasibility"] is None:
                    del out["feasibility"]
            out["next_step"] = f"Call run_optimization(run_id='{run_id}', iterations=20)."
        return out
    return _call("start_optimization", go)


@long_tool(background=False)
def run_optimization(run_id: str, iterations: Annotated[int, Field(ge=1, le=500)] = 20) -> dict:
    """Let the server search for `iterations` simulations and return one summary.
    Preferred way to optimize: do not loop evaluate_candidate yourself."""
    def go():
        out = engine.optimize(con(), run_id, iterations)
        out["next_step"] = _after_run(run_id, out)
        if out.get("stopped_early"):
            out["next_step"] = (f"Time limit hit after {out['iterations_run']} "
                                f"simulations; progress is saved. Call run_optimization("
                                f"run_id='{run_id}') again to continue, or "
                                + out["next_step"][0].lower() + out["next_step"][1:])
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
           "runs", "details")


@mcp.tool()
def query_run(run_id: str = "",
              what: Literal["status", "best", "pareto", "history", "candidates",
                            "candidate", "netlist", "compare", "similar", "sensitivity",
                            "explored", "failed", "trace", "next", "runs",
                            "details"] = "status",
              candidate_id: str = "", candidate_ids: list[str] | None = None,
              parameter: str = "", metric: str = "",
              limit: Annotated[int, Field(ge=1, le=200)] = 20) -> dict:
    """Read saved run data. Default what="status". "runs" lists runs; "details" = hand
    calc, warnings, alternatives. candidate/netlist/similar need candidate_id."""
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
    if what == "details":
        f = _details_path(rid)
        out = json.loads(f.read_text(encoding="utf-8")) if f.is_file() else {}
        return {**out, "next_step": "These are the long-form details of this run."
                if out else "No details stored: this run has no template, warnings or "
                            "finished design yet."}
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


Series = Literal["E6", "E12", "E24", "E48", "E96", "none"]


@long_tool(background=True)
def finish_design(run_id: str, resistors: Series = "E96", capacitors: Series = "E12",
                  inductors: Series = "E12",
                  tolerance_pct: dict[Literal["R", "C", "L"], float] | None = None,
                  yield_runs: Annotated[int, Field(ge=0, le=500)] = 30,
                  supply: str = "", supply_tol_pct: float | None = None,
                  temps_c: list[float] | None = None,
                  seed: int | None = None) -> dict:
    """Snap the best design to standard values, check yield over parts (R 1% C 5% L 20%),
    supply and temperature (templates: 5%, -40/25/85 C); return the parts list."""
    def go():
        sup, tol, temps = _corners(run_id, supply, supply_tol_pct, temps_c)
        out = buildable.finish(
            con(), run_id, {"R": resistors, "C": capacitors, "L": inductors},
            {k: v / 100 for k, v in (tolerance_pct or {}).items()}, yield_runs,
            sup, tol, seed, temps)
        y = out.get("yield")
        if y:
            f = db.state_root(PROJECT) / "simulations" / run_id / "yield" / "samples.json"
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(db.jdumps(y.pop("samples")), encoding="utf-8")
            y["samples_path"] = str(f)
        det = _load_details(run_id)
        chosen = {s: (det["parts"][s]["name"], r)
                  for s, r in (det.get("part_ratings") or {}).items()}
        rating_check = stress.against_ratings(out.get("stress") or {}, chosen)
        if rating_check:
            out["rating_check"] = rating_check
        _save_details(run_id, finish=out)
        out = _short_finish(out)
        if rating_check:
            out["rating_check"] = rating_check
        ok = out["meets_all_limits"]
        pct = (y or {}).get("yield_pct")
        wc = (out.get("yield") or {}).get("worst_case", {})
        out["next_step"] = (
            f"Buildable design found. Call generate_design_report(run_id='{run_id}')."
            if ok and (pct is None or pct >= 90) else
            f"Standard values meet the limits but yield is low, mostly from "
            f"{wc.get('fails_most', 'a limit')}" + (
                f" at {wc['most_failures_at_c']:g} C" if "most_failures_at_c" in wc else "") +
            (f" with {wc['part_driving_failures']['part']} "
             f"{wc['part_driving_failures']['direction']}: tighten its tolerance_pct, "
             if "part_driving_failures" in wc else ": ") +
            "re-optimise with a tighter limit for margin, or relax the limit."
            if ok else
            "No standard-value combination meets every limit. Use a finer series "
            "(E96, E24) or relax the limit that fails.")
        return out
    return _call("finish_design", go)


def _corners(run_id: str, supply: str, tol_pct: float | None,
             temps: list[float] | None) -> tuple:
    """Fill in the corners a template run is checked at when the caller left them out:
    its own supply +-5%, and -40/25/85 C when it has transistors or diodes. A custom
    netlist is checked only at what the caller asks for."""
    run = engine.get_run(con(), run_id)
    template = (run["notes"] or "").startswith("template")
    net = run["netlist_template"]
    if not supply and template and tol_pct is None:
        supply = buildable.default_supply(net) or ""
    if tol_pct is None:
        tol_pct = 5.0 if supply else 0.0
    if temps is None and template and buildable.has_semiconductors(net):
        temps = [-40.0, 25.0, 85.0]
    return supply or None, tol_pct / 100, temps


def _short_stress(st: dict | None) -> dict:
    """Only the ratings to buy; the measured stress goes to details."""
    if not st:
        return {}
    if not st.get("checked"):
        return {"stress": "not checked: " + st.get("why", "")}
    return {"buy_at_least": {n: p["buy"] for n, p in st["parts"].items()}}


def _short_finish(out: dict) -> dict:
    """The parts list, pass/fail and yield. Alternatives, the optimiser's own metrics
    and the per-metric spread go to query_run(what='details')."""
    run = engine.get_run(con(), out["run_id"])
    watched = {c["metric"] for c in db.jloads(run, "constraints", [])}
    y = out.get("yield")
    short_y = None if not y else {
        k: y[k] for k in ("runs", "passed", "yield_pct", "yield_at_least_pct", "seed",
                          "statement", "covers", "samples_path") if k in y}
    if y and y.get("worst_case", {}).get("boards_failing"):
        w = y["worst_case"]
        short_y["worst_case"] = {k: v for k, v in w.items()
                                 if k in ("fails_most", "most_failures_at_c",
                                          "part_driving_failures")}
        if w.get("tightest_limit"):
            short_y["worst_case"]["tightest"] = {
                "metric": w["tightest_limit"], **w["limits"][w["tightest_limit"]]}
    return {
        "run_id": out["run_id"],
        "buildable_candidate": out["buildable_candidate"],
        "parts": {n: {"value": p["value"], "label": p["label"], "series": p["series"],
                      "tolerance_pct": p["tolerance_pct"], "change_pct": p["change_pct"]}
                  for n, p in out["parts"].items()},
        **({"other_values": out["other_values"]} if out["other_values"] else {}),
        "metrics": {k: v for k, v in out["metrics"].items() if k in watched},
        "meets_all_limits": out["meets_all_limits"],
        "worst_margin_pct": out["worst_margin_pct"],
        "nearest_values_meet_limits": out["nearest_values_meet_limits"],
        "yield": short_y,
        **_short_stress(out.get("stress")),
        "more": f"query_run(run_id='{out['run_id']}', what='details')",
    }


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
            d = layout.draw(opamp.include_blocks(
                src.read_text(encoding="utf-8", errors="replace")))
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
