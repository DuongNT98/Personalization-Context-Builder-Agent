"""Domain service layer for CMN-C1-036 PersonalizationContextBuilderAgent.

Pure-Python personalization logic — the six logical pipeline steps
(ingest-validate, normalize, interest-extract, scoring, profile-build,
pseudonymisation) realised as stateless, unit-testable functions. The three
backbone nodes delegate here.

Import isolation (PB-4): this module imports NOTHING from `framework`,
`agenticstar`, or `shared` — it is plain domain logic with no side effects,
no routing, and no credentials.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from datetime import datetime, timezone
from typing import Any

# ── Default config (overridden by config/agent.yaml via constructor DI) ──────
DEFAULT_CONFIG: dict[str, Any] = {
    "event_types": [
        {"name": "product_view", "interest_weight": 0.3},
        {"name": "purchase", "interest_weight": 1.0, "recency_boost": 2.0},
        {"name": "search", "interest_weight": 0.5},
    ],
    "scoring_weights": {"recency": 0.4, "frequency": 0.3, "engagement_depth": 0.3},
    "cdp_integration": {"treasure_data": True, "segment": False, "amplitude": False},
    "context_ttl_hours": 24,
    "top_n_topics": 5,
    # Normalisers for bounded 0..1 scores.
    "frequency_saturation": 10,
}

REQUIRED_EVENT_FIELDS = ("event_type", "timestamp", "user_id")

# Raw-PII detectors — a pseudonymised id must match NONE of these.
_EMAIL_RE = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")
_PHONE_RE = re.compile(r"(?:\+?\d[\d\-\s()]{7,}\d)")


def merge_config(node_config: dict[str, Any] | None) -> dict[str, Any]:
    """Shallow-merge a caller config over DEFAULT_CONFIG (top-level keys only).

    Contract: this is a **top-level** merge. For a nested key (e.g.
    ``scoring_weights`` or ``cdp_integration``) the caller MUST supply the
    **complete** nested object — a partial nested dict REPLACES the default
    wholesale (it does not deep-merge), so any sub-keys omitted by the caller
    fall back to nothing, not to the DEFAULT_CONFIG sub-value. Pass the full
    nested object when overriding. (Templates wire the whole ``agent.domain``
    block from config/agent.yaml, which already carries every sub-key.)
    """
    cfg = {**DEFAULT_CONFIG}
    if node_config:
        cfg.update({k: v for k, v in node_config.items() if v is not None})
    return cfg


# ── Pseudonymisation (S-3 helper) ────────────────────────────────────────────
# Canonical pseudonymised id produced by `pseudonymize()` below.
_ANON_PREFIX = "anon_"


def is_pseudonymized(user_id: str) -> bool:
    """True iff `user_id` is a safe (non-raw-PII) identifier.

    Accepted format: the canonical ``anon_<hex>`` id emitted by ``pseudonymize()``
    is always accepted via a fast-path (it cannot contain raw PII). Any other id
    is accepted only if it matches NO raw-PII pattern (email / phone). The phone
    pattern is broad (a run of >=9 digits with separators), so callers minting
    their own pseudonymised ids should prefer the ``anon_`` form to avoid a
    false-positive rejection on a digit-heavy opaque id.
    """
    if not user_id or not isinstance(user_id, str):
        return False
    if user_id.startswith(_ANON_PREFIX):
        return True
    return not (_EMAIL_RE.search(user_id) or _PHONE_RE.search(user_id))


def pseudonymize(user_id: str) -> str:
    """Deterministically pseudonymise a raw identifier to `anon_<hash12>`."""
    digest = hashlib.sha256(user_id.encode("utf-8")).hexdigest()[:12]
    return f"{_ANON_PREFIX}{digest}"


# ── Step 1+2: ingest-validate + normalize (pre_process) ──────────────────────
def validate_event(event: Any) -> str | None:
    """Return an error string if the event is invalid, else None (S-1)."""
    if not isinstance(event, dict):
        return f"event is not an object: {event!r}"
    missing = [f for f in REQUIRED_EVENT_FIELDS if not event.get(f)]
    if missing:
        return f"event missing required field(s) {missing}: {event!r}"
    if not is_pseudonymized(str(event["user_id"])):
        return f"event user_id is not pseudonymised (raw PII rejected): {event['event_type']}"
    return None


def _event_type_weights(cfg: dict[str, Any]) -> dict[str, dict[str, float]]:
    return {
        et["name"]: {
            "interest_weight": float(et.get("interest_weight", 0.0)),
            "recency_boost": float(et.get("recency_boost", 1.0)),
        }
        for et in cfg.get("event_types", [])
    }


def _to_utc_iso(timestamp: str) -> str:
    """Parse an ISO-8601 timestamp and re-emit normalised UTC ISO-8601."""
    dt = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


def normalize_events(raw_events: list[dict[str, Any]], cfg: dict[str, Any]) -> tuple[list[dict[str, Any]], list[str]]:
    """Validate (S-1) + normalise the raw event stream.

    Returns (normalized_events, errors). Malformed events and unknown
    event_types are dropped; reasons are accumulated in `errors`.
    """
    weights = _event_type_weights(cfg)
    normalized: list[dict[str, Any]] = []
    errors: list[str] = []

    for event in raw_events:
        err = validate_event(event)
        if err:
            errors.append(err)
            continue
        et = event["event_type"]
        if et not in weights:
            errors.append(f"unknown event_type skipped: {et}")
            continue
        try:
            ts = _to_utc_iso(str(event["timestamp"]))
        except (ValueError, TypeError):
            errors.append(f"unparseable timestamp dropped: {event.get('timestamp')!r}")
            continue
        normalized.append(
            {
                "event_type": et,
                "timestamp": ts,
                "user_id": str(event["user_id"]),
                "entity_id": event.get("entity_id", ""),
                "channel": event.get("channel"),
                "interest_weight": weights[et]["interest_weight"],
                "recency_boost": weights[et]["recency_boost"],
            }
        )
    return normalized, errors


# ── Step 3: interest extraction (main) ───────────────────────────────────────
def _topic_of(entity_id: str) -> str:
    """Topic = prefix before ':' in entity_id (e.g. 'electronics:p1' -> 'electronics')."""
    if not entity_id:
        return "unknown"
    return entity_id.split(":", 1)[0]


def extract_interest_scores(normalized: list[dict[str, Any]]) -> dict[str, float]:
    """Aggregate weighted interest per topic (interest_weight * recency_boost)."""
    scores: dict[str, float] = {}
    for e in normalized:
        topic = _topic_of(e.get("entity_id", ""))
        scores[topic] = scores.get(topic, 0.0) + e["interest_weight"] * e["recency_boost"]
    return scores


def infer_price_sensitivity(normalized: list[dict[str, Any]]) -> str:
    """Heuristic: many purchases -> low; mostly browse/search -> high; else medium."""
    if not normalized:
        return "unknown"
    purchases = sum(1 for e in normalized if e["event_type"] == "purchase")
    ratio = purchases / len(normalized)
    if ratio >= 0.4:
        return "low"
    if ratio == 0.0:
        return "high"
    return "medium"


def infer_preferred_channels(normalized: list[dict[str, Any]]) -> list[str]:
    """Distinct channels present in the event metadata, in first-seen order."""
    channels: list[str] = []
    for e in normalized:
        ch = e.get("channel")
        if ch and ch not in channels:
            channels.append(ch)
    return channels


# ── Step 4: scoring (main) ───────────────────────────────────────────────────
def compute_scoring(
    normalized: list[dict[str, Any]],
    cfg: dict[str, Any],
    reference_time: datetime | None = None,
) -> dict[str, float]:
    """Compute recency / frequency / engagement_depth scores (each 0.0-1.0)."""
    weights = cfg.get("scoring_weights", DEFAULT_CONFIG["scoring_weights"])
    ttl_hours = float(cfg.get("context_ttl_hours", 24))
    saturation = float(cfg.get("frequency_saturation", 10))
    ref = reference_time or datetime.now(timezone.utc)

    if not normalized:
        return {"recency_score": 0.0, "frequency_score": 0.0, "engagement_depth_score": 0.0}

    # recency: exponential time-decay over the TTL window, averaged.
    decays = []
    in_window = 0
    for e in normalized:
        dt = datetime.fromisoformat(e["timestamp"])
        age_hours = max(0.0, (ref - dt).total_seconds() / 3600.0)
        decays.append(math.exp(-age_hours / ttl_hours))
        if age_hours <= ttl_hours:
            in_window += 1
    recency_score = sum(decays) / len(decays)

    # frequency: events within the TTL window, saturating at `saturation`.
    frequency_score = min(1.0, in_window / saturation)

    # engagement_depth: mean weighted interest, capped at 1.0.
    depth = sum(e["interest_weight"] * e["recency_boost"] for e in normalized) / len(normalized)
    engagement_depth_score = min(1.0, depth)

    return {
        "recency_score": round(recency_score, 4),
        "frequency_score": round(frequency_score, 4),
        "engagement_depth_score": round(engagement_depth_score, 4),
        # weighted composite for convenience (not surfaced as a score field)
        "composite_score": round(
            weights.get("recency", 0.4) * recency_score
            + weights.get("frequency", 0.3) * frequency_score
            + weights.get("engagement_depth", 0.3) * engagement_depth_score,
            4,
        ),
    }


# ── Step 5: profile build (main) ─────────────────────────────────────────────
def build_profile(
    user_id: str,
    normalized: list[dict[str, Any]],
    interest_scores: dict[str, float],
    scoring_result: dict[str, float],
    cfg: dict[str, Any],
    last_updated_at: str | None = None,
) -> dict[str, Any]:
    """Assemble the final user context profile."""
    top_n = int(cfg.get("top_n_topics", 5))
    ranked = sorted(interest_scores.items(), key=lambda kv: kv[1], reverse=True)
    interest_topics = [topic for topic, _ in ranked[:top_n] if topic != "unknown"]
    return {
        "user_id": user_id,
        "interest_topics": interest_topics,
        "price_sensitivity": infer_price_sensitivity(normalized),
        "preferred_channels": infer_preferred_channels(normalized),
        "recency_score": scoring_result.get("recency_score", 0.0),
        "frequency_score": scoring_result.get("frequency_score", 0.0),
        "engagement_depth_score": scoring_result.get("engagement_depth_score", 0.0),
        "last_updated_at": last_updated_at or datetime.now(timezone.utc).isoformat(),
        "ttl_hours": int(cfg.get("context_ttl_hours", 24)),
    }


# ── Step 3 (LLM path): prompt/response helpers for main's real-LLM inference ─
# Pure stdlib only (PB-4 import isolation — see module docstring). The node
# (src/nodes/main_node.py) owns the actual LLM call; these two functions only
# build its prompt input and validate/sanitise its output, so both are
# unit-testable without a real or fake LLM.
ALLOWED_PRICE_SENSITIVITY = {"low", "medium", "high"}


def summarize_events_for_llm(normalized: list[dict[str, Any]]) -> str:
    """Compact JSON summary of normalized events for an LLM prompt.

    Deliberately excludes ``user_id`` and ``recency_boost``/``interest_weight``
    (internal scoring weights, not signal for the LLM) — only topic/type/
    channel/timestamp, the same fields a human analyst would read.
    """
    rows = [
        {
            "topic": _topic_of(e.get("entity_id", "")),
            "event_type": e["event_type"],
            "channel": e.get("channel"),
            "timestamp": e["timestamp"],
        }
        for e in normalized
    ]
    return json.dumps(rows, ensure_ascii=False)


def validate_llm_attributes(parsed: Any) -> dict[str, Any] | None:
    """Validate + sanitise the LLM's parsed JSON into the profile attribute shape.

    Returns None if the shape is unusable — caller falls back to the
    deterministic heuristic. Never raises.
    """
    if not isinstance(parsed, dict):
        return None
    topics = parsed.get("interest_topics")
    sensitivity = parsed.get("price_sensitivity")
    channels = parsed.get("preferred_channels")
    if not isinstance(topics, list) or not all(isinstance(t, str) for t in topics):
        return None
    if sensitivity not in ALLOWED_PRICE_SENSITIVITY:
        return None
    if not isinstance(channels, list) or not all(isinstance(c, str) for c in channels):
        return None
    return {
        "interest_topics": topics,
        "price_sensitivity": sensitivity,
        "preferred_channels": channels,
    }


# ── Step 6 helper: CDP sink (post_process; mock writer, no live creds) ───────
def cdp_targets(cfg: dict[str, Any]) -> list[str]:
    """Enabled CDP sinks from config (interface only; v1 ships a mock writer)."""
    integ = cfg.get("cdp_integration", {})
    return [name for name in ("treasure_data", "segment", "amplitude") if integ.get(name)]
