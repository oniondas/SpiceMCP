"""Checks for the schematic renderer.

Two things are worth testing about a picture: that it is a real file of the format it
claims, and that it is the *same* picture next time. The rest -- whether it looks
professional -- is not a thing an assert can hold, so the visual spec is checked here
only where it is mechanical: no diagonal wires reach the output, the ink is the style's
ink, and nothing is cropped.

The fixture is a real `.asc`, written to disk and parsed by the real parser. Mocking the
parse would test the emitters against geometry LTspice never produces.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET

import pytest

from spicemcp import asc, render

needs_syms = pytest.mark.skipif(asc.find_sym_dir() is None,
                                reason="LTspice symbol library not installed")

# A real RC low-pass: a ground, two net labels, a T-junction, a directive and a comment
# -- one of every element class the renderer draws, small enough to reason about. The wire
# ends land on the symbols' actual pins (`res` at +16,+16 / +16,+96; `cap` at +16,+0 /
# +16,+64), so this is a schematic LTspice would simulate, not just one it would open.
SRC = """Version 4
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
TEXT 88 424 Left 2 !.tran 1m
TEXT 88 456 Left 2 ;an RC divider
"""


@pytest.fixture
def src(tmp_path):
    f = tmp_path / "rc.asc"
    f.write_text(SRC, encoding="utf-8")
    return f


ALL_STYLES = sorted(render.STYLES)


# --- style-free geometry -------------------------------------------------------

@needs_syms
def test_no_primitive_is_a_diagonal_wire(src):
    """The renderer may not invent a diagonal even if one somehow reached it: a diagonal
    LTspice wire is not a connection, so drawing one would picture a circuit that is
    not the file's."""
    for p in render.build(asc.parse_asc(src)):
        if p.role in ("wire", "netstub"):
            xs, ys = p.pts[0::2], p.pts[1::2]
            for i in range(len(xs) - 1):
                assert xs[i] == xs[i + 1] or ys[i] == ys[i + 1], p


@needs_syms
def test_all_three_styles_share_one_geometry(src):
    """A style changes appearance, never the circuit. Sketch alone perturbs the drawn
    line, and even then only between the exact endpoints."""
    sch = asc.parse_asc(src)
    base = render.build(sch)
    wires = {p.pts for p in base if p.role == "wire"}
    for name in ALL_STYLES:
        st = render.style(name)
        got = render._apply_style_geometry(base, st)
        ends = {(p.pts[0], p.pts[1], p.pts[-2], p.pts[-1])
                for p in got if p.role == "wire"}
        assert ends == {(w[0], w[1], w[-2], w[-1]) for w in wires}, name


@needs_syms
def test_canvas_encloses_every_label(src):
    prims = render.declutter(render.build(asc.parse_asc(src)))
    x0, y0, w, h = render.canvas(prims)
    for p in prims:
        a, b, c, d = p.extent()
        assert x0 <= a and b >= y0 and c <= x0 + w and d <= y0 + h, p


def test_a_quarter_turned_label_is_boxed_and_emitted_turned():
    """A vertical pin name is taller than it is wide. Boxing it the other way up lets the
    canvas crop it and lets `declutter` call a real collision clear."""
    flat = render._text("pinname", (0, 0), "MaxPower1")
    up = render._text("pinname", (0, 0), "MaxPower1", rot=90)
    fw, fh = flat.extent()[2] - flat.extent()[0], flat.extent()[3] - flat.extent()[1]
    uw, uh = up.extent()[2] - up.extent()[0], up.extent()[3] - up.extent()[1]
    assert (round(uw, 6), round(uh, 6)) == (round(fh, 6), round(fw, 6))
    assert up.extent()[3] <= 0 and up.extent()[1] < 0        # climbs out of the anchor
    assert render._text("pinname", (0, 0), "X", ha="right", rot=90).extent()[1] >= 0

    r = {"c": "#252A2E"}
    assert 'transform="rotate(-90.00 0.00 0.00)"' in render._svg_text(up, r)
    assert "transform" not in render._svg_text(flat, r)


# --- output files -------------------------------------------------------------

@needs_syms
@pytest.mark.parametrize("style", ALL_STYLES)
def test_svg_is_valid_xml_and_carries_the_style_ink(src, tmp_path, style):
    out = tmp_path / f"{style}.svg"
    res = render.render(src, str(out), style)
    assert res["success"] and res["format"] == "svg" and res["style"] == style
    assert out.is_file() and out.stat().st_size > 500

    root = ET.parse(out).getroot()                     # raises if not valid XML
    assert root.tag.endswith("svg") and root.get("viewBox")
    body = out.read_text(encoding="utf-8")
    assert render.style(style)["roles"]["wire"]["c"].lower() in body.lower()
    assert render.style(style)["bg"].lower() in body.lower()
    for txt in ("R1", "4k7", "C1", "100n", "out", ".tran 1m"):
        assert f">{txt}<" in body, txt


