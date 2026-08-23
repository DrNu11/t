'use client'

import { useMemo } from 'react'
import type { SimSignal } from '@/lib/replay-data'
import {
  actionColor,
  fmtPct,
  fmtPrice,
  fmtTime,
  verdictColor,
} from '@/lib/replay-data'
import {
  latestConfirmedStructureEvent,
  type MarketStructure,
  type MarketStructureTimeframe,
} from '@/components/quant/market-structure-panel'

type Props = {
  signal: SimSignal | null
}

type ConsensusModel = {
  action?: string
  score?: number
  reasoning?: string
}

type HistoricalStructureContext = {
  asset: string
  source: string
  venue: string
  instrumentId: string
  instrumentType: string
  quoteSource: string
  quoteVenue: string
  quoteInstrumentId: string
  contextReason: string
  decisionEligible: boolean
  structure: MarketStructure
}

export function SignalDetail({ signal }: Props) {
  const consensus = useMemo<Record<string, ConsensusModel>>(() => {
    const c = signal?.extra_models_consensus
    if (!c) return {}
    if (typeof c === 'string') {
      try { return JSON.parse(c) } catch { return {} }
    }
    return c as Record<string, ConsensusModel>
  }, [signal?.extra_models_consensus])
  const historicalStructure = useMemo(
    () => extractHistoricalStructure(signal),
    [signal],
  )

  if (!signal) {
    return (
      <div className="flex h-full items-center justify-center rounded-lg border border-dashed border-border bg-card">
        <span className="font-mono text-xs text-muted-foreground">选择一条信号查看完整推理链</span>
      </div>
    )
  }

  const paperLabel = signal.decision_eligible
    ? `模拟盘 · ${signal.settled ? '已结算' : signal.tracking_quality === 'TRACKABLE' ? '实时跟踪' : '历史不可跟踪'}`
    : `研究隔离 · ${signal.settled ? '已归档' : '不计入持仓'}`

  return (
    <div className="thin-scroll flex h-full min-h-0 flex-col gap-3 overflow-y-auto rounded-md border border-border bg-card p-3">
      {/* Header */}
      <div className="flex flex-wrap items-center gap-2">
        <span className="font-mono text-[10px] text-muted-foreground">#{signal.id}</span>
        <span className="font-mono text-base font-bold text-foreground">{signal.asset}</span>
        <span className={`rounded border px-2 py-0.5 font-mono text-xs font-semibold ${actionColor(signal.action)}`}>
          {signal.action}
        </span>
        <span className={`rounded border px-2 py-0.5 font-mono text-xs font-semibold ${verdictColor(signal.is_correct)}`}>
          {signal.is_correct || 'PENDING'}
        </span>
        <span className={`ml-auto rounded border px-2 py-0.5 font-mono text-[10px] ${signal.decision_eligible ? 'border-hold/50 bg-hold/10 text-hold' : 'border-border bg-secondary text-muted-foreground'}`}>
          {paperLabel}
        </span>
      </div>

      {/* News context */}
      <Section title="触发新闻">
        <div className="font-mono text-[11px] leading-relaxed text-foreground">
          {signal.news_text || '(无原文)'}
        </div>
        <div className="mt-1 flex items-center gap-2 font-mono text-[10px] text-muted-foreground">
          <span>来源：{signal.source || '—'}</span>
          <span>·</span>
          <span>{fmtTime(signal.news_time)}</span>
          <span>·</span>
          <span>score {(signal.score ?? 0).toFixed(2)}</span>
          {signal.event_strength && (
            <>
              <span>·</span>
              <span>强度 {signal.event_strength}</span>
            </>
          )}
          {signal.market_category && (
            <>
              <span>·</span>
              <span>{signal.market_category}</span>
            </>
          )}
        </div>
      </Section>

      {/* Price & PnL */}
      <Section title="价格 · 盈亏">
        <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
          <KV label="入场价" value={fmtPrice(signal.entry_price, signal.asset)} />
          <KV label={signal.settled ? '出场价' : '当前价'} value={fmtPrice(signal.settled ? signal.exit_price : signal.current_price, signal.asset)} />
          <KV label="期间最高" value={fmtPrice(signal.max_price, signal.asset)} />
          <KV label="期间最低" value={fmtPrice(signal.min_price, signal.asset)} />
          <KV label="PnL USDT" value={signal.current_pnl_usdt == null ? '—' : `${signal.current_pnl_usdt >= 0 ? '+' : ''}${signal.current_pnl_usdt.toFixed(2)}`} tone={(signal.current_pnl_usdt ?? 0) >= 0 ? 'good' : 'bad'} />
          <KV label="退出原因" value={signal.exit_reason || (signal.settled ? '已结算' : '跟踪中')} />
          <KV
            label={signal.settled ? '结算 PnL' : '实时 PnL'}
            value={
              <span className={((signal.settled ? signal.forward_pnl : signal.current_pnl_pct) || 0) >= 0 ? 'text-long' : 'text-short'}>
                {fmtPct(signal.settled ? signal.forward_pnl : signal.current_pnl_pct)}
              </span>
            }
          />
          <KV label="MFE" value={fmtPct(signal.settled ? signal.mfe_pct : signal.live_mfe_pct)} tone={((signal.settled ? signal.mfe_pct : signal.live_mfe_pct) || 0) > 0 ? 'good' : 'neutral'} />
          <KV label="MAE" value={fmtPct(signal.settled ? signal.mae_pct : signal.live_mae_pct)} tone={((signal.settled ? signal.mae_pct : signal.live_mae_pct) || 0) > 0 ? 'bad' : 'neutral'} />
          <KV
            label="MFE 用时"
            value={
              signal.mfe_time_mins != null
                ? `${signal.mfe_time_mins.toFixed(0)} min`
                : '—'
            }
          />
        </div>
      </Section>

      {/* Dynamic news classification and direction strength */}
      <Section title="动态方向 / 力量分析">
        {!signal.decision_eligible && (
          <div className="mb-2 rounded border border-border bg-secondary px-2 py-1.5 font-mono text-[10px] leading-relaxed text-muted-foreground">
            此记录属于旧研究或未验证数据，动态字段按历史原值保留，不代表当前正式算法输出。
          </div>
        )}
        <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
          <KV label="类型" value={signal.analysis_type || 'trend'} />
          <KV label="影响区间" value={signal.impact_horizon || signal.expected_horizon || '—'} />
          <KV label="上涨概率" value={signal.bullish_probability == null ? '—' : `${(signal.bullish_probability * 100).toFixed(1)}%`} tone={(signal.bullish_probability ?? 0) >= (signal.bearish_probability ?? 0) ? 'good' : 'neutral'} />
          <KV label="下跌概率" value={signal.bearish_probability == null ? '—' : `${(signal.bearish_probability * 100).toFixed(1)}%`} tone={(signal.bearish_probability ?? 0) > (signal.bullish_probability ?? 0) ? 'bad' : 'neutral'} />
          <KV label="不确定性" value={signal.uncertainty == null ? '—' : `${(signal.uncertainty * 100).toFixed(1)}%`} />
          <KV label="上涨力量" value={signal.bullish_force == null ? '—' : signal.bullish_force.toFixed(2)} tone="good" />
          <KV label="下跌力量" value={signal.bearish_force == null ? '—' : signal.bearish_force.toFixed(2)} tone="bad" />
          <KV label="双向候选" value={signal.dual_side_candidate ? '模拟/复核' : '否'} />
        </div>
        <div className="mt-2 grid grid-cols-2 gap-2 text-[10px] text-muted-foreground">
          <span>止盈：{signal.take_profit_pct ? `${signal.take_profit_pct}%` : '策略默认'}</span>
          <span>止损：{signal.stop_loss_pct ? `${signal.stop_loss_pct}%` : '策略默认'}</span>
          <span className="col-span-2">退出规则：{signal.exit_policy || 'horizon_or_signal_flip'}</span>
        </div>
      </Section>

      <Section title="决策时盘面结构">
        {historicalStructure ? (
          <HistoricalStructureView context={historicalStructure} />
        ) : (
          <div className="rounded border border-dashed border-border bg-secondary/30 px-3 py-2 text-[11px] leading-5 text-muted-foreground">
            该决策快照未保存与 {signal.asset} 匹配的盘面结构。旧记录不补算、不使用其他品种替代。
          </div>
        )}
      </Section>

      {/* Reasoning path */}
      <Section title="大模型归因">
        <pre className="thin-scroll max-h-48 overflow-y-auto whitespace-pre-wrap rounded border border-border bg-secondary p-2 font-mono text-[11px] leading-relaxed text-foreground">
          {signal.reasoning || '(无主模型推理)'}
        </pre>
        {signal.reasoning_path && (
          <div className="mt-2 font-mono text-[10px] text-muted-foreground">
            path: <span className="text-foreground">{signal.reasoning_path}</span>
          </div>
        )}
        {signal.prediction_type && (
          <div className="mt-1 font-mono text-[10px] text-muted-foreground">
            prediction_type: <span className="text-foreground">{signal.prediction_type}</span>
          </div>
        )}
        {signal.expected_horizon && (
          <div className="mt-1 font-mono text-[10px] text-muted-foreground">
            horizon: <span className="text-foreground">{signal.expected_horizon}</span>
          </div>
        )}
        {signal.invalidation_condition && (
          <div className="mt-1 font-mono text-[10px] text-muted-foreground">
            invalidation: <span className="text-foreground">{signal.invalidation_condition}</span>
          </div>
        )}
      </Section>

      {/* 4 sub-models consensus */}
      {Object.keys(consensus).length > 0 && (
        <Section title="四模型子决策 (extra_models_consensus)">
          <div className="grid grid-cols-1 gap-2 sm:grid-cols-2">
            {Object.entries(consensus).map(([model, c]) => (
              <div key={model} className="rounded border border-border bg-secondary p-2">
                <div className="flex items-center justify-between">
                  <span className="font-mono text-[11px] font-semibold text-foreground">{model}</span>
                  <span className={`rounded border px-1.5 py-0.5 font-mono text-[10px] ${actionColor(c.action || '')}`}>
                    {c.action || '—'}
                  </span>
                </div>
                <div className="mt-1 font-mono text-[10px] text-muted-foreground">
                  score {c.score != null ? (c.score as number).toFixed(2) : '—'}
                </div>
                {c.reasoning && (
                  <div className="mt-1 line-clamp-3 font-mono text-[10px] leading-relaxed text-muted-foreground">
                    {c.reasoning}
                  </div>
                )}
              </div>
            ))}
          </div>
        </Section>
      )}

      {/* Doubao 2nd-pass */}
      {signal.doubao_reasoning && (
        <Section title="豆包复核 (doubao)">
          <div className="flex items-center gap-2">
            <span className="font-mono text-[11px] text-muted-foreground">doubao action:</span>
            <span
              className={`rounded border px-1.5 py-0.5 font-mono text-[10px] ${actionColor(
                signal.doubao_action || ''
              )}`}
            >
              {signal.doubao_action || '—'}
            </span>
          </div>
          <pre className="thin-scroll mt-2 max-h-32 overflow-y-auto whitespace-pre-wrap rounded border border-border bg-secondary p-2 font-mono text-[11px] leading-relaxed text-foreground">
            {signal.doubao_reasoning}
          </pre>
        </Section>
      )}

      {/* Meta */}
      <Section title="元数据">
        <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
          <KV label="news_id" value={String(signal.news_id)} />
          <KV label="cluster_size" value={String(signal.cluster_size ?? '—')} />
          <KV label="market_confirmation" value={signal.market_confirmation || '—'} />
          <KV label="timeframe_match" value={signal.timeframe_match || '—'} />
          <KV label="direct_catalyst" value={signal.direct_catalyst != null ? String(signal.direct_catalyst) : '—'} />
          <KV label="event_phase" value={signal.event_phase || '—'} />
          <KV label="entry_time" value={fmtTime(signal.entry_time)} />
          <KV label="settled" value={String(signal.settled)} />
          <KV label="quality_status" value={signal.quality_status || '—'} />
          <KV label="decision_eligible" value={signal.decision_eligible ? 'true' : 'false'} />
          <KV label="tracking_quality" value={signal.tracking_quality || '—'} />
          <KV label="recorded_verdict" value={signal.recorded_is_correct || '—'} />
        </div>
        {signal.decision_context && (
          <pre className="thin-scroll mt-2 max-h-40 overflow-y-auto whitespace-pre-wrap rounded border border-border bg-secondary p-2 font-mono text-[11px] leading-relaxed text-muted-foreground">
            {signal.decision_context}
          </pre>
        )}
      </Section>
    </div>
  )
}

