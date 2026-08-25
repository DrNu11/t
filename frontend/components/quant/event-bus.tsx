'use client'

import { useCallback, useEffect, useRef, useState } from 'react'
import { ChevronDown, ChevronRight, Download, Radio, Wifi, WifiOff } from 'lucide-react'
import type { ApiEvent, MarketCategory, Signal } from '@/lib/quant-data'
import { computeMFE, computeMAE, computeMFETime, computeMAETime, getImpactThreshold, calculateHeatScore } from '@/lib/signal-utils'
import { SignalBadge } from './signal-badge'
import { API_BASE } from '@/lib/api'

const SSE_URL = `${API_BASE}/events/stream`
const MAX_EVENTS = 10000
const INITIAL_HISTORY_LIMIT = 200

const EVENT_CATEGORIES: { key: MarketCategory; label: string }[] = [
  { key: 'ALL', label: '全部' },
  { key: 'CRYPTO', label: '加密' },
  { key: 'GOLD', label: '黄金' },
  { key: 'OIL', label: '原油' },
  { key: 'MACRO', label: '宏观' },
  { key: 'OTHER', label: '其他' },
]

function eventCategory(event: ApiEvent): MarketCategory {
  if (event.market_category && event.market_category !== 'OTHER' && event.market_category !== 'ALL') {
    return event.market_category
  }
  const asset = event.target_asset?.toUpperCase() ?? ''
  const text = event.news_text.toLowerCase()
  if (/BTC|ETH|SOL|CRYPTO/.test(asset) || /bitcoin|ethereum|crypto|binance|stablecoin/.test(text)) return 'CRYPTO'
  if (/XAU|GOLD/.test(asset) || /gold|bullion/.test(text)) return 'GOLD'
  if (/WTI|CL|OIL/.test(asset) || /crude|oil|opec|hormuz|energy price/.test(text)) return 'OIL'
  if (/FED|CPI|GDP|PMI|BOJ|PBOC|ECB/.test(asset) || /central bank|inflation|treasury|bond|yield|interest rate|pmi|gdp|tariff/.test(text)) return 'MACRO'
  return 'OTHER'
}

function shortTimestamp(value: string) {
  const match = value.match(/(\d{2}-\d{2})[ T](\d{2}:\d{2})/)
  return match ? `${match[1]} ${match[2]}` : value
}

type Props = {
  activeMarket?: MarketCategory
  onMarketChange?: (market: MarketCategory) => void
  onEventsUpdated?: (events: ApiEvent[]) => void
  onPriceUpdate?: (asset: string, price: number) => void
}

function VipBadge({ tag }: { tag: string }) {
  const label = tag.replace('[', '').replace(']', '')
  let color = 'border-hold/50 bg-hold/10 text-hold'
  if (tag.includes('FED')) color = 'border-primary/50 bg-primary/10 text-primary'
  if (tag.includes('MUSK')) color = 'border-primary/40 bg-primary/8 text-primary'
  return (
    <span className={'inline-flex items-center rounded border px-1.5 py-0 text-[9px] font-bold uppercase tracking-wider ' + color}>
      {label}
    </span>
  )
}

function VerdictBadge({ verdict }: { verdict: string }) {
  if (!verdict) return null
  let color = 'border-muted-foreground/30 bg-muted/20 text-muted-foreground'
  if (verdict === 'WIN') color = 'border-long/50 bg-long/10 text-long'
  if (verdict === 'LOSS') color = 'border-short/50 bg-short/10 text-short'
  return (
    <span className={'inline-flex items-center rounded border px-1.5 py-0 text-[9px] font-bold uppercase tracking-wider ' + color}>
      {verdict === 'WIN' ? 'CORRECT' : verdict === 'LOSS' ? 'WRONG' : verdict}
    </span>
  )
}

function AnalysisStatusBadge({ status }: { status: ApiEvent['analysis_status'] }) {
  const labels = {
    PENDING: '待 AI 分析',
    PROCESSING: 'AI 分析中',
    FAILED: 'AI 分析失败',
  } as const
  if (status === 'DONE') return null
  const color = status === 'FAILED'
    ? 'border-short/40 bg-short/10 text-short'
    : 'border-hold/40 bg-hold/10 text-hold'
  return (
    <span className={'inline-flex items-center rounded border px-1.5 py-0 text-[9px] font-bold tracking-wider ' + color}>
      {labels[status] ?? '待 AI 分析'}
    </span>
  )
}

