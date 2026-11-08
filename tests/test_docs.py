"""Shipped PDFs must open. v0.13.0 shipped a report that was two builds written over
each other: it had a valid head and tail, but its cross-reference table pointed into the
middle of the other copy. This checks the structure a PDF reader relies on first."""
from __future__ import annotations

import re
from pathlib import Path

import pytest

DOCS = Path(__file__).parents[1] / "docs"
PDFS = sorted(DOCS.glob("*/*.pdf"))


@pytest.mark.parametrize("pdf", PDFS, ids=[p.name for p in PDFS])
def test_pdf_has_one_body_and_a_valid_cross_reference_table(pdf):
    b = pdf.read_bytes()
    assert b.startswith(b"%PDF-"), "no PDF header"
    assert b.count(b"%PDF-") == 1, "two PDF headers: two files written into one"
    assert b.rstrip().endswith(b"%%EOF")
    start = int(re.findall(rb"startxref\s+(\d+)", b)[-1])
    assert b[start:start + 4] == b"xref", "startxref does not point at the xref table"
    # every object offset in the table must point at that object's header
    m = re.match(rb"xref\s+0\s+(\d+)\s+", b[start:])
    n = int(m.group(1))
    rows = b[start + m.end():start + m.end() + 20 * n].split(b"\n")
    for i, row in enumerate(rows[:n]):
        off, _, kind = row.split()[:3]
        if kind == b"n":
            assert re.match(rb"\d+ 0 obj", b[int(off):int(off) + 12]), (pdf.name, i)


def test_the_shipped_pdfs_are_found():
    assert {p.name for p in PDFS} >= {"SpiceMCP_Engineering_Report.pdf",
                                      "SpiceMCP_Circuit_Catalog.pdf"}
