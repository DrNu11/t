'use client'

import { useEffect, useState } from 'react'
import { API_BASE } from '@/lib/api'

type StructureLevel = {
  price?: number | null
}

type StructureEvent = {
  kind?: string
  direction?: string
  level?: number | null
  confirmed_at?: number | null
  provisional?: boolean
}

type TimeframeStructure = NonNullable<MarketStructureTimeframe['structure']>

export type MarketStructureTimeframe = {
  status?: string
  decision_eligible?: boolean
  bars_used?: number
  last_closed_at?: number | null
  trend_score?: number
  trend?: string
  confidence?: number
  indicators?: {
    ema20?: number | null
    ema50?: number | null
    ema200?: number | null
    rsi14?: number | null
    atr14?: number | null
    atr_pct?: number | null
  }
  structure?: {
    status?: string
    support?: StructureLevel | null
    resistance?: StructureLevel | null
    latest_bos?: StructureEvent | null
    latest_choch?: StructureEvent | null
  }
}

export type MarketStructure = {
  schema_version?: string
  as_of_ms?: number | null
  source?: string
  status?: string
  indicator_engine?: string
  decision_eligible?: boolean
  trend_score?: number
  trend?: string
  confidence?: number
  alignment?: string
  alignment_score?: number
  available_timeframes?: string[]
  timeframes?: Record<string, MarketStructureTimeframe>
  warnings?: string[]
}

type MarketStructureResponse = {
  asset: string
  status: string
  decision_eligible: boolean
  source?: string
  quote_source?: string
  quote_venue?: string
  quote_instrument_id?: string
  quote_instrument_type?: string
  venue?: string
  instrument_id?: string
  instrument_type?: string
  updated_at?: string | number | null
  reason?: string | null
  structure?: MarketStructure | null
}

type Props = {
  asset: string
}

const TIMEFRAMES = ['15m', '1h', '4h', '1d'] as const

