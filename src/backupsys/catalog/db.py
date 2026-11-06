"""Catalogo locale (SQLite): storico delle esecuzioni, esiti per destinazione, errori."""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from backupsys import appdirs

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT NOT NULL,
    job_name TEXT NOT NULL,
    run_ts TEXT NOT NULL,
    trigger TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL,
    files_scanned INTEGER DEFAULT 0,
    bytes_scanned INTEGER DEFAULT 0,
    message TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS runs_job ON runs(job_id, started_at);
CREATE TABLE IF NOT EXISTS run_dests (
    run_id INTEGER NOT NULL REFERENCES runs(id),
    dest_id TEXT NOT NULL,
    dest_name TEXT NOT NULL,
    status TEXT NOT NULL,
    copied INTEGER DEFAULT 0,
    versioned INTEGER DEFAULT 0,
    skipped INTEGER DEFAULT 0,
    bytes INTEGER DEFAULT 0,
    errors INTEGER DEFAULT 0,
    pruned_versions INTEGER DEFAULT 0,
    pruned_bytes INTEGER DEFAULT 0,
    message TEXT DEFAULT '',
    PRIMARY KEY (run_id, dest_id)
);
CREATE TABLE IF NOT EXISTS run_errors (
    run_id INTEGER NOT NULL REFERENCES runs(id),
    dest_id TEXT,
    path TEXT NOT NULL,
    error TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS dest_state (
    job_id TEXT NOT NULL,
    dest_id TEXT NOT NULL,
    last_success_at TEXT,
    PRIMARY KEY (job_id, dest_id)
);
"""

#: Esiti possibili.
OK = "ok"
WARNING = "warning"  # completato con errori su alcuni file
FAILED = "failed"
SKIPPED = "skipped"  # destinazione non disponibile
PAUSED = "paused"  # guard anti-ransomware: serve conferma
RUNNING = "running"
CANCELLED = "cancelled"


@dataclass
class RunRow:
    id: int
    job_id: str
    job_name: str
    run_ts: str
    trigger: str
    started_at: str
    finished_at: str | None
    status: str
    files_scanned: int
    bytes_scanned: int
    message: str


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


class Catalog:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or appdirs.data_dir() / "catalog.sqlite"
        self._lock = threading.RLock()
        with self._conn() as c:
            c.executescript(SCHEMA)
            # un'esecuzione rimasta "running" dopo un crash/spegnimento viene marcata come interrotta
            c.execute(
                "UPDATE runs SET status=?, message='Interrotta (programma chiuso o PC spento)' WHERE status=?",
                (CANCELLED, RUNNING),
            )

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            conn = sqlite3.connect(self.path, timeout=30)
            conn.row_factory = sqlite3.Row
            try:
                conn.execute("PRAGMA journal_mode=WAL")
                yield conn
                conn.commit()
            finally:
                conn.close()

    # ------------------------------------------------------------------ scrittura
    def start_run(self, job_id: str, job_name: str, run_ts: str, trigger: str) -> int:
        with self._conn() as c:
            cur = c.execute(
                "INSERT INTO runs(job_id, job_name, run_ts, trigger, started_at, status) VALUES (?,?,?,?,?,?)",
                (job_id, job_name, run_ts, trigger, _now(), RUNNING),
            )
            return int(cur.lastrowid or 0)

    def finish_run(self, run_id: int, status: str, files: int, nbytes: int, message: str = "") -> None:
        with self._conn() as c:
            c.execute(
                "UPDATE runs SET finished_at=?, status=?, files_scanned=?, bytes_scanned=?, message=? WHERE id=?",
                (_now(), status, files, nbytes, message, run_id),
            )

    def record_dest(self, run_id: int, dest_id: str, dest_name: str, **fields: Any) -> None:
        cols = ["run_id", "dest_id", "dest_name", *fields]
        with self._conn() as c:
            c.execute(
                f"INSERT OR REPLACE INTO run_dests({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                (run_id, dest_id, dest_name, *fields.values()),
            )

    def record_errors(self, run_id: int, dest_id: str | None, errors: list[tuple[str, str]]) -> None:
        if not errors:
            return
        with self._conn() as c:
            c.executemany(
                "INSERT INTO run_errors(run_id, dest_id, path, error) VALUES (?,?,?,?)",
                [(run_id, dest_id, p, e) for p, e in errors[:5000]],
            )

    def mark_success(self, job_id: str, dest_id: str, when: str | None = None) -> None:
        with self._conn() as c:
            c.execute(
                "INSERT INTO dest_state(job_id, dest_id, last_success_at) VALUES (?,?,?) "
                "ON CONFLICT(job_id, dest_id) DO UPDATE SET last_success_at=excluded.last_success_at",
                (job_id, dest_id, when or _now()),
            )

    # ------------------------------------------------------------------ lettura
    def runs(self, job_id: str | None = None, limit: int = 100) -> list[RunRow]:
        with self._conn() as c:
            if job_id:
                rows = c.execute(
                    "SELECT * FROM runs WHERE job_id=? ORDER BY id DESC LIMIT ?", (job_id, limit)
                ).fetchall()
            else:
                rows = c.execute("SELECT * FROM runs ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
            return [RunRow(**dict(r)) for r in rows]

    def run(self, run_id: int) -> RunRow | None:
        with self._conn() as c:
            r = c.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
            return RunRow(**dict(r)) if r else None

    def run_dests(self, run_id: int) -> list[dict[str, Any]]:
        with self._conn() as c:
            return [dict(r) for r in c.execute("SELECT * FROM run_dests WHERE run_id=?", (run_id,))]

    def run_errors(self, run_id: int, limit: int = 1000) -> list[dict[str, Any]]:
        with self._conn() as c:
            return [dict(r) for r in c.execute("SELECT * FROM run_errors WHERE run_id=? LIMIT ?", (run_id, limit))]

    def last_run(self, job_id: str) -> RunRow | None:
        rs = self.runs(job_id, limit=1)
        return rs[0] if rs else None

    def last_started_at(self, job_id: str) -> datetime | None:
        """Ultimo avvio (esclusi i job in pausa): base per calcolare la prossima esecuzione."""
        with self._conn() as c:
            r = c.execute(
                "SELECT started_at FROM runs WHERE job_id=? AND trigger != 'connect' ORDER BY id DESC LIMIT 1",
                (job_id,),
            ).fetchone()
            return datetime.fromisoformat(r["started_at"]) if r else None

    def last_success(self, job_id: str, dest_id: str) -> datetime | None:
        with self._conn() as c:
            r = c.execute(
                "SELECT last_success_at FROM dest_state WHERE job_id=? AND dest_id=?", (job_id, dest_id)
            ).fetchone()
            return datetime.fromisoformat(r["last_success_at"]) if r and r["last_success_at"] else None
