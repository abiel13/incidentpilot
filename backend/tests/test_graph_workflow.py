from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.types import Command

from app.config import Settings
from app.database import Repository


async def make_run(repo: Repository, incident_id: str) -> tuple[str, str, dict]:
    incident = await repo.get_incident(incident_id)
    assert incident
    run_id = f"test-run-{uuid4().hex[:8]}"
    thread_id = f"test-thread-{uuid4()}"
    await repo.create_investigation(run_id, incident_id, thread_id)
    initial = {
        "incident_id": incident_id, "run_id": run_id, "thread_id": thread_id,
        "title": incident["title"], "description": incident["description"], "service": incident["service"],
        "severity": incident["severity"], "incident_kind": incident["kind"], "current_iteration": 0,
        "evidence": [], "findings": [], "events": [], "approvals": [], "revision": 0,
    }
    return run_id, thread_id, initial


async def wait_for_review(graph, initial: dict, thread_id: str) -> tuple[dict, dict]:
    config = {"configurable": {"thread_id": thread_id}, "recursion_limit": 40}
    result = await graph.ainvoke(initial, config=config)
    snapshot = await graph.aget_state(config)
    assert any(getattr(task, "interrupts", ()) for task in snapshot.tasks), "the graph must pause at its native approval interrupt"
    assert result.get("remediation", {}).get("requires_human_approval") is True
    return result, config


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("incident_id", "expected_kind", "expected_iterations"),
    [
        ("seed-api-5xx", "api_errors", 1),
        ("seed-db-contention", "database_contention", 2),
        ("seed-job-config", "invalid_configuration", 1),
    ],
)
async def test_seeded_scenarios_reach_approval_and_complete(tmp_path: Path, incident_id: str, expected_kind: str, expected_iterations: int):
    db_path = tmp_path / "scenario.sqlite3"
    repo = Repository(db_path)
    await repo.initialize()
    async with AsyncSqliteSaver.from_conn_string(str(db_path)) as saver:
        from app.graph import build_graph_with_checkpointer
        graph = build_graph_with_checkpointer(repo, Settings(database_path=db_path, llm_provider="demo"), saver)
        run_id, thread_id, initial = await make_run(repo, incident_id)
        paused, config = await wait_for_review(graph, initial, thread_id)
        assert paused["classification"]["kind"] == expected_kind
        assert paused["diagnosis"]["iterations"] == expected_iterations
        assert paused["evidence"]
        assert len(paused["findings"]) == (5 if expected_iterations == 2 else 3)
        assert len({item["finding_id"] for item in paused["findings"]}) == len(paused["findings"])
        assert paused["remediation"]["simulated"] is True
        final = await graph.ainvoke(Command(resume={"decision": "approve", "change_request": None}), config=config)
        assert final["report"]["status"] == "completed"
        assert final["report"]["outcome"]["verified"] is True
        assert final["report"]["evidence"]
        assert final["report"]["outcome"]["action"]["action_id"] == f"action-{run_id}"
        saved = await repo.get_run(run_id)
        assert saved and saved["status"] == "completed"
        assert len(await repo.list_lessons(expected_kind)) == 1
        config_now = await repo.get_config(incident_id)
        assert config_now[paused["remediation"]["changes"][0]["key"]] == paused["remediation"]["changes"][0]["value"]


@pytest.mark.asyncio
async def test_transient_log_tool_error_retries_once(tmp_path: Path):
    db_path = tmp_path / "retry.sqlite3"
    repo = Repository(db_path)
    await repo.initialize()
    async with AsyncSqliteSaver.from_conn_string(str(db_path)) as saver:
        from app.graph import build_graph_with_checkpointer
        graph = build_graph_with_checkpointer(repo, Settings(database_path=db_path, llm_provider="demo"), saver)
        run_id, thread_id, initial = await make_run(repo, "seed-api-5xx")
        paused, _ = await wait_for_review(graph, initial, thread_id)
        async with repo.connect() as db:
            cursor = await db.execute("SELECT attempt_count FROM tool_attempts WHERE run_id=? AND tool_name='query_seeded_logs' AND iteration=0", (run_id,))
            attempts = (await cursor.fetchone())[0]
        assert attempts == 2
        assert any("gateway deployment" in item["detail"].lower() or "upstream_route" in item["detail"].lower() for item in paused["evidence"])


