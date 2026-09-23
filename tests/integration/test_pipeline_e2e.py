# End-to-end pipeline test: a real Graph.invoke() run must walk the fixed
# AgentBaseGraph lifecycle (initialize -> pre_process -> main -> post_process ->
# finalize) and surface a correctly-built context profile.
#
# Relocated from tests/proof_of_boundary/test_pb_invoke_order.py (originally
# added there for CMN-C1-036-CR-R4-01, "rename PB-6 test to exact MANIFEST name")
# during the SDK 1.0.1 reintegration: that exact filename is now the scaffold's
# generic per-node S-1->S-2->execute()->S-3->S-4 call-order boundary test (see
# tests/proof_of_boundary/test_pb_invoke_order.py), which this repo did not
# previously have. This file's domain-specific, real-Graph.invoke() coverage is
# kept here rather than dropped.

import json

from framework.schemas.invocation_context import InvocationContext
from framework.schemas.trust_level import TrustLevel
from src.graph.graph import Graph


def _invoke(events):
    agent = Graph()
    agent.compile()
    ctx = InvocationContext(
        caller_trust_level=TrustLevel.VERIFIED_EXTERNAL,
        session_id="pb6",
        caller_id="tester",
    )
    return agent.invoke(json.dumps(events), ctx=ctx)


class TestPipelineOrderBoundary:
    EVENTS = [
        {"event_type": "purchase", "timestamp": "2026-06-24T07:00:00Z", "entity_id": "electronics:p1", "user_id": "anon_abc", "channel": "app"},
        {"event_type": "product_view", "timestamp": "2026-06-24T06:30:00Z", "entity_id": "wearables:w1", "user_id": "anon_abc", "channel": "web"},
        {"event_type": "search", "timestamp": "2026-06-24T06:00:00Z", "entity_id": "electronics:q", "user_id": "anon_abc", "channel": "web"},
    ]

    def test_node_history_exact_order(self):
        result = _invoke(self.EVENTS)
        assert result["node_history"] == [
            "InitializeNode", "PreProcessNode", "MainNode", "PostProcessNode", "FinalizeNode",
        ]

    def test_success_status_and_profile_fields(self):
        result = _invoke(self.EVENTS)
        assert result["status"] == "success"
        profile = result["context_profile"]
        assert profile["user_id"] == "anon_abc"
        # electronics (purchase 1.0×2.0 + search 0.5×1.0 = 2.5) ranks above wearables (0.3)
        assert profile["interest_topics"] == ["electronics", "wearables"]
        assert profile["price_sensitivity"] == "medium"  # 1 purchase / 3 ≈ 0.33
        assert profile["preferred_channels"] == ["app", "web"]
        assert profile["ttl_hours"] == 24

    def test_empty_input_routes_to_error(self):
        """No valid events → status error; main is reached but its body is skipped
        (status=error), post_process is routed past, and no profile is built."""
        result = _invoke([])
        assert result["status"] == "error"
        # MainNode appears (it is invoked) but its execute() is skipped on error,
        # so route() sends main -> finalize and PostProcessNode never runs.
        assert result["node_history"] == [
            "InitializeNode", "PreProcessNode", "MainNode", "FinalizeNode",
        ]
        assert "PostProcessNode" not in result["node_history"]
        assert result["context_profile"] == {}
