"""External data providers used by the Trident MVP data seam."""

from .contracts import MacroEvent, MacroProvider, MarketProvider, MarketQuote, NewsEvent, NewsProvider
from .binance_paxg import BinancePaxgProvider, get_binance_paxg_provider
from .coingecko_paxg import CoinGeckoPaxgProvider, get_coingecko_paxg_provider
from .oanda import OandaProvider, get_oanda_provider
from .official_macro import OfficialMacroCalendarProvider, get_official_macro_calendar_provider
from .trading_economics import TradingEconomicsProvider, get_trading_economics_provider

__all__ = [
    "MacroEvent",
    "MacroProvider",
    "MarketProvider",
    "MarketQuote",
    "NewsEvent",
    "NewsProvider",
    "BinancePaxgProvider",
    "CoinGeckoPaxgProvider",
    "OfficialMacroCalendarProvider",
    "OandaProvider",
    "TradingEconomicsProvider",
    "get_binance_paxg_provider",
    "get_coingecko_paxg_provider",
    "get_official_macro_calendar_provider",
    "get_oanda_provider",
    "get_trading_economics_provider",
]
