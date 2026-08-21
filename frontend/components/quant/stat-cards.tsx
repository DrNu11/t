'use client'

import { useEffect, useState } from 'react'
import { Activity, Gauge, Zap } from 'lucide-react'
import { cn } from '@/lib/utils'
import { API_BASE } from '@/lib/api'
import type { ApiEvent, MarketCategory } from '@/lib/quant-data'

type Props = {
  events: ApiEvent[]
  activeMarket: MarketCategory
}

type TodayStats = {
  news_count: number
  analyzed_count: number
  total_count?: number
}

export function StatCards({ events, activeMarket }: Props) {
  const [today, setToday] = useState<TodayStats | null>(null)
  const filtered = activeMarket === 'ALL'
    ? events
    : events.filter((e) => e.market_category === activeMarket)

  const sample = filtered.length
  const rawAvg = sample > 0
    ? filtered.reduce((s, e) => s + e.score, 0) / sample
    : 0
  const buyPct = sample > 0
    ? Math.round(filtered.filter((e) => e.action === 'BUY').length / sample * 100)
    : 0
  const newsCount = today?.news_count ?? sample
  const analyzedCount = today?.analyzed_count ?? filtered.filter((e) => e.analysis_status === 'DONE').length

  const labelMap: Record<string, string> = {
    ALL: '全市场',
    CRYPTO: '加密货币',
    GOLD: '黄金',
    OIL: '原油',
    MACRO: '宏观',
    OTHER: '其他',
  }

  useEffect(() => {
    let cancelled = false
    async function loadToday() {
      try {
        const res = await fetch(`${API_BASE}/events/today`)
        if (!res.ok) return
        const data = await res.json() as TodayStats
        if (!cancelled) setToday(data)
      } catch {
        // 计数接口失败时回退到当前列表长度
      }
    }
    void loadToday()
    const timer = setInterval(() => void loadToday(), 15_000)
    return () => {
      cancelled = true
      clearInterval(timer)
    }
  }, [])

  // Thresholds tuned for LLM raw scores in -1.0 ~ +1.0 range (typical: -0.5 ~ +0.5)
  const tone = rawAvg >= 0.2 ? 'text-long' : rawAvg <= -0.2 ? 'text-short' : 'text-primary'
  const mood = rawAvg >= 0.25 ? '看涨' : rawAvg <= -0.25 ? '看跌' : '中性'
  const moodSub = rawAvg >= 0.25 ? '偏多信号' : rawAvg <= -0.25 ? '偏空信号' : '信号分散'

  return (
    <div className="grid grid-cols-1 gap-3 sm:grid-cols-3">
      <div className="glass rounded-lg border border-border px-4 py-3">
        <div className="flex items-center justify-between">
          <span className="text-xs uppercase tracking-wider text-muted-foreground">
            今日信号
          </span>
          <Activity className="h-4 w-4 text-long" aria-hidden="true" />
        </div>
        <p className={cn('mt-2 font-mono text-2xl font-bold tabular-nums', 'text-long')}>
          {newsCount.toLocaleString()}
        </p>
        <p className="mt-0.5 font-mono text-[11px] text-muted-foreground">
          {labelMap[activeMarket] ?? activeMarket} · 今日 {newsCount} · 库内 {today?.total_count ?? newsCount} · 已分析 {analyzedCount} · 看多 {buyPct}%
        </p>
      </div>

      <div className="glass rounded-lg border border-border px-4 py-3">
        <div className="flex items-center justify-between">
          <span className="text-xs uppercase tracking-wider text-muted-foreground">
            市场情绪
          </span>
          <Gauge className={cn('h-4 w-4', tone)} aria-hidden="true" />
        </div>
        <p className={cn('mt-2 font-mono text-2xl font-bold tabular-nums', tone)}>
          {mood} {rawAvg > 0 ? '+' : ''}{rawAvg.toFixed(2)}
        </p>
        <p className="mt-0.5 font-mono text-[11px] text-muted-foreground">
          {moodSub}
        </p>
      </div>

      <div className="glass rounded-lg border border-border px-4 py-3">
        <div className="flex items-center justify-between">
          <span className="text-xs uppercase tracking-wider text-muted-foreground">
            AI 延迟
          </span>
          <Zap className="h-4 w-4 text-primary" aria-hidden="true" />
        </div>
        <p className="mt-2 font-mono text-2xl font-bold tabular-nums text-primary">
          0.8s
        </p>
        <p className="mt-0.5 font-mono text-[11px] text-muted-foreground">
          p95 · 1.2s
        </p>
      </div>
    </div>
  )
}
