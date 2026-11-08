"""Final design report, rendered from run state.

Every number in the document comes from SQLite or from a simulation this module
launches. Nothing is passed in as prose and nothing is inferred: the report is a *view*
of `state.db`, the same way `get_best_candidate` is, so a report can never disagree with
the run it describes.

The rule that shapes the whole file: **a section with no data says so, and says which
argument would produce it.** An absent DC audit prints as "not run -- pass
`dc_audit=True`", never as a table of zeros and never as a silently dropped heading. A
reviewer reads a missing section as "checked, nothing to report" unless told otherwise,
which is the same failure `feasibility.checks_skipped` exists to prevent at the other end
of the pipeline.

Plots and the robustness sections need a `.raw`, and `experiments` stores only the log
path, so those sections re-simulate the winning netlist. That re-run doubles as the
reproduction check the report claims: when the fresh metrics disagree with the stored
ones, section 3 says so instead of quietly printing the stored pair.

Runnable as a script:

    python -m spicemcp.report amp_001 --project . --dc-audit --pvt --supply V1 --mc 100
"""

from __future__ import annotations

import math
import os
import re
from pathlib import Path

from . import db, engine, feasibility, ir, plots, raw, robustness, sim

# Unit per SPICE element letter. Only the elements whose third field really is a value:
# `M1 d g s b NM` has a node there, and labelling that "Ω" produces a plausible, wrong BOM.
_UNIT = {"r": "Ω", "c": "F", "l": "H", "v": "V", "i": "A"}
_KIND = {"r": "Resistor", "c": "Capacitor", "l": "Inductor", "v": "Voltage source",
         "i": "Current source", "d": "Diode", "m": "MOSFET", "q": "BJT", "j": "JFET",
         "z": "MESFET", "x": "Subcircuit", "e": "VCVS", "g": "VCCS", "f": "CCCS",
         "h": "CCVS", "k": "Coupling", "s": "Switch", "w": "Switch",
         "b": "Behavioural source", "t": "Transmission line", "o": "Lossy line"}
_DEV_KIND = {"mos": "MOSFET", "bjt": "BJT"}
_OP_SIDE = {">": "lo", ">=": "lo", "<": "hi", "<=": "hi"}


class ReportError(engine.EngineError):
    """Bad arguments, or a run with nothing to report."""


# ----------------------------------------------------------------------------- helpers

def eng(v, unit: str = "") -> str:
    """`4.7e-9, "F"` -> `4.7 nF`.

    Wraps the plot axis formatter so a value reads identically in the BOM and on the
    curve it produced -- two spellings of the same capacitor is how a review ends up
    debating whether they are the same part. The prefix class has to admit uppercase:
    `_eng` returns `M`, `G`, `T` for the positive decades and only `k` is lowercase. Its
    ASCII `u` becomes `µ` here, because this is prose rather than a matplotlib label.
    """
    if v is None:
        return "—"
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
        return str(v)
    m = re.match(r"^(-?[\d.]+)([a-zA-Zµ]?)$", plots._eng(float(v)))
    if not m:
        return f"{v:.4g} {unit}".strip()
    mant, prefix = m.groups()
    if prefix == "u":
        prefix = "µ"
    return f"{mant} {prefix}{unit}".rstrip() if (prefix or unit) else mant


def _num(v, digits: int = 4) -> str:
    if v is None:
        return "—"
    if isinstance(v, bool):
        return "yes" if v else "no"
    if isinstance(v, (int, float)) and math.isfinite(v):
        return f"{v:.{digits}g}"
    return str(v)


def _mark(ok) -> str:
    """Scorecard cell. `None` is its own state -- unknown is not a pass."""
    return "⚠️ UNKNOWN" if ok is None else ("✅ PASS" if ok else "❌ FAIL")


def _rel(target, start: Path) -> str:
    """Relative POSIX path for markdown; absolute only when the two share no root."""
    try:
        return Path(os.path.relpath(Path(target), start)).as_posix()
    except ValueError:                                  # different drive on Windows
        return Path(target).as_posix()


def _table(head: list[str], rows: list) -> list[str]:
    """A GitHub table, or a note when there are no rows.

    The note matters: an empty table renders as a header with nothing under it, which
    reads as "measured, all fine". Cell pipes are escaped -- a behavioural source
    (`B1 out 0 V=u(t)|x|`) would otherwise silently split into extra columns.
    """
    if not rows:
        return ["_(nothing to report here)_"]
    return ["| " + " | ".join(head) + " |",
            "|" + "---|" * len(head),
            *["| " + " | ".join("—" if c is None else str(c).replace("|", "\\|")
                                for c in r) + " |" for r in rows]]


def _skipped(why: str, how: str) -> list[str]:
    return [f"> **Not run.** {why}", ">", f"> To include it: `{how}`", ""]


def _fig(title: str, path, out_dir: Path, note: str = "") -> list[str]:
    return [f"![{title}]({_rel(path, out_dir)})", "",
            f"*{title}.{(' ' + note) if note else ''}*", ""]


def _spec_from_constraints(cons: list[dict]) -> dict:
    """`[{metric,op,value}]` -> `{metric: (lo, hi)}` for the yield and corner checks.

    Without this the Monte Carlo has no spec to count against, and `_stats` then omits
    `yield_pct` entirely -- which would render as a distribution table with no yield
    column and a `pass: true` that means "nothing was checked".
    """
    out: dict[str, list] = {}
    for c in cons:
        side = _OP_SIDE.get(c["op"])
        if side is None:                                # ==/!= is not an interval
            continue
        b = out.setdefault(c["metric"], [None, None])
        b[0 if side == "lo" else 1] = c["value"]
    return {k: tuple(v) for k, v in out.items()}


