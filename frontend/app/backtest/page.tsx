'use client'

import { useEffect, useState } from 'react'
import {
  fetchStrategies,
  fmtRate,
  fmtUsd,
  optimizeStrategy,
  runBacktest,
  type BacktestReport,
  type Strategy,
} from '@/lib/strategy-data'

export default function BacktestPage() {
  const [strategies, setStrategies] = useState<Strategy[]>([])
  const [strategyId, setStrategyId] = useState<number | null>(null)
  const [report, setReport] = useState<BacktestReport | null>(null)
  const [busy, setBusy] = useState(false)
  const [message, setMessage] = useState('')

  useEffect(() => {
    fetchStrategies().then((items) => {
      setStrategies(items)
      setStrategyId(items[0]?.id ?? null)
    }).catch((err) => setMessage(String(err)))
  }, [])

  async function onRun() {
    if (!strategyId) return
    setBusy(true)
    try {
      const result = await runBacktest({ strategy_id: strategyId, persist: true })
      setReport(result.report)
      setMessage(`回测完成 · run #${result.run_id ?? '—'} · ${result.report.note}`)
    } catch (err) {
      setMessage(String(err))
    } finally {
      setBusy(false)
    }
  }

  async function onOptimize() {
    if (!strategyId) return
    setBusy(true)
    try {
      const result = await optimizeStrategy(strategyId, true)
      setMessage(`${result.mode} 优化已写入 v${result.version.version}：${result.note}`)
      const next = await runBacktest({ strategy_id: strategyId, persist: true })
      setReport(next.report)
    } catch (err) {
      setMessage(String(err))
    } finally {
      setBusy(false)
    }
  }

  return (
    <main className="thin-scroll mx-auto w-full max-w-[1600px] flex-1 overflow-y-auto p-4 md:p-6">
      <div className="mb-4 flex flex-wrap items-end justify-between gap-3">
        <div>
          <h2 className="font-mono text-sm uppercase tracking-widest text-muted-foreground">策略回测</h2>
          <p className="mt-1 text-sm text-muted-foreground">只回放本平台已结算真实信号的 entry/exit，不生成模拟行情。</p>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <select value={strategyId ?? ''} onChange={(e) => setStrategyId(Number(e.target.value))} className="rounded border border-border bg-card px-2 py-2 font-mono text-xs">
            {strategies.map((item) => <option key={item.id} value={item.id}>{item.name}</option>)}
          </select>
          <button disabled={busy || !strategyId} onClick={() => void onRun()} className="rounded-lg border border-primary/40 bg-primary/10 px-3 py-2 text-xs text-primary disabled:opacity-50">运行回测</button>
          <button disabled={busy || !strategyId} onClick={() => void onOptimize()} className="rounded-lg border border-border px-3 py-2 text-xs text-foreground disabled:opacity-50">LLM 优化后再回测</button>
        </div>
      </div>
      {message && <div className="mb-4 rounded border border-border bg-secondary/30 px-3 py-2 text-xs text-muted-foreground">{message}</div>}
      {report && (
        <>
          {report.insufficient_sample && (
            <div className="mb-4 rounded border border-hold/40 bg-hold/10 px-3 py-2 text-xs text-hold">
              已结算样本不足 8 条，结果仅供参考，不会编造成交。
            </div>
          )}
          <div className="grid gap-3 md:grid-cols-5">
            <Metric label="样本 / 入场" value={`${report.sample_size} / ${report.taken}`} />
            <Metric label="胜率" value={fmtRate(report.winrate)} />
            <Metric label="累计盈亏" value={fmtUsd(report.total_pnl_usdt)} tone={report.total_pnl_usdt >= 0 ? 'long' : 'short'} />
            <Metric label="最大回撤" value={fmtUsd(report.max_drawdown_usdt)} tone="short" />
            <Metric label="盈亏比" value={report.profit_factor == null ? '—' : report.profit_factor.toFixed(2)} />
          </div>
          <section className="surface-card mt-4 overflow-hidden">
            <div className="border-b border-border px-4 py-3 font-mono text-[11px] text-muted-foreground">成交明细（真实 forward_pnl）</div>
            <div className="thin-scroll max-h-[460px] overflow-auto">
              <table className="w-full text-left text-xs">
                <thead className="sticky top-0 bg-card font-mono text-[10px] text-muted-foreground">
                  <tr>
                    <th className="px-3 py-2">ID</th>
                    <th className="px-3 py-2">品种</th>
                    <th className="px-3 py-2">方向</th>
                    <th className="px-3 py-2">分数</th>
                    <th className="px-3 py-2">PnL %</th>
                    <th className="px-3 py-2">PnL USDT</th>
                    <th className="px-3 py-2">判定</th>
                  </tr>
                </thead>
                <tbody>
                  {report.trades.map((trade) => (
                    <tr key={trade.id} className="border-t border-border">
                      <td className="px-3 py-2 font-mono">{trade.id}</td>
                      <td className="px-3 py-2">{trade.asset}</td>
                      <td className={`px-3 py-2 ${trade.action === 'BUY' ? 'text-long' : 'text-short'}`}>{trade.action}</td>
                      <td className="px-3 py-2 font-mono">{Number(trade.sentiment_score).toFixed(2)}</td>
                      <td className="px-3 py-2 font-mono">{Number(trade.forward_pnl_pct).toFixed(2)}%</td>
                      <td className={`px-3 py-2 font-mono ${trade.pnl_usdt >= 0 ? 'text-long' : 'text-short'}`}>{fmtUsd(trade.pnl_usdt)}</td>
                      <td className="px-3 py-2">{trade.is_correct || '—'}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </section>
        </>
      )}
    </main>
  )
}

function Metric({ label, value, tone }: { label: string; value: string; tone?: 'long' | 'short' }) {
  return (
    <div className="surface-card p-4">
      <div className="font-mono text-[10px] uppercase tracking-wider text-muted-foreground">{label}</div>
      <div className={`mt-2 font-mono text-lg ${tone === 'long' ? 'text-long' : tone === 'short' ? 'text-short' : 'text-foreground'}`}>{value}</div>
    </div>
  )
}
