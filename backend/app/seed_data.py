"""Deterministic, explicitly simulated incident and service data."""

from __future__ import annotations

SEED_INCIDENTS: list[dict] = [
    {
        "id": "seed-api-5xx",
        "title": "API errors increased after the gateway deployment",
        "description": "Checkout API 5xx responses rose from 0.4% to 18% immediately after gateway release 2026.10.23. The payment service is healthy, but requests routed through the new edge configuration fail.",
        "service": "checkout-api",
        "severity": "critical",
        "kind": "api_errors",
        "is_seed": True,
    },
    {
        "id": "seed-db-contention",
        "title": "Application responses slowed during database contention",
        "description": "Order API p95 latency is 4.8 seconds and database connections are saturated. The slowdown began during the nightly analytics workload; the database itself is reachable and error rates remain low.",
        "service": "order-api",
        "severity": "high",
        "kind": "database_contention",
        "is_seed": True,
    },
    {
        "id": "seed-job-config",
        "title": "Background billing jobs fail after configuration update",
        "description": "Billing workers reject newly queued jobs after a configuration update. The queue is growing, the worker process is healthy, and job payloads fail validation before execution.",
        "service": "billing-worker",
        "severity": "high",
        "kind": "invalid_configuration",
        "is_seed": True,
    },
]