function ObservationBadge({ signal }: { signal: Signal }) {
  return (
    <span
      className="inline-flex items-center rounded border border-hold/50 bg-hold/10 px-1.5 py-0.5 font-mono text-[9px] font-bold tracking-wider text-hold"
      title="来源尚未通过质量验证，仅供 AI 观察，不构成交易信号"
    >
      AI 观察 {signal}
    </span>
  )
}

function HeatBadge({ size }: { size: number }) {
  if (size < 2) return null
  const score = calculateHeatScore(size)
  let colors: string
  if (score > 80) {
    colors = 'border-short/60 bg-short/15 text-short'
  } else if (score > 50) {
    colors = 'border-hold/50 bg-hold/10 text-hold'
  } else {
    colors = 'border-muted-foreground/30 bg-muted/20 text-muted-foreground'
  }
  return (
    <span className={'inline-flex items-center gap-1 whitespace-nowrap rounded border px-1.5 py-0 text-[10px] font-bold tracking-wider ' + colors}>
      🔥 热度 {score} (关联 {size} 源)
    </span>
  )
}

function DoubaoBadge({ action, reason }: { action: string; reason: string }) {
  const disagree = action && action !== 'HOLD'
  return (
    <span
      className="inline-flex items-center gap-1 rounded border border-primary/40 bg-primary/8 px-1.5 py-0 text-[9px] font-bold uppercase tracking-wider text-primary"
      title={reason?.slice(0, 200) || 'Doubao secondary verification'}
    >
      DB {action}
      {disagree && <span className="inline-block h-1 w-1 rounded-full bg-hold" title="Model disagreement" />}
    </span>
  )
}

