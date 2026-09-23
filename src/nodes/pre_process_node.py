"""pre_process slot — EventStreamIngest + EventNormalize (S-1 validation).

Parses the caller's JSON event array, validates required fields, rejects raw-PII
user_ids, and normalises timestamps + event-type weights. Realises logical steps
1-2 of the design (see docs/02_design.md §1.1). Delegates domain logic to
`src.services.service`.
"""

from __future__ import annotations

import json
from typing import Any

from framework.nodes import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from framework.utils.audit_logger import emit_trace_event

from src.services import service


class PreProcessNode(FunctionNode):
    """Ingest, validate (S-1), and normalise the behavioural event stream."""

    # S-1 gate: ingest accepts unauthenticated public callers (secure default).
    required_trust_level = TrustLevel.ANONYMOUS

    def __init__(self, node_config: dict[str, Any] | None = None) -> None:
        super().__init__()
        self._config = service.merge_config(node_config)

    def execute(self, state: dict[str, Any]) -> dict[str, Any]:
        # raw_events may be seeded directly (tests) or arrive as JSON in user_input.
        raw_events = state.get("raw_events")
        if not raw_events:
            user_input = (state.get("user_input") or "").strip()
            try:
                parsed = json.loads(user_input) if user_input else []
            except json.JSONDecodeError as exc:
                return {
                    "status": AgentStatus.ERROR.value,
                    "errors": [f"input is not valid JSON: {exc}"],
                    "error_log": ["PreProcess: user_input is not a valid JSON event array"],
                }
            raw_events = parsed if isinstance(parsed, list) else [parsed]

        normalized, errors = service.normalize_events(raw_events, self._config)

        # S-4 — unconditional domain audit event (no PII/secrets in payload).
        emit_trace_event(
            "events_ingested",
            {"raw_count": len(raw_events), "normalized_count": len(normalized)},
            state,
        )

        if not normalized:
            return {
                "raw_events": raw_events,
                "normalized_events": [],
                "errors": errors or ["no valid events in input"],
                "status": AgentStatus.ERROR.value,
                "error_log": ["PreProcess: no valid events after S-1 validation"],
            }

        return {
            "raw_events": raw_events,
            "normalized_events": normalized,
            "errors": errors,
            "user_id": normalized[0]["user_id"],
        }
