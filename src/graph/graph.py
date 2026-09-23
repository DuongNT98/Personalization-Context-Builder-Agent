"""Graph composition for CMN-C1-036 PersonalizationContextBuilderAgent.

Cat 1 template on `AgentBaseGraph` (SDK 1.0.1): the six logical pipeline steps
(ingest/normalize/interest/scoring/build/output) are realised across the three
fixed domain slots (pre_process / main / post_process), each delegating to the
pure-Python service layer. See docs/02_design.md §1.1 for the mapping rationale.

Config is constructor-injected via the base class's own `config` keyword
(`Graph(config=...)`, matching `AgentBaseGraph.__init__` — both
`AgentRegistry._compile_and_cache()` and `src/api/server.py` build the graph
this way) and threaded to each node in `register_nodes()` — not passed through
`__call__` (SDK 1.0.1 nodes take `execute(self, state)` with no config
argument).
"""

from __future__ import annotations

from typing import Any, cast

from framework.graph.agent_base_graph import AgentBaseGraph
from framework.schemas.agent_status import AgentStatus

from src.nodes.main_node import MainNode
from src.nodes.post_process_node import PostProcessNode
from src.nodes.pre_process_node import PreProcessNode
from src.schemas.state import State


class PersonalizationContextBuilderGraph(AgentBaseGraph):
    """Builds a personalization context profile from a behavioural event stream."""

    @property
    def name(self) -> str:
        return "PersonalizationContextBuilderAgent"

    @property
    def state_schema(self) -> type:
        return State

    def register_nodes(self) -> None:
        # super() injects the backbone initialize + finalize nodes.
        super().register_nodes()
        self._nodes["pre_process"] = PreProcessNode(node_config=self.config)
        self._nodes["main"] = MainNode(node_config=self.config)
        self._nodes["post_process"] = PostProcessNode(node_config=self.config)

    def _extra_initial_state(self) -> dict[str, Any]:
        return {
            "raw_events": [],
            "normalized_events": [],
            "errors": [],
            "interest_scores": {},
            "scoring_result": {},
            "context_profile": {},
            "user_id": "",
        }

    def get_output(self, state: dict[str, Any]) -> dict[str, Any]:
        out = cast(dict[str, Any], super().get_output(state))  # output, status, trace_id, correlation_id, node_history
        # S-3: only surface context_profile on the success path. PostProcessNode's
        # S-3 gate can reject a raw (non-pseudonymised) profile and set
        # status=error; state["context_profile"] still holds the pre-gate value
        # in that case (MainNode wrote it), so surfacing it unconditionally here
        # would leak it around the gate (fail-closed get_output() leak, SDK
        # 1.0.1 §2/§8).
        is_success = state.get("status") == AgentStatus.SUCCESS.value
        out.update(
            {
                "context_profile": state.get("context_profile", {}) if is_success else {},
                "errors": state.get("errors", []),
            }
        )
        return out


# Backwards-compatible alias for the scaffold/server entrypoint (`Graph`).
Graph = PersonalizationContextBuilderGraph