@pytest.mark.asyncio
async def test_approval_interrupt_survives_checkpointer_restart(tmp_path: Path):
    db_path = tmp_path / "restart.sqlite3"
    repo = Repository(db_path)
    await repo.initialize()
    run_id, thread_id, initial = await make_run(repo, "seed-api-5xx")
    config = {"configurable": {"thread_id": thread_id}, "recursion_limit": 40}
    async with AsyncSqliteSaver.from_conn_string(str(db_path)) as saver:
        from app.graph import build_graph_with_checkpointer
        graph = build_graph_with_checkpointer(repo, Settings(database_path=db_path, llm_provider="demo"), saver)
        await graph.ainvoke(initial, config=config)
        paused = await graph.aget_state(config)
        assert any(getattr(task, "interrupts", ()) for task in paused.tasks)
    # Simulate a backend process restart: create a new saver and compiled graph on the same file.
    async with AsyncSqliteSaver.from_conn_string(str(db_path)) as saver:
        from app.graph import build_graph_with_checkpointer
        graph_after_restart = build_graph_with_checkpointer(repo, Settings(database_path=db_path, llm_provider="demo"), saver)
        saved = await graph_after_restart.aget_state(config)
        assert any(getattr(task, "interrupts", ()) for task in saved.tasks)
        final = await graph_after_restart.ainvoke(Command(resume={"decision": "approve", "change_request": None}), config=config)
        assert final["report"]["status"] == "completed"
        assert final["report"]["run_id"] == run_id


@pytest.mark.asyncio
async def test_rejection_never_executes_action_and_change_request_reinterrupts(tmp_path: Path):
    db_path = tmp_path / "reviews.sqlite3"
    repo = Repository(db_path)
    await repo.initialize()
    async with AsyncSqliteSaver.from_conn_string(str(db_path)) as saver:
        from app.graph import build_graph_with_checkpointer
        graph = build_graph_with_checkpointer(repo, Settings(database_path=db_path, llm_provider="demo"), saver)
        reject_run, reject_thread, reject_input = await make_run(repo, "seed-job-config")
        _, reject_config = await wait_for_review(graph, reject_input, reject_thread)
        rejected = await graph.ainvoke(Command(resume={"decision": "reject", "change_request": "Hold until change window."}), config=reject_config)
        assert rejected["report"]["status"] == "rejected"
        assert rejected["report"]["outcome"].get("action") is None
        assert (await repo.get_config("seed-job-config"))["billing.retries"] == "many"

        _, change_thread, change_input = await make_run(repo, "seed-db-contention")
        _, change_config = await wait_for_review(graph, change_input, change_thread)
        await graph.ainvoke(Command(resume={"decision": "request_changes", "change_request": "Limit analytics concurrency to 10 connections."}), config=change_config)
        next_review = await graph.aget_state(change_config)
        assert any(getattr(task, "interrupts", ()) for task in next_review.tasks)
        assert next_review.values["remediation"]["changes"][0]["value"] == 10
        assert "not measured" in next_review.values["remediation"]["verification"]
        final = await graph.ainvoke(Command(resume={"decision": "approve", "change_request": None}), config=change_config)
        assert final["report"]["status"] == "completed"
        assert "not measured" in final["report"]["outcome"]["verification_summary"]
        assert (await repo.get_config("seed-db-contention"))["analytics.concurrency"] == 10


