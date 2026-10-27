from __future__ import annotations

import asyncio
import json
import sys
import tempfile
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))

from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver  # noqa: E402
from langgraph.types import Command  # noqa: E402

from app.config import Settings  # noqa: E402
from app.database import Repository  # noqa: E402
from app.graph import build_graph  # noqa: E402


CASES = {
    "seed-api-5xx": {"kind": "api_errors", "terms": ["payments-v2", "payments-v1"], "iterations": 1},
    "seed-db-contention": {"kind": "database_contention", "terms": ["44", "analytics"], "iterations": 2},
    "seed-job-config": {"kind": "invalid_configuration", "terms": ["retries", "many"], "iterations": 1},
}


async def evaluate() -> list[dict]:
    outcomes: list[dict] = []
    with tempfile.TemporaryDirectory(prefix="incidentpilot-eval-") as temp_dir:
        path = Path(temp_dir) / "evaluation.sqlite3"
        repo = Repository(path)
        await repo.initialize()
        async with AsyncSqliteSaver.from_conn_string(str(path)) as saver:
            graph = build_graph(repo, Settings(database_path=path, llm_provider="demo"), saver)
            for incident_id, spec in CASES.items():
                incident = await repo.get_incident(incident_id)
                assert incident
                run_id = f"eval-{uuid4().hex[:10]}"
                thread_id = f"eval-thread-{uuid4()}"
                await repo.create_investigation(run_id, incident_id, thread_id)
                state = {
                    "incident_id": incident_id, "run_id": run_id, "thread_id": thread_id,
                    "title": incident["title"], "description": incident["description"], "service": incident["service"],
                    "severity": incident["severity"], "incident_kind": incident["kind"], "current_iteration": 0,
                    "evidence": [], "findings": [], "events": [], "approvals": [], "revision": 0,
                }
                config = {"configurable": {"thread_id": thread_id}, "recursion_limit": 40}
                paused = await graph.ainvoke(state, config=config)
                snapshot = await graph.aget_state(config)
                assert any(getattr(task, "interrupts", ()) for task in snapshot.tasks), "approval was not required"
                text = paused["diagnosis"]["diagnosis"].lower()
                assert all(term.lower() in text for term in spec["terms"]), (incident_id, text)
                assert paused["classification"]["kind"] == spec["kind"]
                assert paused["diagnosis"]["iterations"] == spec["iterations"]
                assert paused["evidence"], "diagnosis must cite evidence"
                assert all(item["evidence_ids"] for item in paused["findings"] if item["confidence"] >= .7)
                final = await graph.ainvoke(Command(resume={"decision": "approve", "change_request": None}), config=config)
                report = final["report"]
                assert report["status"] == "completed" and report["outcome"]["verified"]
                assert report["outcome"]["action"]["simulated"]
                outcomes.append({
                    "incident_id": incident_id,
                    "diagnosis_pass": True,
                    "evidence_count": len(paused["evidence"]),
                    "specialist_count": len(paused["findings"]),
                    "iterations": paused["diagnosis"]["iterations"],
                    "approval_required": True,
                    "verified_simulated_completion": True,
                })
    return outcomes


if __name__ == "__main__":
    results = asyncio.run(evaluate())
    print(json.dumps({"mode": "deterministic_demo", "live_model_quality_tested": False, "scenarios": results}, indent=2))
