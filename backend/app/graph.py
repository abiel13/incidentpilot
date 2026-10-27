from __future__ import annotations

import json
import logging
from typing import Annotated, Any, TypedDict

from langchain_core.messages import AIMessage, AnyMessage, ToolMessage
from langchain_core.tools import tool
from langgraph.config import get_stream_writer
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.prebuilt import ToolNode
from langgraph.types import RetryPolicy, Send, interrupt
from pydantic import BaseModel, Field

from app.config import Settings
from app.database import Repository
from app.live_model import ProviderError, ProviderTransientError, live_summary

logger = logging.getLogger("incidentpilot.graph")


def merge_records_by_id(current: list[dict] | None, incoming: list[dict] | None) -> list[dict]:
    """Append reducer for parallel branches; stable IDs prevent duplicate findings on replay."""
    merged: dict[str, dict] = {}
    for item in [*(current or []), *(incoming or [])]:
        key = str(item.get("id") or item.get("evidence_id") or item.get("finding_id") or item.get("approval_id") or json.dumps(item, sort_keys=True))
        merged[key] = item
    return list(merged.values())


class Event(TypedDict, total=False):
    id: str
    kind: str
    message: str
    payload: dict


class Approval(TypedDict, total=False):
    approval_id: str
    decision: str
    note: str
    proposal_revision: int


class IncidentState(TypedDict, total=False):
    incident_id: str
    run_id: str
    thread_id: str
    title: str
    description: str
    service: str
    severity: str
    incident_kind: str
    classification: dict
    current_iteration: int
    max_iterations: int
    specialist_ids: list[str]
    evidence: Annotated[list[dict], merge_records_by_id]
    findings: Annotated[list[dict], merge_records_by_id]
    events: Annotated[list[Event], merge_records_by_id]
    memory_lessons: list[dict]
    memory_used: list[str]
    diagnosis: dict
    remediation: dict
    approvals: Annotated[list[Approval], merge_records_by_id]
    outcome: dict
    report: dict
    review_decision: str
    review_message: str
    revision: int
    branch_seed: bool
    source_checkpoint_id: str | None


class SpecialistState(TypedDict, total=False):
    incident_id: str
    run_id: str
    description: str
    service: str
    specialist_id: str
    iteration: int
    messages: Annotated[list[AnyMessage], add_messages]
    evidence: list[dict]
    findings: list[dict]


class ToolArgs(BaseModel):
    incident_id: str = Field(min_length=3, max_length=100)
    run_id: str = Field(min_length=3, max_length=100)
    iteration: int = Field(ge=0, le=2)
    window_minutes: int = Field(default=5, ge=1, le=60)


class SearchLogsArgs(ToolArgs):
    service: str = Field(min_length=2, max_length=80)
    search_term: str = Field(default="error", min_length=1, max_length=100)


class MetricArgs(ToolArgs):
    metric: str = Field(default="all", min_length=1, max_length=80)


class DeploymentArgs(ToolArgs):
    service: str = Field(min_length=2, max_length=80)


class ConfigArgs(ToolArgs):
    service: str = Field(min_length=2, max_length=80)
    setting: str = Field(default="all", min_length=1, max_length=80)


class RunbookArgs(ToolArgs):
    query: str = Field(min_length=1, max_length=240)


class TransientToolError(Exception):
    """A retryable fault injected by the local simulator."""


def _emit(data: dict) -> None:
    try:
        get_stream_writer()(data)
    except RuntimeError:
        # Nodes can also be invoked in unit tests without a stream consumer.
        return


def _records_json(records: list[dict], **meta: Any) -> str:
    return json.dumps({"records": records, **meta}, ensure_ascii=False)


