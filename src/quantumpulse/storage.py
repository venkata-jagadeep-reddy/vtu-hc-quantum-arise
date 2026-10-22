"""SQLite persistence for monitoring history.

Storing every monitoring round makes it possible to answer questions that a
single report cannot: how often did the detector fire on a healthy circuit,
how quickly did it react after a modification was introduced, and which
checkpoints are the most informative.

The schema is intentionally small and flat so that the data can be loaded
into pandas with a single ``read_sql_query`` call.
"""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp         TEXT NOT NULL,
    program_name      TEXT NOT NULL,
    program_fingerprint TEXT,
    label             TEXT NOT NULL DEFAULT 'nominal',
    tamper_id         TEXT,
    tamper_target     TEXT,
    backend           TEXT,
    noise_profile     TEXT,
    shots             INTEGER,
    seed              INTEGER,
    flagged           INTEGER NOT NULL DEFAULT 0,
    anomaly_score     REAL,
    first_bad_cp      TEXT,
    top1_gid          TEXT,
    top1_correct      INTEGER,
    confidence        REAL,
    execution_seconds REAL,
    duration_seconds  REAL
);

CREATE TABLE IF NOT EXISTS checkpoint_results (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id        INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    cp_id         TEXT NOT NULL,
    name          TEXT,
    position      INTEGER,
    observed_tvd  REAL,
    threshold     REAL,
    null_p_value  REAL,
    anomaly_score REAL,
    flagged       INTEGER NOT NULL DEFAULT 0,
    observed_probs TEXT,
    expected_probs TEXT
);

CREATE TABLE IF NOT EXISTS localization_results (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id       INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    rank         INTEGER NOT NULL,
    gid          TEXT NOT NULL,
    position     INTEGER,
    gate         TEXT,
    stage        TEXT,
    hypothesis   TEXT,
    hypothesis_kind TEXT,
    tvd_to_observed REAL,
    score        REAL,
    is_true_location INTEGER
);

