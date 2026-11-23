from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
import sqlite3
from typing import Iterator


SCHEMA = """
CREATE TABLE IF NOT EXISTS trial (
    trial_id TEXT PRIMARY KEY,
    design TEXT NOT NULL,
    version INTEGER NOT NULL,
    phase TEXT NOT NULL CHECK (phase IN ('draft','open','closed')),
    close_cut INTEGER CHECK (close_cut >= 0)
);
CREATE TABLE IF NOT EXISTS site (
    trial_id TEXT NOT NULL REFERENCES trial(trial_id),
    site_id TEXT NOT NULL,
    data TEXT NOT NULL,
    PRIMARY KEY (trial_id, site_id)
);
CREATE TABLE IF NOT EXISTS enrollment (
    trial_id TEXT NOT NULL,
    unit_id TEXT NOT NULL,
    site_id TEXT NOT NULL,
    arm TEXT NOT NULL CHECK (arm IN ('standard','assisted')),
    data TEXT NOT NULL,
    PRIMARY KEY (trial_id, unit_id),
    FOREIGN KEY (trial_id, site_id) REFERENCES site(trial_id, site_id)
);
CREATE TABLE IF NOT EXISTS delivery (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    trial_id TEXT NOT NULL REFERENCES trial(trial_id),
    delivery_id TEXT NOT NULL,
    digest TEXT NOT NULL,
    accepted INTEGER NOT NULL DEFAULT 0,
    repeated INTEGER NOT NULL DEFAULT 0,
    UNIQUE (trial_id, delivery_id)
);
CREATE TABLE IF NOT EXISTS signal (
    trial_id TEXT NOT NULL,
    event_id TEXT NOT NULL,
    revision INTEGER NOT NULL,
    unit_id TEXT NOT NULL,
    cycle_id TEXT NOT NULL,
    delivery_seq INTEGER NOT NULL REFERENCES delivery(seq),
    event TEXT NOT NULL,
    digest TEXT NOT NULL,
    PRIMARY KEY (trial_id, event_id, revision),
    FOREIGN KEY (trial_id, unit_id) REFERENCES enrollment(trial_id, unit_id)
);
CREATE INDEX IF NOT EXISTS signal_cut ON signal(trial_id, delivery_seq);
CREATE INDEX IF NOT EXISTS signal_cycle ON signal(trial_id, unit_id, cycle_id);
CREATE TABLE IF NOT EXISTS evidence (
    trial_id TEXT NOT NULL REFERENCES trial(trial_id),
    evidence_id TEXT NOT NULL,
    cut INTEGER NOT NULL,
    document TEXT NOT NULL,
    PRIMARY KEY (trial_id, evidence_id)
);
CREATE TABLE IF NOT EXISTS review (
    trial_id TEXT NOT NULL,
    evidence_id TEXT NOT NULL,
    market TEXT NOT NULL,
    desk TEXT NOT NULL CHECK (desk IN ('field','methods')),
    assessment TEXT NOT NULL CHECK (assessment IN ('endorse','object')),
    reviewer TEXT NOT NULL,
    note TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    PRIMARY KEY (trial_id, evidence_id, market, desk),
    FOREIGN KEY (trial_id, evidence_id) REFERENCES evidence(trial_id, evidence_id)
);
CREATE TABLE IF NOT EXISTS bulletin (
    trial_id TEXT NOT NULL,
    market TEXT NOT NULL,
    edition INTEGER NOT NULL,
    evidence_id TEXT NOT NULL,
    outcome TEXT NOT NULL CHECK (outcome IN ('advance','hold')),
    digest TEXT NOT NULL,
    document TEXT NOT NULL,
    PRIMARY KEY (trial_id, market, edition),
    UNIQUE (trial_id, market, evidence_id),
    FOREIGN KEY (trial_id, evidence_id) REFERENCES evidence(trial_id, evidence_id)
);
PRAGMA user_version=1;
"""


class Store:
    def __init__(self, location: str | Path):
        self.location = Path(location)
        self.location.parent.mkdir(parents=True, exist_ok=True)
        session = self.connect()
        try:
            version = session.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1):
                raise RuntimeError(f"Unsupported database version {version}")
            session.execute("PRAGMA journal_mode=WAL")
            session.executescript(SCHEMA)
        finally:
            session.close()

    def connect(self) -> sqlite3.Connection:
        session = sqlite3.connect(self.location, timeout=10, isolation_level=None)
        session.row_factory = sqlite3.Row
        session.execute("PRAGMA foreign_keys=ON")
        return session

    @contextmanager
    def transaction(self, *, write: bool = False) -> Iterator[sqlite3.Connection]:
        session = self.connect()
        try:
            session.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            yield session
            session.commit()
        except BaseException:
            session.rollback()
            raise
        finally:
            session.close()