export function MarketStructurePanel({ asset }: Props) {
  const [snapshot, setSnapshot] = useState<MarketStructureResponse | null>(null)
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    let active = true
    let inFlight = false
    let controller: AbortController | null = null

    async function load() {
      if (inFlight) return
      inFlight = true
      controller = new AbortController()
      try {
        const response = await fetch(`${API_BASE}/market/structure/${encodeURIComponent(asset)}`, {
          signal: controller.signal,
        })
        if (!response.ok) throw new Error(`HTTP ${response.status}`)
        const payload = await response.json() as MarketStructureResponse
        if (active) setSnapshot(payload)
      } catch (error) {
        if (error instanceof DOMException && error.name === 'AbortError') return
        if (active) {
          setSnapshot({
            asset,
            status: 'unavailable',
            decision_eligible: false,
            reason: error instanceof Error ? error.message : 'request_failed',
          })
        }
      } finally {
        inFlight = false
        if (active) setLoading(false)
      }
    }

    setSnapshot(null)
    setLoading(true)
    void load()
    const timer = window.setInterval(() => void load(), 60_000)
    return () => {
      active = false
      controller?.abort()
      window.clearInterval(timer)
    }
  }, [asset])

  const structure = snapshot?.structure
  const usable = Boolean(snapshot?.decision_eligible && structure?.decision_eligible)
  const source = structure?.source || snapshot?.source || '未提供'
  const instrument = snapshot?.instrument_id || asset
  const instrumentType = snapshot?.instrument_type === 'perpetual_swap' ? '永续合约' : snapshot?.instrument_type || '品种未知'

  return (
    <aside className="thin-scroll flex h-full min-h-[300px] flex-col overflow-y-auto rounded-lg border border-border bg-card p-3">
      <div className="flex items-start justify-between gap-2 border-b border-border pb-2">
        <div>
          <h2 className="font-mono text-[10px] uppercase tracking-widest text-muted-foreground">Market Structure</h2>
          <div className="mt-1 flex items-baseline gap-2">
            <span className="font-mono text-base font-bold text-foreground">{asset}</span>
            <span className="font-mono text-[10px] text-muted-foreground">闭合 K 线</span>
          </div>
        </div>
        <StatusBadge loading={loading} usable={usable} />
      </div>

      {loading && !snapshot ? (
        <div className="flex flex-1 items-center justify-center font-mono text-xs text-muted-foreground">正在读取真实 OHLCV…</div>
      ) : !usable || !structure ? (
        <div className="flex flex-1 flex-col justify-center rounded-md border border-dashed border-border bg-secondary/30 p-3">
          <div className="font-mono text-xs font-semibold text-hold">盘面结构不可用</div>
          <p className="mt-2 text-[11px] leading-5 text-muted-foreground">
            {snapshot?.reason || '未取得通过质量校验的真实闭合 K 线。'}
          </p>
          <p className="mt-2 font-mono text-[10px] leading-4 text-muted-foreground/70">
            不以代币、随机或其他品种行情替代。来源：{source}
          </p>
        </div>
      ) : (
        <>
          <div className="grid grid-cols-3 gap-2 py-3">
            <Metric label="综合趋势" value={trendLabel(structure.trend)} tone={trendTone(structure.trend)} />
            <Metric label="趋势分" value={formatSigned(structure.trend_score)} tone={scoreTone(structure.trend_score)} />
            <Metric label="置信度" value={formatPct(structure.confidence)} />
          </div>

          <div className="mb-2 flex flex-wrap items-center gap-x-2 gap-y-1 rounded border border-border bg-secondary/40 px-2 py-1.5 font-mono text-[9px] text-muted-foreground">
            <span>周期一致：{alignmentLabel(structure.alignment)} {formatPct(structure.alignment_score)}</span>
            <span>·</span>
            <span>结构 {snapshot?.venue || source} · {instrument} · {instrumentType}</span>
            {snapshot?.quote_source && snapshot.quote_source !== source && (
              <><span>·</span><span>报价 {snapshot.quote_venue || snapshot.quote_source} · {snapshot.quote_instrument_id || asset}</span></>
            )}
            <span>·</span>
            <span>{formatTime(structure.as_of_ms ?? snapshot?.updated_at)}</span>
          </div>

          <div className="space-y-2">
            {TIMEFRAMES.map((timeframe) => (
              <TimeframeRow
                key={timeframe}
                timeframe={timeframe}
                data={structure.timeframes?.[timeframe]}
              />
            ))}
          </div>

          <div className="mt-2 border-t border-border pt-2 font-mono text-[9px] leading-4 text-muted-foreground/70">
            EMA / RSI / ATR 参与综合评分；BOS / CHoCH 仅作研究参考，任何单一指标都不得执行买卖。60 秒刷新。
          </div>
          <div className="mt-2 rounded border border-hold/30 bg-hold/5 px-2 py-1.5 font-mono text-[9px] leading-4 text-hold">
            合约基差提示：若旁侧 K 线不是 {snapshot?.venue || source} {instrument} 同一合约，支撑/阻力的绝对价位不可直接对齐。
          </div>
        </>
      )}
    </aside>
  )
}

function TimeframeRow({ timeframe, data }: { timeframe: string; data?: MarketStructureTimeframe }) {
  if (!data || data.status === 'unavailable') {
    return (
      <div className="flex items-center justify-between rounded border border-border bg-secondary/20 px-2.5 py-2">
        <span className="font-mono text-xs font-semibold text-foreground">{timeframe}</span>
        <span className="font-mono text-[10px] text-muted-foreground">不可用</span>
      </div>
    )
  }

  if (data.decision_eligible !== true) {
    return (
      <div className="flex items-center justify-between rounded border border-hold/30 bg-hold/5 px-2.5 py-2">
        <span className="font-mono text-xs font-semibold text-foreground">{timeframe}</span>
        <span className="font-mono text-[10px] text-hold">质量闸门未通过，仅观察</span>
      </div>
    )
  }

  const indicators = data.indicators || {}
  const structure = data.structure || {}
  const event = latestConfirmedStructureEvent(structure)

  return (
    <div className="rounded border border-border bg-secondary/20 px-2.5 py-2">
      <div className="flex items-center gap-2">
        <span className="font-mono text-xs font-semibold text-foreground">{timeframe}</span>
        <span className={`font-mono text-[10px] ${trendTone(data.trend)}`}>{trendLabel(data.trend)}</span>
        <span className={`ml-auto font-mono text-[10px] ${scoreTone(data.trend_score)}`}>{formatSigned(data.trend_score)}</span>
        <span className="font-mono text-[9px] text-muted-foreground">置信 {formatPct(data.confidence)}</span>
      </div>
      <div className="mt-1.5 grid grid-cols-2 gap-x-3 gap-y-1 font-mono text-[9px] text-muted-foreground">
        <span>RSI {formatNumber(indicators.rsi14, 1)}</span>
        <span>ATR {formatNumber(indicators.atr_pct, 2)}%</span>
        <span className="col-span-2 truncate" title={emaText(indicators)}>{emaText(indicators)}</span>
        <span>S {formatPrice(structure.support?.price)}</span>
        <span>R {formatPrice(structure.resistance?.price)}</span>
      </div>
      <div className="mt-1.5 flex items-center justify-between font-mono text-[9px] text-muted-foreground/80">
        <span>{event ? `${event.kind || '结构'} ${directionLabel(event.direction)} @ ${formatPrice(event.level)} · ${formatTime(event.confirmed_at)}` : 'BOS / CHoCH：尚未确认'}</span>
        <span>{data.bars_used ?? 0} bars</span>
      </div>
    </div>
  )
}

