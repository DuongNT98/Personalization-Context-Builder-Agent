# CMN-C1-036 — Unit tests: Main Node (TC-16, TC-17)

from src.nodes.main_node import MainNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from src.services import service


class _FakeLLM:
    """Test-double for AzureOpenAIClient — same `complete(messages) -> dict` contract."""

    def __init__(self, content: str) -> None:
        self._content = content
        self.calls: list[list[dict[str, str]]] = []

    def complete(self, messages: list[dict[str, str]]) -> dict[str, object]:
        self.calls.append(messages)
        return {"content": self._content}


def _seeded_state():
    raw = [
        {"event_type": "purchase", "timestamp": "2026-06-24T07:00:00Z", "entity_id": "electronics:p1", "user_id": "anon_abc", "channel": "app"},
        {"event_type": "product_view", "timestamp": "2026-06-24T06:00:00Z", "entity_id": "electronics:p2", "user_id": "anon_abc", "channel": "web"},
    ]
    normalized, _ = service.normalize_events(raw, service.merge_config(None))
    return {"normalized_events": normalized, "user_id": "anon_abc", "node_history": [], "error_log": []}


class TestMainNode:
    def setup_method(self):
        self.node = MainNode()

    def test_s1_trust_level_declared(self):
        """TC-08: main node enforces VERIFIED_EXTERNAL trust."""
        assert MainNode.required_trust_level == TrustLevel.VERIFIED_EXTERNAL

    def test_success_path_builds_profile(self):
        """TC-16: valid normalized events → profile + SUCCESS."""
        result = self.node.execute(_seeded_state())
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["interest_scores"] == {"electronics": 2.3}
        assert set(result["scoring_result"]) >= {"recency_score", "frequency_score", "engagement_depth_score"}
        profile = result["context_profile"]
        assert profile["user_id"] == "anon_abc"
        assert profile["interest_topics"] == ["electronics"]
        assert profile["price_sensitivity"] == "low"
        assert profile["preferred_channels"] == ["app", "web"]

    def test_error_short_circuit(self):
        """TC-17: upstream status=error → main does not recompute (returns empty)."""
        result = self.node.execute({"status": AgentStatus.ERROR.value, "normalized_events": []})
        assert result == {}

    def test_execute_signature(self):
        """Node contract: execute(self, state), no _invoke_impl."""
        import inspect
        params = list(inspect.signature(MainNode.execute).parameters.keys())
        assert params[:2] == ["self", "state"]
        assert "_invoke_impl" not in MainNode.__dict__


class TestMainNodeLLM:
    """LLM-backed InterestExtract path (test-double injection — no real Azure call)."""

    def test_llm_response_overrides_heuristic_attributes(self):
        """Valid LLM JSON overrides interest_topics/price_sensitivity/preferred_channels;
        deterministic scoring_result is untouched."""
        llm = _FakeLLM(
            '{"interest_topics": ["gadgets", "wearables"], "price_sensitivity": "high", '
            '"preferred_channels": ["app"]}'
        )
        node = MainNode(llm=llm)
        result = node.execute(_seeded_state())

        assert result["status"] == AgentStatus.SUCCESS.value
        profile = result["context_profile"]
        assert profile["interest_topics"] == ["gadgets", "wearables"]
        assert profile["price_sensitivity"] == "high"
        assert profile["preferred_channels"] == ["app"]
        # Deterministic fields are unaffected by the LLM path.
        assert set(result["scoring_result"]) >= {
            "recency_score",
            "frequency_score",
            "engagement_depth_score",
        }
        assert result["interest_scores"] == {"electronics": 2.3}
        assert len(llm.calls) == 1
        assert llm.calls[0][0]["role"] == "system"

    def test_llm_prose_wrapped_json_is_extracted(self):
        """LLM output wrapped in prose/markdown still parses (extract_json_object)."""
        llm = _FakeLLM(
            'Sure, here is the analysis:\n```json\n'
            '{"interest_topics": ["electronics"], "price_sensitivity": "low", '
            '"preferred_channels": ["web"]}\n```'
        )
        result = MainNode(llm=llm).execute(_seeded_state())
        profile = result["context_profile"]
        assert profile["interest_topics"] == ["electronics"]
        assert profile["preferred_channels"] == ["web"]

    def test_malformed_llm_response_falls_back_to_heuristic(self):
        """Invalid shape (missing price_sensitivity) -> heuristic values, no crash."""
        llm = _FakeLLM('{"interest_topics": ["electronics"]}')
        result = MainNode(llm=llm).execute(_seeded_state())

        assert result["status"] == AgentStatus.SUCCESS.value
        profile = result["context_profile"]
        assert profile["interest_topics"] == ["electronics"]  # heuristic ranking, unchanged
        assert profile["price_sensitivity"] == "low"
        assert profile["preferred_channels"] == ["app", "web"]

    def test_llm_exception_falls_back_to_heuristic(self):
        """LLM call raising (e.g. API error) -> heuristic values, no crash."""

        class _RaisingLLM:
            def complete(self, messages):
                raise RuntimeError("simulated Azure OpenAI API error")

        result = MainNode(llm=_RaisingLLM()).execute(_seeded_state())
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["context_profile"]["price_sensitivity"] == "low"

    def test_no_secret_configured_falls_back_to_heuristic(self):
        """No llm= injected and no secret provisioned -> heuristic, no crash.

        This is the real production shape when register_nodes() never passes
        llm= and ctx.secrets.require("AZURE_OPENAI_API_KEY") has nothing bound
        (e.g. local/dev/CI with no Azure key configured).
        """
        result = MainNode().execute(_seeded_state())
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["context_profile"]["interest_topics"] == ["electronics"]

    def test_empty_events_skips_llm_call(self):
        """No normalized events -> _infer_attributes never calls the LLM."""
        llm = _FakeLLM('{"interest_topics": [], "price_sensitivity": "low", "preferred_channels": []}')
        state = {"normalized_events": [], "user_id": "anon_abc", "node_history": [], "error_log": []}
        MainNode(llm=llm).execute(state)
        assert llm.calls == []
