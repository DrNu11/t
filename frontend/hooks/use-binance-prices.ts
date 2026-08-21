'use client'

import { useEffect, useMemo, useState } from 'react'
import { API_BASE } from '@/lib/api'

export type PriceInfo = { price: number; change24h: number }
export type PriceMap = Record<string, PriceInfo>

type MarketResponse = {
  items: Array<{ asset: string; price: number; change24h: number; sourceCount: number; updated_at: string }>
  sources: Record<string, { status: string }>
  updated_at: string
}

const API_URL = `${API_BASE}/market/prices`

export function useBinancePrices() {
  const [prices, setPrices] = useState<PriceMap>({})
  const [connected, setConnected] = useState(false)
  const [sourceCount, setSourceCount] = useState(0)
  const [lastUpdated, setLastUpdated] = useState<string | null>(null)

  useEffect(() => {
    let active = true
    async function poll() {
      try {
        const response = await fetch(API_URL, { cache: 'no-store' })
        if (!response.ok) throw new Error(String(response.status))
        const data = await response.json() as MarketResponse
        if (!active) return
        setPrices(Object.fromEntries(data.items.map((item) => [item.asset, { price: item.price, change24h: item.change24h }])))
        setSourceCount(Object.values(data.sources).filter((source) => source.status !== 'unavailable').length)
        setLastUpdated(data.updated_at)
        setConnected(data.items.length > 0)
      } catch {
        if (active) setConnected(false)
      }
    }
    void poll()
    const timer = setInterval(() => void poll(), 3000)
    return () => { active = false; clearInterval(timer) }
  }, [])

  const pricesByPair = useMemo(() => Object.fromEntries(
    Object.entries(prices).map(([asset, info]) => [`${asset}USDT`, info]),
  ), [prices])

  return { prices, pricesByPair, connected, sourceCount, lastUpdated }
}