function HistoricalStructureView({ context }: { context: HistoricalStructureContext }) {
  const { structure } = context
  const usable = context.decisionEligible && structure.decision_eligible === true
  const hasDistinctQuote = Boolean(
    context.quoteSource
    && (
      context.quoteSource !== context.source
      || context.quoteVenue !== context.venue
      || context.quoteInstrumentId !== context.instrumentId
    )
  )
  return (
    <div className="rounded border border-border bg-secondary/20 p-2.5">
      <div className="flex flex-wrap items-center gap-2">
        <span className="font-mono text-xs font-semibold text-foreground">{context.asset}</span>
        <span className={`rounded border px-1.5 py-0.5 font-mono text-[9px] ${usable ? 'border-long/40 bg-long/10 text-long' : 'border-hold/40 bg-hold/10 text-hold'}`}>
          {usable ? '决策时可参与综合评分' : '决策时不可用'}
        </span>
        <span className="ml-auto font-mono text-[9px] text-muted-foreground">
          结构 {context.venue || context.source} · {context.instrumentId || context.asset} · {context.instrumentType || '产品类型未知'}
        </span>
      </div>
      {hasDistinctQuote && (
        <div className="mt-1 font-mono text-[9px] text-muted-foreground">
          报价 {context.quoteVenue || context.quoteSource} · {context.quoteInstrumentId || context.asset}
        </div>
      )}

      {usable ? (
        <>
          <div className="mt-2 grid grid-cols-2 gap-2 sm:grid-cols-4">
            <KV label="综合趋势" value={structureTrendLabel(structure.trend)} tone={structure.trend === 'bullish' ? 'good' : structure.trend === 'bearish' ? 'bad' : 'neutral'} />
            <KV label="趋势分" value={structureSigned(structure.trend_score)} tone={(structure.trend_score ?? 0) > 0.05 ? 'good' : (structure.trend_score ?? 0) < -0.05 ? 'bad' : 'neutral'} />
            <KV label="置信度" value={structurePct(structure.confidence)} />
            <KV label="周期一致" value={`${structureAlignmentLabel(structure.alignment)} ${structurePct(structure.alignment_score)}`} />
          </div>
          <div className="mt-2 grid gap-2 sm:grid-cols-2">
            {(['15m', '1h', '4h', '1d'] as const).map((timeframe) => (
              <HistoricalTimeframe key={timeframe} timeframe={timeframe} data={structure.timeframes?.[timeframe]} />
            ))}
          </div>
          <div className="mt-2 font-mono text-[9px] text-muted-foreground/70">
            快照时间 {structureTime(structure.as_of_ms)} · 仅展示当时已闭合 K 线，未按现在行情回填。
          </div>
        </>
      ) : (
        <p className="mt-2 text-[11px] leading-5 text-muted-foreground">
          该结构没有通过事件时点、来源或质量闸门（{context.contextReason}），仅保留审计记录，不参与正式买卖判断。
        </p>
      )}
    </div>
  )
}

