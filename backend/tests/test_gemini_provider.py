from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.types import Command
from pydantic import ValidationError

from app.config import Settings
from app.database import Repository
from app.graph import build_graph
from app.live_model import ProviderError, ProviderTransientError, live_summary, validate_provider
from test_graph_workflow import make_run, wait_for_review


def gemini_settings(db_path: Path, **kwargs) -> Settings:
    return Settings(database_path=db_path, llm_provider="gemini", gemini_api_key="test-only-key", **kwargs)


def install_provider_mock(monkeypatch, handler):
    original_client = httpx.AsyncClient
    transport = httpx.MockTransport(handler)
    monkeypatch.setattr("app.live_model.httpx.AsyncClient", lambda **kwargs: original_client(transport=transport, **kwargs))


def provider_response(request: httpx.Request, evidence_id: str | None = None) -> httpx.Response:
    body = json.loads(request.content)
    payload = json.loads(body["contents"][0]["parts"][0]["text"])
    selected_id = evidence_id if evidence_id is not None else payload["evidence"][0]["id"]
    result = {"diagnosis": "Model-generated diagnosis used in the final report.", "confidence": 0.81, "uncertainty": "The sample is simulated.", "evidence_ids": [selected_id]}
    return httpx.Response(200, json={
        "candidates": [{"finishReason": "STOP", "content": {"parts": [{"text": json.dumps(result)}]}}],
        "responseId": "provider-response-test", "modelVersion": "test-version",
        "usageMetadata": {"promptTokenCount": 101, "candidatesTokenCount": 42, "totalTokenCount": 143},
    })


def test_provider_selection_and_configuration(tmp_path):
    assert Settings(database_path=tmp_path / "x", llm_provider="demo").llm_provider == "demo"
    with pytest.raises(ValidationError):
        Settings(database_path=tmp_path / "x", llm_provider="misspelled")
    with pytest.raises(ProviderError, match="GEMINI_API_KEY"):
        validate_provider(Settings(database_path=tmp_path / "x", llm_provider="gemini", gemini_api_key=None))
    with pytest.raises(ProviderError, match="GEMINI_MODEL"):
        validate_provider(gemini_settings(tmp_path / "x", gemini_model="models/bad/path"))


@pytest.mark.asyncio
async def test_gemini_response_is_used_in_graph_report_and_survives_resume(tmp_path, monkeypatch):
    db_path = tmp_path / "shared.sqlite3"
    repo = Repository(db_path)
    await repo.initialize()
    calls = []

    def handler(request):
        calls.append(request)
        assert request.headers["x-goog-api-key"] == "test-only-key"
        body = json.loads(request.content)
        assert body["generationConfig"]["responseFormat"]["text"]["mimeType"] == "APPLICATION_JSON"
        assert ":generateContent" in str(request.url)
        return provider_response(request)

    install_provider_mock(monkeypatch, handler)
    settings = gemini_settings(db_path)
    run_id, thread_id, initial = await make_run(repo, "seed-api-5xx")
    before = await repo.get_config("seed-api-5xx")
    async with AsyncSqliteSaver.from_conn_string(str(db_path)) as saver:
        graph = build_graph(repo, settings, saver)
        paused, config = await wait_for_review(graph, initial, thread_id)
        assert paused["diagnosis"]["diagnosis"] == "Model-generated diagnosis used in the final report."
        assert paused["diagnosis"]["model_call"]["response_id"] == "provider-response-test"
        assert paused["diagnosis"]["model_call"]["usage"]["totalTokenCount"] == 143
        assert set(paused["diagnosis"]["evidence_ids"]).issubset({e["id"] for e in paused["evidence"]})
        assert await repo.get_config("seed-api-5xx") == before
        revised = await graph.ainvoke(Command(resume={"decision": "request_changes", "change_request": "Keep payments-v1"}), config=config)
        assert revised["remediation"]["revision"] == 1
        assert await repo.get_config("seed-api-5xx") == before
    async with AsyncSqliteSaver.from_conn_string(str(db_path)) as saver:
        graph = build_graph(repo, settings, saver)
        final = await graph.ainvoke(Command(resume={"decision": "approve"}), config=config)
        assert final["report"]["diagnosis"]["diagnosis"] == "Model-generated diagnosis used in the final report."
        assert final["report"]["diagnosis"]["model_call"]["response_id"] == "provider-response-test"
        assert final["report"]["outcome"]["verified"] is True
        assert final["report"]["outcome"]["action"]["action_id"] == f"action-{run_id}"
        assert len(calls) == 1, "resuming a checkpoint must not invoke Gemini again"


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["unknown_citation", "invalid_json", "rate_limit"])
async def test_invalid_provider_results_never_become_a_diagnosis(tmp_path, monkeypatch, kind):
    settings = gemini_settings(tmp_path / "x")
    evidence = [{"id": "known", "source": "logs", "summary": "a", "detail": "b"}]

    def handler(request):
        if kind == "unknown_citation":
            return provider_response(request, "invented-id")
        if kind == "invalid_json":
            return httpx.Response(200, json={"candidates": [{"content": {"parts": [{"text": "not-json"}]}}]})
        return httpx.Response(429, json={"error": "do not expose this body"})

    install_provider_mock(monkeypatch, handler)
    error_type = ProviderTransientError if kind == "rate_limit" else ProviderError
    with pytest.raises(error_type):
        await live_summary(settings, {"title": "incident"}, evidence, [])


@pytest.mark.asyncio
async def test_provider_failure_is_visible_and_does_not_fall_back(tmp_path, monkeypatch):
    import app.main as main

    db_path = tmp_path / "failed.sqlite3"
    repo = Repository(db_path)
    await repo.initialize()
    settings = gemini_settings(db_path)
    monkeypatch.setattr(main, "settings", settings)
    install_provider_mock(monkeypatch, lambda request: httpx.Response(401, json={"secret": "must-not-be-displayed"}))
    async with AsyncSqliteSaver.from_conn_string(str(db_path)) as saver:
        graph = build_graph(repo, settings, saver)
        run_id, thread_id, initial = await make_run(repo, "seed-job-config")
        request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(repo=repo, graph=graph)))
        await main.run_worker(request, run_id, initial_input=initial)
        run = await repo.get_run(run_id)
        assert run["status"] == "failed"
        assert "authentication" in run["error"].lower()
        assert "must-not-be-displayed" not in run["error"]
        snapshot = await graph.aget_state({"configurable": {"thread_id": thread_id}})
        assert not snapshot.values.get("diagnosis")
        assert not snapshot.values.get("report")


@pytest.mark.asyncio
async def test_transient_provider_error_retries_once(tmp_path, monkeypatch):
    db_path = tmp_path / "retry.sqlite3"
    repo = Repository(db_path)
    await repo.initialize()
    attempts = []

    def handler(request):
        attempts.append(1)
        return httpx.Response(429) if len(attempts) == 1 else provider_response(request)

    install_provider_mock(monkeypatch, handler)
    async with AsyncSqliteSaver.from_conn_string(str(db_path)) as saver:
        graph = build_graph(repo, gemini_settings(db_path), saver)
        _, thread_id, initial = await make_run(repo, "seed-job-config")
        paused, _ = await wait_for_review(graph, initial, thread_id)
        assert len(attempts) == 2
        assert paused["diagnosis"]["model_call"]["status"] == "succeeded"
