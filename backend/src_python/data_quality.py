"""Fail-closed data-source governance for the Trident ingest boundary.

The event bus is deliberately not a source validator.  This module is the
small, explicit policy layer in front of it: every external record receives a
source tier, an ingest decision, and a separate decision-eligibility flag.
Candidate records may be retained for review, but they are never presented to
the AI worker as decision input until an operator enables the source and its
out-of-sample validation has been completed.

The policy is intentionally conservative.  Unknown sources are rejected and
only a small set of direct/official endpoints is treated as decision eligible.
This is not a claim that a vendor is intrinsically accurate; it is an audit
boundary that makes that claim explicit and measurable.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Optional

import config


@dataclass(frozen=True)
class SourcePolicy:
    key: str
    label: str
    authority_tier: str
    kinds: tuple[str, ...]
    mode: str = "candidate"  # verified | candidate | blocked
    decision_env: str = ""
    reference_url: str = ""
    notes: str = ""


@dataclass(frozen=True)
class QualityDecision:
    source: str
    kind: str
    event_id: str
    accepted: bool
    decision_eligible: bool
    authority_tier: str
    reason: str
    quality_status: str
    latency_ms: Optional[float] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# Direct exchange feeds and official statistical publishers are the only
# default decision-eligible tier.  Jin10 is an authorised/licensed provider,
# but its use in decisions remains an explicit operator switch so the team can
# establish empirical lift before it becomes a trading factor.
POLICIES: tuple[SourcePolicy, ...] = (
    SourcePolicy("binance", "Binance direct", "A", ("market", "structure"), mode="verified",
                 reference_url="https://developers.binance.com/docs/binance-spot-api-docs/web-socket-streams",
                 notes="exchange-native stream; validate symbol/clock and sequence gaps"),
    SourcePolicy("okx", "OKX direct", "A", ("market", "structure"), mode="verified",
                 reference_url="https://www.okx.com/docs-v5/en/", notes="exchange-native market feed"),
    SourcePolicy("bitget", "Bitget direct", "A", ("market", "structure"), mode="verified",
                 reference_url="https://www.bitget.com/api-doc", notes="exchange-native market feed"),
    SourcePolicy("gateio", "Gate.io direct", "A", ("market", "structure"), mode="verified",
                 reference_url="https://www.gate.io/docs/developers/apiv4/en/", notes="exchange-native market feed"),
    SourcePolicy("median", "Verified-source aggregate", "B", ("market", "structure"), "candidate",
                 notes="derived value; promote only when constituent feeds and spread checks pass"),
    SourcePolicy("federal_reserve", "Federal Reserve", "A", ("macro", "calendar"), mode="verified",
                 reference_url="https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm", notes="official FOMC releases"),
    SourcePolicy("bls", "U.S. Bureau of Labor Statistics", "A", ("macro", "calendar"), mode="verified",
                 reference_url="https://www.bls.gov/developers/api_signature_v2.htm", notes="official CPI/employment releases"),
    SourcePolicy("bea", "U.S. Bureau of Economic Analysis", "A", ("macro", "calendar"), mode="verified",
                 reference_url="https://apps.bea.gov/API/signup/", notes="official GDP/PCE releases"),
    SourcePolicy("nyfed", "Federal Reserve Bank of New York", "A", ("macro", "market", "fed"), mode="verified",
                 reference_url="https://www.newyorkfed.org/markets/reference-rates/effr", notes="official EFFR/SOFR and reference rates"),
    SourcePolicy("binance_fapi", "Binance Futures derivatives", "A", ("structure", "market", "flow"), mode="verified",
                 reference_url="https://developers.binance.com/docs/derivatives/usds-margined-futures/websocket-market-streams",
                 notes="direct funding/open-interest/order-flow fields"),
    SourcePolicy("census", "U.S. Census Bureau", "A", ("macro", "calendar"), mode="verified",
                 reference_url="https://www.census.gov/data/developers/data-sets.html", notes="official trade/retail/housing data"),
    SourcePolicy("ism", "Institute for Supply Management", "A", ("macro", "calendar"), mode="verified",
                 reference_url="https://www.ismworld.org/supply-management-news-and-reports/reports/", notes="official PMI publisher; terms apply"),
    SourcePolicy("jin10", "金十（授权 Open Data）", "B", ("news", "market", "macro", "calendar"), mode="candidate",
                 reference_url="https://open-data-api.jin10.com/", notes="requires an authorised secret-key and contract review"),
    # Existing feeds are retained as candidate observations for continuity,
    # but are not AI/trading input until explicitly promoted.
    SourcePolicy("financialjuice", "FinancialJuice", "B", ("news",), mode="candidate",
                 reference_url="https://www.financialjuice.com/", notes="licence/terms and timestamp fidelity must be verified"),
    SourcePolicy("techflow", "TechFlow", "C", ("news",), mode="candidate",
                 reference_url="https://www.techflowpost.com/", notes="secondary publisher; corroborate before use"),
    SourcePolicy("eastmoney", "东方财富", "C", ("news",), mode="candidate",
                 reference_url="https://www.eastmoney.com/", notes="secondary publisher; corroborate before use"),
    SourcePolicy("blockbeats", "律动 BlockBeats", "C", ("news",), mode="candidate",
                 reference_url="https://www.theblockbeats.info/", notes="secondary publisher; corroborate before use"),
    SourcePolicy("tree_news", "Tree/Telegram webhook", "C", ("news",), "candidate",
                 notes="operator supplied feed; provenance must be attested"),
    SourcePolicy("alternative_me", "Alternative.me", "C", ("sentiment",), mode="candidate",
                 reference_url="https://alternative.me/crypto/fear-and-greed-index/", notes="derived sentiment, not an official market fact"),
    SourcePolicy("yahoo", "Yahoo Finance", "C", ("market", "macro"), mode="candidate",
                 reference_url="https://finance.yahoo.com/", notes="convenience mirror; do not use as canonical"),
    SourcePolicy("tradingview", "TradingView", "C", ("market", "structure"), mode="candidate",
                 reference_url="https://www.tradingview.com/", notes="display/vendor feed; no assumption of free redistribution"),
    SourcePolicy("social", "Social/public posts", "C", ("sentiment", "news"), "candidate",
                 notes="sentiment hypothesis only; identity, bots and edits require validation"),
    SourcePolicy("sosovalue", "SoSoValue", "C", ("market", "sentiment", "flow"), mode="candidate",
                 reference_url="https://sosovalue.com/", notes="derived ETF/flow view; corroborate before use"),
    SourcePolicy("yahoo_zq", "Yahoo futures-implied proxy", "C", ("macro", "fed"), mode="candidate",
                 reference_url="https://finance.yahoo.com/", notes="market-implied estimate, not an official probability"),
    SourcePolicy("derived", "Derived/feature layer", "C", ("macro", "market", "structure", "sentiment", "fed", "flow"), mode="candidate",
                 notes="must retain formula, inputs, and validation report"),
)

_POLICY_BY_KEY = {item.key: item for item in POLICIES}


def ensure_schema(connection: Any) -> None:
    """Create the small audit/quarantine tables for legacy test databases.

    Normal application startup calls ``db.migrate``.  Ingestion helpers are
    also used by replay/tests against older SQLite files, so the gate must be
    able to initialise only its own tables without assuming a full migration.
    """
    connection.execute(
        """CREATE TABLE IF NOT EXISTS data_quality_audit (
               id INTEGER PRIMARY KEY AUTOINCREMENT,
               source TEXT NOT NULL, kind TEXT NOT NULL,
               event_id TEXT NOT NULL DEFAULT '',
               observed_at TEXT NOT NULL DEFAULT (datetime('now')),
               published_at TEXT NOT NULL DEFAULT '', authority_tier TEXT NOT NULL DEFAULT 'unknown',
               accepted INTEGER NOT NULL DEFAULT 0, decision_eligible INTEGER NOT NULL DEFAULT 0,
               reason TEXT NOT NULL DEFAULT '', latency_ms REAL,
               payload_hash TEXT NOT NULL DEFAULT '', metadata TEXT NOT NULL DEFAULT '{}',
               created_at TEXT NOT NULL DEFAULT (datetime('now'))
           )"""
    )
    connection.execute(
        """CREATE TABLE IF NOT EXISTS data_quarantine (
               id INTEGER PRIMARY KEY AUTOINCREMENT,
               source TEXT NOT NULL, kind TEXT NOT NULL,
               event_id TEXT NOT NULL DEFAULT '', observed_at TEXT NOT NULL DEFAULT (datetime('now')),
               published_at TEXT NOT NULL DEFAULT '', reason TEXT NOT NULL DEFAULT '',
               payload TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL DEFAULT (datetime('now')),
               UNIQUE(source, kind, event_id)
           )"""
    )
    connection.execute(
        """CREATE INDEX IF NOT EXISTS idx_data_quality_audit_event
           ON data_quality_audit(source, kind, event_id, payload_hash)"""
    )
    columns = {row[1] for row in connection.execute("PRAGMA table_info(raw_news)")}
    if columns:
        if "quality_status" not in columns:
            connection.execute("ALTER TABLE raw_news ADD COLUMN quality_status TEXT NOT NULL DEFAULT 'unverified'")
        if "quality_reason" not in columns:
            connection.execute("ALTER TABLE raw_news ADD COLUMN quality_reason TEXT NOT NULL DEFAULT ''")


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _promoted_sources() -> set[str]:
    raw = os.getenv("TRIDENT_DECISION_SOURCE_ALLOWLIST", "")
    promoted = {canonical_source(item) for item in raw.split(",") if item.strip()}
    if _env_bool("TRIDENT_JIN10_DECISION_ENABLED", False):
        promoted.add("jin10")
    return promoted


def canonical_source(source: str) -> str:
    """Collapse source labels used by websocket/webhook adapters."""
    raw = str(source or "").strip()
    value = raw.lower()
    if "jin10" in value or "金十" in raw:
        return "jin10"
    if "financialjuice" in value or value.startswith("ws:") or "fj:" in value:
        return "financialjuice"
    if "techflow" in value or "深潮" in raw:
        return "techflow"
    if "eastmoney" in value or "东方财富" in raw:
        return "eastmoney"
    if "blockbeats" in value or "律动" in raw:
        return "blockbeats"
    if "tree" in value or "telegram" in value or value.startswith("web:"):
        return "tree_news"
    if "alternative" in value or "fear" in value:
        return "alternative_me"
    if "yahoo_zq" in value or "implied" in value:
        return "yahoo_zq"
    if "yahoo" in value:
        return "yahoo"
    if "tradingview" in value:
        return "tradingview"
    if value in {"median", "aggregate", "multi_source_median"}:
        return "median"
    if "binance_fapi" in value or "binance futures" in value or "binance_taker" in value:
        return "binance_fapi"
    if "sosovalue" in value:
        return "sosovalue"
    if value.startswith("derived"):
        return "derived"
    if value in {"binance", "binance direct", "binance_futures"}:
        return "binance"
    if value in {"okx", "okx direct"}:
        return "okx"
    if value in {"bitget", "gate.io", "gateio"}:
        return "gateio" if "gate" in value else "bitget"
    if "federal reserve" in value or "fomc" in value:
        return "federal_reserve"
    if value == "bls" or "bureau of labor" in value:
        return "bls"
    if value == "bea" or "bureau of economic" in value:
        return "bea"
    if "ny fed" in value or "new york fed" in value or value == "nyfed":
        return "nyfed"
    if "census" in value:
        return "census"
    if value == "ism" or "institute for supply" in value:
        return "ism"
    if "social" in value:
        return "social"
    # Preserve explicit test/internal labels as legacy records.  They are
    # visible in the audit API but are not promoted to a verified source.
    if value in {"external", "internal", "test", "source-a", "source-b", "source-c", "source-d"}:
        return value
    return value[:80]


def _policy_for(source: str) -> Optional[SourcePolicy]:
    return _POLICY_BY_KEY.get(canonical_source(source))


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _event_id(value: Any, payload: Any) -> str:
    if value not in (None, ""):
        return str(value)[:200]
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def _parse_time(value: Any) -> Optional[float]:
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        return float(value) / 1000.0 if float(value) > 10_000_000_000 else float(value)
    try:
        text = str(value).strip().replace("Z", "+00:00")
        return datetime.fromisoformat(text).timestamp()
    except (TypeError, ValueError, OverflowError):
        return None


def assess(
    *,
    source: str,
    kind: str,
    event_id: Any = None,
    published_at: Any = None,
    payload: Any = None,
    observed_at: Optional[float] = None,
) -> QualityDecision:
    """Return a fail-closed decision without writing to the database."""
    started = time.perf_counter()
    canonical = canonical_source(source)
    eid = _event_id(event_id, payload)
    policy = _policy_for(source)
    kind = str(kind or "unknown").lower()
    if not str(source or "").strip():
        return QualityDecision(canonical, kind, eid, False, False, "unknown", "missing_source", "blocked", (time.perf_counter() - started) * 1000)
    if not eid:
        return QualityDecision(canonical, kind, eid, False, False, "unknown", "missing_event_id", "blocked", (time.perf_counter() - started) * 1000)
    if policy is None:
        return QualityDecision(canonical, kind, eid, False, False, "unknown", "source_not_allowlisted", "blocked", (time.perf_counter() - started) * 1000)
    if kind not in policy.kinds and "all" not in policy.kinds:
        return QualityDecision(canonical, kind, eid, False, False, policy.authority_tier, "kind_not_supported_by_source", "blocked", (time.perf_counter() - started) * 1000)

    # Shape and clock checks happen before any record can reach a canonical
    # table.  Scheduled calendar rows are allowed to point into the future;
    # observed news/quotes are not.
    if kind == "market":
        if not isinstance(payload, dict):
            return QualityDecision(canonical, kind, eid, False, False, policy.authority_tier, "payload_not_object", "blocked", (time.perf_counter() - started) * 1000)
        try:
            price = float(payload.get("price"))
            if price <= 0 or price != price or price in (float("inf"), float("-inf")):
                raise ValueError
        except (TypeError, ValueError, OverflowError):
            return QualityDecision(canonical, kind, eid, False, False, policy.authority_tier, "invalid_price", "blocked", (time.perf_counter() - started) * 1000)
    if kind in {"structure", "fed", "flow", "sentiment", "macro"} and isinstance(payload, dict):
        value = payload.get("value")
        if value is not None:
            try:
                number = float(value)
                if number != number or number in (float("inf"), float("-inf")):
                    raise ValueError
            except (TypeError, ValueError, OverflowError):
                return QualityDecision(canonical, kind, eid, False, False, policy.authority_tier, "invalid_metric_value", "blocked", (time.perf_counter() - started) * 1000)
    if kind == "news" and isinstance(payload, dict):
        title = str(payload.get("title") or payload.get("text") or payload.get("content") or "").strip()
        if len(title) < 2:
            return QualityDecision(canonical, kind, eid, False, False, policy.authority_tier, "missing_news_text", "blocked", (time.perf_counter() - started) * 1000)
    published_epoch = _parse_time(published_at)
    reference_now = float(observed_at) if observed_at is not None else time.time()
    if published_epoch is None:
        return QualityDecision(canonical, kind, eid, False, False, policy.authority_tier, "missing_or_invalid_timestamp", "blocked", (time.perf_counter() - started) * 1000)
    if published_epoch is not None and kind not in {"calendar"}:
        if published_epoch > reference_now + 300:
            return QualityDecision(canonical, kind, eid, False, False, policy.authority_tier, "future_timestamp", "blocked", (time.perf_counter() - started) * 1000)
        if kind in {"market", "structure"} and published_epoch < reference_now - 3 * 86400:
            return QualityDecision(canonical, kind, eid, False, False, policy.authority_tier, "stale_market_timestamp", "blocked", (time.perf_counter() - started) * 1000)

    if policy.key == "jin10" and not (getattr(config, "JIN10_ENABLED", False) and getattr(config, "JIN10_API_KEY", "")):
        return QualityDecision(canonical, kind, eid, False, False, policy.authority_tier, "jin10_not_authorized_or_disabled", "blocked", (time.perf_counter() - started) * 1000)

    if policy.mode == "verified":
        return QualityDecision(canonical, kind, eid, True, True, policy.authority_tier,
                               "verified_source", "verified", (time.perf_counter() - started) * 1000)

    if policy.key in _promoted_sources():
        return QualityDecision(canonical, kind, eid, True, True, policy.authority_tier,
                               "operator_promoted_after_validation", "verified", (time.perf_counter() - started) * 1000)
    return QualityDecision(canonical, kind, eid, True, False, policy.authority_tier, "candidate_observation_only", "candidate", (time.perf_counter() - started) * 1000)


def _payload_hash(payload: Any) -> str:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def observation_already_recorded(
    connection: Any,
    decision: QualityDecision,
    payload: Any,
) -> bool:
    """Return True only for the exact same governed observation.

    Candidate feeds are commonly polled every few seconds.  De-duplicating on
    source/event alone would hide a changed payload or a later promotion;
    including the payload hash and decision outcome keeps those transitions
    auditable while preventing an unchanged item from growing the audit table
    forever.
    """
    row = connection.execute(
        """SELECT 1 FROM data_quality_audit
           WHERE source=? AND kind=? AND event_id=? AND payload_hash=?
             AND accepted=? AND decision_eligible=? AND reason=?
           LIMIT 1""",
        (
            decision.source,
            decision.kind,
            decision.event_id,
            _payload_hash(payload),
            int(decision.accepted),
            int(decision.decision_eligible),
            decision.reason,
        ),
    ).fetchone()
    return row is not None


def record(
    connection: Any,
    decision: QualityDecision,
    *,
    published_at: Any = None,
    payload: Any = None,
    metadata: Optional[Dict[str, Any]] = None,
    quarantine: bool = False,
    observed_at: Optional[float] = None,
) -> None:
    """Persist an immutable audit row and (optionally) a review quarantine row."""
    observed = float(observed_at) if observed_at is not None else time.time()
    published_epoch = _parse_time(published_at)
    latency = (observed - published_epoch) * 1000 if published_epoch is not None else decision.latency_ms
    meta = dict(metadata or {})
    meta.setdefault("policy_version", "source-policy-v1")
    connection.execute(
        """INSERT INTO data_quality_audit(
               source, kind, event_id, observed_at, published_at, authority_tier,
               accepted, decision_eligible, reason, latency_ms, payload_hash, metadata
           ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            decision.source, decision.kind, decision.event_id, _iso_now(), str(published_at or ""),
            decision.authority_tier, int(decision.accepted), int(decision.decision_eligible),
            decision.reason, float(latency) if latency is not None else None,
            _payload_hash(payload), json.dumps(meta, ensure_ascii=False),
        ),
    )
    if quarantine or not decision.accepted:
        connection.execute(
            """INSERT INTO data_quarantine(
                   source, kind, event_id, observed_at, published_at, reason, payload
               ) VALUES(?,?,?,?,?,?,?)
               ON CONFLICT(source, kind, event_id) DO UPDATE SET
                   observed_at=excluded.observed_at,
                   published_at=excluded.published_at,
                   reason=excluded.reason,
                   payload=excluded.payload""",
            (
                decision.source, decision.kind, decision.event_id, _iso_now(), str(published_at or ""),
                decision.reason, json.dumps(payload if isinstance(payload, (dict, list)) else {"value": str(payload or "")}, ensure_ascii=False, default=str)[:20000],
            ),
        )


def policies_as_dict() -> list[Dict[str, Any]]:
    promoted = _promoted_sources()
    return [
        {**asdict(policy), "decision_enabled": policy.mode == "verified" or policy.key in promoted}
        for policy in POLICIES
    ]


def is_decision_eligible(connection: Any, source: str, event_id: str) -> bool:
    row = connection.execute(
        """SELECT decision_eligible FROM data_quality_audit
           WHERE source=? AND event_id=? ORDER BY id DESC LIMIT 1""",
        (canonical_source(source), str(event_id)),
    ).fetchone()
    # Absence of an audit row is not evidence.  Replay compatibility is an
    # explicit AI-worker setting, never an implicit bypass in this gate.
    return False if row is None else bool(row[0])
