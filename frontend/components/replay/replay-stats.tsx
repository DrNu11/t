'use client'

import { useEffect, useState } from 'react'
import type { ReplayReflection, ReplayStats } from '@/lib/replay-data'
import { fetchReplayReflection, fetchReplayStats, fmtPct } from '@/lib/replay-data'

export function ReplayStats({ refreshKey }: { refreshKey: number }) {
  const [stats, setStats] = useState<ReplayStats | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [reflection, setReflection] = useState<ReplayReflection | null>(null)

  useEffect(() => {
    let cancelled = false
    fetchReplayStats()
      .then((s) => { if (!cancelled) { setStats(s); setError(null) } })
      .catch((e) => { if (!cancelled) setError(String(e)) })
    fetchReplayReflection()
      .then((value) => { if (!cancelled) setReflection(value) })
      .catch(() => { if (!cancelled) setReflection(null) })
    return () => { cancelled = true }
  }, [refreshKey])

  if (error) {
    return (
      <div className="rounded-md border border-short/40 bg-short/10 p-3 text-xs text-short">
        复盘统计加载失败：{error}
      </div>
    )
  }

  if (!stats) {
    return (
      <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
        {Array.from({ length: 4 }).map((_, i) => (
          <div key={i} className="h-24 animate-pulse rounded-lg border border-border bg-secondary/50" />
        ))}
      </div>
    )
  }

  const { overall, by_asset } = stats
  const noData = (overall.total || 0) === 0

  return (
    <div className="space-y-3">
      <div className="flex items-center justify-between">
        <h3 className="font-mono text-xs uppercase tracking-widest text-muted-foreground">
          模拟盘 · 全部交易统计
        </h3>
        <span className="rounded border border-hold/50 bg-hold/10 px-2 py-0.5 font-mono text-[10px] text-hold">
          PAPER TRADING
        </span>
      </div>

      <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
        <StatCard
          label="自动建仓总数"
          value={overall.total || 0}
          sub={`跟踪 ${overall.tracking || 0} · 已结算 ${overall.settled || 0}`}
        />
        <StatCard
          label="胜率"
          value={noData ? '—' : `${((overall.winrate || 0) * 100).toFixed(1)}%`}
          sub={noData ? '等待当前策略建仓' : `W${overall.wins || 0} / L${overall.losses || 0} / HOLD${overall.holds || 0} · 胜率=W/(W+L)`}
          tone={overall.winrate >= 0.5 ? 'good' : overall.winrate >= 0.3 ? 'warn' : 'bad'}
        />
        <StatCard
          label="平均 forward_pnl"
          value={noData ? '—' : fmtPct(overall.avg_pnl)}
          sub={`最佳 ${fmtPct(overall.best_trade)} · 最差 ${fmtPct(overall.worst_trade)}`}
          tone={(overall.avg_pnl || 0) >= 0 ? 'good' : 'bad'}
        />
        <StatCard
          label="平均 MFE / MAE"
          value={noData ? '—' : `${fmtPct(overall.avg_mfe)} / ${fmtPct(overall.avg_mae)}`}
          sub="最大有利/不利幅度"
        />
      </div>

      {by_asset.length > 0 && (
        <div className="rounded-md border border-border bg-card p-3">
          <div className="mb-2 font-mono text-[10px] uppercase tracking-widest text-muted-foreground">
            按品种分布
          </div>
          <div className="grid grid-cols-2 gap-2 sm:grid-cols-3 lg:grid-cols-4">
            {by_asset.map((row) => {
              const decided = (row.wins || 0) + (row.losses || 0)
              const wr = decided ? (row.wins || 0) / decided : 0
              return (
                <div
                  key={row.asset}
                  className="flex items-center justify-between rounded-lg border border-border bg-secondary/30 px-3 py-2"
                >
                  <div>
                    <div className="font-mono text-xs font-semibold text-foreground">{row.asset}</div>
                    <div className="font-mono text-[10px] text-muted-foreground">{row.total} 笔</div>
                  </div>
                  <div className="text-right">
                    <div
                      className={`font-mono text-sm font-semibold ${
                        wr >= 0.5 ? 'text-long' : wr >= 0.3 ? 'text-hold' : 'text-short'
                      }`}
                    >
                      {(wr * 100).toFixed(0)}%
                    </div>
                    <div className={`font-mono text-[10px] ${(row.avg_pnl || 0) >= 0 ? 'text-long/80' : 'text-short/80'}`}>
                      {fmtPct(row.avg_pnl)}
                    </div>
                  </div>
                </div>
              )
            })}
          </div>
        </div>
      )}
      {reflection && (
        <div className="rounded-md border border-border bg-card p-3">
          <div className="mb-2 flex items-center justify-between">
            <div className="font-mono text-[10px] uppercase tracking-widest text-muted-foreground">交易过程复盘 · 正确/失败模式</div>
            <span className="font-mono text-[10px] text-muted-foreground">样本 {reflection.sample}</span>
          </div>
          <div className="grid gap-2 md:grid-cols-3">
            {reflection.by_analysis_type.map((row) => (
              <div key={row.key} className="rounded border border-border bg-secondary/30 px-2 py-1.5 font-mono text-[10px]">
                <div className="text-foreground">{row.key}</div>
                <div className="text-muted-foreground">{row.sample} 笔 · 胜率 {(row.winrate * 100).toFixed(0)}% · PnL {fmtPct(row.avg_forward_pnl)}</div>
              </div>
            ))}
          </div>
          <ul className="mt-2 list-disc space-y-1 pl-4 text-[10px] text-muted-foreground">
            {reflection.recommendations.slice(0, 3).map((item) => <li key={item}>{item}</li>)}
          </ul>
        </div>
      )}
    </div>
  )
}

function StatCard({
  label,
  value,
  sub,
  tone,
}: {
  label: string
  value: string | number
  sub?: string
  tone?: 'good' | 'bad' | 'warn'
}) {
  const toneCls =
    tone === 'good'
      ? 'text-long'
      : tone === 'bad'
      ? 'text-short'
      : tone === 'warn'
      ? 'text-hold'
      : 'text-foreground'
  return (
    <div className="surface-card p-2.5">
      <div className="font-mono text-[10px] uppercase tracking-widest text-muted-foreground">{label}</div>
      <div className={`mt-0.5 font-mono text-lg font-bold ${toneCls}`}>{value}</div>
      {sub && <div className="mt-0.5 font-mono text-[10px] text-muted-foreground">{sub}</div>}
    </div>
  )
}
