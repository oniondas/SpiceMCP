"""The parts list to order: grouped lines, ratings, chosen part numbers, CSV and xlsx."""
from __future__ import annotations

import csv
import zipfile

from spicemcp import bom

NET = """* t
V1 in 0 5
R1 in a 10k
R2 a 0 10k
C1 a 0 100n
RL a 0 1k
D1 a k DS
X1 a b vcc vee out SPMCP_OA
.subckt SPMCP_OA a b c d e
R9 a b 1k
.ends
.model DS D
.op
.end
* second deck, ignored
R7 a 0 1
.end
"""
FIN = {"parts": {"R1": {"tolerance_pct": 1, "series": "E96"},
                 "R2": {"tolerance_pct": 1, "series": "E96"},
                 "C1": {"tolerance_pct": 5, "series": "E12"}},
       "stress": {"parts": {"R1": {"buy": "0.063 W"}, "R2": {"buy": "0.063 W"},
                            "C1": {"buy": "10 V"}, "D1": {"buy": "20 V, 1 A"}}}}


def test_lines_group_identical_parts_and_leave_out_the_test_bench():
    rows = bom.rows(NET, FIN, {"diode": {"name": "1N5819"}})
    by = {r[1]: r for r in rows}
    assert set(by) == {"R1 R2", "C1", "D1", "X1"}             # RL, V1, R9, R7 left out
    assert by["R1 R2"][0] == 2 and by["R1 R2"][3] == "10 kΩ" and by["R1 R2"][4] == "±1%"
    assert by["C1"][3] == "100 nF" and by["C1"][5] == "10 V"
    assert by["D1"][6] == "1N5819" and by["D1"][5] == "20 V, 1 A"
    assert "find_part" in by["X1"][7]


def test_csv_and_xlsx_are_written(tmp_path):
    out = bom.write(bom.rows(NET, FIN), tmp_path)
    with open(out["csv"], encoding="utf-8-sig") as f:
        table = list(csv.reader(f))
    assert table[0] == bom.HEAD and len(table) == 5
    z = zipfile.ZipFile(out["xlsx"])
    assert z.testzip() is None and "xl/worksheets/sheet1.xml" in z.namelist()
    sheet = z.read("xl/worksheets/sheet1.xml").decode()
    assert "10 kΩ" in sheet and '<c r="A2"><v>1</v></c>' in sheet.replace(' s="1"', "")
    assert bom._col(0) == "A" and bom._col(25) == "Z" and bom._col(26) == "AA"


def test_a_fixed_value_off_the_standard_series_is_flagged():
    net = "* t\nV1 in 0 5\nR1 in a 6.63k\nR2 a 0 10k\nC1 a 0 13.1u\n.op\n.end\n"
    notes = {r[1]: r[7] for r in bom.rows(net, {"parts": {"R2": {"tolerance_pct": 1}}})}
    assert "6.65 kΩ (E96)" in notes["R1"] and "13 µF (E24)" in notes["C1"]
    assert notes["R2"] == ""
