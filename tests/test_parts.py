"""Real parts by part number: the library index, find_part, and parts in templates.

The fixture library (tests/fixtures/lib) is laid out like LTspice's own: one-line
.model cards with datasheet ratings in cmp/standard.*, op-amp subcircuits in sub/."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from spicemcp import db, parts as P, server, sim, stress, topologies as T

LIB = Path(__file__).parent / "fixtures" / "lib"
needs_ngspice = pytest.mark.skipif(not sim.find_ngspice(), reason="ngspice not installed")


@pytest.fixture(autouse=True)
def lib(tmp_path, monkeypatch):
    monkeypatch.setenv("SPICEMCP_LIB_DIRS", str(LIB))
    monkeypatch.setenv("SPICEMCP_PARTS_CACHE", str(tmp_path / "idx.json"))
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)


@pytest.fixture()
def project(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "PROJECT", str(tmp_path))
    monkeypatch.setattr(server, "_con", db.connect(tmp_path))
    return tmp_path


def test_the_index_reads_models_with_ratings_and_subcircuits_with_pins():
    d = P.get("1N5819")
    assert d.role == "diode" and d.ratings == {"vpk": 40, "iave": 1, "mfg": "Diodes"}
    m = P.get("si4427bdy")                     # continuation line, case-insensitive
    assert m.role == "pmos" and m.ratings["vds"] == -30 and m.ratings["ron"] == pytest.approx(0.02)
    oa = P.get("TOA10")
    assert oa.role == "opamp" and oa.pins == ["inp", "inn", "vp", "vn", "out"]
    assert P.get("Si7336ADP").role == "nmos"


def test_find_part_ranks_exact_first_filters_by_kind_and_says_where_it_looked():
    out = server.find_part("TOA1")
    assert [m["name"] for m in out["matches"]] == ["TOA1", "TOA10"]
    assert server.find_part("si", "pmos")["matches"][0]["is"] == "pmos"
    assert {m["name"] for m in server.find_part("vishay")["matches"]} == {
        "Si7336ADP", "Si4427BDY"}
    assert str(LIB.resolve()) in out["searched"]
    assert "40 V, 1 A" in server.find_part("1N5819")["matches"][0]["rated"]


def test_the_index_is_cached_until_a_file_changes(tmp_path, monkeypatch):
    lib = tmp_path / "lib"
    lib.mkdir()
    f = lib / "a.lib"
    f.write_text(".model DA D(Is=1n)\n")
    assert [p.name for p in P.index([lib])] == ["DA"]
    f.write_text(".model DA D(Is=1n)\n.model DB D(Is=2n)\n")
    assert [p.name for p in P.index([lib])] == ["DA", "DB"]


def test_an_unknown_part_or_wrong_kind_is_refused_with_a_fix(project):
    out = server.start_from_template("x", "sallen_key_lowpass", {"fc": 1e4},
                                     parts={"opamp": "NOPE123"})
    assert "not found" in out["error"] and "find_part" in out["fix"]
    out = server.start_from_template("y", "buck", {"vin": 12, "vout": 3.3, "iout": 1},
                                     parts={"switch": "Si7336ADP"})
    assert "needs a pmos" in out["error"]
    out = server.start_from_template("z", "rc_lowpass", {"fc": 1e3}, parts={"opamp": "TOA1"})
    assert "no opamp slot" in out["error"]


def test_a_part_replaces_the_generic_device_in_every_deck():
    d = T.get("buck").design({"vin": 12, "vout": 3.3, "iout": 1})
    net = P.apply(d.netlist, "diode", P.get("1N5819"))
    net = P.apply(net, "switch", P.get("Si4427BDY"))
    assert "D1 0 sw 1N5819" in net and "M1 sw g in Si4427BDY" in net
    assert net.count(".model 1N5819 D(") == 1          # only the switching deck uses it
    d = T.get("sallen_key_lowpass").design({"fc": 1e4})
    net = P.apply(d.netlist, "opamp", P.get("TOA10"))
    assert net.count(" TOA10\n") == 3 and net.count(".include") == 3
    assert "parts" in T.get("buck").describe()


def test_ngspice_drops_the_ratings_ltspice_keeps_on_a_model_card():
    ng = sim.to_ngspice("* t\n.model 1N5819 D(Is=31.7u Iave=1 Vpk=40 mfg=Diodes "
                        "type=Schottky)\n.op\n.end\n")
    assert ".model 1N5819 D(Is=31.7u)" in ng


def test_a_chosen_part_is_checked_against_its_ratings():
    st = {"parts": {"D1": {"reverse_v": 12.6, "avg_a": 0.72, "peak_a": 1.2},
                    "M1": {"vds_v": 12.6, "peak_a": 1.2}}}
    out = stress.against_ratings(st, {"diode": ("1N5819", {"vpk": 40, "iave": 1}),
                                      "switch": ("Si4427BDY", {"vds": -30, "ron": 0.02})})
    assert out["D1 1N5819"].startswith("too small: average current needs 1.08 A")
    assert out["M1 Si4427BDY"].startswith("ok: Vds needs 15.8 V, rated 30 V")
    assert "conduction loss" in out["M1 Si4427BDY"]


@needs_ngspice
def test_sallen_key_runs_on_a_library_op_amp(project):
    out = server.start_from_template("sk", "sallen_key_lowpass", {"fc": 1e4},
                                     server.SimOptions(backend="ngspice"),
                                     parts={"opamp": "TOA10"})
    assert out["parts"]["opamp"]["name"] == "TOA10", out
    assert out["start_result"]["status"] == "ok"
    assert out["start_result"]["metrics"]["f3db"] == pytest.approx(1e4, rel=0.05)
    det = json.loads(server._details_path("sk").read_text())
    assert any("TOA10" in ln for ln in det["hand_calculation"])
