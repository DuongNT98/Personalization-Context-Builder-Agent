# 03 — Test Specification: CMN-C1-036 PersonalizationContextBuilderAgent

> Cat 1 · CMN · L1 Base `AgentBaseGraph`. See `docs/02_design.md`.

## Test strategy

- **Types:** unit (service layer + nodes), proof-of-boundary (framework
  boundaries), integration (full `invoke()` pipeline).
- **Determinism:** scoring is time-dependent (recency/frequency vs "now"), so
  time-sensitive assertions use a **fixed `reference_time`** passed to
  `service.compute_scoring()`. The end-to-end `invoke()` test asserts only
  time-independent fields (node order, status, topics, channels, sensitivity).
- **External deps:** none required to run — the CDP write is a mock (no live
  credentials), and every unit/integration/PB test exercises the LLM path via
  a test-double (`_FakeLLM`, `MainNode(llm=...)`), never a real Azure OpenAI
  call. No network access is needed to pass the suite.
- **Coverage target:** ≥ 80% of `src/` (service + nodes).

## TC — framework compliance

| TC-ID | Test | Expected |
|-------|------|----------|
| TC-01 | State is a flat `TypedDict` extending `AgentState`; domain fields only | `test_state_safety.py` PASS (no Pydantic/credential/`InvocationContext`) |
| TC-02 | pre_process rejects malformed / unknown / unparseable events | events dropped to `errors`; valid events normalised |
| TC-03 | No JWT/API keys in `src/` | `gate-credential-scan` PASS |
| TC-04 | Config constructor-injected (no `config` arg to `execute`) | nodes built with `node_config`; defaults applied via `merge_config` |
| TC-05 | S-4 audit event emitted on profile build | `emit_trace_event("context_profile_built", …)` called in `main` |
| TC-06 | S-2 default input gate runs (`@final`, not overridden) | raw PII in `user_input` masked to `[MASKED]` before `execute()` |
| TC-07 | S-3 output gate non-bypassable | raw-PII `user_id` in profile → output blocked, `status=error` |
| TC-08 | S-1 trust level on `main` | `MainNode.required_trust_level == VERIFIED_EXTERNAL` |

## TC — domain logic (unit)

| TC-ID | Test | Expected |
|-------|------|----------|
| TC-10 | `normalize_events` validation | valid+bogus+PII batch → exact normalized count + exact `errors` list |
| TC-11 | `extract_interest_scores` aggregation | topic → exact weighted score (`interest_weight × recency_boost`) |
| TC-12 | `compute_scoring` (fixed `reference_time`) | exact `recency_score` / `frequency_score` / `engagement_depth_score` |
| TC-13 | `infer_price_sensitivity` | purchase-ratio ≥0.4 → `low`; 0 → `high`; else `medium` |
| TC-14 | `build_profile` | top-N topics ranked; `unknown` topic excluded; TTL stamped |
| TC-15 | `pseudonymize` / `is_pseudonymized` | deterministic `anon_<sha256[:12]>`; PII → not pseudonymised |
| TC-16 | `MainNode.execute` happy path | returns `interest_scores`/`scoring_result`/`context_profile` + `status=success` |
| TC-17 | `MainNode.execute` short-circuit | upstream `status=error` → returns `{}` (no recompute) |
| TC-18 | `summarize_events_for_llm` | prompt summary excludes `user_id`/scoring weights, includes topic/type/channel/timestamp |
| TC-19 | `validate_llm_attributes` | well-formed JSON accepted; wrong-type/enum/shape → `None` |
| TC-20 | `MainNode` LLM happy path (test-double) | valid LLM JSON overrides `interest_topics`/`price_sensitivity`/`preferred_channels`; `scoring_result`/`interest_scores` unaffected |
| TC-21 | `MainNode` LLM prose/markdown-wrapped response | `extract_json_object` still parses; profile reflects the LLM values |
| TC-22 | `MainNode` malformed LLM response | invalid shape → heuristic values, `status=success` (no crash) |
| TC-23 | `MainNode` LLM call raises | simulated API error → heuristic values, `status=success` (no crash) |
| TC-24 | `MainNode` no secret configured | no `llm=` injected, no secret bound → heuristic values, `status=success` (no crash) |
| TC-25 | `MainNode` empty events | no normalized events → LLM never called (`llm.calls == []`) |

## PB — proof-of-boundary

| PB-ID | Boundary | Test file | Assertion |
|-------|----------|-----------|-----------|
| PB-2/5 | State serialization safety | `test_state_safety.py` | state schema has no Pydantic/credential/`InvocationContext` annotations |
| PB-4 | Import isolation (L3 ↛ L0) | `test_import_isolation.py` | AST scan of `src/` finds no `agenticstar`/`platform` import |
| PB-S3 | S-3 output gate (pseudonymisation) | `test_boundary_pseudonymization.py` | raw-PII profile → `status=error`, no `formatted_output`; pseudonymised → `formatted_output` emitted |
| PB-6 | Invoke execution order | `test_pb_invoke_order.py` | `node_history == [Initialize, PreProcess, Main, PostProcess, Finalize]`; `status=success`; profile fields exact |

## Out of scope (v1)

- Live CDP writes (Treasure Data / Segment) — interface + mock only.
- Real Azure OpenAI network calls in the test suite — every test uses a
  test-double LLM; a genuine end-to-end call is a manual/STG verification
  step, not part of `pytest`.