# ------------------------------------------------------------------- data extraction

def _first_ok(con, run_id: str):
    """The earliest converged experiment: the baseline the optimizer improved on.

    Earliest *successful*, not iteration 1 -- when the seed point fails to converge, its
    empty metrics printed as the baseline column read as a circuit that measured zero.
    """
    return con.execute(
        "SELECT * FROM experiments WHERE run_id=? AND status='ok' AND score IS NOT NULL"
        " ORDER BY iteration ASC LIMIT 1", (run_id,)).fetchone()


def _bom(netlist: str, params: dict, tol: dict) -> list[list[str]]:
    """One row per element, values in engineering units.

    The unit comes from the element letter read out of the deck, not from the parameter
    name: a knob called `RC` sitting on a capacitor is a capacitance. `scan_netlist`
    reports the third field after the name, which is a value only for two-terminal parts:
    on `M1 out mid 0 0 NM` it is a node and on `V1 in 0 AC 1` it is the word `AC`. So a
    non-numeric field falls back to the whole element line -- verbose, but a truncated
    source spec is a wrong one.
    """
    rows = []
    for e in feasibility.scan_netlist(netlist)["elements"]:
        letter = e["letter"]
        val = ir.parse_value(e["value"]) if (e["value"] and letter in _UNIT) else None
        # A stored netlist is fully substituted, so the deck holds the truth; `params`
        # only supplies the name of the knob that produced the value.
        knob = next((k for k, v in sorted(params.items())
                     if val is not None and isinstance(v, (int, float))
                     and abs(v - val) <= abs(val) * 1e-9), None)
        t = tol.get(letter)
        rows.append([f"`{e['name']}`", _KIND.get(letter, letter.upper()),
                     f"**{eng(val, _UNIT[letter])}**" if val is not None
                     else "_see §2.3_" if letter in ("m", "q", "j", "z", "x")
                     else f"`{e['line'].strip()}`",
                     f"`{knob}`" if knob else "fixed",
                     f"±{t * 100:g} %" if t else "—"])
    return rows


def _tradeoffs(sens: list[dict], goal: dict[str, str]) -> list[list[str]]:
    """Metric pairs one parameter pushes in opposite *useful* directions.

    Derived from this run's measured slopes, not from textbook lore: "bandwidth vs power"
    is printed only when these simulations show that parameter moving both. `goal` decides
    which way is better -- a rising `power` on a `min` objective is a cost, and a slope
    table alone cannot tell the difference, so a direction-blind version of this reports
    "increase R1 improves power" about a circuit that now draws twice as much.

    Two things are dropped rather than guessed at. A metric with no objective: with no
    stated preference there is no such thing as improving it. And a slope of exactly zero,
    which means "no measured effect" -- `engine.sensitivity` has only two direction labels
    and files it under `negative`, so reading the label instead of the number invents a
    whole trade-off row out of a parameter nothing responded to.
    """
    by_param: dict[str, list[tuple[str, float]]] = {}
    for s in sens:
        slope = s.get("estimated_sensitivity")
        if s["metric"] in goal and slope:
            by_param.setdefault(s["parameter"], []).append((s["metric"], slope))
    rows = []
    for p, items in sorted(by_param.items()):
        helps = [(m, (slope > 0) == (goal[m] == "max")) for m, slope in items]
        good = [m for m, h in helps if h]
        bad = [m for m, h in helps if not h]
        if good and bad:
            rows.append([f"increase `{p}`", ", ".join(f"`{m}`" for m in good),
                         ", ".join(f"`{m}`" for m in bad)])
    return rows


def _metric_rows(objs, cons, base: dict, final: dict, checks: list[dict]) -> list[list]:
    """Section 3's table. Verdicts come from `constraint_results` as stored at evaluation
    time, never recomputed -- recomputing lets a report and a run disagree."""
    verdict = {(c.get("constraint") or c.get("metric")): c for c in checks}
    req: dict[str, list[str]] = {}
    for c in cons:
        req.setdefault(c["metric"], []).append(f"`{c['op']} {_num(c['value'])}`")
    goal = {o["metric"]: ("maximise" if o["direction"] == "max" else "minimise")
            for o in objs}
    rows = []
    for m in sorted(set(final) | set(req) | set(goal)):
        b, f = base.get(m), final.get(m)
        target = " and ".join(req.get(m, [])) or "_objective only_"
        if m in goal:
            target += f" ({goal[m]})"
        delta = "—"
        if isinstance(b, (int, float)) and isinstance(f, (int, float)) and b:
            delta = f"{(f - b) / abs(b) * 100:+.1f} %"
        v = verdict.get(m)
        rows.append([f"`{m}`", target, _num(b), f"**{_num(f)}**", delta,
                     _mark(v["passed"]) if v else (_mark(None) if m in req else "—")])
    return rows


def _currents(dc: dict) -> dict[str, float]:
    """Terminal current per device, read out of the `.op` raw.

    `_audit_one` reports voltages and a region but no current, and a bias report without
    Id is half a bias report. LTspice does put `Id(M1)` / `Ic(Q1)` in the `.op` dataset,
    so this reads them rather than deriving them -- a device whose current is genuinely
    absent stays blank instead of being back-computed from a model card.
    """
    if dc.get("status") != "ok" or not dc.get("raw_path"):
        return {}
    try:
        w = raw.read_raw(dc["raw_path"])[0]
    except Exception:                                   # a missing/short raw is not fatal
        return {}
    out = {}
    for d in dc.get("devices", []):
        for key in (f"Id({d['name']})", f"Ic({d['name']})", f"I({d['name']})"):
            v = w.get(key)
            if v:
                out[d["name"]] = float(v[0])
                break
    return out


