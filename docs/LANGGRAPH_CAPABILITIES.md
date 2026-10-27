# LangGraph capability matrix

The pinned runtime is `langgraph==1.2.16` with `langgraph-checkpoint-sqlite==3.1.0`. The implementation follows current LangGraph guidance for `StateGraph`, `Send`, `ToolNode`, `RetryPolicy`, `interrupt`, `Command(resume=...)`, async SQLite checkpoints, state history, and v2 stream chunks.

| Capability | Implementation | Demonstration | Verification evidence |
|---|---|---|---|
| Typed shared state and reducers | `backend/app/graph.py`: `IncidentState`, `SpecialistState`, `merge_records_by_id` | Start any seeded run; inspect citations and specialist results after fan-out | Integration checks assert all parallel findings and evidence survive aggregation without duplicate IDs |
| Focused nodes and explicit transitions | `build_graph`: `intake`, `classify`, `plan`, `aggregate`, `proposal`, `approval`, `execute_remediation`, `verify_outcome`, `write_report` | Follow the progress rail and graph history tab | Checkpoint snapshots identify completed nodes and next destinations |
| Conditional routing | `dispatch`, `route_after_aggregate`, proposal and review routing; `add_conditional_edges` | Database scenario takes another pass; review choices select their own branches; unsupported actions end inconclusive | Evaluation checks the extra database pass; tests cover approval, rejection, changes, and no-action cases |
| Dynamic parallel fan-out | `dispatch` returns `Send("specialist_runner", ...)` once per relevant specialist | Watch three specialists begin and return in parallel | Evidence and finding IDs show specialist and iteration; reducer integration test checks no lost branches |
| Specialist subgraph | `build_specialist_graph`: `prepare_tool_calls -> execute_typed_tools -> summarize_findings`; invoked asynchronously inside `specialist_runner` | Specialist cards and tool activity panel | The compiled child graph runs once per dispatched specialist; API event log captures its custom events |
| Typed tools | Pydantic schemas in `graph.py`; `@tool` functions: `query_seeded_logs`, `read_metrics`, `inspect_deployments`, `read_service_configuration`, `search_runbooks`; `ToolNode` executes calls | Expand Evidence & tools and view tool activity | Tests assert tool names, cited evidence IDs, and structured record details |
| Native human interrupt and resume | `pause_notice` then `approval` calls `interrupt`; API resumes with `Command(resume=...)` on the same `thread_id` | Approve, reject, or ask to lower the database connection cap | Tests cover all choices and confirm rejected runs create no action; restart test closes and reopens the SQLite saver before approval |
| Durable checkpointing | `AsyncSqliteSaver` in FastAPI lifespan; stable run/thread IDs and the same SQLite file as application records | Stop and restart the API while a run is awaiting approval, reopen the run | A local restart integration test and a real Gemini-backed local run resumed the same interrupt. Hosted persistence has not been verified |
| State inspection and checkpoint branch | `/api/investigations/{run_id}/checkpoints` uses `aget_state_history`; `/branch` initializes a new run from a chosen checkpoint snapshot | Graph history → choose **Branch** on a checkpoint | Branch stores source run and checkpoint IDs, gets a new thread ID, and follows the branch-seed path. This is a new investigation, not mutation of the source thread |
| Streaming | `get_stream_writer` in graph nodes/tools; async `astream(..., stream_mode=["updates", "custom"], version="v2")`; SSE endpoint | Observe specialist start, evidence, retry, approval, verification, and report events | UI persists event IDs; event history is stored in SQLite and reconnects replay after the last cursor |
| Bounded loop | `current_iteration`, max one additional pass, explicit `route_after_aggregate`, API recursion limit 40 | Database scenario shows “additional evidence requested” | Evaluation asserts two iterations for database and one for the other seeded cases |
| Retries and failure handling | `RetryPolicy(max_attempts=2, retry_on=(TransientToolError, ...))` on `ToolNode`; one durable one-time API log timeout | API scenario shows timeout then recovered tool call | Test asserts exactly two attempts for the injected fault and a successful evidence result |
| Cross-investigation memory | SQLite `lessons` table; stored only after verification; matched by incident kind in `intake` | Complete API scenario, then start another API investigation and open Memory tab | Test asserts source run ID, persistence across graph recreation, and memory recall in later run |
| Observability | JSON logs; run/thread IDs; persisted event feed; per-run duration; node/tool outcomes | Inspect run timeline and tool activity | Integration tests inspect event rows and final status; optional LangSmith variables in `.env.example` |
| Gemini synthesis | `aggregate` invokes Google's structured API after specialist evidence is collected; the response and valid evidence IDs become graph state | Inspect provider, model, response ID, token usage, diagnosis, and citations in the review | A real local response was used in a completed report; mock tests verify response use, bad citations, failure, and retry |
| Evaluation | `backend/scripts/evaluate_demo.py` and `backend/tests` | Run the documented evaluator | Deterministic checks cover diagnosis, citations, routing, required approval, final configuration checks, retry and loop behavior |

Demo mode proves deterministic orchestration and simulator-specific outcomes; it does not establish diagnosis accuracy. Gemini replaces diagnosis synthesis only. Evidence-ID validation does not prove semantic support, and a real local call is not a general model-quality evaluation.

## Documentation sources checked

- [LangGraph Graph API](https://docs.langchain.com/oss/python/langgraph/graph-api)
- [LangGraph Interrupts](https://docs.langchain.com/oss/python/langgraph/interrupts)
- [LangGraph Streaming](https://docs.langchain.com/oss/python/langgraph/streaming)
- [LangGraph Persistence](https://docs.langchain.com/oss/python/langgraph/persistence)
- [LangGraph on PyPI](https://pypi.org/project/langgraph/1.2.16/)
- [SQLite checkpoint package on PyPI](https://pypi.org/project/langgraph-checkpoint-sqlite/3.1.0/)
