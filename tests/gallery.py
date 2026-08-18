"""Visual gallery: every schematic style and every plot type, as files you can look at.

Not a pytest test -- `test_render.py` and `test_plots.py` assert what an assert can hold
(valid file, deterministic bytes, no diagonal wires). Whether a style *looks* right is a
thing only eyes settle, so this writes the whole matrix to `gallery/` and an index.html
that puts the three styles side by side for comparison.

    python tests/gallery.py            # -> gallery/index.html

The schematics are LTspice-legal: every wire end lands on a real pin offset read from the
installed `.asy` files, so these are sheets LTspice would simulate, not just open.
"""

from __future__ import annotations

import logging
import sys
import webbrowser
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# The style font lists are fallback chains (Inter, then Helvetica, then DejaVu). On a box
# with none of the first choices installed matplotlib logs a line per miss per label,
# which buries the actual output. The fallback itself is working as intended.
logging.getLogger("matplotlib.font_manager").setLevel(logging.ERROR)

from spicemcp import asc, plots, raw, render      # noqa: E402

OUT = Path(__file__).resolve().parents[1] / "gallery"
FX = Path(__file__).parent / "fixtures"

# --- schematics ---------------------------------------------------------------
# res A(16,16) B(16,96) | cap A(16,0) B(16,64) | ind A(16,16) B(16,96)
# diode +(16,0) -(16,64) | voltage +(0,16) -(0,96)
# UniversalOpamp2 In+(-32,16) In-(-32,-16) V+(0,-32) V-(0,32) OUT(32,0)

RC = """Version 4
SHEET 1 880 680
WIRE 240 96 240 112
WIRE 240 192 240 240
WIRE 240 208 352 208
WIRE 240 304 240 336
FLAG 240 96 in
FLAG 352 208 out
FLAG 240 336 0
SYMBOL res 224 96 R0
SYMATTR InstName R1
SYMATTR Value 4k7
SYMBOL cap 224 240 R0
SYMATTR InstName C1
SYMATTR Value 100n
TEXT 88 424 Left 2 !.ac dec 100 1 1meg
TEXT 88 456 Left 2 ;RC low-pass, fc = 339 Hz
"""

# Non-inverting amp: rails, a coloured in/out/fb triple, two junction dots, pin names on
# a five-pin symbol, a load cap and two directives. The dense case for label declutter.
AMP = """Version 4
SHEET 1 1040 760
WIRE 128 256 368 256
WIRE 128 336 128 352
WIRE 368 224 304 224
WIRE 304 192 304 224
WIRE 304 224 304 304
WIRE 304 224 240 224
WIRE 304 384 304 416
WIRE 432 240 560 240
WIRE 480 112 480 240
WIRE 304 112 480 112
WIRE 400 176 400 208
WIRE 400 272 400 304
WIRE 560 240 560 272
WIRE 560 336 560 368
FLAG 128 256 in
FLAG 128 352 0
FLAG 240 224 fb
FLAG 304 416 0
FLAG 400 176 Vdd
FLAG 400 304 Vss
FLAG 560 240 out
FLAG 560 368 0
SYMBOL voltage 128 240 R0
SYMATTR InstName V1
SYMATTR Value SINE(0 100m 1k)
SYMBOL res 288 96 R0
SYMATTR InstName Rf
SYMATTR Value 90k
SYMBOL res 288 288 R0
SYMATTR InstName Rg
SYMATTR Value 10k
SYMBOL cap 544 272 R0
SYMATTR InstName CL
SYMATTR Value 47p
SYMBOL opamps\\UniversalOpamp2 400 240 R0
SYMATTR InstName U1
TEXT 120 500 Left 2 !.tran 5m
TEXT 120 532 Left 2 !.meas gain MAX V(out)
TEXT 120 564 Left 2 ;non-inverting, Av = 1 + Rf/Rg = 10
"""