# ------------------------------------------------------------------------ the sections

def _s1(run, summary, objs, cons, scan) -> list[str]:
    checks = summary.get("constraints") or []
    n_ok = sum(1 for c in checks if c.get("passed"))
    counts: dict[str, int] = {}
    for e in scan["elements"]:
        k = _KIND.get(e["letter"], e["letter"].upper())
        counts[k] = counts.get(k, 0) + 1
    L = ["## 1. Executive Summary & Design Overview", "",
         "### 1.1 Circuit Purpose & Topology", "",
         f"**Run `{run['run_id']}`** — status *{run['status']}*, created "
         f"{run['created_at']}.", ""]
    if run["notes"]:
        L += [f"> {run['notes']}", ""]
    L += ["The optimized deck is a "
          + (", ".join(f"{n}× {k}" for k, n in sorted(counts.items())) or "empty")
          + " network."]
    if scan["n_reactive"]:
        L += ["", f"It carries **{scan['n_reactive']} reactive element(s)** "
                  f"({scan['n_caps']}C / {scan['n_inductors']}L), which caps the filter "
                  f"order it can realise at {scan['n_reactive']}."]
    L += ["", ("It contains " + ", ".join(f"`{a}`" for a in scan["active"])
               + " — the active device(s) providing gain."
               if scan["active"] else
               "It is passive (no active element), so its gain cannot exceed unity at "
               "any frequency."), "",
          "**Why this topology:** topology is the designer's input, not the optimizer's "
          f"output. This run tuned **{len(db.jloads(run, 'param_space'))} parameter(s)** "
          "inside a fixed deck, so what it establishes is that this topology *can* meet "
          "the specification below, and at which component values — not that it is the "
          "only or the best topology that could.", "",
          "### 1.2 Specification Compliance", ""]
    if checks:
        L += [f"**{n_ok} of {len(checks)} constraint(s) met.** Overall verdict: "
              f"{_mark(n_ok == len(checks))}", ""]
    else:
        L += ["_This run declared no constraints, so there is no pass/fail scorecard — "
              "only the objectives in section 3._", ""]
    L += [*_table(["Constraint", "Required", "Achieved", "Verdict"],
                  [[f"`{c['constraint']}`", f"`{c['required']}`", _num(c["actual"]),
                    _mark(c["passed"])] for c in checks]), "",
          f"Winner: **`{summary['best_candidate']}`** — score "
          f"{_num(summary['best_score'])}, chosen from {summary['unique_candidates']} "
          f"unique candidates over {summary['iterations_evaluated']} simulations "
          f"({summary['failures']} failed).", ""]
    if not summary["best_is_feasible"]:
        L += ["> ❌ **No feasible candidate exists in this run.** What follows is the "
              "best-*scoring* design and it violates at least one constraint. Do not "
              "read the sections below as a compliant design.", ""]
    return L


def _s2(cand, params, out_dir: Path, root: Path, tol: dict,
        schematic: str | None) -> list[str]:
    netlist = sim.decks(cand["netlist"])[0]      # the circuit, not its check decks
    cir = root / "candidates" / f"{cand['candidate_id']}.cir"
    L = ["## 2. Schematic & Component Bill of Materials", "",
         "### 2.1 Schematic & Design Files", ""]
    if schematic:
        L += _fig("Schematic", schematic, out_dir,
                  "Supplied by the designer — verify it against the netlist with "
                  "`asc.check()`.")
    else:
        L += ["> **No schematic image.** A netlist has no coordinates, so none can be "
              "rendered from it without placing every part; `spicemcp.asc` draws a `.asc` "
              "from parts *you* position, and inventing a layout here would produce a "
              "picture that is not this circuit.", ">",
              "> To include one: draw it with `spicemcp.asc`, confirm it netlists back to "
              "this deck with `asc.check()`, then pass `schematic=\"path/to/x.png\"`.", ""]
    files = [["Optimized netlist", f"[`{cir.name}`]({_rel(cir, out_dir)})",
              "byte-exact deck that produced every number in this report"]]
    if schematic:
        files.append(["Schematic",
                      f"[`{Path(schematic).name}`]({_rel(schematic, out_dir)})",
                      "editable drawing"])
    L += [*_table(["File", "Path", "What it is"], files), "",
          "### 2.2 Bill of Materials", "",
          *_table(["Ref", "Type", "Optimized value", "Tuned as", "Tolerance"],
                  _bom(netlist, params, tol)), ""]
    if not tol:
        L += ["_Tolerances are blank because no Monte Carlo ran; the values are nominal._",
              ""]
    mods = robustness.models(netlist)
    dev = [[f"`{d['name']}`", _DEV_KIND.get(d["kind"], d["kind"]), f"`{d['model']}`",
            ", ".join(f"`{k}={_num(v)}`" for k, v in
                      list(mods.get(d["model"], {}).items())[:6])
            or "_(no `.model` card in this deck — library or subcircuit device)_"]
           for d in robustness.devices(netlist)]
    L += ["### 2.3 Active Devices", "",
          *_table(["Ref", "Kind", "Model", "Model card"], dev), ""]
    if not dev:
        L += ["_No MOSFETs or BJTs in this deck._", ""]
    return L


