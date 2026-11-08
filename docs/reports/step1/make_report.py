"""Rebuild the step 1 pictorial report: the MCP tool surface, before vs after.

    python docs/reports/step1/make_report.py

`tools_before.json` is the tool list exactly as v0.5.0 served it (captured with
`mcp.list_tools()` before the change). The "after" side is read live from the
current server, and the walkthrough is a real run on the analytic backend, so every
number and every `next_step` in the figures is what the server actually returns.
"""

from __future__ import annotations

import asyncio
import json
import statistics
import sys
import tempfile
import textwrap
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[2]))
from spicemcp import db, server, sim  # noqa: E402

BEFORE_C, AFTER_C = "#E07A5F", "#1E56A0"     # repo palette: C_LIMIT, C_TRACE
INK, MUTED, GRID, BG = "#1F2328", "#57606A", "#E6E8EB", "#FCFCFB"
plt.rcParams.update({"font.family": "DejaVu Sans", "font.size": 10, "axes.edgecolor": GRID,
                     "axes.labelcolor": MUTED, "xtick.color": MUTED, "ytick.color": MUTED,
                     "figure.facecolor": BG, "axes.facecolor": BG, "text.color": INK})


def shapeless(schema) -> int:
    """Inputs whose schema gives the model no keys to fill: bare objects / lists."""
    n = 0
    if isinstance(schema, dict):
        if schema.get("type") == "object" and "properties" not in schema:
            extra = schema.get("additionalProperties")
            n += not (isinstance(extra, dict) and extra)
        if schema.get("type") == "array" and not schema.get("items"):
            n += 1
        n += sum(shapeless(v) for v in schema.values())
    elif isinstance(schema, list):
        n += sum(shapeless(v) for v in schema)
    return n


def stats(tools: list[dict]) -> dict:
    d = [len(t["description"]) for t in tools]
    return {"count": len(tools), "size": sum(len(json.dumps(t)) for t in tools),
            "desc_median": statistics.median(d), "desc_max": max(d),
            "shapeless": sum(shapeless(t["schema"]) for t in tools),
            "max_args": max(len(t["schema"].get("properties", {})) for t in tools),
            "lengths": sorted(((len(t["description"]), t["name"]) for t in tools))}


def after_tools() -> list[dict]:
    return [{"name": t.name, "description": t.description, "schema": t.inputSchema}
            for t in asyncio.run(server.mcp.list_tools())]


def _clean(ax):
    for s in ("top", "right", "left"):
        ax.spines[s].set_visible(False)
    ax.tick_params(length=0)
    ax.grid(axis="y", color=GRID, lw=0.8)
    ax.set_axisbelow(True)


def fig_summary(b: dict, a: dict, path: Path) -> None:
    panels = [("Tools the model must choose from", "count", "{:.0f}"),
              ("Tool list sent to the model (chars)", "size", "{:,.0f}"),
              ("Longest tool description (chars)", "desc_max", "{:,.0f}"),
              ("Inputs with no schema (model guesses keys)", "shapeless", "{:.0f}")]
    fig, axes = plt.subplots(1, 4, figsize=(15, 4.2))
    for ax, (title, key, fmt) in zip(axes, panels):
        vals = [b[key], a[key]]
        bars = ax.bar([0, 1], vals, width=0.55, color=[BEFORE_C, AFTER_C],
                      edgecolor=BG, linewidth=2)
        for x, v in zip([0, 1], vals):
            ax.text(x, v, fmt.format(v), ha="center", va="bottom", fontsize=13,
                    fontweight="bold", color=INK)
        ax.set_xticks([0, 1], ["Before (v0.5)", "After (v0.6)"])
        ax.set_ylim(0, max(vals) * 1.22 or 1)
        ax.set_yticks([])
        ax.set_title(title, fontsize=10.5, color=INK, loc="left")
        _clean(ax)
        ax.grid(False)
    fig.suptitle("Step 1: a smaller, stricter tool surface", x=0.01, ha="left",
                 fontsize=14, fontweight="bold")
    fig.tight_layout(rect=(0, 0, 1, 0.93))
    fig.savefig(path, dpi=150)
    plt.close(fig)


def fig_descriptions(b: dict, a: dict, path: Path) -> None:
    fig, axes = plt.subplots(1, 2, figsize=(14, 8.5), gridspec_kw={"width_ratios": [1, 1]})
    for ax, s, color, title in ((axes[0], b, BEFORE_C, f"Before: {b['count']} tools"),
                                (axes[1], a, AFTER_C, f"After: {a['count']} tools")):
        lengths, names = zip(*s["lengths"])
        y = range(len(names))
        ax.barh(y, lengths, color=color, height=0.7, edgecolor=BG, linewidth=1)
        ax.set_yticks(list(y), names, fontsize=9)
        for yi, v in zip(y, lengths):
            ax.text(v + 15, yi, str(v), va="center", fontsize=8, color=MUTED)
        ax.axvline(server_limit(), color=INK, lw=1, ls=(0, (4, 3)))
        ax.text(server_limit() + 15, -1.1, "200-char limit (enforced by test)",
                fontsize=8, color=INK, va="center")
        ax.set_ylim(-1.6, len(names) - 0.4)
        ax.set_xlim(0, 1650)
        ax.set_title(title, loc="left", fontsize=11, color=INK)
        ax.set_xlabel("description length (characters)")
        for sp in ("top", "right"):
            ax.spines[sp].set_visible(False)
        ax.tick_params(length=0)
        ax.grid(axis="x", color=GRID, lw=0.8)
        ax.set_axisbelow(True)
    fig.suptitle("Every tool description, before vs after", x=0.01, ha="left",
                 fontsize=14, fontweight="bold")
    fig.tight_layout(rect=(0, 0, 1, 0.95))
    fig.savefig(path, dpi=150)
    plt.close(fig)