def build_tools(repo: Repository):
    @tool("query_seeded_logs", args_schema=SearchLogsArgs)
    async def query_seeded_logs(incident_id: str, run_id: str, iteration: int, service: str, search_term: str, window_minutes: int = 5) -> str:
        """Search simulated application logs for the selected incident and service."""
        attempt = await repo.bump_tool_attempt(run_id, "query_seeded_logs", iteration)
        _emit({"kind": "tool_started", "tool": "query_seeded_logs", "incident_id": incident_id, "iteration": iteration, "attempt": attempt})
        if incident_id == "seed-api-5xx" and iteration == 0 and attempt == 1:
            logger.warning("simulated_transient_tool_failure", extra={"run_id": run_id, "tool": "query_seeded_logs", "attempt": attempt})
            _emit({"kind": "tool_retryable_failure", "tool": "query_seeded_logs", "attempt": attempt, "message": "Simulated telemetry timeout; LangGraph will retry this tool step."})
            raise TransientToolError("Simulated one-time log-store timeout.")
        records = await repo.query_records(incident_id, "logs")
        _emit({"kind": "tool_succeeded", "tool": "query_seeded_logs", "record_count": len(records), "attempt": attempt, "retried": attempt > 1})
        return _records_json(records, simulated=True, window_minutes=window_minutes, service=service, search_term=search_term, attempts=attempt)

    @tool("read_metrics", args_schema=MetricArgs)
    async def read_metrics(incident_id: str, run_id: str, iteration: int, metric: str = "all", window_minutes: int = 5) -> str:
        """Read seeded application and infrastructure metrics for the incident."""
        attempt = await repo.bump_tool_attempt(run_id, "read_metrics", iteration)
        _emit({"kind": "tool_started", "tool": "read_metrics", "incident_id": incident_id, "iteration": iteration, "attempt": attempt})
        records = await repo.query_records(incident_id, "metrics")
        if iteration > 0:
            records = [row for row in records if row.get("signal") == "additional_evidence"] or records
        _emit({"kind": "tool_succeeded", "tool": "read_metrics", "record_count": len(records), "attempt": attempt})
        return _records_json(records, simulated=True, metric=metric, window_minutes=window_minutes, attempts=attempt)

    @tool("inspect_deployments", args_schema=DeploymentArgs)
    async def inspect_deployments(incident_id: str, run_id: str, iteration: int, service: str, window_minutes: int = 5) -> str:
        """Inspect simulated deployment history and configuration changes."""
        attempt = await repo.bump_tool_attempt(run_id, "inspect_deployments", iteration)
        _emit({"kind": "tool_started", "tool": "inspect_deployments", "incident_id": incident_id, "iteration": iteration, "attempt": attempt})
        records = await repo.query_records(incident_id, "deployments")
        _emit({"kind": "tool_succeeded", "tool": "inspect_deployments", "record_count": len(records), "attempt": attempt})
        return _records_json(records, simulated=True, service=service, window_minutes=window_minutes, attempts=attempt)

    @tool("read_service_configuration", args_schema=ConfigArgs)
    async def read_service_configuration(incident_id: str, run_id: str, iteration: int, service: str, setting: str = "all", window_minutes: int = 5) -> str:
        """Read current simulated service configuration and its expected schema."""
        attempt = await repo.bump_tool_attempt(run_id, "read_service_configuration", iteration)
        _emit({"kind": "tool_started", "tool": "read_service_configuration", "incident_id": incident_id, "iteration": iteration, "attempt": attempt})
        records = await repo.query_records(incident_id, "config")
        _emit({"kind": "tool_succeeded", "tool": "read_service_configuration", "record_count": len(records), "attempt": attempt})
        return _records_json(records, simulated=True, service=service, setting=setting, current_values=await repo.get_config(incident_id), attempts=attempt)

    @tool("search_runbooks", args_schema=RunbookArgs)
    async def search_runbooks(incident_id: str, run_id: str, iteration: int, query: str, window_minutes: int = 5) -> str:
        """Search the local simulated incident response runbooks."""
        attempt = await repo.bump_tool_attempt(run_id, "search_runbooks", iteration)
        _emit({"kind": "tool_started", "tool": "search_runbooks", "incident_id": incident_id, "iteration": iteration, "attempt": attempt})
        records = await repo.query_records(incident_id, "runbooks")
        _emit({"kind": "tool_succeeded", "tool": "search_runbooks", "record_count": len(records), "attempt": attempt})
        return _records_json(records, simulated=True, query=query, attempts=attempt)

    return [query_seeded_logs, read_metrics, inspect_deployments, read_service_configuration, search_runbooks]


