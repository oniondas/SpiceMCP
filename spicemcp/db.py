"""Persistent optimization state. SQLite is the authoritative source of truth.

Design note (spec S5): best_candidate / best_feasible_candidate are DERIVED by
query, never stored. A worse iteration therefore *cannot* overwrite the best --
the failure mode the spec guards against is structurally impossible here rather
than merely policed.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

STATE_DIR = ".ltspice-mcp"

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id            TEXT PRIMARY KEY,
    created_at        TEXT NOT NULL,
    status            TEXT NOT NULL,          -- running | stopped | finished
    netlist_template  TEXT NOT NULL,
    param_space       TEXT NOT NULL,          -- {name: {min, max, step?, values?}}
    objectives        TEXT NOT NULL,          -- [{metric, direction, weight, ref}]
    constraints       TEXT NOT NULL,          -- [{metric, op, value}]
    sim_config        TEXT NOT NULL,
    seed              INTEGER NOT NULL,
    current_candidate TEXT,
    notes             TEXT
);

CREATE TABLE IF NOT EXISTS designs (
    candidate_id TEXT PRIMARY KEY,
    run_id       TEXT NOT NULL REFERENCES runs(run_id),
    parent_id    TEXT,
    generation   INTEGER NOT NULL,
    params       TEXT NOT NULL,
    netlist      TEXT NOT NULL,               -- fully substituted, byte-exact
    topology_fp  TEXT NOT NULL,
    design_fp    TEXT NOT NULL,
    origin       TEXT NOT NULL,               -- seed | engine | llm
    created_at   TEXT NOT NULL,
    UNIQUE (run_id, design_fp)
);

CREATE TABLE IF NOT EXISTS experiments (
    experiment_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id             TEXT NOT NULL REFERENCES runs(run_id),
    candidate_id       TEXT NOT NULL REFERENCES designs(candidate_id),
    iteration          INTEGER NOT NULL,
    sim_config         TEXT NOT NULL,
    sim_config_fp      TEXT NOT NULL,
    experiment_fp      TEXT NOT NULL,         -- design_fp + sim_config_fp
    status             TEXT NOT NULL,         -- ok | failed
    metrics            TEXT NOT NULL,
    constraint_results TEXT NOT NULL,
    feasible           INTEGER NOT NULL,
    score              REAL,
    failure_type       TEXT,
    failure_message    TEXT,
    failure_detail     TEXT,
    warnings           TEXT,
    log_path           TEXT,
    started_at         TEXT NOT NULL,
    finished_at        TEXT NOT NULL
);
-- Deliberately NOT unique on experiment_fp: a forced re-run appends a row rather
-- than rewriting one, so history stays immutable (spec S4). Dedup is a lookup on
-- this index, not a constraint.
CREATE INDEX IF NOT EXISTS ix_exp_fp ON experiments(run_id, experiment_fp);

CREATE TABLE IF NOT EXISTS sensitivities (
    run_id     TEXT NOT NULL,
    parameter  TEXT NOT NULL,
    metric     TEXT NOT NULL,
    slope      REAL NOT NULL,
    r2         REAL,
    n          INTEGER NOT NULL,
    method     TEXT NOT NULL,
    confidence REAL NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (run_id, parameter, metric)
);

CREATE INDEX IF NOT EXISTS ix_exp_run  ON experiments(run_id, iteration);
CREATE INDEX IF NOT EXISTS ix_exp_cand ON experiments(candidate_id);
CREATE INDEX IF NOT EXISTS ix_des_run  ON designs(run_id, generation);
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def state_root(project_dir: str | Path) -> Path:
    """`.ltspice-mcp/` layout from spec S1."""
    root = Path(project_dir).expanduser().resolve() / STATE_DIR
    for sub in ("cache", "candidates", "simulations", "reports"):
        (root / sub).mkdir(parents=True, exist_ok=True)
    return root


def connect(project_dir: str | Path) -> sqlite3.Connection:
    root = state_root(project_dir)
    con = sqlite3.connect(root / "state.db", timeout=30, isolation_level=None)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA foreign_keys=ON")
    con.executescript(SCHEMA)
    return con


@contextmanager
def transaction(con: sqlite3.Connection):
    """Spec S16: an iteration commits fully or not at all."""
    con.execute("BEGIN IMMEDIATE")
    try:
        yield con
    except Exception:
        con.execute("ROLLBACK")
        raise
    else:
        con.execute("COMMIT")


def next_candidate_id(con: sqlite3.Connection, run_id: str) -> str:
    n = con.execute(
        "SELECT COUNT(*) FROM designs WHERE run_id = ?", (run_id,)
    ).fetchone()[0]
    return f"cand_{n + 1:04d}"


def next_iteration(con: sqlite3.Connection, run_id: str) -> int:
    n = con.execute(
        "SELECT COALESCE(MAX(iteration), 0) FROM experiments WHERE run_id = ?", (run_id,)
    ).fetchone()[0]
    return n + 1


def jloads(row: sqlite3.Row, key: str, default=None):
    raw = row[key]
    return json.loads(raw) if raw else (default if default is not None else {})


def jdumps(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"))
