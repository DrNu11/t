'use client'

import { useEffect, useState } from 'react'
import {
  fetchStrategies,
  fmtRate,
  fmtUsd,
  addStrategyVersion,
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
  const [aiPrompt, setAiPrompt] = useState('')
  const [analysisStructureText, setAnalysisStructureText] = useState('{\n  "sections": ["news", "structure", "macro", "risk"]\n}')

  useEffect(() => {
    fetchStrategies().then((items) => {
      setStrategies(items)
      setStrategyId(items[0]?.id ?? null)
      const version = items[0]?.latest_version
      setAiPrompt(version?.ai_prompt ?? '')
      setAnalysisStructureText(JSON.stringify(version?.analysis_structure ?? { sections: ['news', 'structure', 'macro', 'risk'] }, null, 2))
    }).catch((err) => setMessage(String(err)))
  }, [])

  function selectStrategy(nextId: number) {
    setStrategyId(nextId)
    setReport(null)
    const version = strategies.find((item) => item.id === nextId)?.latest_version
    setAiPrompt(version?.ai_prompt ?? '')
    setAnalysisStructureText(JSON.stringify(
      version?.analysis_structure ?? { sections: ['news', 'structure', 'macro', 'risk'] },
      null,
      2,
    ))
  }

  async function onRun() {
    if (!strategyId) return
    setBusy(true)
    try {
      const structure = parseStructure(analysisStructureText)
      const result = await runBacktest({ strategy_id: strategyId, persist: true, ai_prompt: aiPrompt, analysis_structure: structure })
      setReport(result.report)
      setMessage(`回测完成 · run #${result.run_id ?? '—'} · ${result.report.note}`)
    } catch (err) {
      setMessage(String(err))
    } finally {
      setBusy(false)
    }
  }

  async function onSavePrompt() {
    if (!strategyId) return
    setBusy(true)
    try {
      const strategy = strategies.find((item) => item.id === strategyId)
      const params = strategy?.latest_version?.params
      if (!params) throw new Error('策略参数尚未加载')
      const saved = await addStrategyVersion(strategyId, params, '保存 AI 提示词与分析结构', aiPrompt, parseStructure(analysisStructureText))
      setMessage(`提示词/结构已保存为 v${saved.version}`)
      const items = await fetchStrategies()
      setStrategies(items)
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
          <select value={strategyId ?? ''} onChange={(e) => selectStrategy(Number(e.target.value))} className="rounded border border-border bg-card px-2 py-2 font-mono text-xs">
            {strategies.map((item) => <option key={item.id} value={item.id}>{item.name}</option>)}
          </select>
          <button disabled={busy || !strategyId} onClick={() => void onRun()} className="rounded-lg border border-primary/40 bg-primary/10 px-3 py-2 text-xs text-primary disabled:opacity-50">运行回测</button>
          <button disabled={busy || !strategyId} onClick={() => void onOptimize()} className="rounded-lg border border-border px-3 py-2 text-xs text-foreground disabled:opacity-50">LLM 优化后再回测</button>
          <button disabled={busy || !strategyId} onClick={() => void onSavePrompt()} className="rounded-lg border border-border px-3 py-2 text-xs text-foreground disabled:opacity-50">保存提示词/结构</button>
        </div>
      </div>
      <section className="surface-card mb-4 grid gap-3 p-4 md:grid-cols-2">
        <label className="text-xs text-muted-foreground">
          AI 提示词（保存后影响后续 AI 分析；当前回测只记录配置快照，不重算历史决策）
          <textarea value={aiPrompt} onChange={(event) => setAiPrompt(event.target.value)} rows={7} className="mt-1 w-full rounded border border-border bg-card p-2 font-mono text-xs text-foreground" placeholder="例如：优先结合盘面结构、宏观与情绪，区分冲突类和力量趋势类新闻。" />
        </label>
        <label className="text-xs text-muted-foreground">
          分析结构 JSON
          <textarea value={analysisStructureText} onChange={(event) => setAnalysisStructureText(event.target.value)} rows={7} className="mt-1 w-full rounded border border-border bg-card p-2 font-mono text-xs text-foreground" />
        </label>
      </section>
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

function parseStructure(text: string): Record<string, unknown> {
  try {
    const value = JSON.parse(text)
    return value && typeof value === 'object' && !Array.isArray(value) ? value : { raw: value }
  } catch {
    return { raw: text }
  }
}

function Metric({ label, value, tone }: { label: string; value: string; tone?: 'long' | 'short' }) {
  return (
    <div className="surface-card p-4">
      <div className="font-mono text-[10px] uppercase tracking-wider text-muted-foreground">{label}</div>
      <div className={`mt-2 font-mono text-lg ${tone === 'long' ? 'text-long' : tone === 'short' ? 'text-short' : 'text-foreground'}`}>{value}</div>
    </div>
  )
}