def _s3(summary, objs, cons, base_row, sens, recheck) -> list[str]:
    base = db.jloads(base_row, "metrics") if base_row else {}
    L = ["## 3. Optimization Summary & Verification", "",
         "### 3.1 Performance Metrics", "",
         *_table(["Metric", "Target / Constraint", "Baseline", "Final Optimized", "Δ",
                  "Status"],
                 _metric_rows(objs, cons, base, summary["best_metrics"] or {},
                              summary.get("constraints") or [])), "",
         (f"Baseline is iteration {base_row['iteration']}, the earliest converged "
          f"simulation in this run; Δ is the relative move from it to the winner."
          if base_row else
          "_No converged baseline experiment, so the baseline column is empty._"), ""]
    if recheck:
        L += ["**Reproduction check.** The winning netlist was re-simulated while writing "
              "this report:", "",
              *_table(["Metric", "Stored at evaluation", "Fresh re-run", "Agrees"],
                      [[f"`{k}`", _num(v["stored"]), _num(v["fresh"]),
                        _mark(v["agrees"])] for k, v in sorted(recheck.items())]), ""]
    L += ["### 3.2 Optimization Highlights", "",
          f"- **Search:** {summary['iterations_evaluated']} simulations · "
          f"{summary['unique_candidates']} unique candidates · "
          f"{summary['failures']} failed · Pareto frontier {summary['pareto_size']} wide.",
          f"- **Strongest measured sensitivity:** "
          f"{summary['useful_observation'] or '_none measured_'}",
          f"- **Largest untried region:** "
          f"{summary['promising_unexplored_region'] or '_none_'}", ""]
    if sens:
        L += ["Measured sensitivities — `finite_difference` came from candidate pairs "
              "differing in that parameter alone (causal); `ols_marginal` is a weaker "
              "correlational fit, so the method is reported next to the slope:", "",
              *_table(["Parameter", "Metric", "d(metric)/d(param)", "Method", "R²",
                       "Confidence", "n"],
                      [[f"`{s['parameter']}`", f"`{s['metric']}`",
                        _num(s["estimated_sensitivity"]), s["method"], _num(s.get("r2")),
                        _num(s.get("confidence")), s.get("n")] for s in sens[:12]]), ""]
        if len(sens) > 12:
            L += [f"_({len(sens) - 12} further pairs — `get_parameter_sensitivity`.)_", ""]
    else:
        L += ["_No sensitivities measured: a parameter must be varied at least twice "
              "before a slope exists._", ""]
    trades = _tradeoffs(sens, {o["metric"]: o["direction"] for o in objs})
    L += ["**Trade-offs this run actually measured.** Each row is a parameter whose "
          "measured slopes help one objective and hurt another — the only trade-offs "
          "there is evidence for here, with \"better\" taken from each objective's own "
          "`direction`:", "",
          *_table(["Move", "Improves", "At the cost of"], trades), ""]
    if not trades:
        L += ["_No parameter was measured helping one objective while hurting another._",
              ""]
    if summary.get("at_param_space_bound"):
        L += ["> ⚠️ **Parameters pinned to their search bounds:** "
              + ", ".join(f"`{p}`" for p in summary["at_param_space_bound"]) + ".", ">",
              "> A bounded search always reports *an* optimum. When the winner sits on "
              "the fence you drew, the finding is \"widen `param_space`\", not "
              "\"converged\".", ""]
    else:
        L += ["No optimized parameter sits on a `param_space` edge, so the optimum is the "
              "circuit's rather than the search box's.", ""]
    return L


def _s4(figs: list[dict], out_dir: Path) -> list[str]:
    L = ["## 4. Waveform & Response Analysis", ""]
    if not figs:
        return L + _skipped(
            "No waveform analysis: it needs a `.raw`, which means re-simulating the "
            "winning candidate.", "generate(con, run_id, plot=True)")
    for f in figs:
        L += [f"### {f['title']}", ""]
        if f.get("path"):
            L += _fig(f["title"], f["path"], out_dir)
        L += [*_table(["Quantity", "Value"], f["rows"]), "", f["comment"], ""]
    return L