export function EventBus({ activeMarket = 'ALL', onMarketChange, onEventsUpdated, onPriceUpdate }: Props) {
  const [events, setEvents] = useState<ApiEvent[]>([])
  const [error, setError] = useState<string | null>(null)
  const [connected, setConnected] = useState(false)
  const [expandedIds, setExpandedIds] = useState<Set<number>>(new Set())
  const [libraryCount, setLibraryCount] = useState<number | null>(null)

  const esRef = useRef<EventSource | null>(null)
  const mountedRef = useRef(true)
  const onEventsRef = useRef(onEventsUpdated)
  onEventsRef.current = onEventsUpdated
  const onPriceRef = useRef(onPriceUpdate)
  onPriceRef.current = onPriceUpdate

  const handleSseMessage = useCallback((ev: MessageEvent) => {
    if (!mountedRef.current) return
    try {
      const parsed = JSON.parse(ev.data)
      if (parsed.type === 'price_update' && parsed.asset) {
        onPriceRef.current?.(parsed.asset, parsed.price)
        return
      }
      if (typeof parsed.id === 'number') {
        setEvents((prev) => {
          const incoming = parsed as ApiEvent
          const existingIndex = prev.findIndex((event) => event.news_id === incoming.news_id || event.id === incoming.id)
          if (existingIndex >= 0) {
            const next = [...prev]
            next[existingIndex] = incoming
            return next
          }
          const next = [incoming, ...prev]
          if (next.length > MAX_EVENTS) next.length = MAX_EVENTS
          return next
        })
      }
    } catch {
      // ignore
    }
  }, [])

  useEffect(() => {
    mountedRef.current = true
    let historyRetryTimer: ReturnType<typeof setTimeout> | null = null
    async function loadHistory() {
      let loaded = false
      try {
        const res = await fetch(`${API_BASE}/events?limit=${INITIAL_HISTORY_LIMIT}&compact=true`, {
          cache: 'no-store',
        })
        if (!res.ok) throw new Error(`HTTP ${res.status}`)
        const data: ApiEvent[] = await res.json()
        if (data.length === 0) throw new Error('empty history')
        if (mountedRef.current) {
          setEvents((current) => {
            const liveIds = new Set(current.map((event) => event.news_id))
            return [...current, ...data.filter((event) => !liveIds.has(event.news_id))]
              .sort((a, b) => b.id - a.id)
              .slice(0, MAX_EVENTS)
          })
          setError(null)
          loaded = true
        }
      } catch (err) {
        if (mountedRef.current) {
          setError(err instanceof Error ? err.message : 'fetch failed')
        }
      } finally {
        if (mountedRef.current && !loaded && historyRetryTimer === null) {
          historyRetryTimer = setTimeout(() => {
            historyRetryTimer = null
            void loadHistory()
          }, 5_000)
        }
      }
    }
    async function loadCounts() {
      try {
        const res = await fetch(`${API_BASE}/events/today`)
        if (!res.ok) return
        const data = await res.json() as { news_count?: number; total_count?: number }
        if (!mountedRef.current) return
        const total = Number(data.total_count ?? data.news_count)
        if (Number.isFinite(total)) setLibraryCount(total)
      } catch {
        // 计数接口失败时回退列表长度
      }
    }
    loadHistory()
    void loadCounts()
    const countTimer = setInterval(() => void loadCounts(), 15_000)
    const es = new EventSource(SSE_URL)
    esRef.current = es
    es.onopen = () => { if (mountedRef.current) setConnected(true) }
    es.onmessage = handleSseMessage
    es.onerror = () => { if (mountedRef.current) setConnected(false) }
    return () => {
      mountedRef.current = false
      clearInterval(countTimer)
      if (historyRetryTimer !== null) clearTimeout(historyRetryTimer)
      es.close()
      esRef.current = null
    }
  }, [handleSseMessage])

  useEffect(() => {
    onEventsRef.current?.(events)
  }, [events])

  const toggleExpand = (id: number) => {
    setExpandedIds((prev) => {
      const next = new Set(prev)
      next.has(id) ? next.delete(id) : next.add(id)
      return next
    })
  }

  const exportExcel = () => {
    window.open(`${API_BASE}/export/excel`, '_blank')
  }

  const filteredEvents = activeMarket === 'ALL'
    ? events
    : events.filter((event) => eventCategory(event) === activeMarket)

  return (
    <section className="glass flex h-full flex-col rounded-lg border border-border" aria-label="real time event bus">
      <div className="flex items-center justify-between border-b border-border px-4 py-3">
        <div className="flex items-center gap-2">
          <Radio className="h-4 w-4 text-primary" aria-hidden="true" />
          <h2 className="text-sm font-semibold text-foreground">real time event bus</h2>
        </div>
        <div className="flex items-center gap-3">
          <button
            onClick={exportExcel}
            className="inline-flex items-center gap-1.5 rounded border border-border px-2 py-1 text-[10px] font-medium text-muted-foreground hover:text-foreground hover:border-primary/50 transition-colors"
            title="Export daily report"
          >
            <Download className="h-3 w-3" />
            Report
          </button>
          {connected ? <Wifi className="h-3.5 w-3.5 text-long" /> : <WifiOff className="h-3.5 w-3.5 text-muted-foreground" />}
          <span className="font-mono text-[10px] uppercase tracking-widest text-muted-foreground">live</span>
        </div>
      </div>
      <div className="flex gap-1 overflow-x-auto border-b border-border px-3 py-2 thin-scroll" aria-label="新闻分类">
        {EVENT_CATEGORIES.map((category) => {
          const count = category.key === 'ALL'
            ? (libraryCount ?? events.length)
            : events.filter((event) => eventCategory(event) === category.key).length
          return (
            <button
              key={category.key}
              type="button"
              onClick={() => onMarketChange?.(category.key)}
              className={'inline-flex shrink-0 items-center gap-1 rounded-md px-2 py-1 text-[10px] font-medium transition-colors ' + (
                activeMarket === category.key
                  ? 'bg-primary/12 text-primary'
                  : 'text-muted-foreground hover:bg-secondary hover:text-foreground'
              )}
            >
              {category.label}
              <span className="font-mono text-[9px] opacity-60">{count}</span>
            </button>
          )
        })}
      </div>
      <ol className="thin-scroll flex-1 divide-y divide-border overflow-y-auto">
        {error && events.length === 0 && (
          <li className="px-4 py-6 text-center text-sm text-muted-foreground">connecting {error}</li>
        )}
        {!error && events.length === 0 && (
          <li className="px-4 py-6 text-center text-sm text-muted-foreground">waiting for data</li>
        )}
        {!error && events.length > 0 && filteredEvents.length === 0 && (
          <li className="px-4 py-6 text-center text-sm text-muted-foreground">该分类暂无新闻</li>
        )}
        {filteredEvents.map((item) => {
          const open = expandedIds.has(item.id)
          const isAnalyzed = item.analysis_status === 'DONE' && item.decision_id !== null
          const qualityVerified = (item.quality_status || '').toLowerCase() === 'verified'
          const hasPath = isAnalyzed && !!(item.reasoning_path && item.reasoning_path.trim())
          const hasTracking = !!(item.entry_price && item.entry_price > 0)
          const hasDoubao = !!(item.doubao_action && item.doubao_action !== 'HOLD')
          const expandable = hasPath || hasTracking || (!!(item.doubao_reasoning && item.doubao_reasoning.trim()))
          return (
            <li key={item.id} onClick={() => expandable && toggleExpand(item.id)}
              className={expandable ? 'cursor-pointer transition-colors hover:bg-secondary/30' + (open ? ' bg-secondary/20' : '') : ''}>
              <div className="px-3 py-2.5 sm:px-4">
                <div className="mb-1.5 flex min-w-0 items-center gap-1.5">
                  <span className="shrink-0 rounded bg-secondary px-1.5 py-0.5 font-mono text-[9px] text-muted-foreground">
                    {EVENT_CATEGORIES.find((category) => category.key === eventCategory(item))?.label}
                  </span>
                  <span className="truncate font-mono text-[10px] text-muted-foreground">{shortTimestamp(item.timestamp)}</span>
                  {isAnalyzed && (
                    <span className="hidden shrink-0 font-mono text-[9px] text-muted-foreground/60 xl:inline">
                      AI {shortTimestamp(item.ai_time ?? '')}
                    </span>
                  )}
                  <div className="ml-auto flex shrink-0 items-center gap-1">
                    {isAnalyzed
                      ? (qualityVerified
                        ? <SignalBadge signal={item.action} />
                        : <ObservationBadge signal={item.action} />)
                      : <AnalysisStatusBadge status={item.analysis_status} />}
                    {isAnalyzed && item.vip_tag && <VipBadge tag={item.vip_tag} />}
                    {hasDoubao && <DoubaoBadge action={item.doubao_action} reason={item.doubao_reasoning} />}
                    {item.settled === 1 && item.is_correct && <VerdictBadge verdict={item.is_correct} />}
                    {expandable && (
                      <span className="inline-flex h-4 w-4 items-center justify-center rounded text-muted-foreground/50">
                        {open ? <ChevronDown className="h-3 w-3" /> : <ChevronRight className="h-3 w-3" />}
                      </span>
                    )}
                  </div>
                </div>
                <p
                  className={'break-words font-semibold text-foreground/90 text-pretty ' + (
                    open
                      ? 'text-[13px] leading-relaxed'
                      : 'line-clamp-2 text-[13px] leading-[1.45] sm:text-sm'
                  )}
                  title={item.news_text}
                >
                  {item.news_text}
                </p>
                {(item.cluster_size ?? 1) > 1 && (
                  <div className="mt-1"><HeatBadge size={item.cluster_size ?? 1} /></div>
                )}
                <div className="mt-1.5 flex items-center justify-between gap-2">
                  {isAnalyzed ? (
                    <span className="font-mono text-[11px] tabular-nums text-muted-foreground">
                      score {item.score > 0 ? '+' : ''}{item.score.toFixed(2)}
                    </span>
                  ) : (
                    <span className="font-mono text-[11px] text-muted-foreground">{item.reason}</span>
                  )}
                  {isAnalyzed && item.reason && (
                    <p className="max-w-[70%] truncate text-[11px] leading-relaxed text-muted-foreground">AI {item.reason}</p>
                  )}
                </div>
              </div>
              {open && (
                <div className="border-t border-border/50 bg-secondary/25">
                  {hasPath && (
                    <div className="px-4 py-2.5 border-b border-border/30">
                      <div className="mb-1.5 flex items-center gap-2">
                        <span className="font-mono text-[10px] uppercase tracking-wider text-primary/80">DeepSeek V4 Flash</span>
                        {item.action && (
                          <span className={'font-mono text-[10px] font-bold uppercase tracking-wider ' + (item.action === 'BUY' ? 'text-long' : item.action === 'SELL' ? 'text-short' : 'text-muted-foreground')}>
                            {item.action}
                          </span>
                        )}
                      </div>
                      <pre className="whitespace-pre-wrap break-words font-mono text-[11px] leading-relaxed text-muted-foreground/80 selection:bg-primary/20">{item.reasoning_path}</pre>
                    </div>
                  )}
                  {item.doubao_reasoning && item.doubao_reasoning.trim() && (
                    <div className="px-4 py-2.5 border-b border-border/30">
                      <div className="mb-1.5 flex items-center gap-2">
                        <span className="font-mono text-[10px] uppercase tracking-wider text-primary/80">Doubao</span>
                        {item.doubao_action && (
                          <span className={'font-mono text-[10px] font-bold uppercase tracking-wider ' + (item.doubao_action === 'BUY' ? 'text-long' : item.doubao_action === 'SELL' ? 'text-short' : 'text-muted-foreground')}>
                            {item.doubao_action}
                          </span>
                        )}
                      </div>
                      <pre className="whitespace-pre-wrap break-words font-mono text-[11px] leading-relaxed text-muted-foreground/80 selection:bg-primary/20">{item.doubao_reasoning}</pre>
                    </div>
                  )}
                  {hasTracking && (
                    <div className="px-4 py-2.5">
                      <div className="mb-2 font-mono text-[10px] uppercase tracking-wider text-muted-foreground/60">
                        2h forward test {item.settled === 0 && '(tracking...)'}
                      </div>
                      <div className="grid grid-cols-4 gap-x-3 gap-y-1 font-mono text-[11px]">
                        <span className="text-muted-foreground/60">Entry</span>
                        <span className="text-right tabular-nums text-foreground/90">${item.entry_price?.toFixed(2) ?? '—'}</span>
                        <span className="text-muted-foreground/60">Max</span>
                        <span className="text-right tabular-nums text-long/90">${item.max_price?.toFixed(2) ?? '—'}</span>
                        <span className="text-muted-foreground/60">Exit</span>
                        <span className="text-right tabular-nums text-foreground/90">
                          {item.settled ? '$' + (item.exit_price?.toFixed(2) ?? '—') : 'pending'}
                        </span>
                        <span className="text-muted-foreground/60">Min</span>
                        <span className="text-right tabular-nums text-short/90">${item.min_price?.toFixed(2) ?? '—'}</span>
                      </div>
                      {(() => {
                        const entry = item.entry_price ?? 0
                        const max = item.max_price ?? 0
                        const min = item.min_price ?? 0
                        const mfe = entry > 0 ? computeMFE(item.action, entry, max, min) : null
                        const mae = entry > 0 ? computeMAE(item.action, entry, max, min) : null
                        const mfeTime = computeMFETime(item.action, item.entry_time, item.max_price_time, item.min_price_time)
                        const maeTime = computeMAETime(item.action, item.entry_time, item.max_price_time, item.min_price_time)
                        const threshold = getImpactThreshold(item.target_asset)
                        const isHighImpact = mfe !== null && mfe > threshold
                        return (
                          <>
                            {(mfe !== null || mae !== null) && (
                              <div className="mt-1.5 flex items-center gap-4 font-mono text-[10px]">
                                {mfe !== null && (
                                  <span>
                                    <span className="text-muted-foreground/60">最大浮盈 </span>
                                    <span className="tabular-nums text-long/90">{mfe >= 0 ? '+' : ''}{mfe.toFixed(2)}%</span>
                                    {mfeTime && <span className="tabular-nums text-muted-foreground/50"> ({mfeTime}m)</span>}
                                  </span>
                                )}
                                {mae !== null && (
                                  <span>
                                    <span className="text-muted-foreground/60">最大浮亏 </span>
                                    <span className="tabular-nums text-short/90">{mae >= 0 ? '+' : ''}{mae.toFixed(2)}%</span>
                                    {maeTime && <span className="tabular-nums text-muted-foreground/50"> ({maeTime}m)</span>}
                                  </span>
                                )}
                              </div>
                            )}
                            {isHighImpact && (
                              <div className="mt-1.5 inline-flex items-center gap-1.5 rounded border border-hold/50 bg-hold/10 px-2 py-0.5">
                                <span className="text-[10px]">🔥</span>
                                <span className="font-mono text-[9px] font-bold tracking-wider text-hold">
                                  强影响
                                </span>
                              </div>
                            )}
                          </>
                        )
                      })()}
                      {item.settled === 1 && item.is_correct && (
                        <div className="mt-2 flex items-center gap-2">
                          <span className="text-[10px] text-muted-foreground/60">Verdict</span>
                          <VerdictBadge verdict={item.is_correct} />
                        </div>
                      )}
                    </div>
                  )}
                  {!hasPath && !hasTracking && !(!!(item.doubao_reasoning && item.doubao_reasoning.trim())) && (
                    <div className="px-4 py-2.5 font-mono text-[10px] italic text-muted-foreground/40">no detail available</div>
                  )}
                </div>
              )}
            </li>
          )
        })}
      </ol>
    </section>
  )
}
