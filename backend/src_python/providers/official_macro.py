"""Free official U.S. macro-release calendars.

There is no single free, authoritative replacement for a commercial economic
calendar.  This adapter combines the original publishers instead:

* BLS release schedule (CPI, Employment Situation/NFP and other releases)
* BEA release schedule (GDP, Personal Income and Outlays/PCE, trade)
* Federal Reserve FOMC meeting calendar
* U.S. Census economic-indicator release calendar

The pages are parsed conservatively.  A source failure produces no fabricated
events and is reported in ``health``.  Published values still need the
publisher's actual release endpoint; this adapter is primarily a schedule.
"""

from __future__ import annotations

from datetime import datetime, timezone
from html import unescape
from html.parser import HTMLParser
import hashlib
import re
from typing import Any, Dict, Iterable, List, Optional, Tuple
from zoneinfo import ZoneInfo

import requests

from .contracts import MacroEvent, MacroProvider


_EASTERN = ZoneInfo("America/New_York")
_UTC = timezone.utc
_MONTHS = {
    name.lower(): index
    for index, name in enumerate(
        ("January", "February", "March", "April", "May", "June",
         "July", "August", "September", "October", "November", "December"),
        start=1,
    )
}
_MONTH_PATTERN = "|".join(_MONTHS)
_DATE_RE = re.compile(
    rf"\b(?P<month>{_MONTH_PATTERN})\s+(?P<day>\d{{1,2}})(?:,\s*(?P<year>\d{{4}}))?",
    re.IGNORECASE,
)
_NUMERIC_DATE_RE = re.compile(r"\b(?P<month>\d{1,2})/(?P<day>\d{1,2})/(?P<year>\d{4})\b")
_TIME_RE = re.compile(r"\b(?P<hour>\d{1,2}):(?P<minute>\d{2})\s*(?P<ampm>a\.?m\.?|p\.?m\.?|AM|PM)?\b", re.IGNORECASE)
_YEAR_RE = re.compile(r"\bYear\s+(?P<year>20\d{2})\b", re.IGNORECASE)


