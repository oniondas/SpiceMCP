"""Draw every template, the new instrumentation amplifier and the layout test circuits,
then (where LTspice is installed) let LTspice turn each drawing back into a netlist and
compare it pin by pin with the source netlist. The comparison is written here from
scratch: it does not reuse SpiceMCP's own checker.  Usage: sch_check.py"""
import json, math, re, subprocess, sys, tempfile
from pathlib import Path
ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / "tests"))
from mcpc import OUT
from spicemcp import ir, layout, opamp, render, sim, topologies as T
from ina_netlist import ina
from test_layout import UNSEEN

SPECS = {"rc_lowpass": {"fc": 1000}, "sallen_key_lowpass": {"fc": 10000},
         "noninverting_amp": {"gain": 10, "bw": 100e3},
         "ldo": {"vin": 5, "vout": 3.3, "iload": 0.1},
         "buck": {"vin": 12, "vout": 5, "iout": 1}}
INA_VALUES = {"R1A": 9760, "R1B": 10200, "R2A": 9760, "R2B": 9760, "R3A": 10000,
              "R3B": 10000, "RG": 205}
NPINS = {"r": 2, "c": 2, "l": 2, "v": 2, "i": 2, "d": 2, "e": 4, "g": 4, "s": 4}


def ina_single() -> str:
    """The differential copy only (the common-mode copy is the same circuit again)."""
    net = sim.decks(ina(opamp.subckt(10e6, 10.0)))[0]
    keep = [l for l in net.splitlines()
            if not re.match(r"^(X\dc|R\w+c|CLc|Vc)\s", l)]
    return "\n".join(keep) + "\n"


def connections(netlist: str, names: set | None = None, npins: dict | None = None) -> set[frozenset]:
    """Group (element, pin number) by the node they sit on. Subcircuit bodies, dot
    lines and comments are skipped; ground is node 0 under any spelling LTspice uses."""
    pins, depth = {}, 0
    for raw in netlist.splitlines():
        ln = raw.strip()
        low = ln.lower()
        if low.startswith(".subckt"): depth += 1; continue
        if low.startswith(".ends"): depth -= 1; continue
        if depth or not ln or ln[0] in "*.;+":
            continue
        ln = ln.split(";")[0].replace("§", "")     # LTspice pin-name comments, X§X1 names
        tok = ln.split()
        name, kind = tok[0].lower(), tok[0][0].lower()
        if names and name not in names and name[1:] in names:
            name = name[1:]               # LTspice may prefix the type letter again
        if kind == "x":
            nodes = [t for t in tok[1:] if "=" not in t][:-1]
        elif kind == "m":
            plain = [t for t in tok[1:] if "=" not in t]
            nodes = plain[:-1][:4]          # 3 pins (VDMOS) or 4 pins, then the model
        elif kind == "q":
            nodes = tok[1:4]
        else:
            nodes = tok[1:1 + NPINS.get(kind, 2)]
        if names and npins and name in npins:
            nodes = nodes[:npins[name]]   # LTspice adds the MOSFET body pin (tied to source)
        for i, n in enumerate(nodes):
            n = "0" if n.lower() in ("0", "gnd") else n.lower()
            pins.setdefault(n, set()).add((name, i))
    return {frozenset(v) for v in pins.values()}


def ltspice_netlist(asc: Path, exe: str) -> str:
    net = asc.with_suffix(".net"); net.unlink(missing_ok=True)
    subprocess.run([exe, "-netlist", str(asc)], capture_output=True, timeout=120, cwd=asc.parent)
    raw = net.read_bytes()
    txt = raw.decode("utf-16-le") if raw[:2] in (b"\xff\xfe",) or raw[1:2] == b"\x00" else raw.decode("latin-1")
    return txt


def main():
    exe = sim.find_ltspice()
    work = OUT / "schematics"; work.mkdir(parents=True, exist_ok=True)
    circuits = {}
    for n, s in SPECS.items():
        d = T.get(n).design(s)
        circuits[n] = (ir.render(d.netlist, d.start)[0], None)
    circuits["instrumentation_amp"] = (ina_single(), INA_VALUES)
    circuits.update({f"unseen_{k}": (v, None) for k, v in UNSEEN.items()})
    rows = []
    for name, (net, vals) in circuits.items():
        row = {"circuit": name}
        try:
            first = sim.decks(net)[0]
            if vals:
                for k, v in vals.items(): first = first.replace("{%s}" % k, f"{v:g}")
            p = layout.draw(first).write(work / f"{name}.asc")
            row["lint"] = layout.lint(p)
            render.render(str(p), str(work / f"{name}.png"), "tech_minimal", 150)
            if exe:
                lt = ltspice_netlist(p, exe)
                a = connections(first)
                names = {x[0] for g in a for x in g}
                npins = {}
                for g in a:
                    for el, i in g:
                        npins[el] = max(npins.get(el, 0), i + 1)
                b = connections(lt, names, npins)
                row["same_circuit"] = a == b
                row["groups"] = len(a)
                if a != b:
                    row["only_in_source"] = [sorted(map(list, g)) for g in a - b]
                    row["only_in_ltspice"] = [sorted(map(list, g)) for g in b - a]
                    row["ltspice_netlist"] = lt
        except Exception as e:
            row["error"] = f"{type(e).__name__}: {e}"
        print(name, row.get("lint", {}).get("score"), row.get("same_circuit"), row.get("error", ""))
        rows.append(row)
    (OUT / f"schematics_{'ltspice' if exe else 'drawn_only'}.json").write_text(json.dumps(rows, indent=1))


main()