def _s5(dc, pvt, mc, out_dir: Path) -> list[str]:
    L = ["## 5. Robustness, DC Bias Audit & Yield Analysis", "",
         "### 5.1 DC Operating Point Audit", ""]
    if dc is None:
        L += _skipped("Bias conditions were not audited.",
                      "generate(con, run_id, dc_audit=True)")
    elif dc.get("status") != "ok":
        L += [f"> ❌ The `.op` run failed: {dc.get('failure_message')}", ""]
    elif not dc["devices"]:
        L += [f"_{dc.get('note', 'No transistors in this deck; nothing to bias.')}_", ""]
    else:
        cur = _currents(dc)
        rows = []
        for d in dc["devices"]:
            if d["kind"] == "mos":
                bias = (f"Vds {_num(d.get('vds'))} · Vgs {_num(d.get('vgs'))} · "
                        f"Vth {_num(d.get('vth'))} · Vov {_num(d.get('v_overdrive'))}")
            else:
                bias = (f"Vce {_num(d.get('vce'))} · Vbe {_num(d.get('vbe'))} · "
                        f"Vbc {_num(d.get('vbc'))}")
            rows.append([f"`{d['name']}`", d.get("type") or d["kind"], bias,
                         eng(cur.get(d["name"]), "A"),
                         f"**{d.get('region') or '—'}**",
                         _mark(None if d["verdict"] == "unknown"
                               else d["verdict"] == "pass"),
                         d.get("reason", "")])
        L += [*_table(["Device", "Type", "Bias (V)", "I", "Region", "Verdict", "Why"],
                      rows), ""]
        if not cur:
            L += ["_The current column is blank: this `.op` dataset carries no "
                  "`Id(...)`/`Ic(...)` variable. It is left empty rather than "
                  "back-computed from the model card, which would be a model's opinion "
                  "printed as a measurement._", ""]
        L += [f"Saturation margin: **{_num(dc['margin_v'])} V** of overdrive headroom "
              "(Vds ≥ Vgs−Vth+margin). A device sitting exactly on the boundary is "
              "saturated on paper and in triode over temperature, which is why the "
              "margin is not zero.", "",
              f"{dc['n_fail']} failing and {dc['n_unknown']} unresolved of "
              f"{dc['n_devices']} device(s) — overall {_mark(dc['all_in_region'])}", ""]
        if dc.get("violations"):
            L += ["Violations: " + "; ".join(f"`{v}`" for v in dc["violations"]), ""]
        if dc.get("unresolved"):
            L += ["Unresolved (no `.model` card, so the terminals cannot be resolved "
                  "here): " + "; ".join(f"`{v}`" for v in dc["unresolved"]), ""]

    L += ["### 5.2 PVT Corner Analysis", ""]
    if pvt is None:
        L += _skipped("Process / voltage / temperature corners were not swept.",
                      "generate(con, run_id, pvt=True, supply=\"V1\")")
    elif pvt.get("status") != "ok":
        L += [f"> ❌ Every corner failed to simulate. "
              f"{'; '.join(pvt.get('failed_corners') or [])}", ""]
    else:
        metrics = sorted({k for r in pvt["per_corner"] for k in (r.get("metrics") or {})})
        L += [*_table(["Corner", "Process", "T (°C)", "Vdd", "Status"]
                      + [f"`{m}`" for m in metrics],
                      [[r["corner"],
                        r["process"] + ("" if r["process_applied"] else " *(not applied)*"),
                        _num(r["temp_c"]),
                        f"×{r['vdd_scale']:.2f}"
                        + ("" if r["supply_applied"] else " *(not applied)*"),
                        "ok" if r["status"] == "ok" else "**failed**"]
                       + [_num((r.get("metrics") or {}).get(m)) for m in metrics]
                       for r in pvt["per_corner"]]), "",
              "Worst case across corners:", "",
              *_table(["Metric", "Nominal (TT)", "Min", "Max", "Spread", "Worst dev.",
                       "Worst corner"],
                      [[f"`{m}`", _num(w.get("nominal")),
                        f"{_num(w.get('min'))} ({w.get('min_corner')})",
                        f"{_num(w.get('max'))} ({w.get('max_corner')})",
                        f"{_num(w.get('spread_pct'))} %"
                        if w.get("spread_pct") is not None else "—",
                        f"{_num(w.get('worst_deviation_pct'))} %"
                        if w.get("worst_deviation_pct") is not None else "—",
                        w.get("worst_corner") or "—"]
                       for m, w in sorted(pvt["worst_case"].items())]), ""]
        if pvt.get("spec_checks"):
            L += ["Spec held at every corner?", "",
                  *_table(["Metric", "Limits", "Failing corners", "Verdict"],
                          [[f"`{c['metric']}`",
                            f"`{_num((c.get('limits') or [None, None])[0])} … "
                            f"{_num((c.get('limits') or [None, None])[1])}`",
                            ", ".join(c.get("failing_corners") or []) or "none",
                            _mark(c["passed"])] for c in pvt["spec_checks"]]), ""]
        L += [f"{pvt['corners_ok']} of {pvt['corners_run']} corners converged — overall "
              f"{_mark(pvt['pass'])}", ""]
        for n in pvt.get("notes") or []:
            L += [f"> ⚠️ {n}", ""]

    L += ["### 5.3 Monte Carlo Yield", ""]
    if mc is None:
        L += _skipped("Component tolerances were not sampled.",
                      "generate(con, run_id, monte_carlo=100)")
    elif mc.get("status") != "ok":
        L += [f"> ❌ The Monte Carlo run failed: {mc.get('failure_message')}", ""]
    else:
        L += [f"**{mc['runs_measured']} runs** at ±{mc['tolerances']['r'] * 100:g} % on "
              f"resistors and ±{mc['tolerances']['c'] * 100:g} % on capacitors, "
              f"{mc['distribution']}.", "",
              *_table(["Metric", "Mean", "σ", "CV", "Min", "Max", "−3σ", "+3σ", "Spec",
                       "Cpk", "Yield"],
                      [[f"`{k}`", _num(s.get("mean")), _num(s.get("std")),
                        f"{_num(s.get('cv_pct'))} %" if s.get("cv_pct") is not None
                        else "—",
                        _num(s.get("min")), _num(s.get("max")), _num(s.get("sigma3_lo")),
                        _num(s.get("sigma3_hi")),
                        f"{_num(s.get('spec_lo'))} … {_num(s.get('spec_hi'))}"
                        if "spec_lo" in s else "—",
                        _num(s.get("cpk")),
                        f"**{_num(s.get('yield_pct'))} %**" if "yield_pct" in s else "—"]
                       for k, s in sorted(mc["statistics"].items())
                       if "error" not in s]), ""]
        graded = [s for s in mc["statistics"].values() if "yield_pct" in s]
        L += [f"Overall {_mark(mc['pass'])} — yield counts samples inside the spec limits, "
              "which is not the same claim as \"±3σ fits inside the spec\"."
              if graded else
              "> ⚠️ **No yield was computed:** this run's constraints gave no interval for "
              "any measured metric, so the table is a spread, not a yield. `pass` above "
              "means \"nothing was checked\".", ""]
        if graded:
            L += [f"_{graded[0]['yield_ci_note']}._", ""]
        for k, p in sorted((mc.get("plots") or {}).items()):
            if p and not str(p).startswith("not plotted"):
                L += _fig(f"Monte Carlo distribution — {k}", p, out_dir)
        skipped = mc.get("components_skipped") or []
        if skipped:
            L += ["> ⚠️ Not randomised (the value is an expression, not a number): "
                  + ", ".join(f"`{c['name']}`" for c in skipped) + ".", ""]
    return L


