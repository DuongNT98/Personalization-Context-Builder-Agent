"""main slot — InterestExtract (LLM) + ScoringCalc (deterministic) + ContextProfileBuild.

Core capability of the agent: turn normalised events into a personalization
context profile. Realises logical steps 3-5 of the design (docs/02_design.md
§1.1). S-1: requires VERIFIED_EXTERNAL trust. S-4: emits a domain audit event.

InterestExtract calls a real LLM (Azure OpenAI) to infer `interest_topics`,
`price_sensitivity`, and `preferred_channels` from the event stream — genuine
NL reasoning over ambiguous behavioural signal, the Cat 1 agent-vs-tool value
proposition. ScoringCalc (`recency_score`/`frequency_score`/
`engagement_depth_score`) stays deterministic time-decay math: it is an
objective, reproducible computation from timestamps, and an LLM would only
make it non-reproducible for no benefit.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from framework.nodes import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.invocation_context import InvocationContext
from framework.schemas.trust_level import TrustLevel
from framework.utils.audit_logger import emit_trace_event
from shared.services.llm.azure_openai_client import AzureOpenAIClient
from shared.utils.llm_json import extract_json_object

from src.services import service

_SYSTEM_PROMPT = (
    "You are a personalization analyst. Given a user's behavioural event "
    "stream (a JSON array of {topic, event_type, channel, timestamp}), infer "
    "the user's interest topics, price sensitivity, and preferred channels. "
    "Respond with a single JSON object only, no prose, no markdown fences: "
    '{"interest_topics": [string, ...], "price_sensitivity": "low"|"medium"|"high", '
    '"preferred_channels": [string, ...]}. Rank both lists by strength of '
    "signal, most relevant first. Use only topics/channels evidenced in the "
    "event data — never invent ones absent from it."
)


class MainNode(FunctionNode):
    """Build the personalization context from normalised events."""

    required_trust_level = TrustLevel.VERIFIED_EXTERNAL  # S-1 gate

    def __init__(
        self,
        node_config: dict[str, Any] | None = None,
        llm: Any | None = None,
    ) -> None:
        super().__init__()
        self._config = service.merge_config(node_config)
        # `llm` is a test-double seam only — register_nodes() never passes one
        # in production. The real client is built fresh per invocation in
        # _infer_attributes() from ctx.secrets (api_key + azure_endpoint +
        # azure_deployment, all three declared secrets — none of it lives in
        # config/config.yaml), not cached on self: node instances are
        # constructed in register_nodes() (registry LRU cache, shared across
        # every invocation) before any request's secrets are provisioned, and
        # caching one caller's client would leave it visible to the next caller.
        self._llm = llm

    def execute(self, state: dict[str, Any]) -> dict[str, Any]:
        # Upstream S-1 rejection short-circuits (finalize still routed to).
        if state.get("status") == AgentStatus.ERROR.value:
            return {}

        normalized: list[dict[str, Any]] = state.get("normalized_events", [])
        user_id: str = state.get("user_id", "")

        # Step 3 (aggregate) + Step 4 — deterministic, unchanged.
        interest_scores = service.extract_interest_scores(normalized)
        scoring_result = service.compute_scoring(normalized, self._config)

        # Step 5 — profile assembly. Heuristic baseline first (always
        # available, fully deterministic); LLM-inferred interest_topics /
        # price_sensitivity / preferred_channels override it when the call
        # succeeds and validates. Any failure (secret not provisioned, API
        # error, malformed response, no events) silently keeps the heuristic
        # values — a personalization profile must always be produced, never
        # hard-fail the pipeline over an LLM outage.
        last_updated_at = datetime.now(timezone.utc).isoformat()
        profile = service.build_profile(
            user_id=user_id,
            normalized=normalized,
            interest_scores=interest_scores,
            scoring_result=scoring_result,
            cfg=self._config,
            last_updated_at=last_updated_at,
        )
        llm_attrs = self._infer_attributes(normalized, state)
        if llm_attrs is not None:
            profile.update(llm_attrs)

        # S-4 — domain audit event (no PII/secrets in payload).
        emit_trace_event(
            "context_profile_built",
            {
                "topics": len(profile["interest_topics"]),
                "events_used": len(normalized),
                "recency_score": scoring_result.get("recency_score", 0.0),
                "llm_inferred": llm_attrs is not None,
            },
            state,
        )

        return {
            "interest_scores": interest_scores,
            "scoring_result": scoring_result,
            "context_profile": profile,
            "status": AgentStatus.SUCCESS.value,
        }

    def _infer_attributes(self, normalized: list[dict[str, Any]], state: dict[str, Any]) -> dict[str, Any] | None:
        """LLM-based interest/price-sensitivity/channel inference.

        Returns None on any failure (no events, missing secret, API error,
        malformed response) — caller keeps the heuristic baseline already in
        `profile`. Never raises.
        """
        if not normalized:
            return None
        try:
            llm = self._llm
            if llm is None:
                ctx = InvocationContext.from_state(state)
                llm = AzureOpenAIClient(
                    {
                        "api_key": ctx.secrets.require("AZURE_OPENAI_API_KEY"),
                        "azure_endpoint": ctx.secrets.require("AZURE_OPENAI_ENDPOINT"),
                        "azure_deployment": ctx.secrets.require("AZURE_OPENAI_DEPLOYMENT"),
                    }
                )
            response = llm.complete(
                [
                    {"role": "system", "content": _SYSTEM_PROMPT},
                    {"role": "user", "content": service.summarize_events_for_llm(normalized)},
                ]
            )
            parsed = extract_json_object(response.get("content", ""))
            return service.validate_llm_attributes(parsed)
        except Exception:
            return None
