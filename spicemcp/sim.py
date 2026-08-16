"""Simulation backends.

ponytail: metrics come from LTspice `.meas` directives, read back as text from the
`.log`. That replaces a binary `.raw` waveform parser + metric extractors with one
regex, and it is also the right interface for an optimizer -- the engineering
metric definitions live in the netlist, versioned alongside the circuit.
  skipped: .raw waveform reading. Add when a metric can't be expressed as .meas
  (e.g. custom FFT post-processing).

Two backends:
  ltspice  -- subprocess, real simulation
  analytic -- a registered Python response surface; deterministic, no LTspice
              needed. Used by the test suite and for dry-running an objective.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

# --- failure taxonomy (spec S9) -------------------------------------------------

SIMULATION_FAILURE = "simulation_failure"
CONVERGENCE_FAILURE = "convergence_failure"
STRUCTURAL_ERROR = "structural_error"
CONSTRAINT_VIOLATION = "constraint_violation"
COMPONENT_LIMIT = "component_limit"
NUMERICAL_INSTABILITY = "numerical_instability"

_LOG_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"iteration limit|timestep too small|failed to converge|gmin stepping",
                re.I), CONVERGENCE_FAILURE),
    (re.compile(r"singular matrix|matrix is singular|no dc path", re.I),
     NUMERICAL_INSTABILITY),
    (re.compile(r"unknown subcircuit|could not open|unknown device|"
                r"fewer than two connections|undefined symbol|missing model", re.I),
     STRUCTURAL_ERROR),
    (re.compile(r"fatal error|analysis: fatal", re.I), SIMULATION_FAILURE),
]

_NODE_HINT = re.compile(r"\bnode\s+([A-Za-z_$][\w$]*|\d+)|\bv\(([\w$]+)\)", re.I)

LTSPICE_CANDIDATES = [
    r"C:\Users\{user}\AppData\Local\Programs\ADI\LTspice\LTspice.exe",
    r"C:\Program Files\ADI\LTspice\LTspice.exe",
    r"C:\Program Files\LTC\LTspiceXVII\XVIIx64.exe",
    r"C:\Program Files (x86)\LTC\LTspiceIV\scad3.exe",
]


@dataclass
class SimResult:
    status: str                                  # "ok" | "failed"
    metrics: dict[str, float] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    failure_type: str | None = None
    failure_message: str | None = None
    failure_detail: dict | None = None
    log_path: str | None = None
    raw_path: str | None = None


# --- .meas log parsing ---------------------------------------------------------

_FLOAT = r"[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?"
_MEAS_LINE = re.compile(r"^\s*(\w+)\s*[:=]\s*(.*)$")
# AC results come back as a complex pair: "=(31.69dB,-159.41<degree>)"
_COMPLEX = re.compile(rf"=\s*\(\s*({_FLOAT})\s*[^,]*,\s*({_FLOAT})")
_AT = re.compile(rf"\bAT\b\s*({_FLOAT})")          # uppercase only -- see parse_meas
_EQ = re.compile(rf"=\s*({_FLOAT})")
_BARE = re.compile(rf"^({_FLOAT})\s*$")

# Log lines that look like "name: value" but are settings, not measurements.
# `options` earns its place the hard way: LTspice echoes `Options:plotwinsize=0`, which
# parsed as a metric named `options` worth 0.0 -- and `.options plotwinsize=0` is in every
# netlist meant for waveform measurement, so this phantom rode along on most candidates.
_NOT_METRICS = {"date", "total", "tnom", "temp", "method", "step", "solver",
                "circuit", "compress1storder", "compress2ndorder", "maximum",
                "start", "direct", "matrix", "warning", "note", "error",
                "options", "files"}


def parse_meas(log_text: str) -> tuple[dict[str, float], list[str]]:
    """Extract `.meas` results from an LTspice batch log.

    Verified against LTspice 26.0.2 output. Four real shapes, and which number you
    want differs in each -- getting this wrong returns a plausible wrong value
    rather than an error, so the order of these checks matters:

      gdb:  MAX(db(V(out)))=(31.69dB,-159.41deg) FROM 1 TO 1e7 -> 31.69   (+ _deg)
      bw:   mag(V(out))=0.7071  AT 159158.003411              -> 159158  (crossing)
      p050: V(out) =0.140915 at 6.666666667e-05               -> 0.140915 (value)
      pwr:  AVG(-I(V1)*V(vcc))=-5.33e-19 FROM 0 TO 0.005      -> -5.33e-19

    The parenthesised form must be checked FIRST: `.meas AC g FIND db(V(out)) AT 1k`
    prints both a complex result and a trailing "at 1000", and the 1000 is the
    frequency you asked for, not the measurement.

    Rows 2 and 3 are the trap: identical shape, opposite answers. In a `WHEN` measure
    the number after `=` is the trigger level *you wrote* and `AT` carries the result;
    in a `FIND ... AT` measure it is the reverse. LTspice distinguishes them only by
    case -- it prints uppercase `AT` for a point it found and lowercase `at` for the
    point you specified. Verified both ways on 26.0.2, hence the case-sensitive `_AT`.
      ponytail: case is the only signal in the log. If a future version lower-cases
      the WHEN form, that measure silently returns its trigger level; the fix then is
      to pass the directive text in alongside the log.

    Caveat worth knowing: LTspice reports *every* AC measurement's magnitude in dB,
    even `MAX(mag(V(out)))`. The second component (phase, degrees) is exposed as
    `<name>_deg` so phase margin is reachable.

    ponytail: no .raw parsing. skipped: linear-magnitude AC results -- LTspice
    doesn't offer them; convert in the netlist if you need them.
    """
    metrics: dict[str, float] = {}
    warnings: list[str] = []
    for line in log_text.splitlines():
        low = line.lower()
        if "measurement" in low and "fail" in low:
            warnings.append(line.strip())
            continue
        if low.lstrip().startswith(("warning:", "* warning", "error:")):
            warnings.append(line.strip())
            continue
        m = _MEAS_LINE.match(line)
        if not m:
            continue
        name, rest = m.group(1).lower(), m.group(2)
        if name in _NOT_METRICS:
            continue

        cx = _COMPLEX.search(rest)
        if cx:
            metrics[name] = float(cx.group(1))
            metrics[f"{name}_deg"] = float(cx.group(2))
            continue
        at = _AT.search(rest)
        if at:
            metrics[name] = float(at.group(1))
            continue
        eqs = _EQ.findall(rest)
        if eqs:
            metrics[name] = float(eqs[-1])
            continue
        bare = _BARE.match(rest.strip())
        if bare:
            metrics[name] = float(bare.group(1))
    if not metrics:
        stepped = parse_meas_steps(log_text)
        if stepped:
            warnings.append(
                f"log holds a stepped .meas table ({', '.join(sorted(stepped))}), not "
                f"scalar results: {len(next(iter(stepped.values())))} runs. A scored "
                f"candidate needs one value -- drop the .step, or read the sweep with "
                f"parse_meas_steps()."
            )
    return metrics, warnings


# A `.step`ped run reports each measure as a table instead of `name: value`:
#
#   Measurement: fc
#     step	mag(V(out))=0.7071
#        1	1603.35725121
#        2	1580.96287678
#
# The column after the step index is the result in both measure shapes -- for `WHEN` it
# is the crossing, for `FIND ... AT` it is the value and the specified point follows in
# its own column. So unlike the scalar form there is no uppercase/lowercase `AT` puzzle
# to solve here: the table has already separated them.
_MEAS_HEAD = re.compile(r"^\s*Measurement:\s*(\w+)\s*$", re.M)
_STEP_ROW = re.compile(rf"^\s*(\d+)\s+(\(\s*{_FLOAT}[^)]*\)|{_FLOAT})", re.M)


def parse_meas_steps(log_text: str) -> dict[str, list[float]]:
    """Per-run `.meas` results from a `.step`ped batch log -> {name: [v per run]}.

    Monte Carlo and any swept corner run needs one value per run, and `parse_meas`
    returns nothing for these logs because they carry no `name: value` line at all.
    """
    out: dict[str, list[float]] = {}
    heads = list(_MEAS_HEAD.finditer(log_text))
    for i, h in enumerate(heads):
        end = heads[i + 1].start() if i + 1 < len(heads) else len(log_text)
        block = log_text[h.end():end]
        vals = []
        for row in _STEP_ROW.finditer(block):
            tok = row.group(2)
            cx = re.match(rf"\(\s*({_FLOAT})", tok)
            vals.append(float(cx.group(1) if cx else tok))
        if vals:
            out[h.group(1).lower()] = vals
    return out


# Two ways a `.meas AC` silently returns a log instead of a number. Neither ever
# errors, so both are lint rules rather than parse rules.
#
# 1. db() double-converts: LTspice already reports AC magnitudes in dB. Verified on
#    26.0.2 -- a gain of 100 measures as 40dB via mag(), but 32.04dB (=20*log10(40))
#    via db().
# 2. PARAM arithmetic is dB-converted too, and unrecoverably so. Verified on 26.0.2:
#    `.meas AC k PARAM 100` logs `k: 100=(40dB,0°)`, and with a real expression the
#    raw value is not printed at all -- `.meas AC ferr PARAM abs(fc-15915)` logged
#    `ferr: abs(fc-15915)=(-1.93449774887dB,0°)`, i.e. 20*log10(0.801). Do the
#    arithmetic outside the netlist, or measure it under .meas TRAN.
_AC_DB_MEAS = re.compile(r"^\s*\.meas\w*\s+ac\b.*?\bdb\s*\(", re.I | re.M)
_AC_PARAM_MEAS = re.compile(r"^\s*\.meas\w*\s+ac\b.*?\bparam\b", re.I | re.M)


def lint_netlist(netlist: str) -> list[str]:
    out = []
    if _AC_DB_MEAS.search(netlist):
        out.append("WARNING: `.meas AC ... db(...)` double-converts to dB. Use mag()"
                   " -- LTspice reports AC measurement magnitudes in dB already.")
    if _AC_PARAM_MEAS.search(netlist):
        out.append("WARNING: `.meas AC ... PARAM <expr>` reports 20*log10(expr), and"
                   " the raw value is not recoverable from the log. Compute it outside"
                   " the netlist, or measure it under `.meas TRAN`.")
    return out


def classify_log(log_text: str) -> tuple[str | None, str | None, dict]:
    """-> (failure_type, message, detail). failure_type None means the log is clean."""
    for pat, kind in _LOG_PATTERNS:
        m = pat.search(log_text)
        if not m:
            continue
        line = next(
            (ln.strip() for ln in log_text.splitlines() if pat.search(ln)), m.group(0)
        )
        nodes = sorted(
            {g for hit in _NODE_HINT.finditer(line) for g in hit.groups() if g}
        )
        return kind, line[:300], {"suspected_nodes": nodes} if nodes else {}
    return None, None, {}


# --- analytic backend ----------------------------------------------------------

ANALYTIC_MODELS: dict[str, Callable[[dict], dict]] = {}


def register_analytic(name: str):
    def deco(fn: Callable[[dict], dict]):
        ANALYTIC_MODELS[name] = fn
        return fn

    return deco


def _run_analytic(params: dict, sim_config: dict) -> SimResult:
    name = sim_config.get("model")
    fn = ANALYTIC_MODELS.get(name)
    if fn is None:
        return SimResult(
            status="failed",
            failure_type=STRUCTURAL_ERROR,
            failure_message=f"no analytic model registered as {name!r}",
            failure_detail={"available": sorted(ANALYTIC_MODELS)},
        )
    out = fn(dict(params))
    if isinstance(out, SimResult):
        return out
    return SimResult(status="ok", metrics={k: float(v) for k, v in out.items()})


# --- ltspice backend -----------------------------------------------------------

def find_ltspice(explicit: str | None = None) -> str | None:
    import os

    for cand in filter(None, [explicit, os.environ.get("LTSPICE_EXE")]):
        if Path(cand).is_file():
            return cand
    user = os.environ.get("USERNAME", "")
    for tpl in LTSPICE_CANDIDATES:
        p = tpl.replace("{user}", user)
        if Path(p).is_file():
            return p
    return shutil.which("ltspice") or shutil.which("LTspice")


def _run_ltspice(netlist: str, workdir: Path, stem: str, sim_config: dict) -> SimResult:
    exe = find_ltspice(sim_config.get("ltspice_exe"))
    if not exe:
        return SimResult(
            status="failed",
            failure_type=SIMULATION_FAILURE,
            failure_message="LTspice executable not found; set LTSPICE_EXE",
        )
    workdir.mkdir(parents=True, exist_ok=True)
    # Absolute: LTspice runs with cwd=workdir, so a relative deck path would resolve
    # against workdir a second time ("Could not open input deck for reading").
    cir = (workdir / f"{stem}.cir").resolve()
    cir.write_text(netlist, encoding="utf-8", newline="\r\n")
    log = cir.with_suffix(".log")
    log.unlink(missing_ok=True)

    timeout = float(sim_config.get("timeout_s", 120))
    cmd = [exe, "-b", *sim_config.get("extra_args", []), str(cir)]
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout, cwd=workdir
        )
    except subprocess.TimeoutExpired:
        return SimResult(
            status="failed",
            failure_type=SIMULATION_FAILURE,
            failure_message=f"LTspice timed out after {timeout}s",
            failure_detail={"cmd": cmd},
            log_path=str(log) if log.exists() else None,
        )

    log_text = log.read_text(errors="replace") if log.exists() else (
        proc.stdout + proc.stderr
    )
    ftype, fmsg, detail = classify_log(log_text)
    if ftype:
        return SimResult(status="failed", failure_type=ftype, failure_message=fmsg,
                         failure_detail=detail, log_path=str(log))
    if not log.exists():
        return SimResult(
            status="failed",
            failure_type=SIMULATION_FAILURE,
            failure_message=f"LTspice produced no log (exit {proc.returncode})",
            failure_detail={"stderr": proc.stderr[-500:]},
        )

    metrics, warnings = parse_meas(log_text)
    warnings = lint_netlist(netlist) + warnings
    rawf = cir.with_suffix(".raw")
    # `require_meas=False` is for waveform work: a `.op` audit or a corner sweep is read
    # out of the `.raw` by `raw.py`, and has no `.meas` directive to report.
    if not metrics and sim_config.get("require_meas", True):
        return SimResult(
            status="failed",
            failure_type=SIMULATION_FAILURE,
            failure_message="simulation ran but no .meas results found in log",
            failure_detail={"hint": "add .meas directives naming your metrics"},
            warnings=warnings,
            log_path=str(log),
            raw_path=str(rawf) if rawf.exists() else None,
        )
    return SimResult(status="ok", metrics=metrics, warnings=warnings,
                     log_path=str(log), raw_path=str(rawf) if rawf.exists() else None)


def simulate(netlist: str, params: dict, sim_config: dict, workdir: Path,
             stem: str) -> SimResult:
    backend = sim_config.get("backend", "ltspice")
    if backend == "analytic":
        return _run_analytic(params, sim_config)
    if backend == "ltspice":
        return _run_ltspice(netlist, workdir, stem, sim_config)
    return SimResult(status="failed", failure_type=STRUCTURAL_ERROR,
                     failure_message=f"unknown backend {backend!r}")
