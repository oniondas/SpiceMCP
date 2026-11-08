"""Pre-flight feasibility: is this specification reachable *at all*, before any simulation.

Three layers, cheapest first, and the split is about what kind of wrong each one catches:

* **static** -- the spec contradicts itself. `fc > 10k` together with `fc < 9k` has no
  solution and never needed a simulator to say so.
* **template** -- the deck cannot report or reach the spec. A metric with no `.meas`
  directive is not a circuit problem at all; it is forty iterations of `score=None`,
  which is the single most expensive way this server can be used wrong.
* **physics** -- first-principles bounds. 100 V/us into 10 pF needs 1 mA of slewing
  current whatever the topology, so a 0.5 mW budget at 1.8 V is not a tuning problem.

Two rules make the verdict worth acting on:

1. **Every `error` is a bound that holds for any topology**, so each one is derived from a
   conservation law or a definition, never from a design style: the transconductance
   ceiling uses gm/Id <= 1/Vt (weak inversion, n=1), which no MOSFET beats; the linear
   regulator efficiency ceiling is Vout/Vin by KVL. A wrong `error` is a bug, not a matter
   of taste -- so anything a good engineer would merely raise an eyebrow at is a `warning`,
   and warnings never block a run.
2. **A check that cannot run says so.** Missing `technology_params` land in
   `checks_skipped`, never in a silent pass. "Nothing ruled this out" and "this was
   verified" are different answers, and `status` distinguishes them (`feasible` vs
   `unverified`).

Units are SI throughout: V, A, F, H, s, Hz, W, V/s, V/sqrt(Hz), ohm, degrees, dB.
Slew rate quoted in V/us is the most likely way to mislead this module, so numbers are
echoed back in both and a suspiciously small SI slew rate is flagged in practical mode.

ponytail: metric recognition is by name (`slew_rate`, `sr`, `slew` -> slew rate). No
units metadata on objectives -- add it the day a caller wants dBV vs V distinguished
automatically rather than by naming the metric `..._db`.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field

from . import engine, ir, robustness

ERROR, WARN = "error", "warning"
MODES = ("practical", "theoretical", "concept")
CIRCUIT_TYPES = ("general", "filter", "opamp", "ldo", "dcdc")

KB = 1.380649e-23           # J/K
T_ZERO = 273.15             # 0 C in K
Q_E = 1.602176634e-19       # C

# Element letters that can deliver power gain: controlled sources, behavioural sources,
# transistors, subcircuits, special-function devices. Everything else in a deck is
# passive or a source.
ACTIVE_LETTERS = set("efghbmqjza")
SUBCKT_LETTERS = set("x")
SWITCH_LETTERS = set("sw")

# Practical value windows for on-chip / lab-realistic passives -> (lo, hi, unit).
PASSIVE_WINDOW = {"r": (0.1, 100e6, "ohm"), "c": (0.1e-15, 1e-3, "F"),
                  "l": (1e-12, 1.0, "H")}


class FeasibilityError(engine.EngineError):
    """Bad arguments to the checker itself (unknown mode, etc.).

    Subclasses EngineError so `server._guard` turns it into `{"error": ...}` like every
    other tool-level mistake, instead of a traceback across the MCP boundary.
    """


# --------------------------------------------------------------------------- data models

@dataclass
class FeasibilityIssue:
    severity: str               # "error" = impossible for any topology; "warning" = smell
    layer: str                  # "static" | "template" | "physics" | "practical"
    code: str
    message: str
    suggestion: str | None = None
    details: dict | None = None

    def to_dict(self) -> dict:
        out = {"severity": self.severity, "layer": self.layer, "code": self.code,
               "message": self.message}
        if self.suggestion:
            out["suggestion"] = self.suggestion
        if self.details:
            out["details"] = self.details
        return out


@dataclass
class FeasibilityVerdict:
    mode: str
    circuit_type: str
    violations: list[FeasibilityIssue] = field(default_factory=list)
    details: dict = field(default_factory=dict)
    checks_run: list[str] = field(default_factory=list)
    checks_skipped: list[str] = field(default_factory=list)

    # --- construction
    def add(self, severity: str, layer: str, code: str, message: str,
            suggestion: str | None = None, **details) -> None:
        self.violations.append(FeasibilityIssue(severity, layer, code, message,
                                                suggestion, details or None))

    def ran(self, name: str) -> None:
        self.checks_run.append(name)

    def skip(self, name: str) -> None:
        self.checks_skipped.append(name)

    # --- derived
    @property
    def errors(self) -> list[FeasibilityIssue]:
        return [i for i in self.violations if i.severity == ERROR]

    @property
    def warnings(self) -> list[FeasibilityIssue]:
        return [i for i in self.violations if i.severity == WARN]

    @property
    def passed(self) -> bool:
        """No hard impossibility. Warnings do not block -- an impractical design is
        still a design, and only the caller knows how much pain they will accept."""
        return not self.errors

    @property
    def status(self) -> str:
        if self.errors:
            return "infeasible"          # a proof; nothing unchecked can soften it
        # "unverified" outranks "impractical" on purpose. Warnings are all listed in
        # `violations` either way, so nothing is lost by not naming them here -- whereas
        # "impractical" claims a verdict was reached, and a domain circuit whose domain
        # physics never ran was not verified, however many static checks passed. Saying
        # anything but "unverified" here is the failure mode this module exists to avoid.
        if not self.checks_run:
            return "unverified"
        if self.circuit_type != "general" and not any(
                c.startswith("physics:") for c in self.checks_run):
            return "unverified"
        if self.warnings:
            return "impractical"
        return "feasible"

    @property
    def suggestions(self) -> list[str]:
        out = []
        for i in self.violations:
            if i.suggestion and i.suggestion not in out:
                out.append(i.suggestion)
        return out

    def summary(self) -> str:
        n_e, n_w = len(self.errors), len(self.warnings)
        if n_e:
            return (f"{n_e} hard violation(s): this specification cannot be met by any "
                    f"topology as written. Fix the spec before spending simulations.")
        if self.status == "unverified":
            return ("nothing was ruled out, but the domain physics could not be checked: "
                    + "; ".join(self.checks_skipped[:3] or ["no checkable inputs"]))
        if n_w:
            return (f"no hard impossibility, {n_w} practical concern(s). Buildable, but "
                    f"expect the flagged parts to be the hard ones.")
        return (f"{len(self.checks_run)} check(s) passed; nothing rules this out. "
                f"Not a guarantee that a solution exists in the given param_space.")

    def to_dict(self) -> dict:
        return {"status": self.status, "passed": self.passed, "mode": self.mode,
                "circuit_type": self.circuit_type, "summary": self.summary(),
                "violations": [i.to_dict() for i in self.violations],
                "n_errors": len(self.errors), "n_warnings": len(self.warnings),
                "suggestions": self.suggestions, "details": self.details,
                "checks_run": self.checks_run, "checks_skipped": self.checks_skipped}


# ------------------------------------------------------------------------ name matching

# Canonical quantity -> names and name-tokens that mean it. Matching is on the whole
# metric name or on any `_`-separated token, so `slew_rate`, `sr` and `max_slew` all land
# on slew_rate while `gain_db` lands on gain.
_ALIASES = {
    "power": ("power", "pdiss", "pwr"),
    "slew_rate": ("slew_rate", "slew", "sr", "slewrate"),
    "gbw": ("gbw", "gain_bandwidth", "ugbw", "gainbw", "ft"),
    "bandwidth": ("bandwidth", "bw", "f3db", "fc", "f0"),
    "noise": ("noise", "vn", "en", "vnoise"),
    "phase_margin": ("phase_margin", "phase", "pm", "phasemargin"),
    "gain": ("gain", "av", "avol", "aol", "gainmag"),
    "efficiency": ("efficiency", "eff", "eta"),
    "dropout": ("dropout", "vdo", "vdrop"),
    "duty": ("duty", "dutycycle"),
}
# Single-letter aliases are deliberately absent: `p` would claim `vout_p` for power and
# `d` would claim any delay metric for duty cycle, and a misidentified metric produces a
# confident physics verdict about the wrong quantity.

# technology_params: canonical -> accepted keys, plus the unit the number must be in.
_TECH = {
    "vdd": (("vdd", "vsupply", "vcc", "supply", "v_supply"), "V"),
    "vin": (("vin", "v_in", "vinput"), "V"),
    "vout": (("vout", "v_out", "voutput"), "V"),
    "cload": (("cload", "c_load", "cl"), "F"),
    "iload": (("iload", "i_load", "iout", "i_out"), "A"),
    "itotal": (("itotal", "i_total", "ibias", "i_bias", "current_budget", "islew"), "A"),
    "temp_c": (("temp_c", "temp", "temperature_c"), "C"),
    "ft": (("ft", "f_t", "device_ft", "fmax_device"), "Hz"),
    "rmin": (("rmin", "r_min", "rsource", "r_source", "rs"), "ohm"),
    "vov": (("vov", "v_ov", "overdrive"), "V"),
    "rds_on": (("rds_on", "rdson", "r_ds_on", "rpass", "ron"), "ohm"),
    "f_pass": (("f_pass", "fpass", "fp", "passband_hz"), "Hz"),
    "f_stop": (("f_stop", "fstop", "fs", "stopband_hz"), "Hz"),
    "ripple_db": (("ripple_db", "passband_ripple_db", "ap_db", "ap", "ripple"), "dB"),
    "atten_db": (("atten_db", "stopband_atten_db", "as_db", "astop_db", "atten"), "dB"),
    "filter_kind": (("filter_kind", "approximation", "family"), "name"),
    "lmin": (("lmin", "l_min", "min_channel_length"), "m"),
    "duty_max": (("duty_max", "dmax", "max_duty"), "fraction"),
    "fsw": (("fsw", "f_sw", "switching_hz"), "Hz"),
    "sampled": (("sampled", "switched_capacitor"), "bool"),
}
_TECH_LOOKUP = {k: canon for canon, (keys, _u) in _TECH.items() for k in keys}
_TECH_UNITS = {canon: unit for canon, (_k, unit) in _TECH.items()}


def _tokens(name: str) -> set[str]:
    return {t for t in re.split(r"[^a-z0-9]+", name.lower()) if t}


def _is(metric: str, canon: str) -> bool:
    names = set(_ALIASES[canon])
    m = metric.lower()
    return m in names or bool(_tokens(m) & names)


def _tech_params(tp: dict | None) -> tuple[dict, list[str]]:
    """Canonicalise technology_params -> (values, unrecognised keys).

    Unrecognised keys are reported rather than dropped: `{"c_l": 10e-12}` silently
    ignored looks exactly like a passing check that never ran.
    """
    out, unknown = {}, []
    for k, v in (tp or {}).items():
        canon = _TECH_LOOKUP.get(str(k).strip().lower())
        if canon is None:
            unknown.append(str(k))
            continue
        if canon in ("filter_kind",):
            out[canon] = str(v).strip().lower()
        elif canon == "sampled":
            out[canon] = bool(v)
        else:
            try:
                out[canon] = float(v)
            except (TypeError, ValueError):
                unknown.append(f"{k} (not a number: {v!r})")
    return out, unknown


def _g(x) -> str:
    """Compact number for a message. `None` prints as `?` rather than crashing a f-string."""
    if x is None:
        return "?"
    return f"{float(x):.4g}"


# ---------------------------------------------------------------------- constraint model

def _limits(cons: list[dict]) -> dict[str, dict]:
    """metric -> tightest lower/upper bound plus every raw item.

    The tightest bound is the one that decides feasibility, and on a tie the *strict*
    operator is tighter: `>10` and `>=10` together permit only x>10.
    """
    by: dict[str, dict] = {}
    for c in cons:
        e = by.setdefault(c["metric"], {"lo": None, "lo_op": None, "hi": None,
                                        "hi_op": None, "eq": [], "items": []})
        e["items"].append((c["op"], c["value"]))
        if c["op"] in (">", ">="):
            if e["lo"] is None or (c["value"], c["op"] == ">") > (e["lo"], e["lo_op"] == ">"):
                e["lo"], e["lo_op"] = c["value"], c["op"]
        elif c["op"] in ("<", "<="):
            if e["hi"] is None or (c["value"], c["op"] != "<") < (e["hi"], e["hi_op"] != "<"):
                e["hi"], e["hi_op"] = c["value"], c["op"]
        elif c["op"] == "==":
            e["eq"].append(c["value"])
    return by


def _find(lim: dict[str, dict], canon: str) -> tuple[str | None, dict]:
    for metric in sorted(lim):
        if _is(metric, canon):
            return metric, lim[metric]
    return None, {}


def _db_scale(metric: str) -> bool:
    return "db" in _tokens(metric)


def _as_fraction(x: float) -> tuple[float, str]:
    """An efficiency number and the scale it must have been written on.

    The cut is at 5, not at 1: `eff > 1.5` is someone asking for 150% (a real mistake
    worth an error), while `eff > 1.5` meaning 1.5% is not a specification anyone writes.
    Above 5 the number can only be percent -- nobody demands 5x power gain from a
    regulator. The assumed scale is always named in the message.
    """
    return (x / 100.0, "percent") if x > 5 else (x, "fraction")


# ------------------------------------------------------------------------- netlist scan

_MEAS = re.compile(
    r"^\s*\.meas(?:ure)?\s+(?:(?:ac|dc|tran|op|noise|tf)\s+)?(\w+)", re.I | re.M)
_PARAM_DECL = re.compile(r"^\s*\.param\s+(.*)$", re.I | re.M)
_ASSIGN = re.compile(r"(\w+)\s*=")
_ELEM = re.compile(r"^([A-Za-z])(\S*)\s+(.*)$")


def scan_netlist(template: str) -> dict:
    """Everything the template checks need, in one pass.

    Element lines come from `robustness._lines` so continuations and trailing comments are
    handled the same way the simulation path handles them -- a checker that reads the deck
    differently from the runner is a checker that lies.
    """
    lines = robustness._lines(template)
    elements, letters = [], []
    for ln in lines:
        if ln.lstrip().startswith("."):
            continue
        m = _ELEM.match(ln)
        if not m:
            continue
        letter = m.group(1).lower()
        rest = m.group(3).split()
        letters.append(letter)
        elements.append({"name": m.group(1) + m.group(2), "letter": letter,
                         "value": rest[2] if len(rest) > 2 else None, "line": ln})
    declared = {a.lower() for m in _PARAM_DECL.finditer(template)
                for a in _ASSIGN.findall(m.group(1))}
    n_c = letters.count("c")
    n_l = letters.count("l")
    return {
        "elements": elements,
        "meas": {m.group(1).lower() for m in _MEAS.finditer(template)},
        "placeholders": {m.group(1) for m in ir.PLACEHOLDER.finditer(template)},
        "declared_params": declared,
        "n_caps": n_c, "n_inductors": n_l, "n_reactive": n_c + n_l,
        "active": sorted({e["name"] for e in elements
                          if e["letter"] in ACTIVE_LETTERS | SUBCKT_LETTERS}),
        "switches": sorted({e["name"] for e in elements
                            if e["letter"] in SWITCH_LETTERS}),
        "has_coupling": bool(re.search(r"^\s*k\w*\s+\S+\s+\S+", template, re.I | re.M)),
    }


def _param_kinds(scan: dict | None, param_space: dict | None) -> dict[str, str]:
    """param -> element letter it substitutes into (`R1` -> "r").

    Read from the deck, not from the name: a parameter called `RC` that sits on a
    capacitor is a capacitance, and applying resistor limits to it would invent a
    warning. Falls back to the first letter of the name when there is no deck
    (mode="concept"), which is the only signal available then.
    """
    kinds = {}
    for name in (param_space or {}):
        letter = None
        for e in (scan or {}).get("elements", []):
            if f"{{{name}}}" in e["line"]:
                letter = e["letter"]
                break
        if letter is None and scan is None:
            letter = name[:1].lower()
        if letter in PASSIVE_WINDOW:
            kinds[name] = letter
    return kinds


def _span(spec: dict) -> tuple[float | None, float | None]:
    if "values" in spec:
        vals = [float(v) for v in spec["values"]
                if isinstance(v, (int, float)) and not isinstance(v, bool)]
        return (min(vals), max(vals)) if vals else (None, None)
    try:
        return float(spec["min"]), float(spec["max"])
    except (KeyError, TypeError, ValueError):
        return None, None


# ------------------------------------------------------------- layer 1: static / logical

def _static(v: FeasibilityVerdict, objs: list[dict], lim: dict, param_space: dict | None,
            tech: dict) -> None:    # --- contradictory bounds on one metric
    for metric, e in sorted(lim.items()):
        lo, hi, lo_op, hi_op = e["lo"], e["hi"], e["lo_op"], e["hi_op"]
        if lo is not None and hi is not None:
            if lo > hi:
                v.add(ERROR, "static", "empty_window",
                      f"{metric} is required {lo_op}{_g(lo)} and {hi_op}{_g(hi)}: "
                      f"no value satisfies both",
                      f"widen the window, e.g. {metric} {lo_op}{_g(lo)} and "
                      f"<{_g(lo * 1.05 if lo > 0 else hi)}", metric=metric)
            elif lo == hi and (lo_op == ">" or hi_op == "<"):
                v.add(ERROR, "static", "empty_window",
                      f"{metric} {lo_op}{_g(lo)} and {hi_op}{_g(hi)} is an empty open "
                      f"interval",
                      f"use >= / <= if you meant exactly {_g(lo)}", metric=metric)
            elif lo == hi:
                v.add(WARN, "static", "single_point_window",
                      f"{metric} is pinned to exactly {_g(lo)}; a measured value hits an "
                      f"equality only by luck",
                      f"give {metric} a tolerance band instead of a point", metric=metric)
            else:
                width = (hi - lo) / max(abs(lo), abs(hi), 1e-300)
                if width < 1e-6:
                    v.add(WARN, "static", "window_below_resolution",
                          f"the {metric} window is {width:.2g} of its own magnitude wide; "
                          f".meas will not resolve it reliably",
                          f"widen {metric} to at least ~1e-4 relative width",
                          metric=metric, relative_width=width)
        if len(set(e["eq"])) > 1:
            v.add(ERROR, "static", "conflicting_equalities",
                  f"{metric} is required to equal {sorted(set(e['eq']))} at once",
                  metric=metric)
        for eq in set(e["eq"]):
            if (lo is not None and eq < lo) or (hi is not None and eq > hi):
                v.add(ERROR, "static", "equality_outside_window",
                      f"{metric}=={_g(eq)} falls outside its own "
                      f"[{_g(lo)}, {_g(hi)}] bounds", metric=metric)
    if lim:
        v.ran("static:constraint_consistency")

    # --- objectives pulling both ways on one metric
    dirs: dict[str, set[str]] = {}
    for o in objs:
        dirs.setdefault(o["metric"], set()).add(o["direction"])
    for metric, ds in sorted(dirs.items()):
        if len(ds) > 1:
            v.add(ERROR, "static", "contradictory_objectives",
                  f"{metric} is listed as both max and min",
                  f"keep one direction for {metric} and express the other side as a "
                  f"constraint", metric=metric)
    if objs:
        v.ran("static:objective_consistency")
    else:
        v.add(ERROR, "static", "no_objective",
              "no usable objective: there is nothing to optimize",
              "give at least one {'metric': ..., 'direction': 'max'|'min'}")

    # --- efficiency over unity
    metric, e = _find(lim, "efficiency")
    if metric and e.get("lo") is not None:
        lo = e["lo"]
        frac, scale = _as_fraction(lo)
        v.ran("static:efficiency_under_unity")
        if frac > 1.0:
            v.add(ERROR, "static", "over_unity_efficiency",
                  f"{metric} >= {_g(lo)} ({scale} scale) demands "
                  f"{frac * 100:.1f}% efficiency: more power out than in",
                  f"require {metric} <= 100% ({'0.99' if scale == 'fraction' else '99'} "
                  f"is already very hard)", metric=metric, as_fraction=frac)
        elif frac >= 1.0:
            v.add(ERROR, "static", "lossless_required",
                  f"{metric} >= {_g(lo)} demands exactly 100% efficiency: zero loss in "
                  f"every element", f"back {metric} off below 100%", metric=metric)
        elif frac > 0.95:
            v.add(WARN, "static", "extreme_efficiency",
                  f"{metric} >= {frac * 100:.1f}% is at the edge of the best published "
                  f"converters", f"relax {metric} toward 90-95% unless this is the "
                  f"whole point of the design", metric=metric)

    # --- power against the supply it must come from
    pm, pe = _find(lim, "power")
    vdd = tech.get("vdd") or tech.get("vin")
    iload = tech.get("iload")
    if pm and pe.get("hi") is not None and vdd and iload:
        floor = vdd * iload
        v.ran("static:power_vs_supply_current")
        v.details["theoretical_min_power"] = floor
        if pe["hi"] < floor:
            v.add(ERROR, "static", "power_below_load",
                  f"{pm} < {_g(pe['hi'])} W, but delivering {_g(iload)} A from "
                  f"{_g(vdd)} V already draws {_g(floor)} W before any loss",
                  f"raise the {pm} limit to >= {_g(floor * 1.1)} W or cut the load "
                  f"current below {_g(pe['hi'] / vdd)} A",
                  metric=pm, theoretical_min_power=floor)

    # --- param_space sanity (engine.start_run raises on some of these; report, don't die)
    kinds = _param_kinds(None, param_space) if param_space else {}
    for name, spec in sorted((param_space or {}).items()):
        if not isinstance(spec, dict):
            v.add(ERROR, "static", "bad_param_spec",
                  f"param {name!r} must be {{'min':..,'max':..}} or {{'values':[..]}}",
                  param=name)
            continue
        if "values" in spec:
            vals = list(spec["values"] or [])
            if not vals:
                v.add(ERROR, "static", "empty_values",
                      f"param {name!r} has an empty `values` list: nothing to try",
                      param=name)
            elif len(vals) == 1:
                v.add(WARN, "static", "frozen_param",
                      f"param {name!r} has one value, so it is a constant, not a knob",
                      f"drop {name} from param_space or give it more values", param=name)
        elif not {"min", "max"} <= set(spec):
            v.add(ERROR, "static", "bad_param_spec",
                  f"param {name!r} needs min+max or values", param=name)
            continue
        lo, hi = _span(spec)
        if lo is None:
            v.add(ERROR, "static", "bad_param_spec",
                  f"param {name!r} has non-numeric bounds", param=name)
            continue
        if lo > hi:
            v.add(ERROR, "static", "inverted_bounds",
                  f"param {name!r}: min {_g(lo)} exceeds max {_g(hi)}",
                  f"swap the bounds of {name}", param=name)
        elif lo == hi and "values" not in spec:
            v.add(WARN, "static", "frozen_param",
                  f"param {name!r}: min == max == {_g(lo)}, so nothing can be searched",
                  f"widen {name} or move it into the template as a literal", param=name)
        if name in kinds and lo <= 0:
            # The optimizer evaluates its own bounds, so a bound at 0 is not hypothetical:
            # it is a singular matrix or a divide-by-zero on some iteration.
            v.add(ERROR, "static", "nonpositive_component_bound",
                  f"param {name!r} is a {kinds[name].upper()} value whose range includes "
                  f"{_g(lo)}; the search will evaluate that bound",
                  f"keep {name} strictly positive", param=name)
    if param_space:
        v.ran("static:param_space")


# --------------------------------------------------- layer 2: template / measurement AST

def _template(v: FeasibilityVerdict, objs: list[dict], lim: dict,
              param_space: dict | None, scan: dict) -> None:
    space = set(param_space or {})

    # --- every scored metric needs a .meas that produces it
    wanted = {o["metric"] for o in objs} | set(lim)
    missing = sorted(m for m in wanted
                     if m not in scan["meas"]
                     and not (m.endswith("_deg") and m[:-4] in scan["meas"])
                     and not (m.startswith("param_")
                              and m[6:] in {p.lower() for p in space}))
    v.ran("template:metrics_have_meas")
    if missing:
        v.add(ERROR, "template", "metric_not_measured",
              f"no `.meas` directive produces {missing}; every candidate will come back "
              f"with those metrics absent, scoring None and failing every constraint",
              f"add `.meas` for {missing} (AC phase is available as <name>_deg without a "
              f"directive), or check the spelling against {sorted(scan['meas'])}",
              missing=missing, measured=sorted(scan["meas"]))

    # --- placeholders <-> param_space, both directions
    # `.param` names are matched case-folded because LTspice folds them, but param_space
    # keys are matched exactly because `ir.render` looks them up exactly -- a case slip
    # there really does leave the braces in the deck.
    unfilled = sorted(p for p in scan["placeholders"]
                      if p not in space and p.lower() not in scan["declared_params"])
    unused = sorted(space - scan["placeholders"])
    v.ran("template:placeholders_match_param_space")
    if unfilled:
        v.add(ERROR, "template", "placeholder_without_param",
              f"the template contains {['{%s}' % p for p in unfilled]} but param_space "
              f"and `.param` define neither; those braces reach LTspice literally",
              f"add {unfilled} to param_space or give them a `.param` default",
              placeholders=unfilled)
    if unused:
        v.add(ERROR, "template", "param_without_placeholder",
              f"param_space defines {unused}, which appear nowhere in the template as "
              f"{{{unused[0]}}}: the search would tune a knob wired to nothing",
              f"reference {unused} in the netlist as {{NAME}}, or remove them",
              params=unused)

    # --- gain above unity needs something that can provide it
    gm, ge = _find(lim, "gain")
    obj_gain = [o for o in objs if _is(o["metric"], "gain") and o["direction"] == "max"]
    unity = 0.0 if (gm and _db_scale(gm)) else 1.0
    demands_gain = bool(gm and ge.get("lo") is not None and ge["lo"] > unity)
    if demands_gain or obj_gain:
        v.ran("template:gain_needs_active_element")
        # An RC network is a contraction: |H| <= 1 everywhere, no exceptions. Add an
        # inductor, a coupling K or a switch and that stops being true -- a resonant tank
        # or a transformer steps voltage up with no active device at all -- so the hard
        # error is restricted to the case where the claim is actually a theorem.
        can_step_up = (scan["active"] or scan["switches"] or scan["n_inductors"]
                       or scan["has_coupling"])
        if demands_gain and not can_step_up:
            v.add(ERROR, "template", "gain_without_active",
                  f"{gm} > {_g(ge['lo'])} is required, but the deck holds only R and C: "
                  f"a passive RC network cannot exceed unity gain at any frequency",
                  "add an active element (E/G source, opamp subcircuit, transistor) or "
                  "drop the gain requirement to unity",
                  metric=gm, elements=[e["name"] for e in scan["elements"]])
        elif demands_gain and not scan["active"]:
            v.add(WARN, "template", "passive_step_up_only",
                  f"{gm} > {_g(ge['lo'])} with no active element: only a resonant or "
                  f"transformer step-up can do that, and it is narrowband and lossy",
                  "confirm the step-up is deliberate, or add an active stage", metric=gm)
        elif obj_gain and not can_step_up:
            v.add(WARN, "template", "passive_gain_objective",
                  f"maximising {obj_gain[0]['metric']} on an all-passive RC deck: the "
                  f"optimum is unity gain and the search will just find it", metric=gm)

    v.details["measured_metrics"] = sorted(scan["meas"])
    v.details["reactive_elements"] = scan["n_reactive"]
    v.details["max_order_from_topology"] = scan["n_reactive"]
    v.details["active_elements"] = scan["active"]


# ----------------------------------------------------- layer 3: first-principles physics

def _ellipk(k: float) -> float:
    """Complete elliptic integral of the first kind K(k), by AGM (A&S 17.6).

    AGM(1, sqrt(1-k^2)) = pi/(2K), and the iteration converges quadratically -- six
    passes is machine precision for every k a filter spec produces.
    """
    a, b = 1.0, math.sqrt(max(0.0, 1.0 - k * k))
    for _ in range(60):
        if abs(a - b) <= 1e-16 * a:
            break
        a, b = 0.5 * (a + b), math.sqrt(a * b)
    return math.pi / (a + b)


def min_filter_order(kind: str, ripple_db: float, atten_db: float,
                     f_pass: float, f_stop: float) -> float:
    """Exact minimum order for a lowpass approximation. Fractional -- ceil it to order.

    All three use the same two selectivity numbers: the discrimination
    D = (10^(As/10)-1)/(10^(Ap/10)-1) and the frequency ratio Ws = f_stop/f_pass.
    Verified against the textbook 1 dB / 40 dB / 2:1 case, which needs
    Butterworth 8, Chebyshev 5, elliptic 4.
    """
    ws = f_stop / f_pass
    d = (10.0 ** (atten_db / 10.0) - 1.0) / (10.0 ** (ripple_db / 10.0) - 1.0)
    if kind.startswith(("butter", "maxflat")):
        return math.log10(d) / (2.0 * math.log10(ws))
    if kind.startswith(("cheb", "type1", "type i")):
        return math.acosh(math.sqrt(d)) / math.acosh(ws)
    if kind.startswith(("ellip", "cauer", "zolo")):
        k, k1 = 1.0 / ws, 1.0 / math.sqrt(d)
        return (_ellipk(k) * _ellipk(math.sqrt(1 - k1 * k1))) / (
            _ellipk(k1) * _ellipk(math.sqrt(1 - k * k)))
    raise FeasibilityError(
        f"unknown filter_kind {kind!r}; use butterworth | chebyshev | elliptic")


def _filter_physics(v: FeasibilityVerdict, lim: dict, tech: dict, scan: dict | None,
                    mode: str) -> None:
    need = ("f_pass", "f_stop", "ripple_db", "atten_db")
    have = [k for k in need if tech.get(k) is not None]
    if len(have) < 4:
        v.skip(f"physics:filter_order needs technology_params "
               f"{[k for k in need if k not in have]} (Hz, Hz, dB, dB)")
        return
    fp, fs = tech["f_pass"], tech["f_stop"]
    ap, ats = tech["ripple_db"], tech["atten_db"]
    if fs <= fp:
        v.add(ERROR, "physics", "transition_band_inverted",
              f"f_stop {_g(fs)} Hz is not above f_pass {_g(fp)} Hz: there is no "
              f"transition band to roll off in",
              "for a lowpass, f_stop > f_pass (swap them for a highpass spec)")
        return
    if ats <= ap:
        v.add(ERROR, "physics", "no_attenuation_asked",
              f"stopband attenuation {_g(ats)} dB does not exceed passband ripple "
              f"{_g(ap)} dB, so the 'stopband' is inside the passband",
              "set atten_db well above ripple_db (40 dB / 1 dB is typical)")
        return

    kind = tech.get("filter_kind") or "butterworth"
    orders = {}
    for k in ("butterworth", "chebyshev", "elliptic"):
        try:
            orders[k] = math.ceil(min_filter_order(k, ap, ats, fp, fs) - 1e-12)
        except (ValueError, ZeroDivisionError):        # pathological ratios
            continue
    try:
        exact = min_filter_order(kind, ap, ats, fp, fs)
    except FeasibilityError as e:
        v.add(ERROR, "physics", "unknown_filter_kind", str(e),
              "use filter_kind = butterworth | chebyshev | elliptic")
        return
    n_need = math.ceil(exact - 1e-12)
    v.ran("physics:filter_order")
    v.details.update(filter_kind=kind, min_filter_order_needed=n_need,
                     min_filter_order_exact=round(exact, 3),
                     min_order_by_family=orders,
                     transition_ratio=round(fs / fp, 4))

    if scan is not None:
        available = scan["n_reactive"]
        if available < n_need:
            cheapest = min(orders.items(), key=lambda kv: kv[1]) if orders else None
            v.add(ERROR, "physics", "order_below_requirement",
                  f"a {kind} lowpass meeting {_g(ap)} dB ripple and {_g(ats)} dB at "
                  f"{_g(fs)}/{_g(fp)} Hz needs order {n_need}, but the template has "
                  f"{available} reactive element(s), so it can realise at most order "
                  f"{available}",
                  f"add {n_need - available} more C/L element(s)" + (
                      f", or switch to {cheapest[0]} (order {cheapest[1]})"
                      if cheapest and cheapest[1] <= available else
                      f", or relax the spec (order scales with 1/log(f_stop/f_pass))"),
                  order_needed=n_need, order_available=available)
        else:
            v.ran("physics:template_order_sufficient")
    else:
        v.skip("physics:template_order_sufficient needs a netlist_template")

    if mode == "practical":
        if n_need > 8:
            v.add(WARN, "physics", "high_order_filter",
                  f"order {n_need} means {n_need} poles to place; component sensitivity "
                  f"and tuning effort both grow fast past order 8",
                  "widen the transition band or accept less stopband attenuation",
                  order_needed=n_need)
        if kind.startswith("cheb") and ap > 3.0:
            v.add(WARN, "physics", "large_passband_ripple",
                  f"{_g(ap)} dB of Chebyshev ripple is audible/visible in most "
                  f"applications", "keep ripple_db at or below ~1 dB")
        if orders.get("elliptic") and orders["elliptic"] < orders.get("butterworth", 0) - 2 \
                and not kind.startswith("ellip"):
            v.add(WARN, "physics", "cheaper_family_available",
                  f"an elliptic response meets the same mask at order "
                  f"{orders['elliptic']} versus {orders.get(kind)} for {kind}",
                  "consider filter_kind='elliptic' if passband ripple and stopband "
                  "zeros are acceptable")


def _opamp_physics(v: FeasibilityVerdict, lim: dict, tech: dict, mode: str) -> None:
    cl, vdd = tech.get("cload"), tech.get("vdd") or tech.get("vin")
    tk = T_ZERO + tech.get("temp_c", 27.0)
    vt = KB * tk / Q_E
    itot, ft, vov = tech.get("itotal"), tech.get("ft"), tech.get("vov", 0.2)
    pm_, pe = _find(lim, "power")
    p_cap = pe.get("hi")
    floors = {}

    # --- slew rate: I = C dV/dt is a definition, not a design choice
    sm, se = _find(lim, "slew_rate")
    sr = se.get("lo")
    if sr and cl:
        i_slew = sr * cl
        v.ran("physics:slew_rate_vs_current")
        v.details["slew_current_needed_a"] = i_slew
        if itot:
            v.details["theoretical_max_slew_rate"] = itot / cl
            if sr > itot / cl:
                v.add(ERROR, "physics", "slew_rate_above_current_limit",
                      f"{sm} >= {_g(sr)} V/s ({_g(sr / 1e6)} V/us) into {_g(cl)} F needs "
                      f"{_g(i_slew)} A, above the {_g(itot)} A budget",
                      f"raise the current budget to >= {_g(i_slew)} A, cut C_load below "
                      f"{_g(itot / sr)} F, or accept {_g(itot / cl / 1e6)} V/us",
                      metric=sm, current_needed=i_slew, max_slew_rate=itot / cl)
        if vdd:
            floors["slew"] = vdd * i_slew
    elif sr and not cl:
        v.skip("physics:slew_rate_vs_current needs technology_params cload (F)")

    # --- gain-bandwidth: gm/Id <= 1/(n*Vt) bounds every MOSFET ever fabricated
    gm_, ge = _find(lim, "gbw")
    gbw = ge.get("lo")
    if gbw and cl:
        gm_need = 2 * math.pi * gbw * cl
        i_floor = gm_need * vt                     # n=1: the loosest honest bound
        i_practical = gm_need * vov / 2.0          # strong inversion, gm = 2*Id/Vov
        v.ran("physics:gbw_vs_transconductance")
        v.details.update(gm_needed_s=gm_need, min_bias_current_a=i_floor,
                         practical_bias_current_a=i_practical,
                         gm_bound="gm/Id <= 1/Vt (weak inversion, n=1)")
        if itot:
            v.details["theoretical_max_gbw"] = itot / (vt * 2 * math.pi * cl)
            if gbw > v.details["theoretical_max_gbw"]:
                v.add(ERROR, "physics", "gbw_above_transconductance_limit",
                      f"{gm_} >= {_g(gbw)} Hz into {_g(cl)} F needs gm >= "
                      f"{_g(gm_need)} S; {_g(itot)} A yields at most "
                      f"{_g(itot / vt)} S even at the weak-inversion limit",
                      f"raise the current budget to >= {_g(i_floor)} A (a real design "
                      f"needs nearer {_g(i_practical)} A), or drop GBW below "
                      f"{_g(v.details['theoretical_max_gbw'])} Hz",
                      metric=gm_, gm_needed=gm_need)
            elif mode == "practical" and itot < i_practical:
                v.add(WARN, "physics", "gbw_needs_weak_inversion",
                      f"{_g(itot)} A reaches {_g(gbw)} Hz only at gm/Id "
                      f"{_g(gm_need / itot)} 1/V, i.e. deep weak inversion -- slow "
                      f"devices, big area, poor matching",
                      f"budget nearer {_g(i_practical)} A for Vov={_g(vov)} V")
        if vdd:
            floors["gbw"] = vdd * i_floor
        if ft:
            v.ran("physics:gbw_vs_device_ft")
            if gbw > ft:
                v.add(ERROR, "physics", "gbw_above_device_ft",
                      f"{gm_} >= {_g(gbw)} Hz exceeds the device fT {_g(ft)} Hz: no "
                      f"transistor in this process still has current gain there",
                      f"pick a faster process or keep GBW below {_g(ft / 10)} Hz",
                      metric=gm_, ft=ft)
            elif mode == "practical" and gbw > ft / 10:
                v.add(WARN, "physics", "gbw_close_to_ft",
                      f"{_g(gbw)} Hz is within a decade of fT {_g(ft)} Hz; parasitic "
                      f"poles land inside the loop and phase margin collapses",
                      f"aim for GBW <= fT/10 = {_g(ft / 10)} Hz")
    elif gbw and not cl:
        v.skip("physics:gbw_vs_transconductance needs technology_params cload (F)")

    # --- the power floor implied by the two above
    if floors and vdd:
        floor = max(floors.values())
        v.ran("physics:power_floor")
        v.details["theoretical_min_power"] = max(
            floor, v.details.get("theoretical_min_power", 0.0))
        v.details["power_floor_from"] = max(floors, key=floors.get)
        if p_cap is not None and p_cap < floor:
            driver = max(floors, key=floors.get)
            v.add(ERROR, "physics", "power_below_dynamic_floor",
                  f"{pm_} < {_g(p_cap)} W, but the {driver} requirement alone draws "
                  f"{_g(floor / vdd)} A from {_g(vdd)} V = {_g(floor)} W, before bias, "
                  f"output stage or reference current",
                  f"raise the {pm_} limit above {_g(floor)} W, drop C_load, or relax the "
                  f"{driver} requirement", metric=pm_, floor_w=floor, driver=driver)

    # --- noise: 4kTR is the floor of any resistor, and kT/C of any sampled node
    nm, ne = _find(lim, "noise")
    n_cap = ne.get("hi")
    rmin = tech.get("rmin")
    if n_cap is not None and rmin:
        en = math.sqrt(4 * KB * tk * rmin)
        v.ran("physics:thermal_noise_floor")
        v.details["thermal_noise_floor_v_per_rthz"] = en
        if n_cap < en:
            v.add(ERROR, "physics", "noise_below_thermal_floor",
                  f"{nm} < {_g(n_cap)} V/sqrt(Hz), but {_g(rmin)} ohm at {_g(tk - T_ZERO)} "
                  f"C already generates {_g(en)} V/sqrt(Hz) = {_g(en * 1e9)} nV/sqrt(Hz)",
                  f"drop the source/feedback resistance below "
                  f"{_g(n_cap ** 2 / (4 * KB * tk))} ohm, or relax {nm} above {_g(en)}",
                  metric=nm, floor=en)
        elif mode == "practical" and n_cap < 2 * en:
            v.add(WARN, "physics", "noise_within_2x_of_floor",
                  f"{nm} leaves only {_g(n_cap / en)}x over the {_g(rmin)} ohm thermal "
                  f"floor, so the active devices must contribute almost nothing",
                  "budget the resistor at <= 1/3 of the noise target")
    elif n_cap is not None and not rmin:
        v.skip("physics:thermal_noise_floor needs technology_params rmin (ohm, the "
               "dominant source/feedback resistance)")
    if cl:
        v.details["kt_over_c_noise_vrms"] = math.sqrt(KB * tk / cl)
        if n_cap is not None and (tech.get("sampled") or "rms" in _tokens(nm or "")):
            ktc = math.sqrt(KB * tk / cl)
            v.ran("physics:kt_over_c_noise")
            if n_cap < ktc:
                v.add(ERROR, "physics", "noise_below_kt_over_c",
                      f"{nm} < {_g(n_cap)} Vrms, but sampling onto {_g(cl)} F leaves "
                      f"sqrt(kT/C) = {_g(ktc)} Vrms whatever the circuit",
                      f"grow the sampling capacitor to >= {_g(KB * tk / n_cap ** 2)} F",
                      metric=nm, floor=ktc)

    if not (cl or vdd):
        v.skip("physics:opamp bounds need technology_params vdd (V) and cload (F)")


def _ldo_physics(v: FeasibilityVerdict, lim: dict, tech: dict, mode: str) -> None:
    vin, vout, il = tech.get("vin") or tech.get("vdd"), tech.get("vout"), tech.get("iload")
    if not (vin and vout):
        v.skip("physics:ldo needs technology_params vin (V) and vout (V)")
        return
    v.ran("physics:ldo_headroom")
    if vout > vin:
        v.add(ERROR, "physics", "linear_cannot_step_up",
              f"Vout {_g(vout)} V above Vin {_g(vin)} V: a series pass element can only "
              f"drop voltage",
              "use a boost or charge-pump topology (circuit_type='dcdc')",
              vin=vin, vout=vout)
        return
    eta_max = vout / vin
    v.details["max_linear_efficiency"] = eta_max
    em, ee = _find(lim, "efficiency")
    if em and ee.get("lo") is not None:
        req, _scale = _as_fraction(ee["lo"])
        v.ran("physics:ldo_efficiency_ceiling")
        if req > eta_max:
            v.add(ERROR, "physics", "efficiency_above_linear_ceiling",
                  f"a linear regulator's efficiency is Vout/Vin = {eta_max * 100:.1f}% at "
                  f"best (zero quiescent current); {em} >= {_g(ee['lo'])} is above it",
                  f"drop Vin to <= {_g(vout / req)} V or switch to a buck converter",
                  metric=em, ceiling=eta_max)

    if il:
        p_floor = (vin - vout) * il
        v.details["theoretical_min_power"] = max(
            p_floor, v.details.get("theoretical_min_power", 0.0))
        v.details["pass_device_dissipation_w"] = p_floor
        pm_, pe = _find(lim, "power")
        v.ran("physics:ldo_dissipation")
        if pe.get("hi") is not None and pe["hi"] < p_floor:
            v.add(ERROR, "physics", "power_below_pass_dissipation",
                  f"the pass device must burn (Vin-Vout)*Iload = "
                  f"{_g(vin - vout)} V * {_g(il)} A = {_g(p_floor)} W; {pm_} < "
                  f"{_g(pe['hi'])} W cannot hold",
                  f"drop Vin to <= {_g(vout + pe['hi'] / il)} V, cut Iload, or raise the "
                  f"{pm_} limit above {_g(p_floor)} W", metric=pm_, floor=p_floor)
        if mode == "practical" and p_floor > 1.0:
            v.add(WARN, "physics", "thermal_dissipation",
                  f"{_g(p_floor)} W in one pass device needs real heatsinking (~"
                  f"{_g(p_floor * 50)} C rise at a bare-package 50 C/W)",
                  "check the junction temperature, or pre-regulate with a switcher")

    dm, de = _find(lim, "dropout")
    rds = tech.get("rds_on")
    if rds and il:
        vdo_min = il * rds
        v.details["min_dropout_v"] = vdo_min
        v.ran("physics:ldo_dropout")
        if de.get("hi") is not None and de["hi"] < vdo_min:
            v.add(ERROR, "physics", "dropout_below_rdson_limit",
                  f"{dm} < {_g(de['hi'])} V, but {_g(il)} A through the smallest pass "
                  f"device ({_g(rds)} ohm) drops {_g(vdo_min)} V",
                  f"widen the pass device to <= {_g(de['hi'] / il)} ohm, or accept "
                  f"{_g(vdo_min)} V of dropout", metric=dm, floor=vdo_min)
        if vin - vout < vdo_min:
            v.add(ERROR, "physics", "headroom_below_dropout",
                  f"available headroom Vin-Vout = {_g(vin - vout)} V is below the "
                  f"{_g(vdo_min)} V the pass device drops at {_g(il)} A: it leaves "
                  f"saturation and stops regulating",
                  f"raise Vin above {_g(vout + vdo_min)} V or widen the pass device",
                  headroom=vin - vout, dropout=vdo_min)
    elif de.get("hi") is not None:
        v.skip("physics:ldo_dropout needs technology_params rds_on (ohm) and iload (A)")


def _dcdc_physics(v: FeasibilityVerdict, lim: dict, tech: dict, scan: dict | None,
                  mode: str) -> None:
    vin, vout = tech.get("vin") or tech.get("vdd"), tech.get("vout")
    if not (vin and vout):
        v.skip("physics:dcdc needs technology_params vin (V) and vout (V)")
        return
    v.ran("physics:dcdc_topology")
    if vout > vin:
        topo, duty = "boost", 1.0 - vin / vout
    elif vout < vin:
        topo, duty = "buck", vout / vin
    else:
        topo, duty = "pass-through", 1.0
        v.add(WARN, "physics", "unity_conversion",
              f"Vin == Vout == {_g(vin)} V: a switcher has nothing to convert",
              "use an LDO or a straight connection")
    v.details.update(topology_required=topo, ideal_duty_cycle=round(duty, 4),
                     conversion_ratio=round(vout / vin, 4))

    if scan is not None:
        # Energy has to be stored somewhere to raise a voltage. No inductor, no coupled
        # winding and no switch means the deck cannot do it at any duty cycle.
        storage = scan["n_inductors"] or scan["has_coupling"]
        if topo == "boost" and not storage:
            v.add(ERROR, "physics", "step_up_without_storage",
                  f"Vout {_g(vout)} V above Vin {_g(vin)} V needs an inductor (boost) or "
                  f"a pumped capacitor network; the template has neither",
                  "add the inductor and switch, or use a charge pump topology",
                  inductors=scan["n_inductors"])
        if not scan["switches"] and not scan["active"]:
            v.add(ERROR, "physics", "converter_without_switch",
                  "a switching converter needs a switch (S/W) or an active device; this "
                  "deck has only passives and sources",
                  "add the switching element, or model it with a behavioural source")
        v.ran("physics:dcdc_template_elements")
    else:
        v.skip("physics:dcdc_template_elements needs a netlist_template")

    dmax = tech.get("duty_max", 0.9)
    dm, de = _find(lim, "duty")
    if de.get("hi") is not None and duty > de["hi"]:
        v.add(ERROR, "physics", "duty_above_limit",
              f"a {topo} from {_g(vin)} V to {_g(vout)} V runs at D = {duty:.3f} even "
              f"with no loss; {dm} < {_g(de['hi'])} cannot be met",
              "cascade two stages or use a transformer-coupled topology", duty=duty)
    elif mode == "practical" and duty > dmax:
        v.add(WARN, "physics", "extreme_duty_cycle",
              f"D = {duty:.3f} exceeds the {dmax:g} a real controller holds (minimum "
              f"off-time, gate-drive refresh)",
              "cascade stages, or use a flyback/transformer ratio", duty=duty)

    em, ee = _find(lim, "efficiency")
    if mode == "practical" and em and ee.get("lo") is not None:
        req, _scale = _as_fraction(ee["lo"])
        if req > 0.9 and (duty > 0.8 or duty < 0.1):
            v.add(WARN, "physics", "efficiency_at_extreme_duty",
                  f"{req * 100:.1f}% efficiency at D = {duty:.3f} is hard: conduction "
                  f"loss scales with the RMS current the extreme ratio forces",
                  "relax efficiency or move the conversion ratio toward 1:2")


# --------------------------------------------------------- practical engineering smells

def _practical(v: FeasibilityVerdict, lim: dict, param_space: dict | None,
               scan: dict | None, tech: dict) -> None:
    # --- component values a fab or a supplier will not give you
    kinds = _param_kinds(scan, param_space)
    for name, letter in sorted(kinds.items()):
        lo, hi = _span((param_space or {})[name])
        if lo is None:
            continue
        w_lo, w_hi, unit = PASSIVE_WINDOW[letter]
        if hi > w_hi:
            v.add(WARN, "practical", "component_value_high",
                  f"param {name!r} reaches {_g(hi)} {unit}, past the practical "
                  f"{_g(w_hi)} {unit} ceiling (leakage, tolerance, area)",
                  f"cap {name} near {_g(w_hi)} {unit} and rescale the impedance level",
                  param=name)
        if 0 < lo < w_lo:
            v.add(WARN, "practical", "component_value_low",
                  f"param {name!r} goes down to {_g(lo)} {unit}, below the practical "
                  f"{_g(w_lo)} {unit} floor (parasitics dominate)",
                  f"raise the lower bound of {name} to ~{_g(w_lo)} {unit}", param=name)
        if lo > 0 and hi / lo > 1e8:
            v.add(WARN, "practical", "param_span_extreme",
                  f"param {name!r} spans {math.log10(hi / lo):.1f} decades; a local "
                  f"search wastes most of its budget crossing that",
                  f"narrow {name} once a decade is known, or give it a `values` list",
                  param=name)

    for e in (scan or {}).get("elements", []):
        if e["letter"] not in PASSIVE_WINDOW or not e["value"]:
            continue
        val = ir.parse_value(e["value"])
        if val is None:
            continue
        w_lo, w_hi, unit = PASSIVE_WINDOW[e["letter"]]
        if val <= 0:
            v.add(WARN, "practical", "nonpositive_component",
                  f"{e['name']} is {_g(val)} {unit}; LTspice accepts a negative passive "
                  f"as a negative-impedance element but it often will not converge",
                  f"check {e['name']}", element=e["name"])
        elif val > w_hi or val < w_lo:
            v.add(WARN, "practical", "component_value_extreme",
                  f"{e['name']} = {_g(val)} {unit} is outside the practical "
                  f"{_g(w_lo)}..{_g(w_hi)} {unit} window",
                  f"rescale the impedance level around {e['name']}", element=e["name"])
    if kinds or (scan or {}).get("elements"):
        v.ran("practical:component_values")

    # --- stability margin
    pm, pe = _find(lim, "phase_margin")
    if pm:
        v.ran("practical:phase_margin")
        if pe.get("hi") is not None and pe["hi"] <= 0:
            v.add(ERROR, "practical", "negative_phase_margin_required",
                  f"{pm} < {_g(pe['hi'])} deg asks for an unstable loop",
                  f"require {pm} > 45 deg instead", metric=pm)
        lo = pe.get("lo")
        if lo is not None and 0 < lo < 45:
            hurt = ("ringing and long settling" if lo >= 30
                    else "PVT spread alone can take this loop unstable")
            v.add(WARN, "practical", "low_phase_margin",
                  f"{pm} >= {_g(lo)} deg leaves little margin: {hurt}",
                  f"target {pm} >= 60 deg (45 deg is the usual floor)", metric=pm)

    # --- the unit trap that silently invalidates every slew-rate check
    sm, se = _find(lim, "slew_rate")
    if sm and se.get("lo") is not None and 0 < se["lo"] < 1e4:
        v.add(WARN, "practical", "slew_rate_unit_suspect",
              f"{sm} >= {_g(se['lo'])} reads as V/s here (SI); if you meant "
              f"{_g(se['lo'])} V/us the requirement is {_g(se['lo'] * 1e6)} V/s and this "
              f"check has been passing on the wrong number",
              f"state {sm} in V/s", metric=sm)

    # --- device geometry, when the numbers are there to check it
    lmin = tech.get("lmin")
    wp = [n for n in (param_space or {}) if _tokens(n) & {"w", "width"}]
    lp = [n for n in (param_space or {}) if _tokens(n) & {"l", "length"}]
    if lmin and lp:
        for n in lp:
            lo, _hi = _span(param_space[n])
            if lo is not None and lo < lmin:
                v.add(ERROR, "practical", "below_min_channel_length",
                      f"param {n!r} goes to {_g(lo)} m, below the {_g(lmin)} m process "
                      f"minimum", f"raise the lower bound of {n} to {_g(lmin)} m",
                      param=n)
        v.ran("practical:min_channel_length")
    if wp and lp:
        w_hi = max((_span(param_space[n])[1] or 0) for n in wp)
        l_lo = min((_span(param_space[n])[0] or math.inf) for n in lp)
        if l_lo and w_hi / l_lo > 1000:
            v.add(WARN, "practical", "extreme_aspect_ratio",
                  f"W/L reaches {w_hi / l_lo:.0f}:1, which is a fingered array rather "
                  f"than a device; matching and layout parasitics stop being an "
                  f"afterthought", "split it into m fingers and model the routing")
        v.ran("practical:aspect_ratio")


# ------------------------------------------------------------------------------ entry

def check(objectives: list, constraints=None, param_space: dict | None = None,
          netlist_template: str | None = None, circuit_type: str = "general",
          mode: str = "practical",
          technology_params: dict | None = None) -> FeasibilityVerdict:
    """Run every applicable layer and return one verdict. No simulation, no state.

    `mode`:
      practical   -- physics plus engineering guidelines (warnings)
      theoretical -- hard physical/mathematical limits only
      concept     -- no netlist: spec-vs-physics only, for checking an idea before a
                     deck exists
    """
    if mode not in MODES:
        raise FeasibilityError(f"mode must be one of {MODES}, got {mode!r}")
    if circuit_type not in CIRCUIT_TYPES:
        raise FeasibilityError(
            f"circuit_type must be one of {CIRCUIT_TYPES}, got {circuit_type!r}")

    v = FeasibilityVerdict(mode=mode, circuit_type=circuit_type)

    # Reuse the engine's own normalizers so "what the checker read" and "what the run
    # will use" cannot diverge -- including the dict shorthand and the two-sided list.
    try:
        cons = engine._norm_constraints(constraints)
    except (engine.EngineError, KeyError, TypeError, ValueError) as e:
        v.add(ERROR, "static", "unparseable_constraint", f"constraints rejected: {e}",
              "use {'metric': '>1e5'} or [{'metric':..,'op':'>','value':..}]")
        cons = []
    try:
        objs = engine._norm_objectives(objectives, cons)
    except (engine.EngineError, KeyError, TypeError, ValueError) as e:
        v.add(ERROR, "static", "unparseable_objective", f"objectives rejected: {e}",
              "use [{'metric': 'gain_db', 'direction': 'max'}]")
        objs = []

    tech, unknown = _tech_params(technology_params)
    if unknown:
        v.add(WARN, "static", "unknown_technology_params",
              f"ignored technology_params keys {unknown}; the checks that need them did "
              f"not run", f"expected keys: {sorted(_TECH)}", keys=unknown)
    lim = _limits(cons)

    _static(v, objs, lim, param_space, tech)

    scan = None
    if mode == "concept":
        if netlist_template:
            v.skip("template: mode='concept' ignores netlist_template")
    elif netlist_template:
        scan = scan_netlist(netlist_template)
        _template(v, objs, lim, param_space, scan)
    else:
        v.skip("template: no netlist_template given (pass one, or use mode='concept')")

    if circuit_type == "filter":
        _filter_physics(v, lim, tech, scan, mode)
    elif circuit_type == "opamp":
        _opamp_physics(v, lim, tech, mode)
    elif circuit_type == "ldo":
        _ldo_physics(v, lim, tech, mode)
    elif circuit_type == "dcdc":
        _dcdc_physics(v, lim, tech, scan, mode)

    if mode == "practical":
        _practical(v, lim, param_space, scan, tech)
    else:
        v.skip("practical: engineering guidelines are only checked in mode='practical'")

    v.details["units"] = {k: _TECH_UNITS[k] for k in sorted(tech)}
    v.details["technology_params_used"] = {k: tech[k] for k in sorted(tech)}
    v.details["constraints_read"] = [f"{c['metric']}{c['op']}{c['value']:g}" for c in cons]
    return v


if __name__ == "__main__":                              # smallest useful self-check
    # The textbook 1 dB / 40 dB / 2:1 lowpass: Butterworth 8, Chebyshev 5, elliptic 4.
    orders = {k: math.ceil(min_filter_order(k, 1.0, 40.0, 1e3, 2e3))
              for k in ("butterworth", "chebyshev", "elliptic")}
    assert orders == {"butterworth": 8, "chebyshev": 5, "elliptic": 4}, orders
    assert abs(_ellipk(0.0) - math.pi / 2) < 1e-12
    assert abs(_ellipk(0.5) - 1.6857503548) < 1e-9

    # A spec that contradicts itself, with no netlist and no physics.
    v = check([{"metric": "fc", "direction": "max"}],
              [{"metric": "fc", "op": ">", "value": 10e3},
               {"metric": "fc", "op": "<", "value": 9e3}], mode="concept")
    assert v.status == "infeasible" and not v.passed
    assert v.errors[0].code == "empty_window", v.to_dict()

    # 100 V/us into 10 pF is 1 mA; 0.5 mW at 1.8 V is 0.28 mA.
    v = check([{"metric": "slew_rate", "direction": "max"}],
              {"slew_rate": ">1e8", "power": "<0.0005"}, circuit_type="opamp",
              mode="concept", technology_params={"vdd": 1.8, "cload": 10e-12})
    assert [i.code for i in v.errors] == ["power_below_dynamic_floor"], v.to_dict()
    assert v.details["theoretical_min_power"] > 1.7e-3

    # A metric nobody measures is the expensive mistake, so it is an error.
    v = check([{"metric": "gain_db", "direction": "max"}], {"bandwidth": ">1e5"},
              param_space={"R1": {"min": 1e3, "max": 1e5}},
              netlist_template="V1 in 0 AC 1\nR1 in out {R1}\nC1 out 0 1n\n"
                               ".meas AC gain_db MAX mag(V(out))\n")
    codes = {i.code for i in v.errors}
    assert codes == {"metric_not_measured"}, v.to_dict()
    print("ok", v.status)