def _s6(run, cand, root: Path, out_dir: Path, schematic) -> list[str]:
    cir = root / "candidates" / f"{cand['candidate_id']}.cir"
    directives = [ln.strip() for ln in cand["netlist"].splitlines()
                  if re.match(r"\s*\.(ac|tran|dc|op|noise|tf|four|step|meas)\b", ln, re.I)]
    meas = [d for d in directives if re.match(r"\s*\.meas\b", d, re.I)]
    analyses = [d for d in directives if d not in meas]
    L = ["## 6. LTspice Reproduction Guide", "",
         f"1. **Open the deck.** `{_rel(cir, out_dir)}` — *File → Open*, set the filter "
         "to *All Files*, and pick the `.cir`."
         + (f" The schematic `{_rel(schematic, out_dir)}` shows the same circuit as a "
            "drawing." if schematic else
            " (There is no `.asc` for this run; the netlist is the authoritative deck.)"),
         "2. **The analysis and measurement directives are already in the deck** — there "
         "is nothing to add:", "",
         "   ```spice",
         *([f"   {d}" for d in (analyses + meas)] or ["   (none)"]),
         "   ```", "",
         "3. **Run it:** *Simulate → Run*, or press **F9**.",
         "4. **Read the measurements:** **Ctrl + L**, or *View → SPICE Error Log*. Every "
         "`.meas` result is printed there — that log is where this server reads its "
         "metrics from, so the numbers you see are the numbers in section 3.", ""]
    if any(re.match(r"\s*\.ac\b", d, re.I) for d in analyses):
        L += ["> **Reading the AC log:** LTspice already reports AC `.meas` magnitudes in "
              "dB, so a `db()` wrapper converts twice — it never errors, it just returns "
              "a smaller plausible number. AC phase arrives free as `<name>_deg`.", ""]
    L += ["To re-derive this report from stored state instead of by hand:", "",
          "```python", f'get_optimization_status(run_id="{run["run_id"]}")',
          f'get_best_candidate(run_id="{run["run_id"]}")',
          f'rollback_to_candidate(run_id="{run["run_id"]}", '
          f'candidate_id="{cand["candidate_id"]}")   # restores the stored bytes',
          "```", ""]
    return L


# ------------------------------------------------------------------- plot production

def _plot_sections(raw_path: str, project, node, thd_f0) -> list[dict]:
    """One figure per analysis the `.raw` actually contains.

    Chosen from `w.analysis` rather than from what the caller hoped for: asking for a Bode
    plot of a transient run is an error worth reporting, not an empty axis.
    """
    waves = raw.read_raw(raw_path)
    if not waves:
        return []
    w = waves[0]
    kind = (w.analysis or "").lower()
    out = plots.plots_dir(project)
    figs = []
    if kind.startswith("ac"):
        m = plots.plot_bode(w, out=out / "final_bode.png", node=node, project=project)
        pm, gm = m.get("phase_margin_deg"), m.get("gain_margin_db")
        figs.append({
            "title": "Frequency Response (Bode)", "path": m["plot_path"],
            "rows": [["DC gain", f"{_num(m.get('dc_gain_db'))} dB"],
                     ["Peak gain", f"{_num(m.get('peak_gain_db'))} dB at "
                                   f"{eng(m.get('f_peak_hz'), 'Hz')}"],
                     ["−3 dB bandwidth", eng(m.get("bw_3db_hz"), "Hz")],
                     ["Unity-gain frequency", eng(m.get("ugf_hz"), "Hz")],
                     ["Phase margin", f"{_num(pm)} °" if pm is not None else "—"],
                     ["Gain margin", f"{_num(gm)} dB" if gm is not None else "—"],
                     ["Points swept", m.get("n_points")]],
            "comment": (m.get("note") or
                        f"Magnitude and phase of {m.get('signal')}. The phase-margin "
                        "marker sits at the unity-gain crossing, which is what makes it a "
                        "stability claim rather than arithmetic on a curve — and it is a "
                        "stability claim only if this trace is a **loop** gain. On a "
                        "closed-loop response, read the bandwidth and ignore the "
                        "margins.")})
    elif kind.startswith("tran"):
        m = plots.plot_transient(w, out=out / "final_tran.png", node=node, project=project)
        rise = m.get("t_rise_10_90") or m.get("t_fall_10_90")
        figs.append({
            "title": "Time-Domain (Transient / Step)", "path": m["plot_path"],
            "rows": [["Initial → final",
                      f"{_num(m.get('initial'))} → {_num(m.get('final'))}"],
                     ["Rise time (10–90 %)", eng(rise, "s")],
                     ["Overshoot", f"{_num(m.get('overshoot_pct'))} %"],
                     ["Settling to ±1 %", eng(m.get("t_settle_1pct"), "s")],
                     ["Settling to ±0.1 %", eng(m.get("t_settle_0p1pct"), "s")],
                     ["Peak", f"{_num(m.get('v_max'))} at {eng(m.get('t_max'), 's')}"],
                     ["Slew (+ / −)", f"{eng(m.get('slew_pos_v_per_s'), 'V/s')} / "
                                      f"{eng(m.get('slew_neg_v_per_s'), 'V/s')}"]],
            "comment": (m.get("note") or
                        "Settling is the **last** time the trace leaves the band, not the "
                        "first time it enters: a ringing waveform re-crosses, and "
                        "first-entry reports a settling time the circuit has not reached. "
                        "A blank settling time means the capture window ended before the "
                        "trace stayed inside the band — that is a fact about the `.tran` "
                        "statement, so it is reported as blank rather than as the record "
                        "end.")})
        try:
            t = plots.plot_thd(w, out=out / "final_thd.png", node=node, f0=thd_f0,
                               project=project)
            figs.append({
                "title": "Distortion Spectrum (THD)", "path": t["plot_path"],
                "rows": [["Fundamental", f"{eng(t.get('f0_hz'), 'Hz')} at "
                                         f"{eng(t.get('fundamental_v'), 'V')}"],
                         ["Whole cycles analysed", t.get("cycles")],
                         ["THD", f"{_num(t.get('thd_pct'))} %"],
                         ["THD", f"{_num(t.get('thd_db'))} dB"],
                         ["SINAD", f"{_num(t.get('sinad_db'))} dB"],
                         ["DC offset", eng(t.get("dc_offset"), "V")],
                         ["Harmonics resolved", str(t.get("harmonics_present"))]],
                "comment": "Bars are dBc against the fundamental. Harmonics that were "
                           "never synthesised are drawn as nothing rather than as a bar "
                           "clipped to the axis floor — a clipped bar is full height and "
                           "reads as heavy distortion at every harmonic."})
        except Exception as e:
            figs.append({"title": "Distortion Spectrum (THD)", "path": "",
                         "rows": [["Not produced", str(e)]],
                         "comment": "THD needs a periodic steady-state signal; a step "
                                    "response has no fundamental to measure."})
    else:
        figs.append({"title": f"Unplotted analysis: `{w.plotname}`", "path": "",
                     "rows": [["Analysis", kind or "unknown"]],
                     "comment": "Only `.ac` and `.tran` datasets have a plot here. A "
                                "`.op` result is a bias point — section 5.1 is where it "
                                "is reported."})
    return figs


