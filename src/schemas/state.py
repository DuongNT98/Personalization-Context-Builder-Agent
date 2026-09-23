"""State schema for CMN-C1-036 PersonalizationContextBuilderAgent.

ADR-005: State MUST be a flat TypedDict extending the framework ``AgentState``,
holding only msgpack-serialisable primitives and lists/dicts of them. Rich
model objects, arbitrary class instances, live service handles, and credentials
are prohibited (they break msgpack checkpointing). LangGraph checkpoints the
state to the DB; keep every field a plain JSON-compatible value.

All backbone fields (``user_input``, ``status``, ``session_id``, ``node_history``,
``error_log``, ``result``, ``formatted_output``, ``caller_trust_level``, hitl_*,
...) are inherited from ``AgentState``. Declare only the domain fields below and
mark them ``NotRequired`` — ``invoke()`` seeds the backbone fields, and the
domain defaults are injected by ``Graph._extra_initial_state()``.

Privacy note (S-3): ``user_id`` here is ALWAYS the pseudonymised internal id
(e.g. ``anon_xxx``). Raw PII (email/phone/name) must never be written to state —
state is checkpointed to the DB in plaintext.
"""

from typing import Any, NotRequired

from framework.schemas.agent_state import AgentState


class State(AgentState):
    """Personalization context-builder state (extends framework AgentState)."""

    # ── Ingestion (pre_process: EventStreamIngest + EventNormalize) ──────────
    raw_events: NotRequired[list[dict[str, Any]]]  # type: ignore[valid-type]
    """Input behavioural event stream, one dict per event (parsed from user_input)."""

    normalized_events: NotRequired[list[dict[str, Any]]]  # type: ignore[valid-type]
    """Validated + normalised events (UTC timestamps, enriched with config weights)."""

    errors: NotRequired[list[str]]  # type: ignore[valid-type]
    """S-1 validation errors for malformed/rejected events (distinct from error_log)."""

    # ── Scoring (main: InterestExtract + ScoringCalc) ────────────────────────
    interest_scores: NotRequired[dict[str, float]]  # type: ignore[valid-type]
    """Topic -> aggregated weighted interest score."""

    scoring_result: NotRequired[dict[str, float]]  # type: ignore[valid-type]
    """recency_score, frequency_score, engagement_depth_score (all 0.0-1.0)."""

    # ── Output (main: ContextProfileBuild / post_process: ProfileOutput) ─────
    context_profile: NotRequired[dict[str, Any]]  # type: ignore[valid-type]
    """Final user context profile surfaced as the agent output."""

    user_id: NotRequired[str]  # type: ignore[valid-type]
    """Pseudonymised user identifier — NEVER raw PII (S-3 enforced)."""