# Every symbol class the renderer draws a curve for, in every orientation class. This is
# the sheet that catches a mirrored transistor or an inductor coiling the wrong way -- the
# failure modes that still look like a circuit. Laid out by measuring each instance's real
# bbox rather than by guessing coordinates: a rotated symbol's origin is not its corner,
# and hand-placed parts collide exactly where the picture needs to be readable.
SAMPLE_PARTS = [
    ("res", "R1", "1k", "R0"), ("res", "R2", "10k", "R90"),
    ("cap", "C1", "1u", "R0"), ("cap", "C2", "10n", "R90"),
    ("ind", "L1", "10m", "R0"), ("ind", "L2", "100u", "R270"),
    ("diode", "D1", "", "R0"), ("diode", "D2", "", "R180"),
    ("LED", "LED1", "", "R0"), ("sw", "SW1", "", "R0"),
    ("npn", "Q1", "", "R0"), ("pnp", "Q2", "", "M0"),
    ("nmos", "M1", "", "R0"), ("pmos", "M2", "", "M0"),
    ("voltage", "V1", "5", "R0"), ("current", "I1", "1m", "R0"),
    ("opamps\\UniversalOpamp2", "U1", "", "R0"),
    ("NoSuchVendorPart", "U9", "", "R0"),
]


def _sampler_asc(parts=SAMPLE_PARTS, per_row: int = 6, gap: int = 64,
                 label_pad: int = 72) -> str:
    """Pack the sampler onto a sheet with no overlaps, on LTspice's 16-unit grid."""
    def snap(v):
        return int(round(v / asc.GRID)) * asc.GRID

    rows, x, y, row_h, line = [], 96, 96, 0, []
    for i, (sym, inst, val, rot) in enumerate(parts):
        try:
            meta = asc.symbol(sym)
        except FileNotFoundError:                        # U9: the placeholder's own size
            meta = None
        p = asc.Placed(sym=sym, inst=inst, at=(0, 0), rot=rot, meta=meta)
        bx0, by0, bx1, by1 = p.bbox() if meta else (-32, -24, 32, 24)
        if i and i % per_row == 0:                       # new row
            rows += line
            line, x, y, row_h = [], 96, y + row_h + label_pad, 0
        at = (snap(x - bx0), snap(y - by0))
        line.append(f"SYMBOL {sym} {at[0]} {at[1]} {rot}")
        line.append(f"SYMATTR InstName {inst}")
        if val:
            line.append(f"SYMATTR Value {val}")
        x += (bx1 - bx0) + gap
        row_h = max(row_h, by1 - by0)
    rows += line
    return ("Version 4\nSHEET 1 1400 900\n" + "\n".join(rows)
            + f"\nTEXT 96 {y + row_h + 96} Left 2 "
              ";symbol + orientation sampler -- every curve comes from that part's .asy"
            + f"\nTEXT 96 {y + row_h + 128} Left 2 "
              ";U9 is deliberately unresolvable: a missing part is drawn as a labelled gap\n")


SHEETS = {"rc_lowpass": RC, "noninv_amp": AMP, "parts_sampler": _sampler_asc()}


def schematics() -> list[tuple[str, str, dict]]:
    """Render every sheet in every style, PNG and SVG. Returns (sheet, style, result)."""
    src_dir = OUT / "asc"
    src_dir.mkdir(parents=True, exist_ok=True)
    rows = []
    for name, text in SHEETS.items():
        f = src_dir / f"{name}.asc"
        f.write_text(text, encoding="utf-8")
        for style in sorted(render.STYLES):
            res = render.render(f, str(OUT / f"{name}__{style}.png"), style, dpi=150)
            render.render(f, str(OUT / f"{name}__{style}.svg"), style)
            rows.append((name, style, res))
            print(f"  {name:14s} {style:13s} "
                  f"{res['size_inches'][0]}x{res['size_inches'][1]}in  "
                  f"{res['components']} parts, {res['primitives']} prims"
                  + (f"  UNRESOLVED {res['unresolved_symbols']}"
                     if res["unresolved_symbols"] else ""))
    # Annotations are off by default, so they need their own frame in the gallery.
    render.render(src_dir / "noninv_amp.asc", str(OUT / "noninv_amp__annotated.png"),
                  "tech_minimal", dpi=150,
                  annotations={"in": "100 mV, 1 kHz", "Rf": "sets the gain",
                               "out": "1 V pk"})
    return rows