CREATE TABLE IF NOT EXISTS alerts (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id    INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    timestamp TEXT NOT NULL,
    severity  TEXT NOT NULL,
    message   TEXT NOT NULL,
    payload   TEXT
);
"""


class MonitoringStore:
    """Thin SQLite repository for monitoring history."""

    def __init__(self, path: str = "data/quantumpulse.db") -> None:
        self.path = path
        directory = os.path.dirname(os.path.abspath(path))
        if directory:
            os.makedirs(directory, exist_ok=True)
        self.connection = sqlite3.connect(path)
        self.connection.row_factory = sqlite3.Row
        self.connection.executescript(SCHEMA)
        self.connection.commit()

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> "MonitoringStore":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # ------------------------------------------------------------------
    def record_run(
        self,
        report_dict: Dict[str, Any],
        label: str = "nominal",
        tamper_id: Optional[str] = None,
        tamper_target: Optional[str] = None,
        duration_seconds: Optional[float] = None,
    ) -> int:
        """Persist a monitoring round and its child records."""

        verdict = report_dict.get("verdict", {})
        loc = report_dict.get("localization") or {}
        candidates = loc.get("candidates", [])
        top1 = candidates[0] if candidates else None

        cursor = self.connection.execute(
            """
            INSERT INTO runs (
                timestamp, program_name, program_fingerprint, label, tamper_id,
                tamper_target, backend, noise_profile, shots, seed, flagged,
                anomaly_score, first_bad_cp, top1_gid, top1_correct,
                confidence, execution_seconds, duration_seconds
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                report_dict.get("timestamp", datetime.now(timezone.utc).isoformat()),
                report_dict.get("program_name", ""),
                report_dict.get("program_fingerprint", ""),
                label,
                tamper_id,
                tamper_target,
                (report_dict.get("backend") or {}).get("backend", ""),
                ((report_dict.get("backend") or {}).get("noise") or {}).get("name", ""),
                report_dict.get("shots", 0),
                (report_dict.get("metadata") or {}).get("seed"),
                1 if report_dict.get("flagged") else 0,
                verdict.get("overall_anomaly_score", 0.0),
                verdict.get("first_bad_checkpoint"),
                top1.get("gid") if top1 else None,
                None if top1 is None else (1 if top1.get("is_true_location") else 0),
                loc.get("confidence"),
                (report_dict.get("metadata") or {}).get("execution_seconds"),
                duration_seconds,
            ),
        )
        run_id = int(cursor.lastrowid)

        for cp in verdict.get("checkpoints", []):
            self.connection.execute(
                """
                INSERT INTO checkpoint_results (
                    run_id, cp_id, name, position, observed_tvd, threshold,
                    null_p_value, anomaly_score, flagged, observed_probs,
                    expected_probs
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    run_id,
                    cp["cp_id"],
                    cp.get("name"),
                    cp.get("position"),
                    cp.get("observed_tvd"),
                    cp.get("threshold"),
                    cp.get("null_p_value"),
                    cp.get("anomaly_score"),
                    1 if cp.get("flagged") else 0,
                    json.dumps(cp.get("observed_probs", [])),
                    json.dumps(cp.get("expected_probs", [])),
                ),
            )

        for candidate in candidates:
            self.connection.execute(
                """
                INSERT INTO localization_results (
                    run_id, rank, gid, position, gate, stage, hypothesis,
                    hypothesis_kind, tvd_to_observed, score, is_true_location
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    run_id,
                    candidate.get("rank", 0),
                    candidate.get("gid", ""),
                    candidate.get("position"),
                    candidate.get("gate"),
                    candidate.get("stage"),
                    candidate.get("hypothesis"),
                    candidate.get("hypothesis_kind"),
                    candidate.get("tvd_to_observed"),
                    candidate.get("score"),
                    None
                    if candidate.get("is_true_location") is None
                    else (1 if candidate["is_true_location"] else 0),
                ),
            )

        if report_dict.get("flagged"):
            severity = "high" if top1 and top1.get("is_true_location") else "medium"
            message = (
                f"Deviation detected at {verdict.get('first_bad_checkpoint')}"
                + (
                    f"; top suspected gate {top1.get('gid')} ({top1.get('gate')})"
                    if top1
                    else ""
                )
            )
            self.connection.execute(
                "INSERT INTO alerts (run_id, timestamp, severity, message, payload) VALUES (?,?,?,?,?)",
                (
                    run_id,
                    report_dict.get("timestamp", ""),
                    severity,
                    message,
                    json.dumps(report_dict, default=str),
                ),
            )

        self.connection.commit()
        return run_id

    # ------------------------------------------------------------------
    def query(self, sql: str, params: Iterable[Any] = ()) -> List[sqlite3.Row]:
        return list(self.connection.execute(sql, tuple(params)))

    def runs(self, limit: int = 100) -> List[sqlite3.Row]:
        return self.query(
            "SELECT * FROM runs ORDER BY id DESC LIMIT ?", (limit,)
        )

    def checkpoint_rows(self, run_id: int) -> List[sqlite3.Row]:
        return self.query(
            "SELECT * FROM checkpoint_results WHERE run_id = ? ORDER BY id", (run_id,)
        )

    def alerts(self, limit: int = 100) -> List[sqlite3.Row]:
        return self.query(
            "SELECT * FROM alerts ORDER BY id DESC LIMIT ?", (limit,)
        )

    def summary(self) -> Dict[str, Any]:
        row = self.connection.execute(
            """
            SELECT COUNT(*) AS total,
                   SUM(flagged) AS flagged,
                   SUM(label='tampered') AS tampered
            FROM runs
            """
        ).fetchone()
        return {
            "total_runs": int(row["total"] or 0),
            "flagged_runs": int(row["flagged"] or 0),
            "tampered_runs": int(row["tampered"] or 0),
        }
