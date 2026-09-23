# 02 — Design: CMN-C1-036 PersonalizationContextBuilderAgent

> **Category:** Cat 1 (single technical capability) · **Industry:** CMN
> **L1 Base type:** `AgentBaseGraph` (inherits Level 1 framework directly; no Level 2 base)
> **Source:** see `docs/01_proposal.md`

## 1. Agent architecture overview

The agent delivers one reusable capability — **build a personalization context
from a behavioural event stream**. It is a Cat 1 template and therefore inherits
the fixed `AgentBaseGraph` lifecycle directly (**L1 Base type: `AgentBaseGraph`**).

SDK 1.0.0 `AgentBaseGraph` exposes exactly **three domain slots** —
`pre_process`, `main`, `post_process` — wired into a fixed pipeline the framework
owns (`add_edges`/`route` are inherited and not overridden):

```
START → initialize → pre_process → main → {route} → post_process → finalize → END
                                            │                          │
                                            └────────→ finalize ────────┘  (status=error)
```

### 1.1 Logical pipeline → 3-slot mapping (design decision)

The proposal and the impl issues describe a **6-step logical flow**
(EventStreamIngest → EventNormalize → InterestExtract → ScoringCalc →
ContextProfileBuild → ProfileOutput). On SDK 1.0.0 a Cat 1 `AgentBaseGraph`
cannot register six independent framework nodes in a custom linear chain — only
the three fixed slots exist. The six logical steps are therefore realised as
**three backbone `FunctionNode`s that delegate to a pure-Python service layer**
(`src/services/`). This keeps the approved Cat 1 classification and the
SDK-compliant lifecycle while preserving every step as a named, unit-testable
service function. (A literal 6-node graph would require the Cat 2 `GraphNode`
subgraph pattern and a category renumbering — out of scope for this template.)

| Logical step (issue) | Slot | Module | Responsibility |
|----------------------|------|--------|----------------|
| EventStreamIngest | `pre_process` | `nodes/pre_process_node.py` → `services` | S-1 validation: required fields, reject malformed, PII check on `user_id` |
| EventNormalize | `pre_process` | `nodes/pre_process_node.py` → `services` | UTC ISO-8601 timestamps, map `event_types`, enrich `interest_weight`/`recency_boost` |
| InterestExtract | `main` | `nodes/main_node.py` → `services` + real LLM (Azure OpenAI) | Aggregate per-topic interest (deterministic); LLM infers `interest_topics`, `price_sensitivity`, `preferred_channels` from the event stream, heuristic fallback on any LLM failure |
| ScoringCalc | `main` | `nodes/main_node.py` → `services` | `recency_score` (time-decay), `frequency_score`, `engagement_depth_score` |
| ContextProfileBuild | `main` | `nodes/main_node.py` → `services` | Assemble profile, rank top-N `interest_topics`, stamp `last_updated_at` |
| ProfileOutput | `post_process` | `nodes/post_process_node.py` → `services` | S-3 pseudonymisation gate, optional CDP write, TTL stamp, surface output |

## 2. State schema

Flat `TypedDict` extending `AgentState` (`src/schemas/state.py`). Domain fields
are all `NotRequired` and seeded by `Graph._extra_initial_state()`:

| Field | Type | Written by |
|-------|------|-----------|
| `raw_events` | `list[dict]` | pre_process (parsed from `user_input`) |
| `normalized_events` | `list[dict]` | pre_process |
| `errors` | `list[str]` | pre_process (S-1 rejections) |
| `interest_scores` | `dict[str, float]` | main |
| `scoring_result` | `dict[str, float]` | main |
| `context_profile` | `dict` | main / post_process |
| `user_id` | `str` (pseudonymised) | pre_process |

No Pydantic, no dataclasses, no `InvocationContext`, no credentials in state
(msgpack-checkpointed). `user_id` is always the pseudonymised internal id.

## 3. Node responsibilities