function HistoricalTimeframe({ timeframe, data }: { timeframe: string; data?: MarketStructureTimeframe }) {
  if (!data || data.status === 'unavailable') {
    return <div className="rounded border border-border px-2 py-1.5 font-mono text-[10px] text-muted-foreground">{timeframe} · 不可用</div>
  }
  if (data.decision_eligible !== true) {
    return (
      <div className="rounded border border-hold/30 bg-hold/5 px-2 py-1.5 font-mono text-[10px] text-hold">
        {timeframe} · 质量闸门未通过，仅保留审计记录
      </div>
    )
  }
  const indicators = data.indicators || {}
  const structure = data.structure || {}
  const event = latestConfirmedStructureEvent(structure)
  return (
    <div className="rounded border border-border bg-card/40 px-2 py-1.5">
      <div className="flex items-center gap-2 font-mono text-[10px]">
        <span className="font-semibold text-foreground">{timeframe}</span>
        <span className={data.trend === 'bullish' ? 'text-long' : data.trend === 'bearish' ? 'text-short' : 'text-hold'}>{structureTrendLabel(data.trend)}</span>
        <span className="ml-auto text-muted-foreground">{structureSigned(data.trend_score)} · {structurePct(data.confidence)}</span>
      </div>
      <div className="mt-1 font-mono text-[9px] leading-4 text-muted-foreground">
        <div>EMA20/50/200 {structurePrice(indicators.ema20)} / {structurePrice(indicators.ema50)} / {structurePrice(indicators.ema200)}</div>
        <div>RSI {structureNumber(indicators.rsi14, 1)} · ATR {structureNumber(indicators.atr_pct, 2)}% · S/R {structurePrice(structure.support?.price)} / {structurePrice(structure.resistance?.price)}</div>
        <div>{event ? `${event.kind || '结构'} ${event.direction === 'bullish' ? '向上' : event.direction === 'bearish' ? '向下' : '未知'} @ ${structurePrice(event.level)} · 确认 ${structureTime(event.confirmed_at)}` : 'BOS / CHoCH 尚未确认'}</div>
        <div>BOS / CHoCH 仅作研究参考，不得单独触发交易。</div>
      </div>
    </div>
  )
}

