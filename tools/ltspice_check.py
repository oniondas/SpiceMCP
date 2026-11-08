"""Check SpiceMCP against a real LTspice install (Windows).

    python tools/ltspice_check.py            # everything
    python tools/ltspice_check.py --fixtures # only regenerate tests/fixtures/*.raw/.log

Three checks, all written to docs/reports/step7/ltspice_check.json:

1. Fixtures: every tests/fixtures/*.cir is run through LTspice in batch mode, giving
   the `.raw` and `.log` files the waveform tests read.
2. Schematics: every template and every drawing test circuit is drawn, then netlisted
   back by LTspice itself (`asc.check`). LTspice's netlist must join the same pins as
   the original netlist, which proves the bundled symbols, the op-amp `.lib` and the
   wiring all mean in LTspice what they mean to SpiceMCP.
3. Simulation: every template's hand-sized start is simulated in LTspice, and in
   ngspice where it is installed, and the measurements are put side by side.
"""
from __future__ import annotations

import argparse
import json
import platform
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
from spicemcp import asc, ir, layout, sim, topologies as T  # noqa: E402

SPECS = {
    "rc_lowpass": {"fc": 1000},
    "sallen_key_lowpass": {"fc": 10000},
    "noninverting_amp": {"gain": 10, "bw": 100e3},
    "ldo": {"vin": 5, "vout": 3.3, "iload": 0.1},
    "buck": {"vin": 12, "vout": 5, "iout": 1},
}
OUT = ROOT / "docs" / "reports" / "step7"


def fixtures(exe: str) -> list[dict]:
    rows = []
    for cir in sorted((ROOT / "tests" / "fixtures").glob("*.cir")):
        for ext in (".raw", ".log", ".op.raw"):
            cir.with_suffix(ext).unlink(missing_ok=True)
        t = time.time()
        proc = subprocess.run([exe, "-b", str(cir)], capture_output=True, text=True,
                              timeout=300, cwd=cir.parent)
        rows.append({"fixture": cir.name, "exit": proc.returncode,
                     "raw": cir.with_suffix(".raw").is_file(),
                     "log": cir.with_suffix(".log").is_file(),
                     "seconds": round(time.time() - t, 2)})
        print("fixture", rows[-1])
    return rows


def _groups(pairs) -> set[frozenset]:
    """{(element, pin index) grouped by node}, ground kept as its own group."""
    by_node: dict[str, set] = {}
    for name, i, node in pairs:
        key = "0" if node.lower() in ("0", "gnd") else node.lower()
        by_node.setdefault(key, set()).add((name.lower(), i))
    return {frozenset(v) for v in by_node.values()}


def _from_ltspice(lines: list[str], want: dict[str, int]) -> list[tuple]:
    out = []
    for ln in lines:
        if ln.startswith("."):
            continue
        tok = ln.split()
        name = tok[0].lower()
        if name not in want and name[1:] in want:      # `X§U1` arrives as `XU1`
            name = name[1:]
        if name not in want:
            continue
        for i, node in enumerate(tok[1:1 + want[name]]):
            out.append((name, i, node))
    return out


def schematics(exe: str) -> list[dict]:
    from test_layout import UNSEEN
    circuits = {f"template:{n}": ir.render(T.get(n).design(s).netlist,
                                           T.get(n).design(s).start)[0]
                for n, s in SPECS.items()}
    circuits.update({f"test:{n}": net for n, net in UNSEEN.items()})
    rows = []
    work = Path(tempfile.mkdtemp(prefix="spicemcp_asc_"))
    for name, net in circuits.items():
        row = {"circuit": name}
        try:
            dr = layout.draw(net)
            path = dr.write(work / (name.replace(":", "_") + ".asc"))
            els, _, _ = layout.parse(net)
            want = {e.name.lower(): len(e.pin_nodes) for e in els}
            ours = _groups((e.name, i, n) for e in els for i, n in enumerate(e.pin_nodes))
            lt = asc.check(path, exe)
            theirs = _groups(_from_ltspice(lt, want))
            row.update(same_circuit=ours == theirs,
                       readability=layout.lint(path)["score"],
                       ltspice_netlist=lt)
            if ours != theirs:
                row["only_in_spicemcp"] = [sorted(g) for g in ours - theirs]
                row["only_in_ltspice"] = [sorted(g) for g in theirs - ours]
        except Exception as e:                       # report, keep going
            row.update(same_circuit=False, error=f"{type(e).__name__}: {e}")
        print("schematic", name, row.get("same_circuit"), row.get("error", ""))
        rows.append(row)
    return rows


def simulate() -> list[dict]:
    rows = []
    have_ng = bool(sim.find_ngspice())
    for n, s in SPECS.items():
        d = T.get(n).design(s)
        net, _ = ir.render(d.netlist, d.start)
        row = {"template": n}
        for backend in ("ltspice",) + (("ngspice",) if have_ng else ()):
            t = time.time()
            r = sim.simulate(net, d.start, {"backend": backend},
                             Path(tempfile.mkdtemp(prefix=f"spicemcp_{backend}_")), n)
            row[backend] = {"status": r.status, "metrics": r.metrics,
                            "failure": r.failure_message,
                            "seconds": round(time.time() - t, 2)}
        lim = {m: v for m, v in row["ltspice"]["metrics"].items()}
        row["ltspice_meets_limits"] = all(
            (x.get("min") is None or lim.get(x["metric"], float("nan")) >= x["min"]) and
            (x.get("max") is None or lim.get(x["metric"], float("nan")) <= x["max"])
            for x in d.limits if not x["metric"].startswith("param_"))
        print("simulate", n, row["ltspice"]["status"], row["ltspice_meets_limits"])
        rows.append(row)
    return rows


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--fixtures", action="store_true", help="only regenerate fixtures")
    a = ap.parse_args()
    exe = sim.find_ltspice()
    if not exe:
        sys.exit("LTspice not found: install it or set LTSPICE_EXE")
    report = {"ltspice": exe, "platform": platform.platform(),
              "python": platform.python_version(), "fixtures": fixtures(exe)}
    if not a.fixtures:
        report["schematics"] = schematics(exe)
        report["simulation"] = simulate()
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "ltspice_check.json").write_text(json.dumps(report, indent=1, default=str))
    bad = [r for r in report.get("schematics", []) if not r["same_circuit"]]
    bad += [r for r in report.get("simulation", []) if r["ltspice"]["status"] != "ok"
            or not r["ltspice_meets_limits"]]
    bad += [r for r in report["fixtures"] if not r["raw"]]
    print(f"\n{len(bad)} problem(s); details in {OUT / 'ltspice_check.json'}")


if __name__ == "__main__":
    main()
