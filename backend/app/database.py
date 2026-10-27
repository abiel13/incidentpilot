from __future__ import annotations

import json
import logging
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import aiosqlite

from app.seed_data import ENVIRONMENT, GENERIC_RUNBOOK, SEED_INCIDENTS

logger = logging.getLogger("incidentpilot.database")


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


def dumps(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False)


def loads(value: str | None, fallback: Any = None) -> Any:
    if value is None:
        return fallback
    return json.loads(value)


class Repository:
    """Application persistence and the explicitly simulated service environment."""

    def __init__(self, path: Path):
        self.path = path

    @asynccontextmanager
    async def connect(self):
        db = await aiosqlite.connect(self.path, timeout=10)
        db.row_factory = aiosqlite.Row
        await db.execute("PRAGMA foreign_keys=ON")
        await db.execute("PRAGMA journal_mode=WAL")
        await db.execute("PRAGMA busy_timeout=10000")
        try:
            yield db
        finally:
            await db.close()

    async def initialize(self) -> None:
        async with self.connect() as db:
            await db.executescript(
                """
                CREATE TABLE IF NOT EXISTS incidents (
                    id TEXT PRIMARY KEY, title TEXT NOT NULL, description TEXT NOT NULL,
                    service TEXT NOT NULL, severity TEXT NOT NULL, kind TEXT NOT NULL,
                    is_seed INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS investigations (
                    run_id TEXT PRIMARY KEY, incident_id TEXT NOT NULL REFERENCES incidents(id),
                    thread_id TEXT NOT NULL UNIQUE, status TEXT NOT NULL,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    report_json TEXT, source_run_id TEXT, source_checkpoint_id TEXT,
                    error TEXT, duration_ms INTEGER
                );
                CREATE INDEX IF NOT EXISTS investigations_incident_idx ON investigations(incident_id, created_at DESC);
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL REFERENCES investigations(run_id),
                    kind TEXT NOT NULL, message TEXT NOT NULL, payload_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS events_run_idx ON events(run_id, id);
                CREATE TABLE IF NOT EXISTS env_records (
                    incident_id TEXT NOT NULL, category TEXT NOT NULL, record_id TEXT NOT NULL,
                    payload_json TEXT NOT NULL, PRIMARY KEY(incident_id, category, record_id)
                );
                CREATE TABLE IF NOT EXISTS env_config (
                    incident_id TEXT NOT NULL, name TEXT NOT NULL, value_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL, PRIMARY KEY(incident_id, name)
                );
                CREATE TABLE IF NOT EXISTS tool_attempts (
                    run_id TEXT NOT NULL, tool_name TEXT NOT NULL, iteration INTEGER NOT NULL,
                    attempt_count INTEGER NOT NULL, PRIMARY KEY(run_id, tool_name, iteration)
                );
                CREATE TABLE IF NOT EXISTS actions (
                    action_id TEXT PRIMARY KEY, run_id TEXT NOT NULL UNIQUE REFERENCES investigations(run_id),
                    incident_id TEXT NOT NULL, payload_json TEXT NOT NULL, result_json TEXT NOT NULL,
                    executed_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS lessons (
                    lesson_id TEXT PRIMARY KEY, source_run_id TEXT NOT NULL REFERENCES investigations(run_id),
                    incident_id TEXT NOT NULL, kind TEXT NOT NULL, title TEXT NOT NULL,
                    detail TEXT NOT NULL, verified_at TEXT NOT NULL,
                    UNIQUE(source_run_id, kind)
                );
                """
            )
            now = utc_now()
            for incident in SEED_INCIDENTS:
                await db.execute(
                    "INSERT OR IGNORE INTO incidents VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (incident["id"], incident["title"], incident["description"], incident["service"], incident["severity"], incident["kind"], 1, now),
                )
                for category, records in ENVIRONMENT[incident["id"]].items():
                    if category == "config_values":
                        for key, value in records.items():
                            await db.execute(
                                "INSERT OR IGNORE INTO env_config(incident_id,name,value_json,updated_at) VALUES (?,?,?,?)",
                                (incident["id"], key, dumps(value), now),
                            )
                        continue
                    for record in records:
                        await db.execute(
                            "INSERT OR IGNORE INTO env_records VALUES (?,?,?,?)",
                            (incident["id"], category, record["id"], dumps(record)),
                        )
            await db.commit()
        logger.info("database_initialized", extra={"database_path": str(self.path)})

    async def list_incidents(self) -> list[dict]:
        async with self.connect() as db:
            cursor = await db.execute(
                "SELECT i.*, (SELECT COUNT(*) FROM investigations r WHERE r.incident_id=i.id) AS investigation_count "
                "FROM incidents i ORDER BY is_seed DESC, created_at DESC"
            )
            return [dict(row) for row in await cursor.fetchall()]

    async def get_incident(self, incident_id: str) -> dict | None:
        async with self.connect() as db:
            cursor = await db.execute("SELECT * FROM incidents WHERE id=?", (incident_id,))
            row = await cursor.fetchone()
            return dict(row) if row else None

    async def create_incident(self, payload: dict) -> dict:
        incident_id = f"inc-{datetime.now(UTC).strftime('%y%m%d')}-{__import__('uuid').uuid4().hex[:8]}"
        description = payload["description"]
        lowered = f"{payload['title']} {description}".lower()
        if any(term in lowered for term in ("database", "sql", "connection pool", "latency", "slow response")):
            kind = "database_contention"
        elif any(term in lowered for term in ("config", "configuration", "queue", "job", "worker")):
            kind = "invalid_configuration"
        elif any(term in lowered for term in ("api", "5xx", "error", "deployment", "release")):
            kind = "api_errors"
        else:
            kind = "unknown"
        async with self.connect() as db:
            await db.execute(
                "INSERT INTO incidents VALUES (?,?,?,?,?,?,0,?)",
                (incident_id, payload["title"], description, payload["service"], payload["severity"], kind, utc_now()),
            )
            for category, record in (
                ("logs", {"id": f"{incident_id}-report", "timestamp": utc_now(), "summary": "User-submitted incident report.", "detail": description, "signal": "reported_not_independently_verified"}),
                ("runbooks", {**GENERIC_RUNBOOK, "id": f"{incident_id}-runbook"}),
            ):
                await db.execute("INSERT INTO env_records VALUES (?,?,?,?)", (incident_id, category, record["id"], dumps(record)))
            await db.commit()
        return await self.get_incident(incident_id)  # type: ignore[return-value]

    async def create_investigation(
        self, run_id: str, incident_id: str, thread_id: str,
        source_run_id: str | None = None, source_checkpoint_id: str | None = None,
    ) -> None:
        now = utc_now()
        async with self.connect() as db:
            await db.execute(
                "INSERT INTO investigations(run_id,incident_id,thread_id,status,created_at,updated_at,source_run_id,source_checkpoint_id) VALUES (?,?,?,'queued',?,?,?,?)",
                (run_id, incident_id, thread_id, now, now, source_run_id, source_checkpoint_id),
            )
            await db.commit()

    async def update_run(self, run_id: str, status: str, *, report: dict | None = None, error: str | None = None, duration_ms: int | None = None) -> None:
        fields = ["status=?", "updated_at=?"]
        values: list[Any] = [status, utc_now()]
        if report is not None:
            fields.append("report_json=?")
            values.append(dumps(report))
        if error is not None:
            fields.append("error=?")
            values.append(error[:1000])
        if duration_ms is not None:
            fields.append("duration_ms=?")
            values.append(duration_ms)
        values.append(run_id)
        async with self.connect() as db:
            await db.execute(f"UPDATE investigations SET {', '.join(fields)} WHERE run_id=?", values)
            await db.commit()

    async def get_run(self, run_id: str) -> dict | None:
        async with self.connect() as db:
            cursor = await db.execute(
                "SELECT r.*, i.title AS incident_title, i.description AS incident_description, i.service, i.severity, i.kind AS incident_kind "
                "FROM investigations r JOIN incidents i ON i.id=r.incident_id WHERE r.run_id=?", (run_id,)
            )
            row = await cursor.fetchone()
        if row is None:
            return None
        result = dict(row)
        result["report"] = loads(result.pop("report_json"), None)
        return result

    async def list_runs(self, incident_id: str | None = None) -> list[dict]:
        async with self.connect() as db:
            query = "SELECT r.*, i.title AS incident_title, i.service, i.severity FROM investigations r JOIN incidents i ON i.id=r.incident_id"
            args: tuple = ()
            if incident_id:
                query += " WHERE r.incident_id=?"
                args = (incident_id,)
            query += " ORDER BY r.created_at DESC"
            cursor = await db.execute(query, args)
            rows = [dict(row) for row in await cursor.fetchall()]
        for row in rows:
            row["report"] = loads(row.pop("report_json"), None)
        return rows

    async def add_event(self, run_id: str, kind: str, message: str, payload: dict | None = None) -> dict:
        created_at = utc_now()
        async with self.connect() as db:
            cursor = await db.execute(
                "INSERT INTO events(run_id,kind,message,payload_json,created_at) VALUES (?,?,?,?,?)",
                (run_id, kind, message, dumps(payload or {}), created_at),
            )
            event_id = cursor.lastrowid
            await db.commit()
        return {"id": event_id, "run_id": run_id, "kind": kind, "message": message, "payload": payload or {}, "created_at": created_at}

    async def list_events(self, run_id: str, after: int = 0) -> list[dict]:
        async with self.connect() as db:
            cursor = await db.execute("SELECT * FROM events WHERE run_id=? AND id>? ORDER BY id", (run_id, after))
            rows = [dict(row) for row in await cursor.fetchall()]
        for row in rows:
            row["payload"] = loads(row.pop("payload_json"), {})
        return rows

    async def query_records(self, incident_id: str, category: str) -> list[dict]:
        async with self.connect() as db:
            cursor = await db.execute(
                "SELECT payload_json FROM env_records WHERE incident_id=? AND category=? ORDER BY record_id",
                (incident_id, category),
            )
            records = [loads(row[0], {}) for row in await cursor.fetchall()]
            if category == "config":
                cfg = await db.execute("SELECT name,value_json,updated_at FROM env_config WHERE incident_id=? ORDER BY name", (incident_id,))
                values = await cfg.fetchall()
                for index, row in enumerate(records):
                    current = next((item for item in values if item["name"] == row.get("key")), None)
                    if current:
                        row["value"] = loads(current["value_json"])
                        row["detail"] = f"{row.get('detail', '')} Current simulated value: {row['value']!r}."
            return records

    async def get_config(self, incident_id: str) -> dict[str, Any]:
        async with self.connect() as db:
            cursor = await db.execute("SELECT name,value_json FROM env_config WHERE incident_id=? ORDER BY name", (incident_id,))
            return {row["name"]: loads(row["value_json"]) for row in await cursor.fetchall()}

    async def bump_tool_attempt(self, run_id: str, tool_name: str, iteration: int) -> int:
        async with self.connect() as db:
            cursor = await db.execute(
                "SELECT attempt_count FROM tool_attempts WHERE run_id=? AND tool_name=? AND iteration=?",
                (run_id, tool_name, iteration),
            )
            row = await cursor.fetchone()
            attempt = (row[0] if row else 0) + 1
            await db.execute(
                "INSERT INTO tool_attempts VALUES (?,?,?,?) ON CONFLICT(run_id,tool_name,iteration) DO UPDATE SET attempt_count=excluded.attempt_count",
                (run_id, tool_name, iteration, attempt),
            )
            await db.commit()
            return attempt

    async def claim_resume(self, run_id: str) -> bool:
        async with self.connect() as db:
            cursor = await db.execute(
                "UPDATE investigations SET status='resuming',updated_at=? WHERE run_id=? AND status='approval_required'",
                (utc_now(), run_id),
            )
            await db.commit()
            return cursor.rowcount == 1

    async def release_resume_claim(self, run_id: str, status: str) -> None:
        async with self.connect() as db:
            await db.execute("UPDATE investigations SET status=?,updated_at=? WHERE run_id=? AND status='resuming'", (status, utc_now(), run_id))
            await db.commit()

    async def execute_simulated_action(self, run_id: str, incident_id: str, action_id: str, action: dict) -> dict:
        """Apply changes atomically; one action per run makes replay idempotent."""
        async with self.connect() as db:
            await db.execute("BEGIN IMMEDIATE")
            cursor = await db.execute("SELECT result_json FROM actions WHERE run_id=?", (run_id,))
            existing = await cursor.fetchone()
            if existing:
                await db.commit()
                return loads(existing[0], {})
            changes = action.get("changes", [])
            before: dict[str, Any] = {}
            after: dict[str, Any] = {}
            for change in changes:
                key, value = change["key"], change["value"]
                current = await db.execute("SELECT value_json FROM env_config WHERE incident_id=? AND name=?", (incident_id, key))
                row = await current.fetchone()
                before[key] = loads(row[0]) if row else None
                after[key] = value
                await db.execute(
                    "INSERT INTO env_config VALUES (?,?,?,?) ON CONFLICT(incident_id,name) DO UPDATE SET value_json=excluded.value_json,updated_at=excluded.updated_at",
                    (incident_id, key, dumps(value), utc_now()),
                )
            result = {"action_id": action_id, "simulated": True, "applied": True, "before": before, "after": after, "summary": action.get("summary", "Simulated remediation applied.")}
            await db.execute(
                "INSERT INTO actions VALUES (?,?,?,?,?,?)",
                (action_id, run_id, incident_id, dumps(action), dumps(result), utc_now()),
            )
            await db.commit()
            return result

    async def save_verified_lesson(self, run_id: str, incident_id: str, kind: str, title: str, detail: str) -> dict:
        lesson_id = f"lesson-{run_id}-{kind}"
        async with self.connect() as db:
            await db.execute(
                "INSERT OR IGNORE INTO lessons VALUES (?,?,?,?,?,?,?)",
                (lesson_id, run_id, incident_id, kind, title, detail, utc_now()),
            )
            await db.commit()
        return {"lesson_id": lesson_id, "source_run_id": run_id, "incident_id": incident_id, "kind": kind, "title": title, "detail": detail}

    async def list_lessons(self, kind: str | None = None) -> list[dict]:
        async with self.connect() as db:
            query = "SELECT * FROM lessons"
            args: tuple = ()
            if kind:
                query += " WHERE kind=?"
                args = (kind,)
            query += " ORDER BY verified_at DESC"
            cursor = await db.execute(query, args)
            return [dict(row) for row in await cursor.fetchall()]

    async def find_lessons(self, kind: str, exclude_run_id: str) -> list[dict]:
        async with self.connect() as db:
            cursor = await db.execute(
                "SELECT * FROM lessons WHERE kind=? AND source_run_id<>? ORDER BY verified_at DESC LIMIT 3",
                (kind, exclude_run_id),
            )
            return [dict(row) for row in await cursor.fetchall()]

    async def recover_runs(self) -> None:
        """Mark orphaned workers recoverable; checkpoints remain in LangGraph's SQLite tables."""
        async with self.connect() as db:
            await db.execute(
                "UPDATE investigations SET status='recoverable',updated_at=? WHERE status IN ('running','resuming','queued')",
                (utc_now(),),
            )
            await db.commit()
