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

import os
import re
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from . import ir, raw

# --- failure taxonomy (spec S9) -------------------------------------------------

SIMULATION_FAILURE = "simulation_failure"
CONVERGENCE_FAILURE = "convergence_failure"
STRUCTURAL_ERROR = "structural_error"
CONSTRAINT_VIOLATION = "constraint_violation"
COMPONENT_LIMIT = "component_limit"
NUMERICAL_INSTABILITY = "numerical_instability"

_LOG_PATTERNS: list[tuple[re.Pattern, str]] = [
    # "Starting Gmin stepping" is LTspice trying a second way to the operating point,
    # not a failure: it is followed by "succeeded" or by "Gmin stepping failed".
    (re.compile(r"iteration limit|timestep too small|failed to converge|stepping failed|"
                r"operating point failed", re.I), CONVERGENCE_FAILURE),
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


# --- ngspice backend (secondary) ------------------------------------------------
#
# LTspice is the product's simulator. ngspice is here so the same LTspice deck can be
# checked where LTspice is not installed (CI, Linux, Mac, a reviewer's laptop): every
# template in `topologies.py` is verified against it in the test suite.
#
# Only the `.meas` dialect differs for the decks this project writes, so that is all
# the translation does. The rules mirror what LTspice itself reports:
#   * In AC, LTspice prints every MAX/MIN/FIND of mag(...) in dB, so those become
#     vdb(...) here; a WHEN threshold on mag(...) is linear, so it becomes vm(...).
#   * `PARAM expr` becomes a `let` + `print` in the control block.
# ngspice only runs `.meas` reliably from a `.control` block, so the directives move
# there. `.step` is not translated: sweeps stay an LTspice feature.

_MEAS_DIRECTIVE = re.compile(r"^\s*\.meas(?:ure)?\s+(\w+)\s+(\w+)\s+(.*)$", re.I)
_CPLX_FIND = re.compile(r"^find\s+v\(([^()]*)\)\s+(.*)$", re.I)
_MAG_V = re.compile(r"\bmag\(\s*v\(([^()]*)\)\s*\)", re.I)
_DB_V = re.compile(r"\bdb\(\s*v\(([^()]*)\)\s*\)", re.I)
_NG_RESULT = re.compile(rf"^\s*(\w+)\s*=\s*({_FLOAT})")


def find_ngspice(explicit: str | None = None) -> str | None:
    import os

    for cand in filter(None, [explicit, os.environ.get("NGSPICE_EXE")]):
        if Path(cand).is_file():
            return cand
    return shutil.which("ngspice") or shutil.which("ngspice_con")


def to_ngspice(netlist: str) -> str:
    """Rewrite an LTspice deck's `.meas` lines into an ngspice `.control` block."""
    body, ctrl = [], ["set units=degrees", "run"]
    for line in netlist.splitlines():
        m = _MEAS_DIRECTIVE.match(line)
        if not m:
            if line.strip().lower() == ".end":
                continue
            body.append(line)
            continue
        kind, name, rest = m.group(1).lower(), m.group(2), m.group(3).strip()
        if rest.lower().startswith("param"):
            expr = rest[5:].strip().lstrip("=").strip().strip("'\"{}")
            ctrl += [f"let {name} = {expr}", f"print {name}"]
            continue
        low = rest.lower()
        if kind == "noise":
            # ngspice has no .meas for noise; its noise2 plot holds the integral.
            if re.match(r"^integ\s+v\(\s*onoise\s*\)\s*$", low):
                ctrl += ["setplot noise2", f"let {name} = onoise_total", f"print {name}"]
            elif re.match(r"^integ\s+v\(\s*inoise\s*\)\s*$", low):
                ctrl += ["setplot noise2", f"let {name} = inoise_total", f"print {name}"]
            continue
        cx = _CPLX_FIND.match(rest) if kind == "ac" else None
        if cx:
            # LTspice prints a complex FIND as (dB, degrees); ngspice needs one per line.
            node, tail = cx.group(1), _MAG_V.sub(lambda g: f"vm({g.group(1)})", cx.group(2))
            tail = re.sub(r"\b(at|from|to|td)\s+(?!=)", r"\1=", tail, flags=re.I)
            ctrl += [f"meas ac {name} find vdb({node}) {tail}",
                     f"meas ac {name}_deg find vp({node}) {tail}"]
            continue
        if kind == "ac":
            if low.startswith("when"):
                rest = _MAG_V.sub(lambda g: f"vm({g.group(1)})", rest)
            else:
                head, sep, tail = re.split(r"(\bwhen\b)", rest, maxsplit=1, flags=re.I) + ["", ""] \
                    if re.search(r"\bwhen\b", rest, re.I) else (rest, "", "")
                head = _MAG_V.sub(lambda g: f"vdb({g.group(1)})", head)
                head = _DB_V.sub(lambda g: f"vdb({g.group(1)})", head)
                tail = _MAG_V.sub(lambda g: f"vm({g.group(1)})", tail)
                rest = head + sep + tail
        else:
            agg = _AGG.match(rest)
            if agg and not _PLAIN_VEC.match(agg.group(2).strip()):
                # ngspice measures a vector, not an expression: name it with `let`.
                expr = _ng_expr(agg.group(2))
                ctrl.append(f"let x_{name} = {expr}")
                rest = f"{agg.group(1)} x_{name}{agg.group(3) or ''}"
        rest = re.sub(r"\b(at|from|to|td)\s+(?!=)", r"\1=", rest, flags=re.I)
        ctrl.append(f"meas {kind} {name} {rest}")
    if any("@" in c for c in ctrl):
        body.insert(1, ".options savecurrents")
    return "\n".join(body + [".control", *ctrl, ".endc", ".end", ""])


_AGG = re.compile(r"^(max|min|avg|rms|pp|integ)\s+(.+?)((?:\s+(?:from|to)\b.*)?)$", re.I)
_PLAIN_VEC = re.compile(r"^v\(\s*[^(),\s]+\s*\)$", re.I)
_CURRENT = {"d": "@{}[id]", "r": "@{}[i]", "c": "@{}[i]", "l": "i({})", "v": "i({})",
            "m": "@{}[id]", "q": "@{}[ic]", "j": "@{}[id]"}


def _ng_expr(expr: str) -> str:
    """An LTspice waveform expression in ngspice's vector names: V(a,b) is a
    difference, a device current is `@dev[id]` (needs .options savecurrents)."""
    def volt(m):
        a, b = (x.strip() for x in m.group(1).split(","))
        return f"(v({a})-v({b}))"
    expr = re.sub(r"\bv\(\s*([^(),]+,[^(),]+)\)", volt, expr, flags=re.I)

    def cur(m):
        dev = m.group(2).strip().lower()
        if m.group(1).lower() == "ic":
            return f"@{dev}[ic]"
        return _CURRENT.get(dev[:1], "i({})").format(dev)
    return re.sub(r"\b(id|ic|i)\(\s*([^()\s]+)\s*\)", cur, expr, flags=re.I)


def parse_ngspice(log_text: str) -> tuple[dict[str, float], list[str]]:
    """`name = value [at= ...]` lines, as ngspice's `meas` and `print` write them."""
    metrics, warnings = {}, []
    for line in log_text.splitlines():
        low = line.lower()
        if "failed" in low or low.lstrip().startswith(("error", "warning")):
            warnings.append(line.strip())
            continue
        m = _NG_RESULT.match(line)
        if m and m.group(1).lower() not in _NOT_METRICS:
            metrics[m.group(1).lower()] = float(m.group(2))
    return metrics, warnings


def _run_ngspice(netlist: str, workdir: Path, stem: str, sim_config: dict) -> SimResult:
    exe = find_ngspice(sim_config.get("ngspice_exe"))
    if not exe:
        return SimResult(status="failed", failure_type=SIMULATION_FAILURE,
                         failure_message="ngspice executable not found; set NGSPICE_EXE")
    workdir.mkdir(parents=True, exist_ok=True)
    cir = (workdir / f"{stem}.ng.cir").resolve()
    cir.write_text(to_ngspice(netlist), encoding="utf-8")
    log = cir.with_suffix(".log")
    timeout = float(sim_config.get("timeout_s", 120))
    try:
        proc = subprocess.run([exe, "-b", str(cir)], capture_output=True, text=True,
                              timeout=timeout, cwd=workdir)
    except subprocess.TimeoutExpired:
        return SimResult(status="failed", failure_type=SIMULATION_FAILURE,
                         failure_message=f"ngspice timed out after {timeout}s")
    text = proc.stdout + "\n" + proc.stderr
    log.write_text(text, encoding="utf-8")
    ftype, fmsg, detail = classify_log(text)
    metrics, warnings = parse_ngspice(text)
    if ftype and not metrics:
        return SimResult(status="failed", failure_type=ftype, failure_message=fmsg,
                         failure_detail=detail, log_path=str(log))
    if not metrics and sim_config.get("require_meas", True):
        return SimResult(status="failed", failure_type=SIMULATION_FAILURE,
                         failure_message="ngspice ran but no .meas results found",
                         failure_detail={"tail": text[-600:]}, warnings=warnings,
                         log_path=str(log))
    return SimResult(status="ok", metrics=metrics, warnings=warnings, log_path=str(log))


def workers() -> int:
    """How many simulations run at once: SPICEMCP_WORKERS, else one per CPU core up to
    8. Each simulation is its own simulator process, so threads only wait on them."""
    try:
        n = int(os.environ.get("SPICEMCP_WORKERS", "0"))
    except ValueError:
        n = 0
    return max(1, n or min(os.cpu_count() or 1, 8))


def parallel_map(fn: Callable, items, n: int | None = None) -> list:
    """`[fn(x) for x in items]`, run on up to `workers()` threads, results in order."""
    items = list(items)
    n = min(n or workers(), len(items))
    if n <= 1:
        return [fn(x) for x in items]
    with ThreadPoolExecutor(max_workers=n) as ex:
        return list(ex.map(fn, items))


def decks(netlist: str) -> list[str]:
    """Split a netlist into its decks. Each `.end` closes one deck.

    One analysis per deck is all LTspice runs, so a circuit checked two ways (its
    response in `.dc`, its loop stability in `.ac`, its load step in `.tran`) is written
    as several decks, one after another, each complete with its own sources and models.
    The first deck is the circuit itself: it is the one drawn and audited.
    """
    out, cur = [], []
    for ln in netlist.splitlines():
        cur.append(ln)
        if _END_LINE.match(ln):
            out.append("\n".join(cur) + "\n")
            cur = []
    if any(ln.strip() for ln in cur):
        out.append("\n".join(cur) + "\n")
    return out or [netlist]


def simulate(netlist: str, params: dict, sim_config: dict, workdir: Path,
             stem: str) -> SimResult:
    backend = sim_config.get("backend", "ltspice")
    if backend == "analytic":
        return _run_analytic(params, sim_config)
    if backend not in ("ltspice", "ngspice"):
        return SimResult(status="failed", failure_type=STRUCTURAL_ERROR,
                         failure_message=f"unknown backend {backend!r}")
    run = _run_ltspice if backend == "ltspice" else _run_ngspice
    from .opamp import include_blocks          # opamp is pure text; no import cycle
    parts = [include_blocks(d) for d in decks(netlist)]
    if len(parts) == 1:
        return run(parts[0], workdir, stem, sim_config)
    # Several decks: independent simulations, so they run side by side. Measurements
    # are merged in deck order; the first failure (in that order) is reported, and says
    # which deck failed, since a stability deck can fail where the main one ran.
    results = parallel_map(
        lambda i: run(parts[i], workdir, stem if i == 0 else f"{stem}_d{i + 1}",
                      sim_config), range(len(parts)))
    merged = SimResult(status="ok")
    for i, r in enumerate(results):
        if r.status != "ok":
            r.failure_message = f"deck {i + 1} of {len(parts)}: {r.failure_message}"
            return r
        clash = sorted(set(r.metrics) & set(merged.metrics))
        if clash:
            return SimResult(status="failed", failure_type=STRUCTURAL_ERROR,
                             failure_message=f"measurement names {clash} appear in more "
                                             f"than one deck; give each a unique name",
                             log_path=r.log_path)
        merged.metrics.update(r.metrics)
        merged.warnings += r.warnings
        if i == 0:
            merged.log_path, merged.raw_path = r.log_path, r.raw_path
    return merged


# --- parametric / corner sweep --------------------------------------------------
#
# One LTspice launch, not one per point. `.step` is what the simulator is for: it reuses
# the parsed circuit and writes every dataset into one `.raw`, and the per-run `.meas`
# tables `parse_meas_steps` already reads come out of the same log. N launches would be
# N process starts and N licence checks for the same answer.
#
# Three kinds of sweep, decided by what the netlist already says -- so a caller does not
# have to describe its own deck:
#
#   temp        `.step temp list ...`                  (corner / PVT sweeps)
#   {NAME}      `.step param NAME list ...`            (the deck is already parametric)
#   R1          rewrite `R1`'s value to `{R1_sweep}`, then step that
#
# The third is the one worth having: a plain netlist off the LLM has literal component
# values, and asking the caller to re-author it as a template just to sweep it is how
# a sweep ends up hand-rolled as a loop with a different netlist per point.

_STEP_LINE = re.compile(r"^\s*\.step\b", re.I)
_END_LINE = re.compile(r"^\s*\.end\s*$", re.I)
# An element line: name, then its nodes, then the value. Two nodes covers R/C/L/V/I,
# which is every component anyone sweeps; a 3+ terminal device is swept through its
# model, not its value.
_ELEMENT = re.compile(r"^(\s*(\w+)\s+\S+\s+\S+\s+)([^\s;]+)(.*)$")


class SweepError(ValueError):
    """A sweep that cannot be built. Raised before LTspice is launched, never after."""


def sweep_netlist(netlist: str, param_name: str,
                  values: list[float]) -> tuple[str, str, dict]:
    """-> (netlist with a `.step`, the stepped symbol, what was rewritten).

    Existing `.step` lines are dropped: two of them are a nested sweep, and the caller
    asked for one axis.
    """
    if not param_name or not str(param_name).strip():
        raise SweepError("param_name is empty")
    name = str(param_name).strip()
    if not re.fullmatch(r"[A-Za-z_][\w$]*", name):
        raise SweepError(f"{name!r} is not a usable parameter or component name")
    nums = []
    for v in values:
        try:
            f = float(v)
        except (TypeError, ValueError):
            raise SweepError(f"sweep value {v!r} is not a number") from None
        if f != f or f in (float("inf"), float("-inf")):
            raise SweepError(f"sweep value {v!r} is not finite")
        nums.append(f)
    if len(nums) < 2:
        raise SweepError(f"a sweep needs at least 2 values, got {len(nums)}")
    if len(set(nums)) != len(nums):
        raise SweepError(f"duplicate sweep values: {nums}")

    body = [ln for ln in netlist.splitlines()
            if not _STEP_LINE.match(ln) and not _END_LINE.match(ln)]
    lit = " ".join(ir.fmt_value(v) for v in nums)
    note: dict = {}

    if name.lower() == "temp":
        return _deck(body, f".step temp list {lit}"), "temp", {"kind": "temperature"}

    if re.search(r"\{\s*" + re.escape(name) + r"\s*\}", netlist, re.I):
        return (_deck(body, f".step param {name} list {lit}"), name,
                {"kind": "param", "placeholder": f"{{{name}}}"})

    # Neither -- so it has to be a component whose literal value we replace. The
    # substituted parameter gets its own name so `.step param R1` cannot collide with a
    # `.param R1` the deck already defines for something else.
    sub, hit = f"{name}_sweep", None
    for i, ln in enumerate(body):
        m = _ELEMENT.match(ln)
        if m and m.group(2).lower() == name.lower():
            hit = {"kind": "component", "line": ln.strip(), "was": m.group(3),
                   "now": f"{{{sub}}}"}
            body[i] = f"{m.group(1)}{{{sub}}}{m.group(4)}"
            break
    if hit is None:
        raise SweepError(
            f"nothing to sweep: {name!r} is not `temp`, has no {{{name}}} placeholder in "
            f"the netlist, and is not a two-terminal element in it. Either add "
            f"{{{name}}} where the value goes, or name a component that is there.")
    note = hit
    return _deck(body, f".param {sub}={ir.fmt_value(nums[0])}",
                 f".step param {sub} list {lit}"), sub, note


def _deck(lines: list[str], *what: str) -> str:
    """Assemble a runnable deck with a title line and one `.end`.

    SPICE ignores a deck's first line -- it is the title. Without this guard a netlist
    starting with `V1 in 0 5` loses its source to the title slot and simulates a circuit
    with no supply, which LTspice runs without complaining.
    """
    body = list(lines)
    if not (body and body[0].strip().startswith("*")):
        body.insert(0, "* spicemcp sweep deck")
    return "\n".join(body + list(what) + [".end", ""])


def _step_value(label: str, symbol: str) -> float | None:
    """The swept number out of a `.step r1_sweep=1000` log line.

    Reads the named parameter rather than assuming the log's order matches the values
    asked for -- and `.step temp=-40deg` carries a unit suffix, which `parse_value`
    tolerates.
    """
    for tok in re.finditer(r"([\w$]+)\s*=\s*(\S+)", label):
        if tok.group(1).lower() == symbol.lower():
            return ir.parse_value(tok.group(2))
    return None


def _raw_axis_values(raw_path: str, warnings: list[str]) -> list[float]:
    """The swept values read out of the `.raw` itself, for a log that named none.

    A stepped `.op` gets no `.step` echo in the log at all (verified against
    tests/fixtures/temp_op.log) -- and LTspice stores it as one dataset whose sweep axis
    *is* the step variable, so the axis holds the values the log withheld. Counting
    datasets would say 1 where the run did 3; reading the axis says 3, with the numbers.
    Any other stepped analysis is one dataset per point, so the axis is not the values
    and the dataset count is.
    """
    try:
        waves = raw.read_raw(raw_path)
    except (ValueError, OSError, KeyError) as e:
        warnings.append(f"could not read {raw_path} for step values: {e}")
        return []
    if len(waves) == 1 and waves[0].analysis == "op":
        return [float(v) for v in waves[0].x]
    return [None] * len(waves)


def simulate_sweep(netlist: str, param_name: str, values: list[float],
                   workdir: Path, stem: str = "sweep",
                   sim_config: dict | None = None) -> dict:
    """Run a one-launch `.step` sweep and return one row of metrics per point.

    Metrics are whatever the netlist's own `.meas` directives are named -- nothing here
    knows or invents a metric name. A deck with no `.meas` still returns its points, with
    the `.raw` path and a warning, because a waveform sweep is a legitimate thing to ask
    for; it does not get silently reported as a sweep that measured nothing.

    Each row's `value_source` says where its value came from: `log` (LTspice's own `.step`
    echo), `raw` (the stepped-`.op` sweep axis, which is the only evidence such a run
    leaves) or `requested` (nothing reported it, so the asked-for value is used and the
    row is flagged).
    """
    cfg = dict(sim_config or {})
    cfg.setdefault("backend", "ltspice")
    cfg["require_meas"] = False          # the stepped tables are read below, not by parse_meas
    deck, symbol, rewrote = sweep_netlist(netlist, param_name, values)
    asked = [float(v) for v in values]
    if cfg["backend"] != "ltspice":
        return _sweep_point_by_point(deck, symbol, rewrote, asked, Path(workdir), stem,
                                     cfg, param_name)

    res = _run_ltspice(deck, Path(workdir), stem, cfg)
    out: dict = {"sweep_param": {"name": param_name, "symbol": symbol,
                                 "values": asked},
                 "metrics_table": [], "raw_path": res.raw_path,
                 "log_path": res.log_path, "netlist": deck,
                 "rewrote": rewrote, "warnings": list(res.warnings)}
    if res.status != "ok":
        out.update(status="failed", failure_type=res.failure_type,
                   failure_message=res.failure_message,
                   failure_detail=res.failure_detail)
        return out

    log_text = Path(res.log_path).read_text(errors="replace") if res.log_path else ""
    tables = parse_meas_steps(log_text)
    scalar, _ = parse_meas(log_text) if not tables else ({}, [])

    # Which value each run actually used, taken from the log's own `.step` lines rather
    # than from the order we asked for. A run LTspice skipped would otherwise shift every
    # later row onto the wrong value -- a sweep that reads as smooth and is wrong.
    labels = []
    if res.raw_path and Path(res.raw_path).is_file():
        labels = raw._step_labels(Path(res.raw_path))
    if not labels:
        labels = re.findall(r"^\.step\s+(.+?)\s*$", log_text, re.M)
    from_log = [_step_value(ln, symbol) for ln in labels]
    src = "log"
    if not labels and res.raw_path and Path(res.raw_path).is_file():
        from_log = _raw_axis_values(res.raw_path, out["warnings"])
        src = "raw"

    n = max([len(v) for v in tables.values()] + [len(from_log)] + [0])
    if not n:
        out.update(status="failed", failure_type=SIMULATION_FAILURE,
                   failure_message="the run produced no sweep points: no stepped .meas "
                                   "table, no .step lines in the log and no .raw datasets",
                   failure_detail={"hint": "check that .step survived into the deck",
                                   "log_tail": log_text[-400:]})
        return out

    for i in range(n):
        val = from_log[i] if i < len(from_log) else None
        row = {"step": i + 1,
               "value": val if val is not None else (asked[i] if i < len(asked) else None),
               "value_source": src if val is not None else "requested",
               "metrics": {k: v[i] for k, v in tables.items() if i < len(v)}}
        if val is None:
            row["warning"] = "value not reported by LTspice; taken from the request"
        out["metrics_table"].append(row)

    if n != len(asked):
        out["warnings"].append(
            f"asked for {len(asked)} points, LTspice ran {n} -- the sweep is incomplete")
    missing = [r["step"] for r in out["metrics_table"] if not r["metrics"]]
    if not tables:
        out["warnings"].append(
            "no stepped .meas results in the log: add .meas directives to get metrics per "
            "point, or read the waveforms from raw_path"
            + (f" (the deck measured {sorted(scalar)} once, not per step)" if scalar
               else ""))
    elif missing:
        out["warnings"].append(f"steps with no metrics at all: {missing}")

    out.update(status="ok", points=n,
               metrics=sorted(tables),
               complete=(n == len(asked) and not missing))
    return out


def _sweep_point_by_point(deck: str, symbol: str, rewrote: dict, asked: list[float],
                          workdir: Path, stem: str, cfg: dict, param_name: str) -> dict:
    """The same sweep for a backend without LTspice's `.step`: one simulation per
    value, side by side, each with the stepped symbol set by `.param` (or `.temp`)."""
    body = [ln for ln in deck.splitlines() if not _STEP_LINE.match(ln)]
    cfg = {**cfg, "require_meas": False}

    def point(i: int) -> SimResult:
        v = ir.fmt_value(asked[i])
        lines = list(body)
        if symbol == "temp":
            add = f".temp {v}"
        else:
            lines = [ln for ln in lines if not re.match(
                rf"^\s*\.param\s+{re.escape(symbol)}\s*=", ln, re.I)]
            add = f".param {symbol}={v}"
        end = next((k for k in range(len(lines) - 1, -1, -1)
                    if _END_LINE.match(lines[k])), len(lines))
        lines.insert(end, add)
        return simulate("\n".join(lines) + "\n", {}, cfg, workdir, f"{stem}_{i + 1:03d}")

    res = parallel_map(point, range(len(asked)))
    out: dict = {"sweep_param": {"name": param_name, "symbol": symbol, "values": asked},
                 "metrics_table": [], "raw_path": None, "log_path": None,
                 "netlist": deck, "rewrote": rewrote, "warnings": [],
                 "method": "one simulation per value"}
    bad = [(i, r) for i, r in enumerate(res) if r.status != "ok"]
    if len(bad) == len(res):
        r = bad[0][1]
        out.update(status="failed", failure_type=r.failure_type,
                   failure_message=r.failure_message, failure_detail=r.failure_detail,
                   log_path=r.log_path)
        return out
    names = set()
    for i, r in enumerate(res):
        row = {"step": i + 1, "value": asked[i], "value_source": "requested",
               "metrics": dict(r.metrics) if r.status == "ok" else {}}
        if r.status != "ok":
            row["warning"] = f"failed: {r.failure_message}"
        names |= set(row["metrics"])
        out["metrics_table"].append(row)
        out["warnings"] += r.warnings
    if not names:
        out["warnings"].append("no .meas results: add .meas directives to get metrics "
                               "per point")
    out.update(status="ok", points=len(asked), metrics=sorted(names),
               complete=not bad and bool(names), log_path=res[0].log_path)
    return out
