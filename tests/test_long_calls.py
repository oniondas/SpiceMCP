"""Phase 1: long calls report progress and return in time, simulations run side by side,
built-in blocks are included by name, and ngspice runs sweeps."""
from __future__ import annotations

import asyncio
import json
import shutil
import tempfile
import time
from pathlib import Path

import pytest

from spicemcp import db, opamp, progress, server, sim

needs_ngspice = pytest.mark.skipif(not shutil.which("ngspice"), reason="ngspice not installed")


@sim.register_analytic("slow_rc")
def _slow_rc(p: dict) -> dict:
    time.sleep(0.15)
    fc = 1 / (2 * 3.141592653589793 * p["R1"] * p["C1"])
    return {"fc": fc}


NET = ("V1 in 0 AC 1\nR1 in out {R1}\nC1 out 0 {C1}\n.ac dec 20 10 1Meg\n"
       ".meas AC fc WHEN mag(V(out))=0.7071 FALL=1\n.end\n")


@pytest.fixture()
def project(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "PROJECT", str(tmp_path))
    monkeypatch.setattr(server, "_con", db.connect(tmp_path))
    server.JOBS.clear()
    return tmp_path


def _call(name, **args):
    return json.loads(server.answer_text(asyncio.run(server.mcp.call_tool(name, args))))


def _run(rid):
    out = _call("start_optimization", run_id=rid, netlist=NET,
                params=[{"name": "R1", "min": 1e3, "max": 1e5},
                        {"name": "C1", "min": 1e-10, "max": 1e-7}],
                goals=[{"metric": "fc", "direction": "max"}],
                limits=[{"metric": "fc", "min": 950, "max": 1050}],
                sim_options={"backend": "analytic", "model": "slow_rc"})
    assert "error" not in out, out


def test_parallel_map_keeps_order_and_runs_side_by_side(monkeypatch):
    monkeypatch.setenv("SPICEMCP_WORKERS", "4")
    t = time.monotonic()
    out = sim.parallel_map(lambda x: (time.sleep(0.2), x * x)[1], range(8))
    assert out == [x * x for x in range(8)]
    assert time.monotonic() - t < 1.2               # 8 x 0.2 s serial would be 1.6 s


def test_run_optimization_stops_at_the_time_limit_and_says_to_continue(project,
                                                                      monkeypatch):
    monkeypatch.setenv("SPICEMCP_CALL_BUDGET_S", "0.5")
    _run("rc")
    t = time.monotonic()
    out = _call("run_optimization", run_id="rc", iterations=200)
    assert time.monotonic() - t < 5
    assert 1 <= out["iterations_run"] < 200 and "stopped_early" in out
    assert "again to continue" in out["next_step"]
    more = _call("run_optimization", run_id="rc", iterations=3)
    assert more["iterations_evaluated"] > out["iterations_evaluated"]


def test_finish_design_keeps_working_after_the_call_returns(project, monkeypatch):
    monkeypatch.setenv("SPICEMCP_CALL_BUDGET_S", "0.5")
    monkeypatch.setenv("SPICEMCP_WORKERS", "1")
    _run("rc")
    from spicemcp import engine
    engine.evaluate(server.con(), "rc", {"R1": 15915.494, "C1": 1e-8}, None, None, False,
                    "llm")
    out = _call("finish_design", run_id="rc", yield_runs=20)
    assert out["status"] == "still running" and "again" in out["next_step"]
    for _ in range(200):
        out = _call("finish_design", run_id="rc")    # inputs of the running job apply
        if out.get("status") != "still running":
            break
    assert out["meets_all_limits"] and out["yield"]["runs"] == 20, out
    assert not server.JOBS


def test_tracker_reports_progress_and_deadline():
    t = progress.Tracker(budget_s=0.01)
    progress.install(t)
    try:
        progress.report(3, 10, "three")
        time.sleep(0.02)
        assert progress.out_of_time()
        assert t.snapshot() == {"done": 3, "total": 10, "message": "three"}
    finally:
        progress.install(None)
    assert not progress.out_of_time()


def test_built_in_blocks_are_added_by_name_once():
    n = "* t\nX1 a b vcc 0 out SPMCP_OA\nX2 c d vcc 0 g spmcp_cmp\nX3 e f vcc 0 h SPMCP_OA\n.op\n.end\n"
    full = opamp.include_blocks(n)
    assert full.lower().count(".subckt spmcp_oa") == 1
    assert full.lower().count(".subckt spmcp_cmp") == 1
    assert full.rstrip().endswith(".end") and "Ib pb vss" in full
    assert opamp.include_blocks(full) == full            # already defined: untouched


@needs_ngspice
def test_an_op_amp_by_name_simulates_without_its_subcircuit():
    n = ("* follower\nVcc vcc 0 5\nVee vee 0 -5\nVin inp 0 DC 0 AC 1\n"
         "X1 inp out vcc vee out SPMCP_OA\nCL out 0 10p\n.ac dec 20 1 100Meg\n"
         ".meas AC ugf WHEN mag(V(out))=0.7071 FALL=1\n.end\n")
    r = sim.simulate(n, {}, {"backend": "ngspice"}, Path(tempfile.mkdtemp()), "oa")
    assert r.status == "ok", r.failure_message
    assert 1e6 < r.metrics["ugf"] < 5e6                  # 2 MHz GBW follower


@needs_ngspice
def test_ngspice_runs_a_sweep_one_point_per_simulation():
    n = ("* rc\nV1 in 0 AC 1\nR1 in out 1k\nC1 out 0 100n\n.ac dec 20 10 1Meg\n"
         ".meas AC fc WHEN mag(V(out))=0.7071 FALL=1\n.end\n")
    r = sim.simulate_sweep(n, "R1", [1e3, 2e3], Path(tempfile.mkdtemp()), "s",
                           {"backend": "ngspice"})
    assert r["status"] == "ok" and r["complete"]
    fc = [row["metrics"]["fc"] for row in r["metrics_table"]]
    assert fc[0] == pytest.approx(1591.5, rel=0.01) and fc[1] == pytest.approx(795.8, rel=0.01)
