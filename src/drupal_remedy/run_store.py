"""Persistent run storage for the HTTP API."""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any

from drupal_remedy.scan_payload import utc_now_iso


class PersistentRunStore:
    """SQLite-backed store for scan and fix runs."""

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._path, timeout=30.0, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout = 30000")
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute("PRAGMA journal_mode = WAL")
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS runs (
                    run_id TEXT PRIMARY KEY,
                    run_type TEXT NOT NULL,
                    status TEXT NOT NULL,
                    site_code TEXT NOT NULL,
                    entity_type TEXT NOT NULL,
                    entity_id INTEGER NOT NULL,
                    canonical_url TEXT,
                    created_at TEXT NOT NULL,
                    started_at TEXT,
                    completed_at TEXT,
                    payload_json TEXT,
                    result_json TEXT,
                    error TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_runs_page
                    ON runs (site_code, entity_type, entity_id);
                CREATE INDEX IF NOT EXISTS idx_runs_status
                    ON runs (status);

                CREATE TABLE IF NOT EXISTS latest_scans (
                    site_code TEXT NOT NULL,
                    entity_type TEXT NOT NULL,
                    entity_id INTEGER NOT NULL,
                    run_id TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (site_code, entity_type, entity_id)
                );
                """
            )

    def create(
        self,
        run_id: str,
        *,
        run_type: str,
        site_code: str,
        entity_type: str,
        entity_id: int,
        canonical_url: str | None = None,
        status: str = "queued",
        created_at: str | None = None,
    ) -> dict[str, Any]:
        record = {
            "run_id": run_id,
            "type": run_type,
            "status": status,
            "site_code": site_code,
            "entity_type": entity_type,
            "entity_id": entity_id,
            "canonical_url": canonical_url,
            "created_at": created_at or utc_now_iso(),
            "started_at": None,
            "completed_at": None,
            "payload": None,
            "result": None,
            "error": None,
        }
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO runs (
                    run_id, run_type, status, site_code, entity_type, entity_id,
                    canonical_url, created_at, started_at, completed_at,
                    payload_json, result_json, error
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    run_type,
                    status,
                    site_code,
                    entity_type,
                    entity_id,
                    canonical_url,
                    record["created_at"],
                    None,
                    None,
                    None,
                    None,
                    None,
                ),
            )
        return record

    def mark_running(self, run_id: str, *, started_at: str | None = None) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                "UPDATE runs SET status = ?, started_at = ? WHERE run_id = ?",
                ("running", started_at or utc_now_iso(), run_id),
            )

    def complete(
        self,
        run_id: str,
        *,
        payload: dict[str, Any] | None = None,
        result: dict[str, Any] | None = None,
        completed_at: str | None = None,
        status: str = "completed",
    ) -> None:
        payload_json = json.dumps(payload) if payload is not None else None
        result_json = json.dumps(result) if result is not None else None
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                UPDATE runs
                   SET status = ?,
                       completed_at = ?,
                       payload_json = COALESCE(?, payload_json),
                       result_json = COALESCE(?, result_json),
                       error = NULL
                 WHERE run_id = ?
                """,
                (
                    status,
                    completed_at or utc_now_iso(),
                    payload_json,
                    result_json,
                    run_id,
                ),
            )

    def fail(self, run_id: str, error: str, *, completed_at: str | None = None) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                "UPDATE runs SET status = ?, completed_at = ?, error = ? WHERE run_id = ?",
                ("failed", completed_at or utc_now_iso(), error, run_id),
            )

    def get(self, run_id: str) -> dict[str, Any] | None:
        with self._lock, self._connect() as conn:
            row = conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        return self._row_to_dict(row)

    def set_latest_scan(self, site_code: str, entity_type: str, entity_id: int, run_id: str) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO latest_scans (site_code, entity_type, entity_id, run_id, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(site_code, entity_type, entity_id)
                DO UPDATE SET run_id = excluded.run_id, updated_at = excluded.updated_at
                """,
                (site_code, entity_type, entity_id, run_id, utc_now_iso()),
            )

    def get_latest_scan(self, site_code: str, entity_type: str, entity_id: int) -> dict[str, Any] | None:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                """
                SELECT r.*
                  FROM latest_scans l
                  JOIN runs r ON r.run_id = l.run_id
                 WHERE l.site_code = ? AND l.entity_type = ? AND l.entity_id = ?
                """,
                (site_code, entity_type, entity_id),
            ).fetchone()
        return self._row_to_dict(row)

    def ping(self) -> None:
        """Verify that the run store can accept queries."""
        with self._lock, self._connect() as conn:
            conn.execute("SELECT 1").fetchone()

    def _row_to_dict(self, row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        return {
            "run_id": row["run_id"],
            "type": row["run_type"],
            "status": row["status"],
            "site_code": row["site_code"],
            "entity_type": row["entity_type"],
            "entity_id": row["entity_id"],
            "canonical_url": row["canonical_url"],
            "created_at": row["created_at"],
            "started_at": row["started_at"],
            "completed_at": row["completed_at"],
            "payload": json.loads(row["payload_json"]) if row["payload_json"] else None,
            "result": json.loads(row["result_json"]) if row["result_json"] else None,
            "error": row["error"],
        }
