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

type Props = {
  signal: SimSignal | null
}

type ConsensusModel = {
  action?: string
  score?: number
  reasoning?: string
}

export function SignalDetail({ signal }: Props) {
  if (!signal) {
    return (
      <div className="flex h-full items-center justify-center rounded-lg border border-dashed border-border bg-card">
        <span className="font-mono text-xs text-muted-foreground">选择一条信号查看完整推理链</span>
      </div>
    )
  }

  const consensus = useMemo<Record<string, ConsensusModel>>(() => {
    const c = signal.extra_models_consensus
    if (!c) return {}
    if (typeof c === 'string') {
      try { return JSON.parse(c) } catch { return {} }
    }
    return c as Record<string, ConsensusModel>
  }, [signal.extra_models_consensus])
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