# -------------------------------------------------------------------------- entry point

def generate(con, run_id: str, *, out: str | None = None, schematic: str | None = None,
             plot: bool = True, dc_audit: bool = False, pvt: bool = False,
             monte_carlo: int = 0, r_tol: float = 0.01, c_tol: float = 0.05,
             supply: str | None = None, node: str | None = None,
             thd_f0: float | None = None, corner_lib: dict | None = None,
             margin_v: float = 0.05, sim_config: dict | None = None) -> dict:
    """Render the full design report for `run_id` and write it to disk.

    Everything past the stored state is opt-in, because each flag costs LTspice launches:
    one shared re-run of the winner (it produces both the plots and section 3's
    reproduction check), plus `dc_audit` 1, `pvt` 5, `monte_carlo` 1 stepped launch rather
    than N. The defaults render sections 1-3 and 6 from state alone.

    Sections with no data are printed as "not run" with the argument that produces them,
    never omitted -- a reviewer reads a missing heading as a clean bill of health.
    """
    run = engine.get_run(con, run_id)
    summary = engine.summary(con, run_id)
    if not summary["best_candidate"]:
        raise ReportError(
            f"run {run_id!r} has no successful candidate to report on "
            f"({summary['iterations_evaluated']} experiment(s), "
            f"{summary['failures']} failed); run `evaluate_candidate` or "
            f"`run_optimization` first")

    cand = engine.get_candidate(con, summary["best_candidate"])
    params = db.jloads(cand, "params")
    objs, cons = db.jloads(run, "objectives", []), db.jloads(run, "constraints", [])
    project = engine._project(con)
    root = db.state_root(project)
    out_path = Path(out) if out else root / "reports" / f"{run_id}_DESIGN_REPORT.md"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_dir, work = out_path.parent, root / "simulations" / f"report_{run_id}"
    cfg = {**db.jloads(run, "sim_config", {}), **(sim_config or {})}
    netlist, spec = cand["netlist"], _spec_from_constraints(cons)
    # A non-LTspice run has no netlist semantics at all: analytic metrics come from a
    # registered Python function, so there is no `.raw` to plot and no deck to bias.
    # Reported as skipped rather than run-and-failed, which would read as a broken circuit.
    lt = cfg.get("backend", "ltspice") == "ltspice"
    notes = [] if lt else [f"backend is `{cfg.get('backend')}`, not `ltspice`: no "
                           "waveforms, bias point or tolerance sampling exist for it"]

    figs, recheck, ran = [], {}, []
    if lt and (plot or dc_audit or pvt or monte_carlo):
        # The one simulation everything else reuses. `params` is passed through even though
        # a stored netlist is already substituted, so the analytic backend stays callable.
        res = sim.simulate(netlist, params, {**cfg, "require_meas": False}, work, "final")
        ran.append(f"re-simulated `{cand['candidate_id']}`")
        if res.status == "ok":
            for k, v in sorted((summary["best_metrics"] or {}).items()):
                f = res.metrics.get(k)
                recheck[k] = {"stored": v, "fresh": f, "agrees": None if f is None else (
                    f == v or abs(f - v) <= max(abs(v), abs(f)) * 1e-6)}
            if plot and res.raw_path:
                try:
                    figs = _plot_sections(res.raw_path, project, node, thd_f0)
                except Exception as e:              # a missing matplotlib is not fatal
                    figs = [{"title": "Plots unavailable", "path": "",
                             "rows": [["Error", str(e)]],
                             "comment": "Install with `pip install 'spicemcp[plots]'`; "
                                        "the metrics above came from the `.log` and do "
                                        "not depend on it."}]
            elif plot:
                figs = [{"title": "No waveform data", "path": "",
                         "rows": [["`.raw` written", "no"]],
                         "comment": "The re-run produced no `.raw`, so there is no "
                                    "waveform to analyse. LTspice writes one for `.ac` "
                                    "and `.tran`; a deck with only `.meas` on a `.op` "
                                    "has nothing to plot."}]
        else:
            figs = [{"title": "Waveform analysis unavailable", "path": "",
                     "rows": [["Re-simulation failed", res.failure_message or "?"]],
                     "comment": "The stored metrics in section 3 stand — they were "
                                "measured when the candidate was evaluated — but nothing "
                                "could be re-derived from a fresh run."}]

    dc = pvtr = mc = None
    netlist = sim.decks(netlist)[0]          # audits and corners run on the circuit deck
    if dc_audit and lt:
        dc = robustness.audit_dc_operating_points(netlist, work, sim_config=cfg,
                                                  margin_v=margin_v)
        ran.append("DC operating point audit")
    if pvt and lt:
        pvtr = robustness.evaluate_pvt_corners(netlist, work, supply=supply,
                                               corner_lib=corner_lib, spec=spec,
                                               sim_config=cfg)
        ran.append(f"{pvtr['corners_run']} PVT corners")
    if monte_carlo and lt:
        try:
            mc = robustness.evaluate_monte_carlo(netlist, work, n=monte_carlo,
                                                 r_tol=r_tol, c_tol=c_tol, spec=spec,
                                                 sim_config=cfg, project=project)
            ran.append(f"{monte_carlo}-run Monte Carlo")
        except robustness.RobustnessError as e:
            mc = {"status": "failed", "failure_message": str(e)}

    tol = {"r": r_tol, "c": c_tol, "l": r_tol} if (mc and mc.get("status") == "ok") else {}
    doc = [f"# Design Report — `{run_id}`", "",
           f"*Generated {db.now()} by SpiceMCP from "
           f"`{_rel(root / 'state.db', out_dir)}`. Every value below is read from that "
           "database or from a simulation launched while writing this file; none of it is "
           "hand-entered.*", ""]
    for n in notes:
        doc += [f"> ⚠️ {n}", ""]
    doc += ["---", "",
            *_s1(run, summary, objs, cons, feasibility.scan_netlist(netlist)), "---", "",
            *_s2(cand, params, out_dir, root, tol, schematic), "---", "",
            *_s3(summary, objs, cons, _first_ok(con, run_id),
                 engine.sensitivity(con, run_id), recheck), "---", "",
            *_s4(figs, out_dir), "---", "",
            *_s5(dc, pvtr, mc, out_dir), "---", "",
            *_s6(run, cand, root, out_dir, schematic)]
    text = "\n".join(doc).rstrip() + "\n"
    out_path.write_text(text, encoding="utf-8")
    return {"report_path": str(out_path), "run_id": run_id,
            "candidate_id": cand["candidate_id"],
            "analyses_run": ran or ["none (stored state only)"],
            "figures": [f["path"] for f in figs if f.get("path")],
            "notes": notes, "bytes": len(text.encode("utf-8"))}