@needs_syms
@pytest.mark.parametrize("style", ALL_STYLES)
def test_png_is_a_real_png_of_the_right_size(src, tmp_path, style):
    pytest.importorskip("matplotlib")
    # Pillow ships as a hard dependency of matplotlib, but skip on it explicitly rather
    # than error out if that ever stops being true.
    Image = pytest.importorskip("PIL.Image")
    out = tmp_path / f"{style}.png"
    res = render.render(src, str(out), style, dpi=150)
    assert res["success"] and res["format"] == "png"
    assert out.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"

    with Image.open(out) as im:                        # opening is the real check
        assert im.format == "PNG"
        assert abs(im.width - res["size_inches"][0] * 150) <= 2
        assert im.height > 200


@needs_syms
def test_dpi_scales_the_png_not_the_drawing(src, tmp_path):
    pytest.importorskip("matplotlib")
    a = render.render(src, str(tmp_path / "a.png"), dpi=100)
    b = render.render(src, str(tmp_path / "b.png"), dpi=200)
    assert a["size_units"] == b["size_units"]          # same schematic
    assert (tmp_path / "b.png").stat().st_size > (tmp_path / "a.png").stat().st_size


@needs_syms
def test_the_reported_counts_are_the_schematic(src, tmp_path):
    res = render.render(src, str(tmp_path / "x.svg"))
    sch = asc.parse_asc(src)
    assert res["components"] == len(sch.parts) == 2
    assert res["wires"] == len(sch.wires) == 4
    assert res["labels"] == 2 and res["grounds"] == 1
    assert res["junctions"] == 1 and res["texts"] == 2
    assert res["unresolved_symbols"] == [] and res["warnings"] == []


@needs_syms
def test_rendering_is_deterministic(src, tmp_path):
    """Same file, same style, same bytes -- including `sketch`, whose imperfect line is
    hashed from the geometry rather than drawn from an RNG."""
    for style in ALL_STYLES:
        one = tmp_path / f"1{style}.svg"
        two = tmp_path / f"2{style}.svg"
        render.render(src, str(one), style)
        render.render(src, str(two), style)
        assert one.read_bytes() == two.read_bytes(), style


# --- failures are reported, not swallowed -------------------------------------

def test_a_missing_file_is_an_error_with_a_reason(tmp_path):
    with pytest.raises(render.RenderError, match="no such schematic"):
        render.render(tmp_path / "nope.asc", str(tmp_path / "o.svg"))


@needs_syms
def test_unsupported_format_and_style_and_dpi_are_refused(src, tmp_path):
    with pytest.raises(render.RenderError, match="format"):
        render.render(src, str(tmp_path / "o.pdf"))
    with pytest.raises(render.RenderError, match="unknown style"):
        render.render(src, str(tmp_path / "o.svg"), "neon_gradient")
    with pytest.raises(render.RenderError, match="dpi"):
        render.render(src, str(tmp_path / "o.png"), dpi=4)


@needs_syms
def test_an_unresolvable_symbol_still_renders_and_says_so(tmp_path):
    f = tmp_path / "vendor.asc"
    f.write_text("Version 4\nSHEET 1 880 680\nWIRE 0 0 64 0\n"
                 "SYMBOL NoSuchPart$$ 100 100 R0\nSYMATTR InstName U1\n",
                 encoding="utf-8")
    res = render.render(f, str(tmp_path / "v.svg"))
    assert res["success"] and res["unresolved_symbols"] == ["NoSuchPart$$"]
    assert res["warnings"] and "U1" in (tmp_path / "v.svg").read_text(encoding="utf-8")


# --- the MCP tool's contract --------------------------------------------------

@needs_syms
def test_the_tool_returns_the_documented_shape(src, tmp_path):
    from spicemcp import server                        # noqa: PLC0415
    ok = server.render_schematic(str(src), str(tmp_path / "t.svg"))
    assert ok["success"] is True and ok["format"] == "svg"
    assert ok["style"] == "tech_minimal"
    assert all(isinstance(ok[k], int) for k in ("components", "wires", "labels"))

    bad = server.render_schematic(str(tmp_path / "gone.asc"), str(tmp_path / "t.svg"))
    assert bad["success"] is False and bad["output_path"] is None
    assert bad["fix"] and bad["example"]["asc_path"]
    assert "no such schematic" in bad["error"]