def server_limit() -> int:
    return 200


@sim.register_analytic("report_rc")
def _rc(p: dict) -> dict:
    return {"fc": 1 / (6.283185307179586 * p["R1"] * p["C1"]), "power": 1.0 / p["R1"]}


def walkthrough() -> list[tuple[str, str, str]]:
    """A real run through the MCP call path. Returns (call, key result, next_step)."""
    tmp = Path(tempfile.mkdtemp())
    server.PROJECT, server._con = str(tmp), db.connect(tmp)

    def call(name, **args):
        res = asyncio.run(server.mcp.call_tool(name, args))
        content = res[0] if isinstance(res, tuple) else res
        return json.loads(content[0].text)

    net = ("V1 in 0 AC 1\nR1 in out {R1}\nC1 out 0 {C1}\n.ac dec 20 10 1Meg\n"
           ".meas AC fc WHEN mag(V(out))=0.7071 FALL=1\n.end\n")
    steps = []
    r = call("start_optimization", run_id="rc_001", netlist=net,
             params=[{"name": "R1", "min": 100, "max": 100000},
                     {"name": "C1", "values": [1e-9, 1e-8, 1e-7]}],
             goals=[{"metric": "fc", "direction": "max"}],
             limits=[{"metric": "fc", "max": 20000}, {"metric": "power", "max": 0.005}],
             sim_options={"backend": "analytic", "model": "report_rc"})
    steps.append(('start_optimization(run_id="rc_001", params=[R1 range, C1 list],\n'
                  '  goals=[fc max], limits=[fc <= 20k, power <= 5m])',
                  f"run created, constraints read as {[c['op'] + format(c['value'], 'g') for c in r['constraints']]}",
                  r["next_step"]))
    r = call("run_optimization", run_id="rc_001", iterations=20)
    steps.append(('run_optimization(run_id="rc_001", iterations=20)',
                  f"{r['iterations_run']} sims, best {r['best_candidate']}, "
                  f"fc = {r['best_metrics']['fc']:,.0f} Hz, feasible = {r['best_is_feasible']}",
                  r["next_step"]))
    r = call("query_run", run_id="rc_001", what="sensitivity")
    s = r["sensitivities"][0]
    steps.append(('query_run(run_id="rc_001", what="sensitivity")',
                  f"strongest: {s['parameter']} -> {s['metric']} ({s['method']})",
                  r["next_step"]))
    r = call("query_run", run_id="rc_001", what="netlist")
    steps.append(('query_run(run_id="rc_001", what="netlist")   # forgot candidate_id',
                  f"error: {r['error']}   fix: {r['fix']}",
                  "example: " + json.dumps(r["example"])))
    r = call("generate_design_report", run_id="rc_001", plots=False)
    steps.append(('generate_design_report(run_id="rc_001")',
                  "report written: " + Path(r["report_path"]).name, r["next_step"]))
    return steps


def fig_walkthrough(steps, path: Path) -> None:
    fig = plt.figure(figsize=(14, 1.1 + 1.55 * len(steps)))
    ax = fig.add_axes((0, 0, 1, 1))
    ax.axis("off")
    ax.set_xlim(0, 1)
    ax.set_ylim(0, len(steps) * 1.55 + 1.1)
    top = len(steps) * 1.55 + 0.6
    ax.text(0.02, top, "A real run through the MCP call path: every answer says what to do next",
            fontsize=14, fontweight="bold", va="center")
    for i, (callt, result, nxt) in enumerate(steps):
        y = top - 0.65 - i * 1.55
        is_err = result.startswith("error")
        edge = BEFORE_C if is_err else AFTER_C
        ax.add_patch(plt.Rectangle((0.02, y - 1.25), 0.96, 1.3, fc="white", ec=edge, lw=1.5))
        ax.text(0.035, y - 0.1, f"{i + 1}.", fontsize=12, fontweight="bold", color=edge,
                va="top")
        ax.text(0.06, y - 0.1, callt, family="DejaVu Sans Mono", fontsize=9.5, va="top")
        ax.text(0.06, y - 0.62, "\n".join(textwrap.wrap(result, 140)), fontsize=9.5,
                color=MUTED, va="top")
        label = "fix example" if is_err else "next_step"
        ax.text(0.06, y - 0.98, f"{label}:  " + textwrap.shorten(nxt, 150), fontsize=9.5,
                color=edge, fontweight="bold", va="top")
    fig.savefig(path, dpi=150)
    plt.close(fig)


def main() -> None:
    before = json.loads((HERE / "tools_before.json").read_text())
    b, a = stats(before), stats(after_tools())
    fig_summary(b, a, HERE / "tool_surface_summary.png")
    fig_descriptions(b, a, HERE / "tool_descriptions.png")
    fig_walkthrough(walkthrough(), HERE / "walkthrough.png")
    keys = ("count", "size", "desc_median", "desc_max", "max_args", "shapeless")
    (HERE / "numbers.json").write_text(json.dumps(
        {"before": {k: b[k] for k in keys}, "after": {k: a[k] for k in keys}}, indent=2))
    print(json.dumps({"before": {k: b[k] for k in keys}, "after": {k: a[k] for k in keys}}))


if __name__ == "__main__":
    main()