class _TableParser(HTMLParser):
    """Small dependency-free parser for the simple publisher tables."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: List[List[str]] = []
        self._row: Optional[List[str]] = None
        self._cell: Optional[List[str]] = None

    def handle_starttag(self, tag: str, attrs: List[Tuple[str, Optional[str]]]) -> None:
        tag = tag.lower()
        if tag == "tr":
            if self._row is not None:
                self._finish_row()
            self._row = []
        elif tag in {"td", "th"} and self._row is not None:
            self._cell = []

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in {"td", "th"} and self._cell is not None and self._row is not None:
            self._row.append(" ".join("".join(self._cell).split()))
            self._cell = None
        elif tag == "tr" and self._row is not None:
            self._finish_row()

    def handle_data(self, data: str) -> None:
        if self._cell is not None:
            self._cell.append(data)

    def _finish_row(self) -> None:
        if self._row is not None:
            cleaned = [cell.strip() for cell in self._row]
            if any(cleaned):
                self.rows.append(cleaned)
        self._row = None
        self._cell = None


def _table_rows(body: str) -> List[List[str]]:
    parser = _TableParser()
    parser.feed(body)
    parser.close()
    return parser.rows


def _year_from(body: str, fallback: int) -> int:
    text = unescape(body)
    match = _YEAR_RE.search(text)
    if match is None:
        match = re.search(r"\b(?P<year>20\d{2})\s+FOMC Meetings\b", text, re.IGNORECASE)
    if not match:
        return fallback
    try:
        return int(match.group("year"))
    except ValueError:
        return fallback


def _normalise_ampm(value: str) -> str:
    return re.sub(r"\.(?=[am]\.)", "", value, flags=re.IGNORECASE).replace(".", "").upper()


def _scheduled_timestamp(value: str, default_year: int, *, timezone_name: str = "America/New_York") -> Optional[str]:
    """Parse publisher date/time text into an explicit UTC timestamp."""
    text = " ".join(unescape(str(value or "")).split())
    date_match = _DATE_RE.search(text)
    numeric_match = _NUMERIC_DATE_RE.search(text)
    if date_match:
        month = _MONTHS[date_match.group("month").lower()]
        day = int(date_match.group("day"))
        year = int(date_match.group("year") or default_year)
    elif numeric_match:
        month = int(numeric_match.group("month"))
        day = int(numeric_match.group("day"))
        year = int(numeric_match.group("year"))
    else:
        return None
    time_match = _TIME_RE.search(text)
    hour = int(time_match.group("hour")) if time_match else 0
    minute = int(time_match.group("minute")) if time_match else 0
    ampm = _normalise_ampm(time_match.group("ampm") or "") if time_match else ""
    if ampm == "PM" and hour < 12:
        hour += 12
    elif ampm == "AM" and hour == 12:
        hour = 0
    try:
        local = datetime(year, month, day, hour, minute, tzinfo=ZoneInfo(timezone_name))
    except (TypeError, ValueError):
        return None
    return local.astimezone(_UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _boundary(value: str) -> Optional[datetime]:
    if not value:
        return None
    text = str(value).strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=_UTC)
    return parsed.astimezone(_UTC)


def _in_window(timestamp: str, start: str, end: str) -> bool:
    try:
        event_time = datetime.fromisoformat(timestamp.replace("Z", "+00:00")).astimezone(_UTC)
    except (TypeError, ValueError):
        return False
    lower = _boundary(start)
    upper = _boundary(end)
    return not (lower and event_time < lower) and not (upper and event_time > upper)


def _importance(indicator: str) -> int:
    text = indicator.lower()
    high = (
        "cpi", "consumer price", "employment situation", "nonfarm", "payroll",
        "gdp", "personal income", "outlays", "pce", "fomc", "federal funds",
        "interest rate", "retail sales", "durable goods", "housing starts",
    )
    return 5 if any(token in text for token in high) else 3


def _event_id(source: str, timestamp: str, indicator: str) -> str:
    raw = f"{source}|{timestamp}|{indicator}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:24]


def _event(
    *,
    source: str,
    timestamp: Optional[str],
    indicator: str,
    source_url: str,
    time_period: str = "",
) -> Optional[MacroEvent]:
    indicator = " ".join(str(indicator or "").split())
    if not timestamp or len(indicator) < 2:
        return None
    return MacroEvent(
        event_id=_event_id(source, timestamp, indicator),
        indicator=indicator,
        source=source,
        published_at=timestamp,
        country="US",
        impact=_importance(indicator),
        time_period=" ".join(str(time_period or "").split()),
        source_url=source_url,
    )


def _parse_bea(body: str, url: str, start: str, end: str) -> List[MacroEvent]:
    year = _year_from(body, datetime.now(_UTC).year)
    events: List[MacroEvent] = []
    for row in _table_rows(body):
        if len(row) < 2:
            continue
        timestamp = _scheduled_timestamp(row[0], year)
        indicator = row[-1]
        event = _event(source="bea", timestamp=timestamp, indicator=indicator, source_url=url)
        if event and _in_window(event.published_at, start, end):
            events.append(event)
    return events


def _parse_census(body: str, url: str, start: str, end: str) -> List[MacroEvent]:
    year = _year_from(body, datetime.now(_UTC).year)
    events: List[MacroEvent] = []
    for row in _table_rows(body):
        if len(row) < 3:
            continue
        timestamp = _scheduled_timestamp(f"{row[1]} {row[2]}", year)
        indicator = row[0]
        period = row[3] if len(row) > 3 else ""
        event = _event(
            source="census", timestamp=timestamp, indicator=indicator,
            source_url=url, time_period=period,
        )
        if event and _in_window(event.published_at, start, end):
            events.append(event)
    return events


def _looks_like_date(value: str) -> bool:
    return bool(_DATE_RE.search(value) or _NUMERIC_DATE_RE.search(value))


def _parse_bls(body: str, url: str, start: str, end: str) -> List[MacroEvent]:
    year = _year_from(body, datetime.now(_UTC).year)
    events: List[MacroEvent] = []
    for row in _table_rows(body):
        if len(row) < 2:
            continue
        timestamp = None
        timestamp_index = -1
        for index, cell in enumerate(row):
            if _looks_like_date(cell):
                timestamp = _scheduled_timestamp(" ".join(row[index:]), year)
                timestamp_index = index
                break
        if not timestamp:
            continue
        candidates = [cell for index, cell in enumerate(row) if index != timestamp_index and len(cell) > 2]
        indicator = max(candidates, key=len, default="")
        if indicator.lower() in {"release", "date", "time", "release date"}:
            continue
        event = _event(source="bls", timestamp=timestamp, indicator=indicator, source_url=url)
        if event and _in_window(event.published_at, start, end):
            events.append(event)
    return events


def _parse_fed(body: str, url: str, start: str, end: str) -> List[MacroEvent]:
    events: List[MacroEvent] = []
    heading_matches = list(re.finditer(r"<h4>\s*<a[^>]*>(20\d{2}) FOMC Meetings</a>\s*</h4>", body, re.IGNORECASE))
    for index, heading in enumerate(heading_matches):
        year = int(heading.group(1))
        block_end = heading_matches[index + 1].start() if index + 1 < len(heading_matches) else len(body)
        block = body[heading.end():block_end]
        pattern = re.compile(
            r"fomc-meeting__month[^>]*>.*?<strong>\s*([A-Za-z]+)\s*</strong>.*?"
            r"fomc-meeting__date[^>]*>\s*([^<]+)", re.IGNORECASE | re.DOTALL,
        )
        for match in pattern.finditer(block):
            month = match.group(1).strip()
            date_range = match.group(2).strip()
            first_day = re.search(r"\d{1,2}", date_range)
            if not first_day:
                continue
            timestamp = _scheduled_timestamp(f"{month} {first_day.group(0)}, {year}", year)
            event = _event(
                source="federal_reserve",
                timestamp=timestamp,
                indicator="FOMC meeting",
                source_url=url,
                time_period=f"{month} {date_range}",
            )
            if event and _in_window(event.published_at, start, end):
                events.append(event)
    return events


class OfficialMacroCalendarProvider(MacroProvider):
    """Combine free official publisher schedules without credentials."""

    source = "official_macro"

    def __init__(
        self,
        *,
        enabled: bool = False,
        bls_url: str = "https://www.bls.gov/schedule/{year}/{month:02d}_sched_list.htm",
        bea_url: str = "https://www.bea.gov/news/schedule",
        fed_url: str = "https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm",
        census_url: str = "https://www.census.gov/economic-indicators/calendar-listview.html",
        timeout: float = 8.0,
    ) -> None:
        self.enabled = bool(enabled)
        self.urls = {
            "bls": str(bls_url or "").strip(),
            "bea": str(bea_url or "").strip(),
            "federal_reserve": str(fed_url or "").strip(),
            "census": str(census_url or "").strip(),
        }
        self.timeout = max(1.0, float(timeout))
        self.last_status: Dict[str, Dict[str, Any]] = {}

    @property
    def configured(self) -> bool:
        return bool(self.enabled and any(self.urls.values()))

    def _get(self, url: str) -> str:
        response = requests.get(
            url,
            headers={"Accept": "text/html,application/xhtml+xml", "User-Agent": "TridentAgentMVP/1.0"},
            timeout=self.timeout,
        )
        response.raise_for_status()
        return response.text

    def fetch_macro(self, *, start: str = "", end: str = "", **_: Any) -> List[MacroEvent]:
        if not self.configured:
            self.last_status = {key: {"status": "disabled", "url": url} for key, url in self.urls.items()}
            return []
        current_year = datetime.now(_UTC).year
        parsers = {
            "bls": _parse_bls,
            "bea": _parse_bea,
            "federal_reserve": _parse_fed,
            "census": _parse_census,
        }
        events: List[MacroEvent] = []
        statuses: Dict[str, Dict[str, Any]] = {}
        for source, template in self.urls.items():
            if not template:
                statuses[source] = {"status": "disabled", "url": template}
                continue
            # BLS now publishes its parseable release list by month.  Keep
            # ``{year}`` compatible with older operator overrides and expose
            # ``{month}`` for the current-month list endpoint.
            url = template.format(year=current_year, month=datetime.now(_UTC).month)
            try:
                body = self._get(url)
                parsed = parsers[source](body, url, start, end)
                events.extend(parsed)
                statuses[source] = {"status": "ok", "url": url, "fetched": len(parsed)}
            except Exception as exc:
                statuses[source] = {
                    "status": "unavailable",
                    "url": url,
                    "fetched": 0,
                    "reason": f"{type(exc).__name__}: {str(exc)[:160]}",
                }
        self.last_status = statuses
        # A source page may contain duplicated table rows on responsive layouts;
        # retain one deterministic record per source/time/indicator.
        unique: Dict[str, MacroEvent] = {}
        for event in events:
            unique[f"{event.source}:{event.published_at}:{event.indicator}"] = event
        return sorted(unique.values(), key=lambda item: (item.published_at, item.source, item.indicator))

    def health(self) -> Dict[str, Any]:
        return {
            "provider": self.source,
            "enabled": self.enabled,
            "configured": self.configured,
            "source_type": "official_publisher_calendar",
            "decision_eligible": True,
            "sources": self.last_status or {
                key: {"status": "not_checked", "url": url} for key, url in self.urls.items()
            },
            "note": "free official schedules; actual values must come from the original release endpoint",
        }


_PROVIDER: OfficialMacroCalendarProvider | None = None


def get_official_macro_calendar_provider() -> OfficialMacroCalendarProvider:
    global _PROVIDER
    if _PROVIDER is None:
        import config

        _PROVIDER = OfficialMacroCalendarProvider(
            enabled=getattr(config, "OFFICIAL_MACRO_CALENDAR_ENABLED", False),
            bls_url=getattr(config, "OFFICIAL_BLS_SCHEDULE_URL", ""),
            bea_url=getattr(config, "OFFICIAL_BEA_SCHEDULE_URL", ""),
            fed_url=getattr(config, "OFFICIAL_FED_FOMC_URL", ""),
            census_url=getattr(config, "OFFICIAL_CENSUS_SCHEDULE_URL", ""),
            timeout=getattr(config, "OFFICIAL_MACRO_REQUEST_TIMEOUT", 8.0),
        )
    return _PROVIDER
