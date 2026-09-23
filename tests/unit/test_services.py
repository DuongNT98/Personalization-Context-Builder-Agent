# CMN-C1-036 — Unit tests: domain service layer (TC-10..TC-15)

import hashlib
from datetime import datetime, timezone

from src.services import service

CFG = service.merge_config(None)


def _normalized_sample():
    """Two valid events: a 1h-old purchase and a 2h-old product_view (electronics)."""
    raw = [
        {
            "event_type": "purchase",
            "timestamp": "2026-06-24T07:00:00Z",
            "entity_id": "electronics:p1",
            "user_id": "anon_abc",
            "channel": "app",
        },
        {
            "event_type": "product_view",
            "timestamp": "2026-06-24T06:00:00Z",
            "entity_id": "electronics:p2",
            "user_id": "anon_abc",
            "channel": "web",
        },
    ]
    normalized, errors = service.normalize_events(raw, CFG)
    return normalized, errors


def test_normalize_events_validation():
    """TC-10: valid + unknown-type + raw-PII batch → exact normalized count + errors."""
    raw = [
        {"event_type": "purchase", "timestamp": "2026-06-24T07:00:00Z", "entity_id": "electronics:p1", "user_id": "anon_abc"},
        {"event_type": "bogus", "timestamp": "2026-06-24T07:00:00Z", "user_id": "anon_abc"},
        {"event_type": "product_view", "timestamp": "2026-06-24T06:00:00Z", "user_id": "john@example.com"},
        {"event_type": "search", "timestamp": "not-a-date", "user_id": "anon_abc"},
    ]
    normalized, errors = service.normalize_events(raw, CFG)
    assert len(normalized) == 1
    assert normalized[0]["event_type"] == "purchase"
    assert normalized[0]["timestamp"] == "2026-06-24T07:00:00+00:00"
    # errors follow input order: bogus(idx1), PII(idx2), unparseable(idx3)
    assert errors == [
        "unknown event_type skipped: bogus",
        "event user_id is not pseudonymised (raw PII rejected): product_view",
        "unparseable timestamp dropped: 'not-a-date'",
    ]


def test_extract_interest_scores_aggregation():
    """TC-11: topic → exact weighted score (interest_weight × recency_boost)."""
    normalized, _ = _normalized_sample()
    scores = service.extract_interest_scores(normalized)
    # purchase: 1.0 × 2.0 = 2.0 ; product_view: 0.3 × 1.0 = 0.3 → electronics 2.3
    assert scores == {"electronics": 2.3}


def test_compute_scoring_fixed_reference_time():
    """TC-12: deterministic recency/frequency/engagement at a fixed reference_time."""
    normalized, _ = _normalized_sample()
    ref = datetime(2026, 6, 24, 8, 0, 0, tzinfo=timezone.utc)
    result = service.compute_scoring(normalized, CFG, reference_time=ref)
    assert result["recency_score"] == 0.9396   # mean(exp(-1/24), exp(-2/24))
    assert result["frequency_score"] == 0.2     # 2 events in window / saturation 10
    assert result["engagement_depth_score"] == 1.0  # (2.0 + 0.3)/2 capped at 1.0


def test_compute_scoring_empty():
    """TC-12: empty event list → all-zero scores."""
    result = service.compute_scoring([], CFG, reference_time=datetime(2026, 6, 24, tzinfo=timezone.utc))
    assert result == {"recency_score": 0.0, "frequency_score": 0.0, "engagement_depth_score": 0.0}


def test_infer_price_sensitivity():
    """TC-13: purchase ratio drives sensitivity bucket."""
    normalized, _ = _normalized_sample()  # 1 purchase / 2 = 0.5 ≥ 0.4
    assert service.infer_price_sensitivity(normalized) == "low"
    views_only = [{"event_type": "product_view"}, {"event_type": "search"}]
    assert service.infer_price_sensitivity(views_only) == "high"
    mixed = [{"event_type": "purchase"}, {"event_type": "product_view"}, {"event_type": "search"}]
    assert service.infer_price_sensitivity(mixed) == "medium"  # 1/3 ≈ 0.33


def test_build_profile_structure():
    """TC-14: profile ranks topics, excludes 'unknown', stamps TTL."""
    normalized, _ = _normalized_sample()
    interest = {"electronics": 2.3, "books": 0.3, "unknown": 5.0}
    scoring = {"recency_score": 0.9396, "frequency_score": 0.2, "engagement_depth_score": 1.0}
    profile = service.build_profile("anon_abc", normalized, interest, scoring, CFG, last_updated_at="2026-06-24T08:00:00+00:00")
    assert profile["user_id"] == "anon_abc"
    assert profile["interest_topics"] == ["electronics", "books"]  # 'unknown' excluded
    assert profile["preferred_channels"] == ["app", "web"]
    assert profile["price_sensitivity"] == "low"
    assert profile["recency_score"] == 0.9396
    assert profile["ttl_hours"] == 24
    assert profile["last_updated_at"] == "2026-06-24T08:00:00+00:00"


def test_summarize_events_for_llm_excludes_user_id_and_weights():
    """TC: LLM prompt summary carries topic/type/channel/timestamp only."""
    normalized, _ = _normalized_sample()
    summary = service.summarize_events_for_llm(normalized)
    assert "anon_abc" not in summary
    assert "interest_weight" not in summary
    assert "electronics" in summary
    assert "purchase" in summary


def test_validate_llm_attributes_accepts_well_formed():
    parsed = {
        "interest_topics": ["electronics", "wearables"],
        "price_sensitivity": "high",
        "preferred_channels": ["app"],
    }
    assert service.validate_llm_attributes(parsed) == parsed


def test_validate_llm_attributes_rejects_bad_shapes():
    assert service.validate_llm_attributes(None) is None
    assert service.validate_llm_attributes([]) is None
    assert service.validate_llm_attributes({"interest_topics": "not-a-list"}) is None
    assert service.validate_llm_attributes(
        {"interest_topics": [], "price_sensitivity": "extreme", "preferred_channels": []}
    ) is None
    assert service.validate_llm_attributes(
        {"interest_topics": [], "price_sensitivity": "low", "preferred_channels": [1, 2]}
    ) is None


def test_pseudonymize_deterministic():
    """TC-15: pseudonymise is deterministic; PII fails the pseudonymised check."""
    expected = "anon_" + hashlib.sha256(b"john@example.com").hexdigest()[:12]
    assert service.pseudonymize("john@example.com") == expected
    assert service.is_pseudonymized("anon_abc") is True
    assert service.is_pseudonymized("john@example.com") is False
    assert service.is_pseudonymized("+81-90-1234-5678") is False
    # anon_ fast-path: a digit-heavy canonical id must NOT trip the phone regex.
    assert service.is_pseudonymized("anon_1234567890ab") is True
    assert service.pseudonymize("x").startswith("anon_")
