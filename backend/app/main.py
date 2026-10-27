from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, PlainTextResponse, StreamingResponse
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.types import Command

from app.config import PROJECT_ROOT, get_settings
from app.database import Repository
from app.graph import build_graph
from app.live_model import ProviderError, validate_provider
from app.models import IncidentCreate, InvestigationCreate, ReviewRequest
from app.security import POCSecurityMiddleware, validate_deployment


class JsonLogFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {"timestamp": datetime.now(UTC).isoformat(), "level": record.levelname, "logger": record.name, "message": record.getMessage()}
        for field in ("run_id", "thread_id", "node", "tool", "attempt", "incident_id", "provider", "model", "duration_ms", "response_id", "usage"):
            value = getattr(record, field, None)
            if value is not None:
                payload[field] = value
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


def configure_logging() -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(JsonLogFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(logging.INFO)


configure_logging()
logger = logging.getLogger("incidentpilot.api")
settings = get_settings()
active_tasks: dict[str, asyncio.Task] = {}
run_locks: dict[str, asyncio.Lock] = {}


def run_lock(run_id: str) -> asyncio.Lock:
    return run_locks.setdefault(run_id, asyncio.Lock())


@asynccontextmanager
async def lifespan(app: FastAPI):
    if settings.app_env == "production":
        validate_provider(settings)
        validate_deployment(settings)
        if not (PROJECT_ROOT / "frontend" / "dist" / "index.html").is_file():
            raise RuntimeError("The production frontend build is missing.")
    repo = Repository(settings.database_path)
    await repo.initialize()
    await repo.recover_runs()
    if settings.langsmith_tracing and settings.langsmith_api_key:
        os.environ["LANGSMITH_TRACING"] = "true"
        os.environ["LANGSMITH_API_KEY"] = settings.langsmith_api_key
        os.environ["LANGSMITH_PROJECT"] = settings.langsmith_project
        logger.info("langsmith_tracing_enabled", extra={"project": settings.langsmith_project})
    elif settings.langsmith_tracing:
        logger.warning("langsmith_tracing_skipped", extra={"reason": "LANGSMITH_API_KEY is not configured"})
    async with AsyncSqliteSaver.from_conn_string(str(settings.database_path)) as checkpointer:
        app.state.repo = repo
        app.state.graph = build_graph(repo, settings, checkpointer)
        app.state.checkpointer = checkpointer
        logger.info("application_started", extra={"database_path": str(settings.database_path), "llm_provider": settings.llm_provider, "durable_checkpointing": "sqlite"})
        yield
        for task in list(active_tasks.values()):
            if not task.done():
                task.cancel()
        await asyncio.gather(*active_tasks.values(), return_exceptions=True)
        logger.info("application_stopped")


app = FastAPI(title="IncidentPilot", version="1.0.0", description="Simulated incident investigation workspace", lifespan=lifespan)
app.add_middleware(POCSecurityMiddleware, settings=settings)
app.add_middleware(CORSMiddleware, allow_origins=[] if settings.app_env == "production" else [settings.frontend_origin, "http://localhost:5173", "http://127.0.0.1:5173"], allow_methods=["*"], allow_headers=["*"], expose_headers=["Content-Disposition"])


def graph_config(thread_id: str, checkpoint_id: str | None = None) -> dict:
    configurable: dict[str, str] = {"thread_id": thread_id}
    if checkpoint_id:
        configurable["checkpoint_id"] = checkpoint_id
    return {"configurable": configurable, "recursion_limit": 40, "metadata": {"application": "IncidentPilot", "thread_id": thread_id}}


def interrupted(snapshot: Any) -> bool:
    for task in getattr(snapshot, "tasks", ()):
        if getattr(task, "interrupts", ()):
            return True
    return False


async def snapshot_for(request: Request, run: dict):
    return await request.app.state.graph.aget_state(graph_config(run["thread_id"]))


async def run_worker(request: Request, run_id: str, initial_input: dict | None = None, resume_value: dict | None = None) -> None:
    repo, graph = request.app.state.repo, request.app.state.graph
    run = await repo.get_run(run_id)
    if not run:
        return
    config = graph_config(run["thread_id"])
    started = time.perf_counter()
    await repo.update_run(run_id, "running")
    logger.info("graph_run_started", extra={"run_id": run_id, "thread_id": run["thread_id"], "incident_id": run["incident_id"]})
    await repo.add_event(run_id, "investigation_started", "Investigation started in the simulated environment.", {"thread_id": run["thread_id"], "llm_provider": settings.llm_provider})
    graph_input: Any = Command(resume=resume_value) if resume_value is not None else initial_input
    try:
        async for part in graph.astream(graph_input, config=config, stream_mode=["updates", "custom"], version="v2"):
            if part["type"] == "custom":
                payload = part["data"]
                kind = payload.get("kind", "progress") if isinstance(payload, dict) else "progress"
                message = payload.get("message", kind.replace("_", " ").capitalize()) if isinstance(payload, dict) else str(payload)
                await repo.add_event(run_id, kind, message, payload if isinstance(payload, dict) else {"value": payload})
            elif part["type"] == "updates":
                for node_name, update in part["data"].items():
                    await repo.add_event(run_id, "node_completed", f"Workflow step completed: {node_name.replace('_', ' ')}.", {"node": node_name, "updated_fields": sorted(update.keys()) if isinstance(update, dict) else []})
        snapshot = await graph.aget_state(config)
        if interrupted(snapshot):
            await repo.update_run(run_id, "approval_required")
        elif isinstance(snapshot.values, dict) and snapshot.values.get("report"):
            report = snapshot.values["report"]
            await repo.update_run(run_id, report.get("status", "completed"), report=report)
        elif snapshot.next:
            await repo.update_run(run_id, "recoverable")
        else:
            await repo.update_run(run_id, "failed", error="Graph ended without an approval interrupt or final report.")
        current = await repo.get_run(run_id)
        duration_ms = round((time.perf_counter() - started) * 1000)
        if current:
            await repo.update_run(run_id, current["status"], duration_ms=duration_ms)
        logger.info("graph_run_finished", extra={"run_id": run_id, "thread_id": run["thread_id"], "duration_ms": duration_ms})
    except asyncio.CancelledError:
        await repo.update_run(run_id, "recoverable", error="Server stopped while this run was active; the last LangGraph checkpoint was preserved.")
        raise
    except ProviderError as exc:
        logger.warning("model_synthesis_failed", extra={"run_id": run_id, "thread_id": run["thread_id"], "provider": settings.llm_provider})
        await repo.update_run(run_id, "failed", error=str(exc))
        await repo.add_event(run_id, "investigation_failed", "The model request failed. The latest checkpoint was retained.", {"error": str(exc), "provider": settings.llm_provider})
    except Exception as exc:
        logger.exception("graph_run_failed", extra={"run_id": run_id, "thread_id": run["thread_id"]})
        await repo.update_run(run_id, "failed", error=f"{type(exc).__name__}: {str(exc)}")
        await repo.add_event(run_id, "investigation_failed", "The investigation stopped with a recoverable error. The latest checkpoint was retained.", {"error": f"{type(exc).__name__}: {str(exc)}"})
    finally:
        active_tasks.pop(run_id, None)


def spawn_worker(request: Request, run_id: str, initial_input: dict | None = None, resume_value: dict | None = None) -> None:
    task = asyncio.create_task(run_worker(request, run_id, initial_input, resume_value), name=f"incidentpilot:{run_id}")
    active_tasks[run_id] = task


async def present_run(request: Request, run: dict) -> dict:
    result = dict(run)
    snapshot = await snapshot_for(request, run)
    if interrupted(snapshot):
        result["status"] = "approval_required"
    if isinstance(snapshot.values, dict):
        state = snapshot.values
        result["current"] = {
            "classification": state.get("classification"), "diagnosis": state.get("diagnosis"),
            "evidence": state.get("evidence", []), "findings": state.get("findings", []),
            "remediation": state.get("remediation"), "approvals": state.get("approvals", []),
            "outcome": state.get("outcome"), "memory_lessons": state.get("memory_lessons", []),
        }
        if state.get("report"):
            result["report"] = state["report"]
    return result


@app.get("/api/health")
async def health(request: Request) -> dict:
    return {"status": "ok", "app": "IncidentPilot", "llm_provider": settings.llm_provider, "simulation": True, "checkpointing": "SQLite"}


@app.get("/api/incidents")
async def list_incidents(request: Request) -> list[dict]:
    return await request.app.state.repo.list_incidents()


@app.post("/api/incidents", status_code=201)
async def create_incident(payload: IncidentCreate, request: Request) -> dict:
    return await request.app.state.repo.create_incident(payload.model_dump())


@app.get("/api/incidents/{incident_id}")
async def get_incident(incident_id: str, request: Request) -> dict:
    incident = await request.app.state.repo.get_incident(incident_id)
    if not incident:
        raise HTTPException(404, "Incident not found")
    return incident


@app.get("/api/investigations")
async def list_investigations(request: Request, incident_id: str | None = None) -> list[dict]:
    return await request.app.state.repo.list_runs(incident_id)


@app.post("/api/investigations", status_code=202)
async def start_investigation(payload: InvestigationCreate, request: Request) -> dict:
    if settings.app_env == "production" and len(active_tasks) >= 3:
        raise HTTPException(429, "The demo is busy; try again after an investigation completes.")
    incident = await request.app.state.repo.get_incident(payload.incident_id)
    if not incident:
        raise HTTPException(404, "Incident not found")
    run_id = f"run-{uuid4().hex[:12]}"
    thread_id = f"incidentpilot-{uuid4()}"
    await request.app.state.repo.create_investigation(run_id, incident["id"], thread_id)
    initial = {
        "incident_id": incident["id"], "run_id": run_id, "thread_id": thread_id,
        "title": incident["title"], "description": incident["description"], "service": incident["service"],
        "severity": incident["severity"], "incident_kind": incident["kind"], "current_iteration": 0,
        "evidence": [], "findings": [], "events": [], "approvals": [], "revision": 0,
    }
    spawn_worker(request, run_id, initial_input=initial)
    return {"run_id": run_id, "thread_id": thread_id, "incident_id": incident["id"], "status": "queued"}


@app.get("/api/investigations/{run_id}")
async def get_investigation(run_id: str, request: Request) -> dict:
    run = await request.app.state.repo.get_run(run_id)
    if not run:
        raise HTTPException(404, "Investigation not found")
    return await present_run(request, run)


@app.post("/api/investigations/{run_id}/review", status_code=202)
async def review_investigation(run_id: str, payload: ReviewRequest, request: Request) -> dict:
    if payload.decision == "request_changes" and not payload.change_request:
        raise HTTPException(422, "Describe the requested proposal change.")
    run = await request.app.state.repo.get_run(run_id)
    if not run:
        raise HTTPException(404, "Investigation not found")
    if run["status"] in ("completed", "rejected", "inconclusive"):
        return {"run_id": run_id, "status": run["status"], "already_finished": True}
    async with run_lock(run_id):
        if run_id in active_tasks:
            raise HTTPException(409, "A review response is already being processed.")
        claimed = await request.app.state.repo.claim_resume(run_id)
        if not claimed:
            current = await request.app.state.repo.get_run(run_id)
            if current and current["status"] in ("completed", "rejected", "inconclusive"):
                return {"run_id": run_id, "status": current["status"], "already_finished": True}
            raise HTTPException(409, "This investigation is not waiting for a review response.")
        value = {"decision": payload.decision, "change_request": payload.change_request}
        spawn_worker(request, run_id, resume_value=value)
    return {"run_id": run_id, "status": "resuming"}


@app.post("/api/investigations/{run_id}/resume", status_code=202)
async def resume_recoverable(run_id: str, request: Request) -> dict:
    run = await request.app.state.repo.get_run(run_id)
    if not run:
        raise HTTPException(404, "Investigation not found")
    if run_id in active_tasks:
        raise HTTPException(409, "Investigation is already running.")
    snapshot = await snapshot_for(request, run)
    if interrupted(snapshot):
        raise HTTPException(409, "This investigation is paused for approval. Submit a review decision instead.")
    if not snapshot.next:
        raise HTTPException(409, "No recoverable graph work remains for this investigation.")
    async with run_lock(run_id):
        async with request.app.state.repo.connect() as db:
            cursor = await db.execute("UPDATE investigations SET status='running',updated_at=? WHERE run_id=? AND status IN ('recoverable','failed')", (__import__('app.database', fromlist=['utc_now']).utc_now(), run_id))
            await db.commit()
            if cursor.rowcount != 1:
                raise HTTPException(409, "Investigation status changed; refresh and try again.")
        spawn_worker(request, run_id)
    return {"run_id": run_id, "status": "resuming"}


@app.get("/api/investigations/{run_id}/events")
async def stream_events(run_id: str, request: Request, after_id: int = Query(default=0, ge=0)) -> StreamingResponse:
    run = await request.app.state.repo.get_run(run_id)
    if not run:
        raise HTTPException(404, "Investigation not found")

    async def event_source():
        cursor = after_id
        quiet_cycles = 0
        while True:
            if await request.is_disconnected():
                break
            rows = await request.app.state.repo.list_events(run_id, cursor)
            for event in rows:
                cursor = event["id"]
                yield f"id: {cursor}\nevent: update\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"
            status = (await request.app.state.repo.get_run(run_id) or {}).get("status")
            if rows:
                quiet_cycles = 0
            else:
                quiet_cycles += 1
            if status not in ("queued", "running", "resuming") and quiet_cycles >= 2:
                yield f"event: done\ndata: {json.dumps({'status': status})}\n\n"
                break
            if quiet_cycles and quiet_cycles % 20 == 0:
                yield ": keep-alive\n\n"
            await asyncio.sleep(0.35)

    return StreamingResponse(event_source(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/api/investigations/{run_id}/checkpoints")
async def checkpoint_history(run_id: str, request: Request, limit: int = Query(default=40, ge=1, le=100)) -> dict:
    run = await request.app.state.repo.get_run(run_id)
    if not run:
        raise HTTPException(404, "Investigation not found")
    snapshots = []
    async for snap in request.app.state.graph.aget_state_history(graph_config(run["thread_id"]), limit=limit):
        values = snap.values if isinstance(snap.values, dict) else {}
        snapshots.append({
            "checkpoint_id": snap.config.get("configurable", {}).get("checkpoint_id"),
            "created_at": snap.created_at,
            "next": list(snap.next),
            "node": snap.metadata.get("source") or snap.metadata.get("step"),
            "evidence_count": len(values.get("evidence", [])),
            "finding_count": len(values.get("findings", [])),
            "approval_count": len(values.get("approvals", [])),
            "has_diagnosis": bool(values.get("diagnosis")),
            "interrupted": interrupted(snap),
        })
    return {"run_id": run_id, "thread_id": run["thread_id"], "checkpoints": snapshots}


@app.post("/api/investigations/{run_id}/branch", status_code=202)
async def branch_investigation(run_id: str, request: Request, checkpoint_id: str = Query(..., min_length=8, max_length=255)) -> dict:
    if settings.app_env == "production" and len(active_tasks) >= 3:
        raise HTTPException(429, "The demo is busy; try again after an investigation completes.")
    source = await request.app.state.repo.get_run(run_id)
    if not source:
        raise HTTPException(404, "Source investigation not found")
    source_snapshot = await request.app.state.graph.aget_state(graph_config(source["thread_id"], checkpoint_id))
    if not isinstance(source_snapshot.values, dict):
        raise HTTPException(400, "Checkpoint has no graph state to branch from.")
    state = source_snapshot.values
    run_id_new = f"run-{uuid4().hex[:12]}"
    thread_id_new = f"incidentpilot-{uuid4()}"
    await request.app.state.repo.create_investigation(run_id_new, source["incident_id"], thread_id_new, source_run_id=run_id, source_checkpoint_id=checkpoint_id)
    initial = {
        "incident_id": state.get("incident_id", source["incident_id"]), "run_id": run_id_new, "thread_id": thread_id_new,
        "title": state.get("title", source["incident_title"]), "description": state.get("description", source["incident_description"]),
        "service": state.get("service", source["service"]), "severity": state.get("severity", source["severity"]),
        "incident_kind": state.get("incident_kind", source["incident_kind"]), "classification": state.get("classification"),
        "current_iteration": state.get("current_iteration", 0), "evidence": state.get("evidence", []),
        "findings": state.get("findings", []), "memory_lessons": state.get("memory_lessons", []),
        "approvals": [], "revision": 0, "branch_seed": True, "source_checkpoint_id": checkpoint_id,
    }
    await request.app.state.repo.add_event(run_id_new, "branch_created", "A new investigation branch started from a saved checkpoint snapshot.", {"source_run_id": run_id, "source_checkpoint_id": checkpoint_id})
    spawn_worker(request, run_id_new, initial_input=initial)
    return {"run_id": run_id_new, "thread_id": thread_id_new, "incident_id": source["incident_id"], "source_run_id": run_id, "source_checkpoint_id": checkpoint_id, "status": "queued", "branch": True}


@app.get("/api/memory")
async def list_memory(request: Request, kind: str | None = None) -> list[dict]:
    return await request.app.state.repo.list_lessons(kind)


@app.get("/api/investigations/{run_id}/report.md")
async def export_report(run_id: str, request: Request) -> PlainTextResponse:
    run = await request.app.state.repo.get_run(run_id)
    if not run:
        raise HTTPException(404, "Investigation not found")
    report = run.get("report")
    if not report:
        snapshot = await snapshot_for(request, run)
        report = snapshot.values.get("report") if isinstance(snapshot.values, dict) else None
    if not report:
        raise HTTPException(409, "The report is not ready yet.")
    dx = report.get("diagnosis", {})
    action = report.get("remediation", {})
    outcome = report.get("outcome", {})
    citations = "\n".join(f"- [{item.get('id')}] **{item.get('source')}** — {item.get('summary')}" for item in report.get("evidence", [])) or "- No independently verifiable evidence is available."
    approval = report.get("approval", [])
    approvals = ", ".join(item.get("decision", "") for item in approval) or "No action approved"
    markdown = f"""# Incident report: {report.get('incident_title', 'Incident')}\n\n**Status:** {report.get('status')}  \n**Environment:** {report.get('environment_label')}  \n**Run:** `{run_id}`  \n**Service:** {report.get('service')} · **Severity:** {report.get('severity')}\n\n## Diagnosis\n\n{dx.get('diagnosis', 'Pending')}\n\n**Confidence:** {dx.get('confidence', 0):.0%}  \n**Remaining uncertainty:** {report.get('remaining_uncertainty', 'Not recorded')}\n\n## Evidence\n\n{citations}\n\n## Proposed remediation\n\n{action.get('summary', 'No action proposed')}\n\n**Human decision:** {approvals}\n\n## Outcome verification\n\n{outcome.get('verification_summary', 'No remediation was executed.')}  \n**Verified:** {outcome.get('verified', False)}  \n**Simulated action:** {outcome.get('action', {}).get('action_id', 'None')}\n\n---\nGenerated by IncidentPilot. All integrations and remediation actions in this proof of concept are simulated.\n"""
    model_call = dx.get("model_call") or {}
    provenance = f"**Source:** {dx.get('source', 'deterministic demo rules')}  \n"
    if model_call:
        markdown = markdown.replace("**Confidence:**", "**Model-reported confidence (not calibrated):**", 1)
        provenance += f"**Provider response:** {model_call.get('provider')} / {model_call.get('model')} · {model_call.get('stage')} · {model_call.get('status')} · {model_call.get('duration_ms')} ms  \n"
        if model_call.get("response_id"):
            provenance += f"**Provider response ID:** {model_call['response_id']}  \n"
        if model_call.get("usage"):
            provenance += f"**Usage:** {json.dumps(model_call['usage'], ensure_ascii=False)}  \n"
        provenance += f"**Cited evidence IDs:** {', '.join(dx.get('evidence_ids', [])) or 'None'}  \n"
    markdown = markdown.replace("## Evidence\n\n", f"## Diagnosis provenance\n\n{provenance}\n## Simulated evidence\n\n", 1)
    markdown = markdown.replace("## Proposed remediation", "## Proposed simulated remediation", 1)
    markdown = markdown.replace("All integrations and remediation actions in this proof of concept are simulated.", "Source records, remediation actions, and verification in this proof of concept are simulated. Model provenance is recorded above.")
    return PlainTextResponse(markdown, media_type="text/markdown", headers={"Content-Disposition": f'attachment; filename="incident-report-{run_id}.md"'})


@app.get("/{full_path:path}", include_in_schema=False)
async def frontend(full_path: str):
    directory = (PROJECT_ROOT / "frontend" / "dist").resolve()
    target = (directory / full_path).resolve()
    if full_path.startswith("api/") or not target.is_relative_to(directory):
        raise HTTPException(404, "Not found")
    if target.is_file():
        return FileResponse(target)
    if full_path.startswith("assets/"):
        raise HTTPException(404, "Asset not found")
    index = directory / "index.html"
    if not index.is_file():
        raise HTTPException(404, "Frontend build not found")
    return FileResponse(index)