function StatusBadge({ loading, usable }: { loading: boolean; usable: boolean }) {
  const label = loading ? '刷新中' : usable ? '可参与综合评分' : '不可用'
  const tone = loading
    ? 'border-primary/40 bg-primary/10 text-primary'
    : usable
      ? 'border-long/40 bg-long/10 text-long'
      : 'border-hold/40 bg-hold/10 text-hold'
  return <span role="status" aria-live="polite" className={`rounded border px-2 py-1 font-mono text-[10px] ${tone}`}>{label}</span>
}

function Metric({ label, value, tone = 'text-foreground' }: { label: string; value: string; tone?: string }) {
  return (
    <div className="rounded border border-border bg-secondary/30 px-2 py-1.5">
      <div className="font-mono text-[9px] text-muted-foreground">{label}</div>
      <div className={`mt-0.5 truncate font-mono text-xs font-semibold tabular-nums ${tone}`}>{value}</div>
    </div>
  )
}

function trendLabel(value?: string) {
  if (value === 'bullish') return '多头'
  if (value === 'bearish') return '空头'
  if (value === 'range') return '震荡'
  return '未知'
}

function alignmentLabel(value?: string) {
  if (value === 'bullish') return '多头一致'
  if (value === 'bearish') return '空头一致'
  if (value === 'mixed') return '周期冲突'
  if (value === 'range') return '震荡一致'
  return '未知'
}

function directionLabel(value?: string) {
  return value === 'bullish' ? '向上' : value === 'bearish' ? '向下' : '未知'
}

function trendTone(value?: string) {
  return value === 'bullish' ? 'text-long' : value === 'bearish' ? 'text-short' : value === 'range' ? 'text-hold' : 'text-muted-foreground'
}

function scoreTone(value?: number) {
  return typeof value !== 'number' ? 'text-muted-foreground' : value > 0.05 ? 'text-long' : value < -0.05 ? 'text-short' : 'text-hold'
}

function formatSigned(value?: number) {
  return typeof value === 'number' && Number.isFinite(value) ? `${value >= 0 ? '+' : ''}${value.toFixed(3)}` : '—'
}

function formatPct(value?: number) {
  return typeof value === 'number' && Number.isFinite(value) ? `${(value * 100).toFixed(0)}%` : '—'
}

function formatNumber(value?: number | null, digits = 2) {
  return typeof value === 'number' && Number.isFinite(value) ? value.toFixed(digits) : '—'
}

function formatPrice(value?: number | null) {
  if (typeof value !== 'number' || !Number.isFinite(value)) return '—'
  return value.toLocaleString('en-US', { maximumFractionDigits: Math.abs(value) >= 100 ? 2 : 5 })
}

function emaText(indicators: NonNullable<MarketStructureTimeframe['indicators']>) {
  return `EMA 20/50/200 ${formatPrice(indicators.ema20)} / ${formatPrice(indicators.ema50)} / ${formatPrice(indicators.ema200)}`
}

function formatTime(value?: string | number | null) {
  if (value == null) return '时间未知'
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return String(value)
  return date.toLocaleString('zh-CN', { hour12: false, month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' })
}

export function latestConfirmedStructureEvent(structure?: TimeframeStructure): StructureEvent | null {
  if (!structure) return null
  const candidates = [structure.latest_bos, structure.latest_choch].filter(
    (event): event is StructureEvent => Boolean(event && event.provisional !== true),
  )
  if (candidates.length === 0) return null
  return candidates.reduce((latest, event) => (
    (event.confirmed_at ?? 0) > (latest.confirmed_at ?? 0) ? event : latest
  ))
}
