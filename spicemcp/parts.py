"""Real parts by part number, from the SPICE libraries already on this computer.

LTspice ships thousands of vendor models: `.subckt` op-amps and regulators in
`lib/sub`, and one-line `.model`s for MOSFETs, diodes and transistors in
`lib/cmp/standard.*`, many with their datasheet ratings (`Vds=30 Ron=4m mfg=Vishay`,
`Vpk=40 Iave=1 mfg=OnSemi`). This module indexes them, finds a part by name, and puts
a chosen part into a template netlist in place of the generic one.

Where it looks: the LTspice library folders (Windows: %LOCALAPPDATA%\\LTspice\\lib and
Documents\\LTspiceXVII\\lib; macOS: ~/Library/Application Support/LTspice/lib) and every
folder in SPICEMCP_LIB_DIRS (separated by the OS path separator). The index is cached
and rebuilt when a file changes.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path

EXTS = {".lib", ".sub", ".mod", ".cir", ".txt", ".mos", ".dio", ".bjt", ".jft", ".sp",
        ".spi", ".spice", ".inc", ".ckt"}
MAX_FILE = 20_000_000
RATING_KEYS = {"vds": "Vds", "ron": "Ron", "qg": "Qg", "vpk": "Vpk", "iave": "Iave",
               "ipk": "Ipk", "vceo": "Vceo", "icrating": "Icrating", "mfg": "mfg"}

_SUBCKT = re.compile(r"^\s*\.subckt\s+(\S+)\s*(.*)$", re.I)
_MODEL = re.compile(r"^\s*\.model\s+(\S+)\s+([A-Za-z]+)\s*\(?(.*)$", re.I)
_KV = re.compile(r"(\w+)\s*=\s*(\"[^\"]*\"|[^\s()]+)")


class PartError(ValueError):
    pass


@dataclass
class Part:
    name: str
    kind: str                     # subckt | model
    type: str                     # opamp/subckt pins, or the model type (D, VDMOS, NMOS...)
    file: str
    pins: list[str] = field(default_factory=list)
    channel: str = ""             # n | p for MOSFETs
    ratings: dict = field(default_factory=dict)
    text: str = ""                # the full .model card (one line), for inlining

    @property
    def role(self) -> str:
        t = self.type.upper()
        if self.kind == "subckt":
            return "opamp" if len(self.pins) == 5 else f"subckt{len(self.pins)}"
        if t in ("VDMOS", "NMOS", "PMOS"):
            return "pmos" if self.channel == "p" else "nmos"
        return {"D": "diode", "NPN": "npn", "PNP": "pnp"}.get(t, t.lower())

    def short(self) -> dict:
        out = {"name": self.name, "is": self.role}
        if self.pins:
            out["pins"] = " ".join(self.pins)
        if self.ratings:
            out["rated"] = rated_text(self.ratings)
        out["file"] = Path(self.file).name
        return out


def rated_text(r: dict) -> str:
    bits = []
    for k, unit in (("vds", "V"), ("vpk", "V"), ("vceo", "V"), ("iave", "A"), ("ipk", "A pk"),
                    ("icrating", "A"), ("ron", "Ω")):
        if k in r:
            v = r[k]
            bits.append(f"{abs(v) if isinstance(v, (int, float)) else v} {unit}")
    if "mfg" in r:
        bits.append(str(r["mfg"]))
    return ", ".join(bits)


def lib_dirs() -> list[Path]:
    home = Path.home()
    cands = [Path(p) for p in os.environ.get("SPICEMCP_LIB_DIRS", "").split(os.pathsep) if p]
    local = os.environ.get("LOCALAPPDATA")
    if local:
        cands.append(Path(local) / "LTspice" / "lib")
    cands += [home / "Documents" / "LTspiceXVII" / "lib", home / "Documents" / "LTspice" / "lib",
              home / "Library" / "Application Support" / "LTspice" / "lib"]
    seen, out = set(), []
    for c in cands:
        try:
            r = c.expanduser().resolve()
        except OSError:
            continue
        if r.is_dir() and r not in seen:
            seen.add(r)
            out.append(r)
    return out


def _num(v: str):
    from .ir import parse_value
    x = parse_value(v)
    return v.strip('"') if x is None else (int(x) if x == int(x) and abs(x) < 1e6 else x)


def scan_file(path: Path) -> list[Part]:
    """Every .subckt and .model in one file. Encrypted bodies are skipped: only the
    header line is needed to use a part."""
    try:
        if path.stat().st_size > MAX_FILE:
            return []
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    lines = text.splitlines()
    out, i = [], 0
    while i < len(lines):
        ln = lines[i]
        j = i + 1
        while j < len(lines) and lines[j].lstrip().startswith("+"):
            ln += " " + lines[j].lstrip()[1:]
            j += 1
        m = _SUBCKT.match(ln)
        if m:
            pins = []
            for t in m.group(2).split():
                if t.lower().startswith("params:") or "=" in t or t.startswith(("{", ";")):
                    break
                pins.append(t)
            out.append(Part(m.group(1), "subckt", f"subckt{len(pins)}", str(path), pins))
        else:
            m = _MODEL.match(ln)
            if m:
                body = m.group(3)
                kv = {k.lower(): v for k, v in _KV.findall(body)}
                chan = "p" if re.search(r"\bpchan\b", body, re.I) or m.group(2).upper() in (
                    "PMOS",) else ("n" if m.group(2).upper() in ("VDMOS", "NMOS") else "")
                rat = {k: _num(kv[k]) for k in RATING_KEYS if k in kv}
                out.append(Part(m.group(1), "model", m.group(2).upper(), str(path), [],
                                chan, rat, re.sub(r"\s+", " ", ln.strip())))
        i = j
    return out


def _index_path() -> Path:
    return Path(os.environ.get("SPICEMCP_PARTS_CACHE",
                               Path.home() / ".cache" / "spicemcp" / "parts_index.json"))


def index(dirs: list[Path] | None = None) -> list[Part]:
    """All parts in the library folders, from the cache when no file changed."""
    dirs = lib_dirs() if dirs is None else dirs
    files = sorted(p for d in dirs for p in d.rglob("*")
                   if p.is_file() and p.suffix.lower() in EXTS
                   or p.is_file() and p.name.lower().startswith("standard."))
    stamp = [[str(p), p.stat().st_mtime_ns, p.stat().st_size] for p in files]
    cache = _index_path()
    try:
        old = json.loads(cache.read_text(encoding="utf-8"))
        if old.get("stamp") == stamp:
            return [Part(**p) for p in old["parts"]]
    except (OSError, ValueError, TypeError):
        pass
    parts = [p for f in files for p in scan_file(f)]
    try:
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps({"stamp": stamp, "parts": [asdict(p) for p in parts]}),
                         encoding="utf-8")
    except OSError:
        pass
    return parts


def find(query: str, role: str = "any", limit: int = 10,
         dirs: list[Path] | None = None) -> list[Part]:
    """Parts whose name contains `query` (case-insensitive), exact matches first."""
    q = query.strip().lower()
    hits = [p for p in index(dirs)
            if (q in p.name.lower() or q == str(p.ratings.get("mfg", "")).lower())
            and (role == "any" or p.role == role)]
    hits.sort(key=lambda p: (p.name.lower() != q, not p.name.lower().startswith(q),
                             len(p.name), p.name.lower()))
    uniq, seen = [], set()
    for p in hits:
        if p.name.lower() not in seen:
            seen.add(p.name.lower())
            uniq.append(p)
    return uniq[:limit]


def get(name: str, dirs: list[Path] | None = None) -> Part:
    for p in find(name, dirs=dirs, limit=50):
        if p.name.lower() == name.strip().lower():
            return p
    near = [p.name for p in find(name[:4], dirs=dirs, limit=5)] if len(name) >= 4 else []
    raise PartError(f"part {name!r} not found in the SPICE libraries"
                    + (f"; similar: {near}" if near else ""))


# --- putting a chosen part into a template --------------------------------------------

SLOTS = {   # slot -> (what it must be, the generic it replaces, pins it is wired by)
    "opamp": ("opamp", "SPMCP_OA", "in+ in- V+ V- out"),
    "switch": ("pmos", "PSW", "drain gate source"),
    "diode": ("diode", "DS", "anode cathode"),
}


def _end_insert(netlist: str, lines: list[str], only_if: str) -> str:
    """Add `lines` before the .end of every deck that mentions `only_if`."""
    out, deck = [], []
    for ln in netlist.splitlines():
        if ln.strip().lower() == ".end":
            if any(re.search(rf"\b{re.escape(only_if)}\b", d, re.I) for d in deck):
                deck += lines
            out += deck + [ln]
            deck = []
        else:
            deck.append(ln)
    return "\n".join(out + deck) + "\n"


def apply(netlist: str, slot: str, part: Part) -> str:
    """The netlist with the generic `slot` device replaced by `part`."""
    if slot not in SLOTS:
        raise PartError(f"unknown part slot {slot!r}; use {sorted(SLOTS)}")
    role, generic, wiring = SLOTS[slot]
    ok = part.role == role or (slot == "switch" and part.kind == "subckt"
                               and len(part.pins) == 3) or (
        slot == "diode" and part.kind == "subckt" and len(part.pins) == 2)
    if not ok:
        raise PartError(f"{part.name} is a {part.role}; the {slot} slot needs a {role}"
                        + (" (P-channel)" if slot == "switch" else ""))
    lines, hit = [], False
    for ln in netlist.splitlines():
        t = ln.split()
        if t and t[-1].upper() == generic and t[0][:1].upper() in "XMD" and not \
                t[0].startswith("."):
            hit = True
            if part.kind == "subckt" and t[0][:1].upper() != "X":
                ln = " ".join(["X" + t[0], *t[1:-1], part.name])
            else:
                ln = " ".join([*t[:-1], part.name])
        lines.append(ln)
    if not hit:
        raise PartError(f"this template has no {slot} to replace")
    net = "\n".join(lines) + "\n"
    inc = [f'.include "{Path(part.file).as_posix()}"'] if part.kind == "subckt" \
        else [part.text]
    return _end_insert(net, inc, part.name)