function extractHistoricalStructure(signal: SimSignal | null): HistoricalStructureContext | null {
  if (!signal?.decision_context) return null
  let parsed: unknown
  try {
    parsed = typeof signal.decision_context === 'string'
      ? JSON.parse(signal.decision_context)
      : signal.decision_context
  } catch {
    return null
  }
  if (!isRecord(parsed) || !isRecord(parsed.assets)) return null
  const asset = normalizeStructureAsset(signal.asset)
  const assetContext = parsed.assets[asset]
  if (!isRecord(assetContext) || !isRecord(assetContext.market_structure)) return null
  const structure = assetContext.market_structure as MarketStructure
  const eventTimeEligible = parsed.market_context_eligible === true && parsed.timestamp_mismatch === false
  const structureSource = firstNonEmptyString(
    assetContext.structure_source,
    structure.source,
    assetContext.source,
  ) || 'unknown'
  return {
    asset,
    source: structureSource,
    venue: firstNonEmptyString(assetContext.structure_venue, assetContext.venue, structureSource),
    instrumentId: firstNonEmptyString(assetContext.structure_instrument_id, assetContext.instrument_id),
    instrumentType: firstNonEmptyString(assetContext.structure_instrument_type, assetContext.instrument_type),
    quoteSource: firstNonEmptyString(assetContext.quote_source, assetContext.source),
    quoteVenue: firstNonEmptyString(assetContext.quote_venue, assetContext.venue),
    quoteInstrumentId: firstNonEmptyString(assetContext.quote_instrument_id, assetContext.instrument_id),
    contextReason: typeof parsed.market_context_reason === 'string'
      ? parsed.market_context_reason
      : 'missing_event_time_audit',
    decisionEligible: eventTimeEligible && assetContext.decision_eligible === true && structure.decision_eligible === true,
    structure,
  }
}