def main(argv=None) -> int:
    """`python -m spicemcp.report <run_id>` -> writes the report, prints its path."""
    import argparse
    p = argparse.ArgumentParser(prog="python -m spicemcp.report",
                                description="Write a design report for a SpiceMCP run.")
    p.add_argument("run_id")
    p.add_argument("--project", default=os.environ.get("SPICEMCP_PROJECT") or os.getcwd(),
                   help="project dir holding .ltspice-mcp/ (default: $SPICEMCP_PROJECT)")
    p.add_argument("--out", help="output .md path (default: "
                                 ".ltspice-mcp/reports/<run>_DESIGN_REPORT.md)")
    p.add_argument("--schematic", help="PNG/SVG to embed in section 2")
    p.add_argument("--no-plot", action="store_true", help="skip the waveform re-run")
    p.add_argument("--dc-audit", action="store_true", help="+1 sim: bias point per device")
    p.add_argument("--pvt", action="store_true", help="+5 sims: TT/SS/FF/SF/FS corners")
    p.add_argument("--supply", help="DC source to scale +-10%% for the V axis, e.g. V1")
    p.add_argument("--mc", type=int, default=0, metavar="N",
                   help="+1 sim: N-run Monte Carlo on passive tolerances")
    p.add_argument("--r-tol", type=float, default=0.01)
    p.add_argument("--c-tol", type=float, default=0.05)
    p.add_argument("--node", help="node to measure in the plots (default: V(out))")
    a = p.parse_args(argv)
    res = generate(db.connect(a.project), a.run_id, out=a.out, schematic=a.schematic,
                   plot=not a.no_plot, dc_audit=a.dc_audit, pvt=a.pvt, monte_carlo=a.mc,
                   r_tol=a.r_tol, c_tol=a.c_tol, supply=a.supply, node=a.node)
    print(res["report_path"])
    for n in res["notes"]:
        print(f"note: {n}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