- **pre_process (EventStreamIngest + EventNormalize)** — `FunctionNode`. Parses
  `user_input` as a JSON event array into `raw_events`. **S-1 gate:** every event
  must carry `event_type`, `timestamp`, `user_id`; malformed events are appended
  to `errors` and dropped (not passed downstream). Rejects events whose `user_id`
  matches a raw-PII pattern (email/phone). Normalises timestamps to UTC ISO-8601,
  maps each `event_type` to its configured weights (unknown types skipped), and
  writes `normalized_events`. If zero valid events remain → `status=error`
  (short-circuits `main` → `finalize`).
- **main (InterestExtract + ScoringCalc + ContextProfileBuild)** — `FunctionNode`,
  `required_trust_level = VERIFIED_EXTERNAL` (S-1). Computes `interest_scores`
  and `scoring_result` (recency/frequency/engagement via configured
  `scoring_weights`) deterministically — objective time-decay arithmetic, not a
  judgement call. Assembles the heuristic `context_profile` baseline (top-N
  topics, `price_sensitivity`, `preferred_channels`, scores,
  `last_updated_at`), then calls a real LLM (Azure OpenAI, via
  `AzureOpenAIClient`) to infer `interest_topics`/`price_sensitivity`/
  `preferred_channels` from the normalised event stream — genuine NL reasoning
  over ambiguous behavioural signal, overriding the heuristic values when the
  call succeeds and the response validates. Any failure (secret not
  provisioned, API error, malformed/unparseable response, no events) silently
  keeps the heuristic values instead — the profile is always produced. Emits
  an S-4 domain audit event (`llm_inferred: bool`). Sets `status=success` so
  `route()` proceeds to `post_process`.
- **post_process (ProfileOutput)** — `FunctionNode`. **S-3 output gate (mandatory,
  non-bypassable):** re-verifies `user_id` is pseudonymised before surfacing;
  a raw-PII hit blocks output (`status=error` + `errors`). Stamps the profile with
  `context_ttl_hours` metadata, optionally writes to the configured CDP
  (Treasure Data / Segment — mocked here, no live credentials), and writes the
  profile JSON to `formatted_output` (surfaced by `get_output()`). The framework's
  `@final` credential-scan output gate also runs automatically.

## 4. Config surface (`config/agent.yaml` → `Graph(node_config=...)`)

Config is **constructor-injected** at `Graph.__init__` (not threaded through
`__call__`) and passed to each node in `register_nodes()`:

| Key | Default | Meaning |
|-----|---------|---------|
| `event_types[]` | product_view/purchase/search w/ weights | per-type `interest_weight`, optional `recency_boost` |
| `scoring_weights` | recency 0.4 / frequency 0.3 / engagement_depth 0.3 | linear combination weights |
| `cdp_integration` | treasure_data:true, segment/amplitude:false | output sink toggles |
| `context_ttl_hours` | 24 | recency/frequency window + profile TTL |
| `top_n_topics` | 5 | number of ranked interest topics in the profile (heuristic fallback only — the LLM ranks its own list) |

Azure OpenAI wiring is **not** in this file at all — `AZURE_OPENAI_API_KEY`,
`AZURE_OPENAI_ENDPOINT`, and `AZURE_OPENAI_DEPLOYMENT` are all three declared
secrets (`agent.yaml` `requires.secrets`), resolved via
`ctx.secrets.require(...)` inside `main`'s `execute()` (per invocation, never
cached on the node instance — see §6). Endpoint and deployment are not
credentials by themselves, but are treated as secrets here rather than
runtime config, so the whole triple is provisioned/rotated through one
mechanism.

## 5. I/O contract

**Input** — `user_input` is a JSON array of behavioural events:

```json
[{"event_type":"product_view","timestamp":"2026-06-24T07:00:00Z","entity_id":"electronics:p1","user_id":"anon_xyz"}]
```

Required per event: `event_type`, `timestamp`, `user_id`. `entity_id` topic
prefix (`topic:id`) drives interest aggregation.

**Output** — `get_output().output` is the context profile JSON:

```json
{
  "user_id": "anon_xyz",
  "interest_topics": ["electronics", "wearables"],
  "price_sensitivity": "medium",
  "preferred_channels": ["web", "app"],
  "recency_score": 0.82,
  "frequency_score": 0.54,
  "engagement_depth_score": 0.40,
  "last_updated_at": "2026-06-24T07:01:00Z",
  "ttl_hours": 24
}
```

## 6. Security design (5-layer)

| Layer | Design |
|-------|--------|
| S-1 Trust | `main` declares `required_trust_level = VERIFIED_EXTERNAL`; the framework `__call__` enforces it before `execute()`. |
| S-1 Validation | pre_process validates required event fields and rejects raw-PII `user_id` at ingest. |
| S-2 Input | `FunctionNode` default `_security_gate_input` PII scan + injection detection (`evaluate_injection_content`) runs automatically against `user_input` before `execute()` — so an attacker-controlled `entity_id`/`channel` string is screened once before it ever reaches the LLM prompt built in `main`. Raw PII is masked to `[MASKED]`. The pre_process raw-PII rejection (S-1) is therefore a secondary gate that fires for directly-seeded `raw_events` (e.g. internal callers / tests) that bypass S-2. Domain consent/role scope is an `_extra_security_gate_input` extension point (access scoped to marketing/personalisation role at the gateway). |
| S-3 Output | **Mandatory pseudonymisation gate** in post_process `execute()` (blocks raw-PII `user_id`); plus the `FunctionNode` `@final` credential-scan output gate. The LLM's JSON response is structurally validated (`service.validate_llm_attributes`) before it can reach the profile — an unparseable/wrong-shape response is discarded, never surfaced raw. |
| S-4 Audit | `emit_trace_event()` from `shared.utils.audit_logger` for profile-build (incl. `llm_inferred: bool`) and CDP-write side effects. Framework emits node lifecycle events. |
| S-5 Supply chain | `[project].dependencies` exact-pinned (`==`); no hardcoded secrets. `AZURE_OPENAI_API_KEY`/`AZURE_OPENAI_ENDPOINT`/`AZURE_OPENAI_DEPLOYMENT` via `ctx.secrets.require()` inside `main`'s `execute()` (per-invocation, never cached — node instances are shared across invocations via the registry's LRU cache). CDP token via `ctx.secrets.require()` when live writes are enabled. |

## 7. CDP integration design

Treasure Data CDP is the **preferred/strategic** sink; Segment
and Amplitude are optional. v1 ships the integration **interface + mock writer**
(no live credentials committed). When `cdp_integration.treasure_data: true`, the
profile is written via the CDP client whose token comes from
`ctx.secrets.require("TREASURE_DATA_API_KEY")` (declared in `agent.yaml`
`requires.secrets`, not in state). With all toggles false the profile is returned
inline only. APPI consent must be confirmed upstream before ingestion (owner:
platform security; pre-Phase-3 gate).

## 8. Error handling strategy

- **Per-event (recoverable):** malformed/PII events are dropped to `errors`; the
  pipeline continues on the remaining valid events. Domain failures are carried in
  the `errors` domain field, **not** `status=error`, so downstream `execute()`
  still runs (per SDK 1.0.0 routing: an `error` status skips every downstream
  node body).
- **No valid events (terminal):** pre_process sets `status=error`; `route()` sends
  `main → finalize`; `get_output()` returns `status:error` with `errors` populated.
- **Node exception:** `BaseNode.__call__` catches it, sets `status=error`, appends
  to `error_log` — the graph never crashes.
- **S-3 violation:** raw-PII `user_id` at output → output blocked, `status=error`,
  reason in `errors`. Non-bypassable regardless of config.
- **LLM failure (recoverable, not an error path):** a missing/unprovisioned
  `AZURE_OPENAI_API_KEY`/`AZURE_OPENAI_ENDPOINT`/`AZURE_OPENAI_DEPLOYMENT`, an
  Azure OpenAI API error, or a malformed/invalid JSON response from the LLM
  never sets `status=error` and never raises —
  `main` silently keeps the deterministic heuristic values for
  `interest_topics`/`price_sensitivity`/`preferred_channels` instead. A
  personalization profile must always be produced.