def graphs() -> list[tuple[str, dict]]:
    """One of every plot type, off the checked-in `.raw` fixtures."""
    rows = [
        ("bode (ac_loop: PM/GM annotated)",
         plots.plot_bode(FX / "ac_loop.raw", out=OUT / "plot_bode.png")),
        ("bode (rc_ac: -3 dB, no gain margin)",
         plots.plot_bode(FX / "rc_ac.raw", out=OUT / "plot_bode_rc.png")),
        ("transient (rlc_tran: overshoot + settling bands)",
         plots.plot_transient(FX / "rlc_tran.raw", out=OUT / "plot_tran.png")),
        ("transient (rc_tran: clean exponential)",
         plots.plot_transient(FX / "rc_tran.raw", out=OUT / "plot_tran_rc.png")),
        ("THD (thd_tran: harmonic bars in dBc)",
         plots.plot_thd(FX / "thd_tran.raw", out=OUT / "plot_thd.png")),
    ]
    # The histogram takes numbers, not a waveform. mc_op sweeps `run` 1..30, so the
    # samples are one vector, not one wave per step. The spec is set to fail a few units
    # on purpose -- a 100%-yield histogram shows neither limit doing any work.
    vals = [abs(v) for v in raw.read_raw(FX / "mc_op.raw")[0].signal("V(out)")[1]]
    mu = sum(vals) / len(vals)
    rows.append(("histogram (mc_op: yield vs spec)",
                 plots.plot_histogram(vals, out=OUT / "plot_hist.png", label="V(out)",
                                      spec=(mu * 0.994, mu * 1.006))))
    for label, r in rows:
        print(f"  {label:46s} -> {Path(r['plot_path']).name}")
    return rows


def index(sheet_rows, plot_rows) -> Path:
    """One page, styles side by side. Plain <img> tags -- no build step, no dependency."""
    styles = sorted(render.STYLES)
    h = ["<!doctype html><meta charset=utf-8><title>spicemcp gallery</title>",
         "<style>body{font:14px/1.5 system-ui;margin:32px;background:#fbfbfa;"
         "color:#222}h2{margin:36px 0 4px}img{max-width:100%;border:1px solid #ddd;"
         "background:#fff}table{border-collapse:collapse;width:100%}"
         "td{vertical-align:top;padding:6px;width:33%}th{font:600 13px system-ui;"
         "text-align:left;padding:6px}code{background:#eee;padding:1px 4px}</style>",
         "<h1>spicemcp visual gallery</h1>",
         f"<p>{len(SHEETS)} schematics &times; {len(styles)} styles, "
         f"{len(plot_rows)} plots. PNG shown; an <code>.svg</code> of every frame sits "
         f"beside it in this folder.</p>"]
    for name in SHEETS:
        h.append(f"<h2>{name}</h2><table><tr>"
                 + "".join(f"<th>{s}</th>" for s in styles) + "</tr><tr>"
                 + "".join(f'<td><a href="{name}__{s}.svg">'
                           f'<img src="{name}__{s}.png"></a></td>' for s in styles)
                 + "</tr></table>")
    h.append('<h2>annotations (tech_minimal)</h2>'
             '<img src="noninv_amp__annotated.png" style="max-width:60%">')
    h.append("<h2>plots</h2>")
    for label, r in plot_rows:
        h.append(f'<p><b>{label}</b><br>'
                 f'<img src="{Path(r["plot_path"]).name}" style="max-width:70%"></p>')
    p = OUT / "index.html"
    p.write_text("\n".join(h), encoding="utf-8")
    return p


if __name__ == "__main__":
    if asc.find_sym_dir() is None:
        raise SystemExit("no LTspice symbol library found -- schematics need the .asy files")
    OUT.mkdir(parents=True, exist_ok=True)
    print("schematics:")
    sheets = schematics()
    print("plots:")
    ps = graphs()
    page = index(sheets, ps)
    print(f"\n{page}")
    if "--open" in sys.argv:
        webbrowser.open(page.as_uri())
