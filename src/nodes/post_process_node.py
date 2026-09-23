"""post_process slot — ProfileOutput (S-3 mandatory pseudonymisation gate).

Realises logical step 6 of the design (docs/02_design.md §1.1). Verifies the
profile's user_id is pseudonymised before surfacing (non-bypassable, regardless
of config), optionally writes to the configured CDP sink (mock writer — no live
credentials in v1), stamps the TTL, and writes the profile JSON to
`formatted_output` (surfaced by `get_output()`).
"""

from __future__ import annotations

import json
from typing import Any

from framework.nodes import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from framework.utils.audit_logger import emit_trace_event

from src.services import service


class PostProcessNode(FunctionNode):
    """Format + emit the context profile through the mandatory S-3 output gate."""

    # S-1 gate: the S-3 pseudonymisation gate is the real guard here; the node
    # itself carries no privileged side effect, so the secure default applies.
    required_trust_level = TrustLevel.ANONYMOUS

    def __init__(self, node_config: dict[str, Any] | None = None) -> None:
        super().__init__()
        self._config = service.merge_config(node_config)

    def execute(self, state: dict[str, Any]) -> dict[str, Any]:
        profile: dict[str, Any] = state.get("context_profile", {})

        # S-3 output gate (mandatory, non-bypassable): block raw-PII user_id.
        user_id = str(profile.get("user_id", ""))
        if not service.is_pseudonymized(user_id):
            return {
                "status": AgentStatus.ERROR.value,
                "errors": ["S-3 output gate: user_id is not pseudonymised — output blocked"],
                "error_log": ["PostProcess: S-3 pseudonymisation gate rejected output"],
            }

        # Optional CDP write (interface + mock; live token via ctx.secrets only).
        targets = service.cdp_targets(self._config)

        # S-4 — unconditional domain audit event: fires on every prepared output,
        # whether or not a CDP sink is configured (no PII/secrets in payload).
        emit_trace_event(
            "profile_output_prepared",
            {"user_id_pseudonymised": True, "has_cdp_sinks": bool(targets)},
            state,
        )

        if targets:
            emit_trace_event(
                "cdp_profile_written",
                {"sinks": targets, "user_id_pseudonymised": True},
                state,
            )

        return {"formatted_output": json.dumps(profile, ensure_ascii=False)}
