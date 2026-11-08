"""The parts list to order: CSV and Excel, written by finish_design.

One line per distinct part (same kind, value, tolerance and rating), with the quantity
and the reference designators, the way a purchasing BOM reads. Values come from the
finished, standard-value design; the "rating" column is the stress check's "buy at
least"; a part chosen by number (find_part) is named. A fixed value the optimiser did
not tune and that is off the standard series is flagged with the nearest standard one. Sources, loads (RL, CL) and
loop-test fixtures are the test bench, not board parts, and are left out.

The .xlsx is written with the standard library (it is a zip of XML files), so there is
no extra dependency.
"""
from __future__ import annotations

import csv
import re
import zipfile
from pathlib import Path
from xml.sax.saxutils import escape

from . import buildable, ir, sim, stress

KIND = {"r": "Resistor", "c": "Capacitor", "l": "Inductor", "d": "Diode",
        "m": "MOSFET", "q": "Transistor (BJT)", "x": "IC"}
UNIT = {"r": "Ω", "c": "F", "l": "H"}
STD = {"r": "E96", "c": "E24", "l": "E24"}     # what a fixed value should sit on
HEAD = ["Qty", "Reference", "Part", "Value", "Tolerance", "Rated at least", "Part number",
        "Notes"]
_BENCH = re.compile(r"^(r|c)(l|load)\d*$|^(lbrk|cinj)$", re.I)


def _eng(v: float, unit: str) -> str:
    for k, s in ((1e9, "G"), (1e6, "M"), (1e3, "k"), (1, ""), (1e-3, "m"), (1e-6, "µ"),
                 (1e-9, "n"), (1e-12, "p")):
        if abs(v) >= k * 0.9995:
            return f"{v / k:.3g} {s}{unit}"
    return f"{v:.3g} {unit}"


def rows(netlist: str, finish: dict, parts_chosen: dict | None = None) -> list[list]:
    """BOM lines for the first deck of a finished netlist (values already filled in)."""
    deck = sim.decks(netlist)[0]
    tol = {n.lower(): p.get("tolerance_pct") for n, p in (finish.get("parts") or {}).items()}
    series = {n.lower(): p.get("series") for n, p in (finish.get("parts") or {}).items()}
    buy = {n.lower(): p.get("buy") for n, p in
           ((finish.get("stress") or {}).get("parts") or {}).items()}
    named = {stress.REFDES.get(slot, "X1").lower(): part["name"]
             for slot, part in (parts_chosen or {}).items()}
    lines: dict[tuple, list[str]] = {}
    depth = 0
    for ln in deck.splitlines():
        t = ln.split()
        if not t or t[0].startswith(("*", ";", "+")):
            continue
        low = t[0].lower()
        if low == ".subckt":
            depth += 1
            continue
        if low == ".ends":
            depth -= 1
            continue
        k = low[0]
        if depth or k not in KIND or _BENCH.match(low):
            continue
        std = ""
        if k in UNIT:
            v = ir.parse_value(t[3]) if len(t) > 3 else None
            value = _eng(v, UNIT[k]) if v is not None else " ".join(t[3:])
            part = ""
            if v and low not in series:
                s_ = buildable.snap(v, STD[k])
                if abs(s_ / v - 1) > 0.001:
                    std = f"not a standard value: use {_eng(s_, UNIT[k])} ({STD[k]})"
        else:
            n = {"d": 2, "m": 3, "q": 3}.get(k)
            if k == "x":
                args = [a for a in t[1:] if "=" not in a]
                part = args[-1]
            else:
                part = t[n + 1] if len(t) > n + 1 else ""
            value = ""
        pn = named.get(low) or named.get("x" + low) or ""
        tl = tol.get(low)
        key = (KIND[k], value, part, f"±{tl:g}%" if tl else "", buy.get(low) or "", pn, std)
        lines.setdefault(key, []).append(t[0])
    out = []
    for (kind, value, part, tl, rating, pn, std), refs in lines.items():
        note = [std] if std else []
        if series.get(refs[0].lower()):
            note.append(f"{series[refs[0].lower()]} series")
        if part.upper().startswith("SPMCP_"):
            note.append("built-in model: choose a real part (find_part)")
        out.append([len(refs), " ".join(refs), kind, value or part, tl, rating, pn,
                    "; ".join(note)])
    return sorted(out, key=lambda r: (r[2], r[1]))


