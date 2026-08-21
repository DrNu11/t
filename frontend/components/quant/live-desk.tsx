'use client'

import { useCallback, useEffect, useMemo, useState } from 'react'
import type { ApiEvent, MarketCategory } from '@/lib/quant-data'
import type { PriceMap } from '@/hooks/use-binance-prices'
import { useBinancePrices } from '@/hooks/use-binance-prices'
import { EventBus } from '@/components/quant/event-bus'
import { AgentChat } from '@/components/quant/agent-chat'
import { TradingViewChart } from '@/components/quant/tradingview-chart'
import { SignalLedger } from '@/components/quant/signal-ledger'
import { StatCards } from '@/components/quant/stat-cards'
import { MarketTabs } from '@/components/quant/market-tabs'
import { PriceTicker } from '@/components/quant/price-ticker'

export function LiveDesk() {
  const [events, setEvents] = useState<ApiEvent[]>([])
  const [activeMarket, setActiveMarket] = useState<MarketCategory>('ALL')
  const [entryPrices, setEntryPrices] = useState<Record<number, number>>({})
  const [binanceConnected, setBinanceConnected] = useState(false)
  const [xauPrice, setXauPrice] = useState<{ price: number; change24h: number } | null>(null)
  const [wtiPrice, setWtiPrice] = useState<{ price: number; change24h: number } | null>(null)
  const { prices, pricesByPair: binancePairs, connected: marketConnected } = useBinancePrices()

  const pricesByPair: PriceMap = useMemo(() => {
    const merged = { ...binancePairs }
    if (xauPrice) {
      merged['XAUUSDT'] = xauPrice
      merged['GOLDUSDT'] = xauPrice
    }
    if (wtiPrice) merged['WTIUSDT'] = wtiPrice
    return merged
  }, [binancePairs, xauPrice, wtiPrice])

  const pricesForTicker = useMemo(() => {
    const merged = { ...prices }
    if (xauPrice) merged['XAU'] = xauPrice
    if (wtiPrice) merged['WTI'] = wtiPrice
    return merged
  }, [prices, xauPrice, wtiPrice])

  const handlePriceUpdate = useCallback((asset: string, price: number) => {
    if (asset === 'XAU') setXauPrice({ price, change24h: 0 })
    else if (asset === 'WTI') setWtiPrice({ price, change24h: 0 })
  }, [])

  useEffect(() => {
    setBinanceConnected(marketConnected || xauPrice !== null || wtiPrice !== null)
  }, [marketConnected, xauPrice, wtiPrice])

  useEffect(() => {
    if (Object.keys(pricesByPair).length === 0) return
    setEntryPrices((prev) => {
      const next = { ...prev }
      let changed = false
      for (const ev of events) {
        if (ev.id in next) continue
        const asset = ev.target_asset && ev.target_asset !== 'NONE' ? ev.target_asset : null
        if (!asset) continue
        const info = pricesByPair[`${asset}USDT`]
        if (info?.price) {
          next[ev.id] = info.price
          changed = true
        }
      }
      return changed ? next : prev
    })
  }, [events, pricesByPair])

  return (
    <main className="mx-auto grid max-w-[1600px] grid-cols-1 gap-4 p-4 md:p-6 lg:h-[calc(100vh-9rem)] lg:grid-cols-[minmax(320px,380px)_1fr]">
      <div className="order-2 min-h-0 overflow-hidden lg:order-1">
        <EventBus
          activeMarket={activeMarket}
          onMarketChange={setActiveMarket}
          onEventsUpdated={setEvents}
          onPriceUpdate={handlePriceUpdate}
        />
      </div>
      <div className="order-1 flex min-h-0 flex-col gap-4 overflow-hidden lg:order-2">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <MarketTabs active={activeMarket} onChange={setActiveMarket} />
          <div className="flex items-center gap-2">
            <PriceTicker prices={pricesForTicker} connected={binanceConnected} activeMarket={activeMarket} />
            <span className="font-mono text-[10px] text-muted-foreground">{events.length} 条事件</span>
          </div>
        </div>
        <StatCards events={events} activeMarket={activeMarket} />
        {activeMarket === 'CRYPTO' || activeMarket === 'GOLD' || activeMarket === 'OIL' ? (
          <div key={activeMarket} className="h-[350px] shrink-0 md:h-[420px]">
            <TradingViewChart
              symbol={
                activeMarket === 'CRYPTO' ? 'BINANCE:BTCUSDT'
                  : activeMarket === 'GOLD' ? 'OANDA:XAUUSD'
                    : 'OANDA:WTICOUSD'
              }
              interval="60"
            />
          </div>
        ) : (
          <div key={activeMarket} className="min-h-0 flex-1">
            <AgentChat events={events} activeMarket={activeMarket} />
          </div>
        )}
        {activeMarket === 'CRYPTO' || activeMarket === 'GOLD' || activeMarket === 'OIL' ? (
          <SignalLedger
            events={events}
            activeMarket={activeMarket}
            livePrices={pricesByPair}
            entryPrices={entryPrices}
          />
        ) : null}
      </div>
    </main>
  )
}