def build_specialist_graph(tools: list):
    async def prepare(state: SpecialistState) -> dict:
        specialist = state["specialist_id"]
        iteration = state.get("iteration", 0)
        window = 10 if iteration > 0 else 5
        common = {"incident_id": state["incident_id"], "run_id": state["run_id"], "iteration": iteration, "window_minutes": window}
        calls: list[dict] = []
        if specialist == "logs":
            calls = [{"name": "query_seeded_logs", "args": {**common, "service": state.get("service", "unknown"), "search_term": state.get("description", "error")[:96]}, "id": f"{state['run_id']}-logs-{iteration}", "type": "tool_call"}]
        elif specialist == "changes":
            calls = [
                {"name": "inspect_deployments", "args": {**common, "service": state.get("service", "unknown")}, "id": f"{state['run_id']}-deploy-{iteration}", "type": "tool_call"},
                {"name": "read_service_configuration", "args": {**common, "service": state.get("service", "unknown")}, "id": f"{state['run_id']}-config-{iteration}", "type": "tool_call"},
                {"name": "search_runbooks", "args": {**common, "query": state.get("description", "incident")[:200]}, "id": f"{state['run_id']}-runbook-{iteration}", "type": "tool_call"},
            ]
        else:
            calls = [{"name": "read_metrics", "args": {**common, "metric": "all"}, "id": f"{state['run_id']}-metrics-{iteration}", "type": "tool_call"}]
        _emit({"kind": "specialist_started", "specialist": specialist, "iteration": iteration, "message": f"{specialist.replace('_', ' ').title()} specialist started."})
        return {"messages": [AIMessage(content="", tool_calls=calls)]}

    async def collect(state: SpecialistState) -> dict:
        tool_messages = [item for item in state.get("messages", []) if isinstance(item, ToolMessage)]
        evidence: list[dict] = []
        tool_names: list[str] = []
        for message in tool_messages:
            tool_names.append(message.name or "simulated_tool")
            try:
                content = json.loads(message.content if isinstance(message.content, str) else json.dumps(message.content))
            except (json.JSONDecodeError, TypeError):
                content = {"records": []}
            for record in content.get("records", []):
                if not isinstance(record, dict) or not record.get("id"):
                    continue
                evidence.append({
                    "id": f"{state['incident_id']}:{state.get('iteration', 0)}:{record['id']}",
                    "source_record_id": record["id"],
                    "source": message.name or "simulated_tool",
                    "timestamp": record.get("timestamp", ""),
                    "summary": record.get("summary", ""),
                    "detail": record.get("detail", ""),
                    "signal": record.get("signal"),
                    "iteration": state.get("iteration", 0),
                })
        root = [item for item in evidence if item.get("signal") == "root_cause"]
        if root:
            summary = " ".join(item["summary"] for item in root)
            confidence = min(0.95, 0.72 + 0.08 * len(root))
        elif evidence:
            summary = "Evidence was collected, but no direct cause signal was found in this specialist's sources."
            confidence = 0.48
        else:
            summary = "The simulated environment did not contain matching telemetry for this specialist."
            confidence = 0.2
        finding_id = f"finding:{state['incident_id']}:{state.get('iteration', 0)}:{state['specialist_id']}"
        finding = {"finding_id": finding_id, "specialist": state["specialist_id"], "iteration": state.get("iteration", 0), "summary": summary, "confidence": round(confidence, 2), "evidence_ids": [item["id"] for item in evidence], "tools": tool_names}
        _emit({"kind": "evidence_found", "specialist": state["specialist_id"], "iteration": state.get("iteration", 0), "count": len(evidence), "message": f"{state['specialist_id'].replace('_', ' ').title()} specialist returned {len(evidence)} evidence item(s)."})
        _emit({"kind": "specialist_completed", "specialist": state["specialist_id"], "iteration": state.get("iteration", 0), "message": f"{state['specialist_id'].replace('_', ' ').title()} specialist completed."})
        return {"evidence": evidence, "findings": [finding]}

    builder = StateGraph(SpecialistState)
    builder.add_node("prepare_tool_calls", prepare)
    builder.add_node(
        "execute_typed_tools",
        ToolNode(tools),
        retry_policy=RetryPolicy(max_attempts=2, retry_on=(TransientToolError,), initial_interval=0.05, backoff_factor=1.2),
    )
    builder.add_node("summarize_findings", collect)
    builder.add_edge(START, "prepare_tool_calls")
    builder.add_edge("prepare_tool_calls", "execute_typed_tools")
    builder.add_edge("execute_typed_tools", "summarize_findings")
    builder.add_edge("summarize_findings", END)
    return builder.compile()