@pytest.mark.asyncio
async def test_checkpoint_history_and_branch_snapshot_are_available(tmp_path: Path):
    db_path = tmp_path / "history.sqlite3"
    repo = Repository(db_path)
    await repo.initialize()
    async with AsyncSqliteSaver.from_conn_string(str(db_path)) as saver:
        from app.graph import build_graph_with_checkpointer
        graph = build_graph_with_checkpointer(repo, Settings(database_path=db_path, llm_provider="demo"), saver)
        _, thread_id, initial = await make_run(repo, "seed-api-5xx")
        _, config = await wait_for_review(graph, initial, thread_id)
        snapshots = [snapshot async for snapshot in graph.aget_state_history(config, limit=20)]
        assert snapshots
        assert all(item.config["configurable"].get("checkpoint_id") for item in snapshots)
        assert any(item.values.get("diagnosis") for item in snapshots if isinstance(item.values, dict))


@pytest.mark.asyncio
async def test_action_transaction_is_idempotent(tmp_path: Path):
    db_path = tmp_path / "idempotency.sqlite3"
    repo = Repository(db_path)
    await repo.initialize()
    run_id, _, _ = await make_run(repo, "seed-api-5xx")
    proposal = {"summary": "Restore known-good route.", "changes": [{"key": "gateway.upstream_route", "value": "payments-v1"}]}
    first = await repo.execute_simulated_action(run_id, "seed-api-5xx", f"action-{run_id}", proposal)
    second = await repo.execute_simulated_action(run_id, "seed-api-5xx", f"action-{run_id}", proposal)
    assert first == second
    assert first["before"]["gateway.upstream_route"] == "payments-v2"
    assert first["after"]["gateway.upstream_route"] == "payments-v1"
    async with repo.connect() as db:
        cursor = await db.execute("SELECT COUNT(*) FROM actions WHERE run_id=?", (run_id,))
        assert (await cursor.fetchone())[0] == 1


@pytest.mark.asyncio
async def test_unknown_incident_does_not_create_a_fictitious_action(tmp_path: Path):
    db_path = tmp_path / "unknown.sqlite3"
    repo = Repository(db_path)
    await repo.initialize()
    incident = await repo.create_incident({"title": "Unknown service failure", "description": "The service is failing and no telemetry is connected.", "service": "unknown-service", "severity": "high"})
    run_id, thread_id, initial = await make_run(repo, incident["id"])
    async with AsyncSqliteSaver.from_conn_string(str(db_path)) as saver:
        from app.graph import build_graph_with_checkpointer
        graph = build_graph_with_checkpointer(repo, Settings(database_path=db_path, llm_provider="demo"), saver)
        result = await graph.ainvoke(initial, config={"configurable": {"thread_id": thread_id}, "recursion_limit": 40})
        assert result["report"]["status"] == "inconclusive"
        assert not result["report"]["outcome"].get("action")
        async with repo.connect() as db:
            cursor = await db.execute("SELECT COUNT(*) FROM actions WHERE run_id=?", (run_id,))
            assert (await cursor.fetchone())[0] == 0


@pytest.mark.asyncio
async def test_verified_memory_is_recalled_by_later_matching_run(tmp_path: Path):
    db_path = tmp_path / "memory.sqlite3"
    repo = Repository(db_path)
    await repo.initialize()
    async with AsyncSqliteSaver.from_conn_string(str(db_path)) as saver:
        from app.graph import build_graph_with_checkpointer
        graph = build_graph_with_checkpointer(repo, Settings(database_path=db_path, llm_provider="demo"), saver)
        _, first_thread, first_input = await make_run(repo, "seed-api-5xx")
        _, first_config = await wait_for_review(graph, first_input, first_thread)
        await graph.ainvoke(Command(resume={"decision": "approve", "change_request": None}), config=first_config)
        _, second_thread, second_input = await make_run(repo, "seed-api-5xx")
        second_paused, _ = await wait_for_review(graph, second_input, second_thread)
        assert second_paused["memory_lessons"]
        assert second_paused["memory_lessons"][0]["source_run_id"] != second_input["run_id"]

