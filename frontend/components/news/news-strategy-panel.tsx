'use client'

import { useEffect, useState } from 'react'
import {
  fetchInternalNews, fetchNewsAnalysis, fetchStrategyAdvice, fetchTechFlowNews,
  type InternalNews, type NewsAnalysis, type StrategyAdvice, type TechFlowItem,
} from '@/lib/news-data'

const FACTOR_LABELS: Record<string, string> = {
  news_sentiment: '新闻情绪', market_confirmation: '市场确认', trend: '趋势',
  volatility: '波动适配', funding: '资金费率', cluster_heat: '聚合热度',
  historical_confidence: '历史置信',
}

export function NewsStrategyPanel() {
  const [source, setSource] = useState<'internal' | 'techflow'>('internal')
  const [internal, setInternal] = useState<InternalNews[]>([])
  const [techflow, setTechflow] = useState<TechFlowItem[]>([])
  const [techStatus, setTechStatus] = useState('')
  const [selectedInternal, setSelectedInternal] = useState<InternalNews | null>(null)
  const [selectedTech, setSelectedTech] = useState<TechFlowItem | null>(null)
  const [analysis, setAnalysis] = useState<NewsAnalysis | null>(null)
  const [advice, setAdvice] = useState<StrategyAdvice | null>(null)
  const [loading, setLoading] = useState(false)

  useEffect(() => {
    void Promise.all([fetchInternalNews(), fetchTechFlowNews()]).then(([events, flow]) => {
      setInternal(events)
      setTechflow(flow.items)
      setTechStatus(flow.status === 'ok' ? '' : flow.error || '来源暂不可用')
      if (events[0]) void selectInternal(events[0])
    }).catch(() => setTechStatus('新闻加载失败'))
  }, [])

  async function selectInternal(item: InternalNews) {
    setSelectedInternal(item); setSelectedTech(null); setAdvice(null); setLoading(true)
    try { setAnalysis(await fetchNewsAnalysis(item.id)) } catch { setAnalysis(null) } finally { setLoading(false) }
  }

  async function generateAdvice() {
    if (!selectedInternal) return
    setLoading(true)
    try { setAdvice(await fetchStrategyAdvice(selectedInternal.id)) } finally { setLoading(false) }
  }

  const actionTone = analysis?.analysis.action === 'BUY' ? 'text-long' : analysis?.analysis.action === 'SELL' ? 'text-short' : 'text-foreground'

  return (
    <div className="grid h-full min-h-0 grid-cols-1 gap-4 lg:grid-cols-[360px_1fr]">
      <section className="surface-card flex min-h-[300px] flex-col overflow-hidden">
        <div className="flex border-b border-border p-2">
          {(['internal', 'techflow'] as const).map((key) => <button key={key} onClick={() => setSource(key)} className={`flex-1 rounded-lg px-3 py-2 font-mono text-xs transition-colors ${source === key ? 'bg-primary/10 text-primary' : 'text-muted-foreground hover:bg-secondary hover:text-foreground'}`}>{key === 'internal' ? '内部系统' : 'TechFlow 深潮'}</button>)}
        </div>
        <div className="thin-scroll min-h-0 flex-1 overflow-y-auto">
          {source === 'internal' ? internal.map((item) => <button key={item.id} onClick={() => void selectInternal(item)} className={`block w-full border-b border-border p-3 text-left hover:bg-secondary/60 ${selectedInternal?.id === item.id ? 'bg-primary/5' : ''}`}><div className="line-clamp-2 text-sm text-foreground">{item.news_text}</div><div className="mt-2 font-mono text-[10px] text-muted-foreground">{item.source} · {item.timestamp}</div></button>) : techflow.map((item) => <button key={item.id} onClick={() => { setSelectedTech(item); setSelectedInternal(null); setAnalysis(null); setAdvice(null) }} className="block w-full border-b border-border p-3 text-left hover:bg-secondary/60"><div className="line-clamp-2 text-sm text-foreground">{item.title}</div><div className="mt-2 font-mono text-[10px] text-muted-foreground">{item.published_at}</div></button>)}
          {source === 'techflow' && techStatus && <div className="p-4 text-xs text-muted-foreground">{techStatus}</div>}
        </div>
      </section>

      <section className="surface-card thin-scroll min-h-0 overflow-y-auto p-4 md:p-5">
        {selectedTech ? <div className="space-y-4"><div className="font-mono text-[10px] text-primary">TECHFLOW 深潮</div><div className="text-base text-foreground">{selectedTech.title}</div><p className="whitespace-pre-wrap text-sm leading-6 text-muted-foreground">{selectedTech.summary || '暂无摘要'}</p>{selectedTech.url && <a href={selectedTech.url} target="_blank" rel="noreferrer" className="inline-flex rounded-lg border border-primary/40 px-3 py-2 text-xs text-primary">查看原文</a>}</div> : analysis ? <div className="space-y-5">
          <div><div className="font-mono text-[10px] text-muted-foreground">{analysis.news.source} · {analysis.news.published_at}</div><p className="mt-3 whitespace-pre-wrap text-sm leading-6 text-foreground">{analysis.news.content}</p></div>
          <div className="flex flex-wrap items-center gap-4 border-y border-border py-3"><span className={`font-mono text-lg font-bold ${actionTone}`}>{analysis.analysis.action}</span>{analysis.analysis.raw_action !== analysis.analysis.action && <span className="rounded border border-hold/40 px-2 py-0.5 font-mono text-[10px] text-hold">原始 {analysis.analysis.raw_action} 已被置信度闸门拦截</span>}<span className="font-mono text-xs text-muted-foreground">最终分 {analysis.analysis.final_score.toFixed(3)}</span><span className={`font-mono text-xs ${analysis.analysis.confidence_detail.passed_gate ? 'text-long' : 'text-hold'}`}>统一置信度 {analysis.analysis.confidence.toFixed(1)}/100（闸门 {analysis.analysis.confidence_detail.gate}）</span></div>
          <p className="text-xs leading-5 text-muted-foreground">{analysis.analysis.verdict}</p>

          <div className="rounded-lg border border-border bg-secondary/20 p-3">
            <div className="font-mono text-[10px] text-muted-foreground">证据层（0-10 归一）</div>
            <div className="mt-3 space-y-2">{Object.entries(analysis.analysis.evidence).map(([key, item]) => <div key={key} className="flex items-center gap-3"><span className="w-20 shrink-0 font-mono text-[11px] text-foreground">{FACTOR_LABELS[key] || key}</span><div className="h-1.5 flex-1 overflow-hidden rounded bg-muted"><div className={`h-full ${item.evidence_score >= 5 ? 'bg-long' : 'bg-short'}`} style={{ width: `${item.evidence_score * 10}%` }} /></div><span className="w-10 shrink-0 text-right font-mono text-[11px] text-muted-foreground">{item.evidence_score.toFixed(1)}</span></div>)}</div>
          </div>

          <div className="grid gap-3 md:grid-cols-2">
            <div className="rounded-lg border border-border bg-secondary/20 p-3"><div className="font-mono text-[10px] text-muted-foreground">校验层 · 卡方检验</div><div className={`mt-2 font-mono text-sm ${analysis.analysis.significance.significant ? 'text-long' : 'text-hold'}`}>χ² {analysis.analysis.significance.chi_square.toFixed(3)} · p {analysis.analysis.significance.p_value.toFixed(4)}</div><div className="mt-2 text-[11px] leading-5 text-muted-foreground">{analysis.analysis.significance.note}</div><div className="mt-2 font-mono text-[10px] text-muted-foreground/70">显著性权重 ×{analysis.analysis.confidence_detail.significance_weight}</div></div>
            <div className="rounded-lg border border-border bg-secondary/20 p-3"><div className="font-mono text-[10px] text-muted-foreground">反幻觉交叉校验</div>{analysis.analysis.contradictions.length === 0 ? <div className="mt-2 text-[11px] text-long">未检测到证据矛盾</div> : <ul className="mt-2 space-y-1.5">{analysis.analysis.contradictions.map((item) => <li key={item.type + item.detail} className="text-[11px] leading-5 text-short">· {item.detail}</li>)}</ul>}<div className="mt-2 font-mono text-[10px] text-muted-foreground/70">置信度扣减 −{analysis.analysis.confidence_detail.contradiction_penalty}</div></div>
          </div>

          <div className="grid gap-3 md:grid-cols-2">{Object.entries(analysis.analysis.factors).map(([key, factor]) => <div key={key} className="rounded-lg border border-border bg-secondary/20 p-3"><div className="flex justify-between font-mono text-xs"><span className="text-foreground">{FACTOR_LABELS[key] || key}</span><span className={factor.score >= 0 ? 'text-long' : 'text-short'}>{factor.score.toFixed(2)}</span></div><div className="mt-2 h-1.5 overflow-hidden rounded bg-muted"><div className={`h-full ${factor.score >= 0 ? 'bg-long' : 'bg-short'}`} style={{ width: `${Math.abs(factor.score) * 50}%`, marginLeft: factor.score < 0 ? `${50 - Math.abs(factor.score) * 50}%` : '50%' }} /></div><div className="mt-2 text-[11px] text-muted-foreground">{factor.explanation}</div></div>)}</div>
          <div className="rounded-lg border border-border bg-secondary/20 p-3"><div className="font-mono text-[10px] text-muted-foreground">最新 AI 决策</div><div className="mt-2 text-sm text-foreground">{analysis.decision ? `${analysis.decision.suggested_action} · ${analysis.decision.reasoning}` : '尚无 AI 决策'}</div>{analysis.decision?.reasoning_path && <p className="mt-2 whitespace-pre-wrap text-xs leading-5 text-muted-foreground">{analysis.decision.reasoning_path}</p>}</div>
          <button disabled={loading} onClick={() => void generateAdvice()} className="rounded-lg border border-primary/40 bg-primary/10 px-4 py-2 text-xs text-primary disabled:opacity-50">{loading ? '生成中…' : '生成策略建议'}</button>
          {advice && <div className="rounded-lg border border-primary/30 bg-primary/5 p-4"><div className="mb-3 font-mono text-[10px] text-primary">{advice.mode === 'llm' ? 'AI 建议' : '规则降级建议'} · 未应用</div><div className="grid grid-cols-2 gap-3 md:grid-cols-4">{(['signal_threshold','notional_multiplier','trailing_callback_rate','holding_horizon_minutes'] as const).map((key) => <div key={key}><div className="break-all font-mono text-[9px] text-muted-foreground">{key}</div><div className="mt-1 font-mono text-sm text-foreground">{advice.current_values[key]} → {advice.advice[key]}</div><div className="text-[9px] text-muted-foreground/70">上下限 {advice.bounds[key].min}–{advice.bounds[key].max}</div></div>)}</div><p className="mt-4 text-xs text-muted-foreground">{advice.advice.reason}</p><p className="mt-2 text-xs text-hold">风险：{advice.advice.risk_notes}</p><p className="mt-2 text-xs text-short">回滚：{advice.advice.rollback_condition}</p><p className="mt-3 border-t border-border pt-3 font-mono text-[10px] text-muted-foreground/70">校验：置信度 {advice.validation.confidence.toFixed(1)}/100 · p={advice.validation.significance.p_value} · 矛盾 {advice.validation.contradictions.length} 项 · 闸门后动作 {advice.validation.gated_action}</p></div>}
        </div> : <div className="text-sm text-muted-foreground">{loading ? '加载分析中…' : '请选择新闻'}</div>}
      </section>
    </div>
  )
}
