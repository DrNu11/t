"""Provider contracts and canonical event shapes.

The contracts deliberately remain independent of the existing SQLite schema.
Adapters can project these records into ``raw_news``, ``market_ticks`` or a
future macro table without making the current event bus depend on a vendor.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, Iterable, List, Optional


@dataclass(frozen=True)
class NewsEvent:
    event_id: str
    published_at: str
    source: str
    title: str
    body: str = ""
    category: str = "财经快讯"
    impact_assets: List[str] = field(default_factory=list)
    importance: Optional[int] = None
    url: str = ""

    def to_legacy(self) -> Dict[str, Any]:
        """Return the shape consumed by the existing news watcher."""
        return {
            "id": self.event_id,
            "title": self.title,
            "summary": self.body,
            "url": self.url,
            "published_at": self.published_at,
            "source": self.source,
            "category": self.category,
            "impact_assets": list(self.impact_assets),
            "importance": self.importance,
        }


@dataclass(frozen=True)
class MarketQuote:
    symbol: str
    asset: str
    price: float
    source: str
    event_ts: Optional[int] = None
    change_pct: Optional[float] = None
    volume: Optional[float] = None
    bid: Optional[float] = None
    ask: Optional[float] = None
    high: Optional[float] = None
    low: Optional[float] = None
    previous_close: Optional[float] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class MacroEvent:
    event_id: str
    indicator: str
    source: str
    published_at: str = ""
    actual: Optional[float] = None
    previous: Optional[float] = None
    consensus: Optional[float] = None
    unit: str = ""
    country: str = ""
    impact: Optional[int] = None
    time_period: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class NewsProvider(ABC):
    @abstractmethod
    def fetch_news(self, **kwargs: Any) -> List[NewsEvent]:
        raise NotImplementedError


class MarketProvider(ABC):
    @abstractmethod
    def fetch_quotes(self, **kwargs: Any) -> List[MarketQuote]:
        raise NotImplementedError


class MacroProvider(ABC):
    @abstractmethod
    def fetch_macro(self, **kwargs: Any) -> List[MacroEvent]:
        raise NotImplementedError