def build_graph(repo: Repository, settings: Settings, checkpointer: Any = None):
    tools = build_tools(repo)
    specialist_graph = build_specialist_graph(tools)

    async def intake(state: IncidentState) -> dict:
        lessons = state.get("memory_lessons")
        if lessons is None:
            lessons = await repo.find_lessons(state.get("incident_kind", "unknown"), state.get("run_id", ""))
        _emit({"kind": "intake_completed", "message": "Incident intake validated; simulated environment selected.", "incident_id": state.get("incident_id")})
        if lessons:
            _emit({"kind": "memory_recalled", "message": f"Recalled {len(lessons)} verified lesson(s) from earlier investigations.", "lessons": [item.get("title") for item in lessons]})
        return {"memory_lessons": lessons, "memory_used": [lesson.get("lesson_id", "") for lesson in lessons], "current_iteration": state.get("current_iteration", 0), "max_iterations": 1, "status": "running"}

    async def classify(state: IncidentState) -> dict:
        kind = state.get("incident_kind", "unknown")
        tags = {"api_errors": ["logs", "changes", "performance"], "database_contention": ["logs", "changes", "performance"], "invalid_configuration": ["logs", "changes", "performance"], "unknown": ["logs", "changes", "performance"]}
        summaries = {"api_errors": "Post-deployment API error increase", "database_contention": "Application latency with database contention indicators", "invalid_configuration": "Background job failures after a configuration change", "unknown": "Unclassified incident; collect broad evidence and preserve uncertainty"}
        _emit({"kind": "incident_classified", "classification": kind, "message": f"Incident classified as {kind.replace('_', ' ')}."})
        return {"classification": {"kind": kind, "summary": summaries.get(kind, summaries["unknown"]), "route": tags.get(kind, tags["unknown"]), "source": "deterministic incident rules"}, "specialist_ids": tags.get(kind, tags["unknown"])}

    async def plan(state: IncidentState) -> dict:
        specialist_ids = state.get("specialist_ids", ["logs", "changes", "performance"])
        _emit({"kind": "investigation_plan", "iteration": state.get("current_iteration", 0), "specialists": specialist_ids, "message": f"Dispatching {len(specialist_ids)} specialist investigations in parallel."})
        return {}

    def dispatch(state: IncidentState) -> list[Send]:
        specialists = state.get("specialist_ids", ["logs", "changes", "performance"])
        if state.get("current_iteration", 0) > 0 and state.get("incident_kind") == "database_contention":
            specialists = ["logs", "performance"]
        return [Send("specialist_runner", {
            "incident_id": state["incident_id"], "run_id": state["run_id"], "description": state["description"],
            "service": state["service"], "specialist_id": specialist, "iteration": state.get("current_iteration", 0),
        }) for specialist in specialists]

    async def run_specialist(state: SpecialistState) -> dict:
        child_input = {**state, "messages": []}
        child_result: dict = {}
        parent_writer = get_stream_writer()
        async for part in specialist_graph.astream(child_input, stream_mode=["updates", "custom"], version="v2"):
            if part["type"] == "custom":
                parent_writer(part["data"])
            elif part["type"] == "updates":
                for value in part["data"].values():
                    if isinstance(value, dict) and any(key in value for key in ("evidence", "findings")):
                        child_result.update(value)
        return {"evidence": child_result.get("evidence", []), "findings": child_result.get("findings", [])}

    async def aggregate(state: IncidentState) -> dict:
        kind = state.get("incident_kind", "unknown")
        evidence = state.get("evidence", [])
        root = [item for item in evidence if item.get("signal") == "root_cause"]
        iteration = state.get("current_iteration", 0)
        used_lessons = state.get("memory_lessons", [])
        if kind == "api_errors" and root:
            diagnosis_text = "The gateway deployment points to payments-v2 while the registered payment service is payments-v1. The gateway rejects requests before they reach the healthy backend."
            confidence = 0.96
            uncertainty = "The local simulator has no live traffic beyond the seeded sample."
        elif kind == "database_contention" and root:
            if iteration == 0:
                diagnosis_text = "The shared database connection pool is nearly saturated during the analytics workload; more samples are needed to separate peak concurrency from baseline use."
                confidence = 0.64
                uncertainty = "The first five-minute sample does not yet show the full duration of pool saturation."
            else:
                diagnosis_text = "The analytics batch workload consumes 44 of 50 shared database connections, leaving no burst capacity for order-api. The extended sample confirms sustained pool wait as the cause of the latency increase."
                confidence = 0.93
                uncertainty = "The simulator verifies pool and latency signals; it does not measure production impact."
        elif kind == "invalid_configuration" and root:
            diagnosis_text = "billing.retries is configured as the string 'many', but the worker schema requires an integer. Workers reject each job before execution."
            confidence = 0.95
            uncertainty = "The simulator verifies configuration validation and queue behavior only."
        else:
            diagnosis_text = "The available simulated environment does not provide enough independent evidence to identify a root cause."
            confidence = 0.3
            uncertainty = "Only the submitted incident description and generic runbook are available; production telemetry has not been connected."
        additional = kind == "database_contention" and iteration < state.get("max_iterations", 1) and confidence < 0.75
        if additional:
            _emit({"kind": "additional_evidence_requested", "message": "Confidence is below 0.75; requesting a wider metrics sample.", "confidence": confidence, "next_iteration": iteration + 1})
        summary = {"diagnosis": diagnosis_text, "confidence": confidence, "uncertainty": uncertainty, "root_cause_signal_count": len(root), "iterations": iteration + 1}
        live = None
        if not additional:
            if settings.llm_provider != "demo":
                _emit({"kind": "model_call_started", "provider": settings.llm_provider, "stage": "evidence synthesis", "message": "Sending retrieved evidence to the configured model for diagnosis."})
            try:
                live = await live_summary(settings, {"title": state.get("title"), "description": state.get("description"), "service": state.get("service"), "severity": state.get("severity")}, evidence, used_lessons, state.get("findings", []))
            except ProviderError:
                _emit({"kind": "model_call_failed", "provider": settings.llm_provider, "stage": "evidence synthesis", "message": "Model synthesis failed; no model diagnosis was recorded."})
                raise
        if live and not additional:
            summary.update(live)
            summary["source"] = "Google Gemini · evidence synthesis" if settings.llm_provider == "gemini" else "OpenAI-compatible model · evidence synthesis"
            _emit({"kind": "model_call_completed", "message": "Model diagnosis received and validated against available evidence IDs.", **live.get("model_call", {"provider": settings.llm_provider, "stage": "evidence synthesis", "status": "succeeded"})})
        else:
            summary["source"] = "deterministic demo rules"
        _emit({"kind": "diagnosis_revised", "message": "Specialist findings aggregated into an evidence-linked diagnosis.", "confidence": summary["confidence"], "iteration": iteration})
        update: dict[str, Any] = {"diagnosis": summary}
        if additional:
            update["current_iteration"] = iteration + 1
        return update

    def route_after_aggregate(state: IncidentState) -> str:
        diagnosis = state.get("diagnosis", {})
        if state.get("current_iteration", 0) > 0 and state.get("incident_kind") == "database_contention" and diagnosis.get("iterations", 0) == 1:
            return "plan"
        return "proposal"

    async def proposal(state: IncidentState) -> dict:
        kind = state.get("incident_kind", "unknown")
        proposals = {
            "api_errors": {"summary": "Restore the gateway upstream alias to payments-v1.", "rationale": "This returns traffic to the last registered payment-service name and matches the known-good route.", "changes": [{"key": "gateway.upstream_route", "value": "payments-v1"}], "verification": "Check the simulated gateway route value. Traffic recovery is not measured."},
            "database_contention": {"summary": "Cap analytics concurrency at 12 connections.", "rationale": "Leave interactive headroom in the shared pool while keeping the batch job active.", "changes": [{"key": "analytics.concurrency", "value": 12}], "verification": "Check the simulated concurrency setting. Pool and latency recovery are not measured."},
            "invalid_configuration": {"summary": "Set billing.retries to the validated integer 3.", "rationale": "Revision 27 changed the integer to a string. Restore the last schema-valid value before workers reload configuration.", "changes": [{"key": "billing.retries", "value": 3}], "verification": "Check the simulated retries setting. Job recovery is not measured."},
            "unknown": {"summary": "No remediation is proposed because the simulator has insufficient verified evidence.", "rationale": "Connect the relevant telemetry source and rerun the investigation before changing service state.", "changes": [], "verification": "No simulated environment change will be applied."},
        }
        current = proposals.get(kind, proposals["unknown"])
        rem = {**current, "simulated": True, "revision": state.get("revision", 0), "requires_human_approval": True}
        _emit({"kind": "remediation_proposed", "message": "A simulated remediation is ready for human approval.", "proposal": rem})
        return {"remediation": rem}

    async def pause_notice(state: IncidentState) -> dict:
        _emit({"kind": "approval_required", "message": "Investigation paused. Review the simulated remediation before execution.", "proposal": state.get("remediation", {})})
        await repo.update_run(state["run_id"], "approval_required")
        return {}

    async def approval(state: IncidentState) -> dict:
        # Keep interrupt first: LangGraph replays the node from its beginning on resume.
        response = interrupt({
            "kind": "remediation_approval",
            "proposal": state.get("remediation", {}),
            "diagnosis": state.get("diagnosis", {}),
            "allowed_decisions": ["approve", "reject", "request_changes"],
        })
        decision = response.get("decision") if isinstance(response, dict) else None
        if decision not in ("approve", "reject", "request_changes"):
            raise ValueError("Review response must be approve, reject or request_changes.")
        note = (response.get("change_request") or "").strip()[:1000] if isinstance(response, dict) else ""
        revision = state.get("revision", 0)
        approval_record = {"approval_id": f"approval:{state['run_id']}:{revision}:{decision}", "decision": decision, "note": note, "proposal_revision": revision}
        _emit({"kind": "review_recorded", "decision": decision, "message": f"Reviewer selected {decision.replace('_', ' ')}."})
        if decision in ("approve", "reject"):
            return {"approvals": [approval_record], "review_decision": decision, "review_message": note}
        if not note:
            raise ValueError("Please describe the requested changes before submitting.")
        return {"approvals": [approval_record], "review_decision": decision, "review_message": note}

    async def revise_proposal(state: IncidentState) -> dict:
        note = state.get("review_message", "").strip()
        rem = dict(state.get("remediation", {}))
        kind = state.get("incident_kind", "unknown")
        if kind == "database_contention":
            numbers = [int(item) for item in __import__('re').findall(r"\b\d{1,2}\b", note)]
            requested = next((value for value in numbers if 1 <= value <= 49), None)
            if requested is not None:
                rem["changes"] = [{"key": "analytics.concurrency", "value": requested}]
                rem["summary"] = f"Cap analytics concurrency at {requested} connections."
        elif kind == "api_errors" and "payments-v1" in note.lower():
            rem["changes"] = [{"key": "gateway.upstream_route", "value": "payments-v1"}]
        elif kind == "invalid_configuration":
            numbers = [int(item) for item in __import__('re').findall(r"\b\d\b", note)]
            requested = next((value for value in numbers if 0 <= value <= 10), None)
            if requested is not None:
                rem["changes"] = [{"key": "billing.retries", "value": requested}]
                rem["summary"] = f"Set billing.retries to the requested validated value {requested}."
        rem["revision"] = state.get("revision", 0) + 1
        rem["reviewer_change_request"] = note
        rem["verification"] = "Check the approved simulated configuration values. Service, traffic, and queue recovery are not measured."
        rem["summary"] = f"{rem.get('summary', 'Updated simulated remediation')} (updated per reviewer request)."
        _emit({"kind": "proposal_revised", "message": "The remediation proposal was revised from the reviewer's request and is awaiting approval again.", "proposal": rem})
        return {"remediation": rem, "revision": state.get("revision", 0) + 1}

    async def execute_remediation(state: IncidentState) -> dict:
        approvals = state.get("approvals", [])
        if not approvals or approvals[-1].get("decision") != "approve":
            raise PermissionError("Simulated remediation requires a recorded human approval.")
        remediation = state.get("remediation", {})
        if not remediation.get("simulated") or not remediation.get("requires_human_approval"):
            raise ValueError("Remediation proposal failed simulation/approval validation.")
        action_id = f"action-{state['run_id']}"
        result = await repo.execute_simulated_action(state["run_id"], state["incident_id"], action_id, remediation)
        _emit({"kind": "remediation_executed", "message": "Approved action applied in the simulated environment.", "action": result})
        return {"outcome": {"action": result, "approved": True, "simulated": True}}

    async def verify_outcome(state: IncidentState) -> dict:
        remediation = state.get("remediation", {})
        config = await repo.get_config(state["incident_id"])
        changes = remediation.get("changes", [])
        checks = [{"key": change["key"], "expected": change["value"], "actual": config.get(change["key"]), "passed": config.get(change["key"]) == change["value"]} for change in changes]
        verified = bool(checks) and all(check["passed"] for check in checks)
        verification_summary = "The simulated configuration matches the approved changes. Service, traffic, and queue recovery were not measured." if verified else "No configuration changes were verified, or one or more simulated checks did not match."
        outcome = {**state.get("outcome", {}), "verified": verified, "verification_checks": checks, "verification_summary": verification_summary, "verified_at": __import__('datetime').datetime.now(__import__('datetime').UTC).isoformat()}
        if verified:
            lesson = await repo.save_verified_lesson(state["run_id"], state["incident_id"], state.get("incident_kind", "unknown"), remediation.get("summary", "Verified remediation"), state.get("diagnosis", {}).get("diagnosis", ""))
            outcome["verified_lesson"] = lesson
            _emit({"kind": "lesson_persisted", "message": "A verified lesson was saved for future investigations.", "lesson": lesson})
        _emit({"kind": "outcome_verified", "message": "Simulated outcome verification completed.", "verified": verified, "checks": checks})
        return {"outcome": outcome}

    async def write_report(state: IncidentState) -> dict:
        approved = bool(state.get("approvals")) and state["approvals"][-1].get("decision") == "approve"
        outcome = state.get("outcome", {})
        status = "completed" if approved and outcome.get("verified") else "rejected" if state.get("review_decision") == "reject" else "inconclusive"
        report = {
            "run_id": state.get("run_id"), "incident_id": state.get("incident_id"), "incident_title": state.get("title"),
            "service": state.get("service"), "severity": state.get("severity"), "status": status,
            "diagnosis": state.get("diagnosis", {}),
            "evidence": [{"id": item.get("id"), "source": item.get("source"), "summary": item.get("summary"), "detail": item.get("detail"), "signal": item.get("signal")} for item in state.get("evidence", [])],
            "remediation": state.get("remediation", {}), "approval": state.get("approvals", []), "outcome": outcome,
            "remaining_uncertainty": state.get("diagnosis", {}).get("uncertainty", "No additional uncertainty recorded."),
            "memory_used": state.get("memory_lessons", []), "generated_at": __import__('datetime').datetime.now(__import__('datetime').UTC).isoformat(),
            "environment_label": "SIMULATED ENVIRONMENT",
        }
        _emit({"kind": "report_completed", "message": "Incident report completed.", "status": status})
        await repo.update_run(state["run_id"], status, report=report)
        return {"report": report}

    def route_after_review(state: IncidentState) -> str:
        decision = state.get("review_decision")
        if decision == "approve":
            return "execute_remediation"
        if decision == "reject":
            return "write_report"
        return "revise_proposal"

    builder = StateGraph(IncidentState)
    builder.add_node("intake", intake)
    builder.add_node("classify", classify)
    builder.add_node("plan", plan)
    builder.add_node("specialist_runner", run_specialist)
    builder.add_node("aggregate", aggregate, retry_policy=RetryPolicy(max_attempts=2, retry_on=(ProviderTransientError,), initial_interval=0.5, backoff_factor=2.0))
    builder.add_node("proposal", proposal)
    builder.add_node("pause_notice", pause_notice)
    builder.add_node("approval", approval)
    builder.add_node("revise_proposal", revise_proposal)
    builder.add_node("execute_remediation", execute_remediation)
    builder.add_node("verify_outcome", verify_outcome)
    builder.add_node("write_report", write_report)
    builder.add_edge(START, "intake")
    builder.add_conditional_edges("intake", lambda state: "aggregate" if state.get("branch_seed") else "classify", ["aggregate", "classify"])
    builder.add_edge("classify", "plan")
    builder.add_conditional_edges("plan", dispatch, ["specialist_runner"])
    builder.add_edge("specialist_runner", "aggregate")
    builder.add_conditional_edges("aggregate", route_after_aggregate, ["plan", "proposal"])
    builder.add_conditional_edges("proposal", lambda state: "pause_notice" if state.get("remediation", {}).get("changes") else "write_report", ["pause_notice", "write_report"])
    builder.add_edge("pause_notice", "approval")
    builder.add_conditional_edges("approval", route_after_review, ["execute_remediation", "write_report", "revise_proposal"])
    builder.add_edge("revise_proposal", "pause_notice")
    builder.add_edge("execute_remediation", "verify_outcome")
    builder.add_edge("verify_outcome", "write_report")
    builder.add_edge("write_report", END)
    return builder.compile(checkpointer=checkpointer)


def build_graph_with_checkpointer(repo: Repository, settings: Settings, checkpointer: Any):
    """Convenience name used by tests that explicitly reopen a durable saver."""
    return build_graph(repo, settings, checkpointer)