function normalizeStructureAsset(value: string): string {
  const asset = value.trim().toUpperCase().replace(/[^A-Z0-9]/g, '')
  if (asset === 'GOLD' || asset === 'XAUUSD' || asset === 'XAUUSDT' || asset === 'XAU') return 'XAU'
  if (asset === 'BTCUSD' || asset === 'BTCUSDT' || asset === 'BTC') return 'BTC'
  return asset
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

function firstNonEmptyString(...values: unknown[]): string {
  for (const value of values) {
    if (typeof value === 'string' && value.trim()) return value
  }
  return ''
}

function structureTrendLabel(value?: string) {
  return value === 'bullish' ? '多头' : value === 'bearish' ? '空头' : value === 'range' ? '震荡' : '未知'
}

function structureAlignmentLabel(value?: string) {
  return value === 'bullish' ? '多头一致' : value === 'bearish' ? '空头一致' : value === 'mixed' ? '周期冲突' : value === 'range' ? '震荡一致' : '未知'
}

function structureSigned(value?: number) {
  return typeof value === 'number' && Number.isFinite(value) ? `${value >= 0 ? '+' : ''}${value.toFixed(3)}` : '—'
}

function structurePct(value?: number) {
  return typeof value === 'number' && Number.isFinite(value) ? `${(value * 100).toFixed(0)}%` : '—'
}

function structureNumber(value?: number | null, digits = 2) {
  return typeof value === 'number' && Number.isFinite(value) ? value.toFixed(digits) : '—'
}

function structurePrice(value?: number | null) {
  if (typeof value !== 'number' || !Number.isFinite(value)) return '—'
  return value.toLocaleString('en-US', { maximumFractionDigits: Math.abs(value) >= 100 ? 2 : 5 })
}

function structureTime(value?: number | null) {
  if (typeof value !== 'number' || !Number.isFinite(value)) return '未知'
  return new Date(value).toLocaleString('zh-CN', { hour12: false })
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div>
      <h4 className="mb-1 font-mono text-[10px] uppercase tracking-widest text-muted-foreground">{title}</h4>
      {children}
    </div>
  )
}

function KV({
  label,
  value,
  tone,
}: {
  label: string
  value: React.ReactNode
  tone?: 'good' | 'bad' | 'neutral'
}) {
  const toneCls =
    tone === 'good' ? 'text-long' : tone === 'bad' ? 'text-short' : 'text-foreground'
  return (
    <div className="rounded border border-border bg-secondary px-2 py-1.5">
      <div className="font-mono text-[10px] text-muted-foreground">{label}</div>
      <div className={`font-mono text-xs font-semibold ${toneCls}`}>{value}</div>
    </div>
  )
}
