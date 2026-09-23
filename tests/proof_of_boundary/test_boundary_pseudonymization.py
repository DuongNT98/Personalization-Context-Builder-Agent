# PB-S3: Output pseudonymisation gate (S-3, mandatory, non-bypassable)
# Boundary: post_process execute() must block any profile whose user_id is raw PII,
# regardless of config, and emit output only for a pseudonymised user_id.

from src.nodes.post_process_node import PostProcessNode
from src.nodes.pre_process_node import PreProcessNode
from framework.schemas.agent_status import AgentStatus


class TestPseudonymizationBoundary:
    def test_s3_blocks_raw_pii_email(self):
        """Raw-PII (email) user_id at output → blocked, no formatted_output."""
        node = PostProcessNode()
        out = node.execute({"context_profile": {"user_id": "john@example.com", "interest_topics": ["books"]}})
        assert out["status"] == AgentStatus.ERROR.value
        assert "formatted_output" not in out
        assert out["errors"] == ["S-3 output gate: user_id is not pseudonymised — output blocked"]

    def test_s3_blocks_raw_pii_phone(self):
        """Raw-PII (phone) user_id at output → blocked."""
        node = PostProcessNode()
        out = node.execute({"context_profile": {"user_id": "+81-90-1234-5678", "interest_topics": []}})
        assert out["status"] == AgentStatus.ERROR.value
        assert "formatted_output" not in out

    def test_s3_emits_for_pseudonymised(self):
        """Pseudonymised user_id → output emitted as JSON containing the profile."""
        node = PostProcessNode()
        out = node.execute({"context_profile": {"user_id": "anon_abc", "interest_topics": ["books"]}})
        assert out.get("status") != AgentStatus.ERROR.value
        assert '"user_id": "anon_abc"' in out["formatted_output"]

    def test_s1_directly_seeded_pii_rejected(self):
        """S-1 secondary gate: directly-seeded raw_events with PII id → rejected."""
        node = PreProcessNode()
        out = node.execute({"raw_events": [
            {"event_type": "product_view", "timestamp": "2026-06-24T06:00:00Z", "entity_id": "books:b1", "user_id": "jane@example.com"},
        ]})
        assert out["status"] == AgentStatus.ERROR.value
        assert out["normalized_events"] == []