ENVIRONMENT: dict[str, dict[str, list[dict]]] = {
    "seed-api-5xx": {
        "logs": [
            {"id": "api-log-01", "timestamp": "2026-10-23T09:58:00Z", "summary": "Baseline 5xx rate was 0.4% before gateway release.", "detail": "checkout-api request failures remained below the 1% alert threshold.", "signal": "baseline"},
            {"id": "api-log-02", "timestamp": "2026-10-23T10:04:00Z", "summary": "502 responses rose to 18.2% within two minutes of release.", "detail": "edge-router logged upstream_route_not_found for /v2/payments; correlation id edge-8842.", "signal": "root_cause"},
            {"id": "api-log-03", "timestamp": "2026-10-23T10:06:00Z", "summary": "Payment service health checks remain green.", "detail": "Failures occur at the gateway before requests reach payment-service.", "signal": "scope"},
        ],
        "metrics": [
            {"id": "api-metric-01", "timestamp": "2026-10-23T10:05:00Z", "summary": "HTTP 5xx rate: 18.2% (baseline 0.4%).", "detail": "Measured across checkout-api over the last 5 minutes.", "signal": "root_cause"},
            {"id": "api-metric-02", "timestamp": "2026-10-23T10:05:00Z", "summary": "Payment service CPU: 42%; health check: healthy.", "detail": "Backend saturation is not indicated.", "signal": "scope"},
        ],
        "deployments": [
            {"id": "api-deploy-01", "timestamp": "2026-10-23T10:02:00Z", "summary": "edge-router 2026.10.23 deployed; upstream route changed to payments-v2.", "detail": "Change CHG-4821 introduced route alias payments-v2. The service is registered as payments-v1.", "signal": "root_cause"},
            {"id": "api-deploy-02", "timestamp": "2026-10-22T16:20:00Z", "summary": "Previous stable edge-router release 2026.10.21.", "detail": "Known-good route alias was payments-v1.", "signal": "baseline"},
        ],
        "config": [
            {"id": "api-config-01", "timestamp": "2026-10-23T10:07:00Z", "summary": "Configured route payments-v2 does not match registered service payments-v1.", "detail": "gateway.upstream_route=payments-v2; registry entry=payments-v1.", "signal": "root_cause", "key": "gateway.upstream_route", "value": "payments-v2"},
        ],
        "runbooks": [
            {"id": "api-runbook-01", "timestamp": "current", "summary": "Gateway upstream route mismatch: restore the last verified route alias.", "detail": "Confirm the backend is healthy, restore the known-good service alias, then verify 5xx rate below 1% for five minutes.", "signal": "remediation"},
        ],
        "config_values": {"gateway.upstream_route": "payments-v2", "registry.payment_service": "payments-v1"},
    },
    "seed-db-contention": {
        "logs": [
            {"id": "db-log-01", "timestamp": "2026-10-23T01:04:00Z", "summary": "Analytics export began at 01:00 and holds long-running reads.", "detail": "order-api requests wait on the shared analytics connection pool; no database disconnects observed.", "signal": "root_cause"},
            {"id": "db-log-02", "timestamp": "2026-10-23T01:06:00Z", "summary": "Connection acquisition wait increased to 3.9 seconds.", "detail": "Pool waiters=47; active connections=48 of 50.", "signal": "root_cause"},
        ],
        "metrics": [
            {"id": "db-metric-01", "timestamp": "2026-10-23T01:05:00Z", "summary": "Database pool utilization: 96%; 47 waiting requests.", "detail": "First-pass metrics show sustained saturation during analytics export.", "signal": "root_cause"},
            {"id": "db-metric-02", "timestamp": "2026-10-23T01:05:00Z", "summary": "Order API p95 latency: 4.8 seconds (normal 380 ms).", "detail": "Database CPU is 61%; connection wait is the dominant latency component.", "signal": "scope"},
            {"id": "db-metric-03", "timestamp": "2026-10-23T01:10:00Z", "summary": "Extended sample: pool wait peaks at 4.2 seconds while analytics holds 44 connections.", "detail": "A 10-minute window confirms analytics concurrency consumes nearly the whole shared pool; normal order traffic uses 6 connections.", "signal": "additional_evidence"},
        ],
        "deployments": [
            {"id": "db-deploy-01", "timestamp": "2026-10-22T21:00:00Z", "summary": "No order-api deployment in the incident window.", "detail": "Application version is unchanged since the prior day.", "signal": "scope"},
        ],
        "config": [
            {"id": "db-config-01", "timestamp": "2026-10-23T01:07:00Z", "summary": "Analytics worker is allowed 44 concurrent database connections.", "detail": "shared_pool.max_connections=50; analytics.concurrency=44; order-api reserved minimum=6.", "signal": "root_cause", "key": "analytics.concurrency", "value": 44},
        ],
        "runbooks": [
            {"id": "db-runbook-01", "timestamp": "current", "summary": "Shared pool contention: cap batch workload concurrency and verify queue wait.", "detail": "Reduce batch concurrency, keep reserved interactive capacity, then confirm pool wait under 100 ms and p95 latency recovers.", "signal": "remediation"},
        ],
        "config_values": {"analytics.concurrency": 44, "shared_pool.max_connections": 50},
    },
    "seed-job-config": {
        "logs": [
            {"id": "job-log-01", "timestamp": "2026-10-23T07:12:00Z", "summary": "Billing worker rejects jobs before handler execution.", "detail": "ConfigValidationError: retries must be an integer; received 'many'. Queue ack is withheld.", "signal": "root_cause"},
            {"id": "job-log-02", "timestamp": "2026-10-23T07:14:00Z", "summary": "Worker processes are healthy and repeatedly fail the same config check.", "detail": "No dependency timeouts or database connection errors are present.", "signal": "scope"},
        ],
        "metrics": [
            {"id": "job-metric-01", "timestamp": "2026-10-23T07:15:00Z", "summary": "Billing queue depth: 1,842 jobs; successful completions: 0 since 07:10.", "detail": "Worker process uptime is normal; validation failures account for rejected jobs.", "signal": "root_cause"},
        ],
        "deployments": [
            {"id": "job-deploy-01", "timestamp": "2026-10-23T07:10:00Z", "summary": "billing-worker configuration revision 27 applied.", "detail": "The change replaced integer retries=3 with string retries='many'. No code image changed.", "signal": "root_cause"},
        ],
        "config": [
            {"id": "job-config-01", "timestamp": "2026-10-23T07:11:00Z", "summary": "billing.retries is the string 'many'; expected integer 0–10.", "detail": "Worker config schema validation rejects the value before job execution.", "signal": "root_cause", "key": "billing.retries", "value": "many"},
        ],
        "runbooks": [
            {"id": "job-runbook-01", "timestamp": "current", "summary": "Invalid worker configuration: restore a schema-valid value and restart workers.", "detail": "Restore the last validated retry count, confirm config reload, and verify the queue drains without validation errors.", "signal": "remediation"},
        ],
        "config_values": {"billing.retries": "many", "billing.worker_enabled": True},
    },
}

GENERIC_RUNBOOK = {
    "id": "generic-runbook-01",
    "timestamp": "current",
    "summary": "Collect service logs, metrics and recent changes before proposing an action.",
    "detail": "This simulated workspace only contains the incident report you submitted. Connect authorized telemetry sources to verify production evidence.",
    "signal": "limitation",
}