def write(rows_: list[list], folder: Path, stem: str = "bom") -> dict:
    folder.mkdir(parents=True, exist_ok=True)
    c = folder / f"{stem}.csv"
    with c.open("w", newline="", encoding="utf-8-sig") as f:   # BOM so Excel reads UTF-8
        w = csv.writer(f)
        w.writerow(HEAD)
        w.writerows(rows_)
    x = folder / f"{stem}.xlsx"
    _xlsx([HEAD] + rows_, x)
    return {"csv": str(c), "xlsx": str(x)}


def _col(i: int) -> str:
    s = ""
    i += 1
    while i:
        i, r = divmod(i - 1, 26)
        s = chr(65 + r) + s
    return s


def _xlsx(table: list[list], path: Path) -> None:
    """A one-sheet workbook: inline strings, numbers as numbers, bold header."""
    body = []
    for r, row in enumerate(table, 1):
        cells = []
        for c, v in enumerate(row):
            ref = f"{_col(c)}{r}"
            style = ' s="1"' if r == 1 else ""
            if isinstance(v, (int, float)) and r > 1:
                cells.append(f'<c r="{ref}"{style}><v>{v}</v></c>')
            else:
                cells.append(f'<c r="{ref}" t="inlineStr"{style}><is><t>'
                             f'{escape(str(v))}</t></is></c>')
        body.append(f'<row r="{r}">{"".join(cells)}</row>')
    widths = "".join(f'<col min="{i + 1}" max="{i + 1}" width="{w}" customWidth="1"/>'
                     for i, w in enumerate((6, 18, 16, 14, 11, 26, 16, 40)))
    sheet = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
             '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
             f'<cols>{widths}</cols><sheetData>{"".join(body)}</sheetData></worksheet>')
    ns = "http://schemas.openxmlformats.org"
    files = {
        "[Content_Types].xml":
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            f'<Types xmlns="{ns}/package/2006/content-types">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.'
            'relationships+xml"/><Default Extension="xml" ContentType="application/xml"/>'
            '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.'
            'openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
            '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.'
            'openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
            '<Override PartName="/xl/styles.xml" ContentType="application/vnd.'
            'openxmlformats-officedocument.spreadsheetml.styles+xml"/></Types>',
        "_rels/.rels":
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            f'<Relationships xmlns="{ns}/package/2006/relationships">'
            f'<Relationship Id="rId1" Type="{ns}/officeDocument/2006/relationships/'
            'officeDocument" Target="xl/workbook.xml"/></Relationships>',
        "xl/workbook.xml":
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            f'<workbook xmlns="{ns}/spreadsheetml/2006/main" '
            f'xmlns:r="{ns}/officeDocument/2006/relationships"><sheets>'
            '<sheet name="BOM" sheetId="1" r:id="rId1"/></sheets></workbook>',
        "xl/_rels/workbook.xml.rels":
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            f'<Relationships xmlns="{ns}/package/2006/relationships">'
            f'<Relationship Id="rId1" Type="{ns}/officeDocument/2006/relationships/'
            'worksheet" Target="worksheets/sheet1.xml"/>'
            f'<Relationship Id="rId2" Type="{ns}/officeDocument/2006/relationships/'
            'styles" Target="styles.xml"/></Relationships>',
        "xl/styles.xml":
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            f'<styleSheet xmlns="{ns}/spreadsheetml/2006/main">'
            '<fonts count="2"><font><sz val="11"/><name val="Calibri"/></font>'
            '<font><b/><sz val="11"/><name val="Calibri"/></font></fonts>'
            '<fills count="2"><fill><patternFill patternType="none"/></fill>'
            '<fill><patternFill patternType="gray125"/></fill></fills>'
            '<borders count="1"><border/></borders>'
            '<cellStyleXfs count="1"><xf/></cellStyleXfs>'
            '<cellXfs count="2"><xf fontId="0"/><xf fontId="1" applyFont="1"/></cellXfs>'
            '</styleSheet>',
        "xl/worksheets/sheet1.xml": sheet,
    }
    tmp = path.with_suffix(".part")
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as z:
        for name, text in files.items():
            z.writestr(name, text)
    tmp.replace(path)
