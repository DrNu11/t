'use client'

import { useCallback, useEffect, useRef, useState } from 'react'
import type { NewsSourceKey, NewsSourceSettings, PaperTrack, PaperTradingSettings, ReplayPositions, SimSignal } from '@/lib/replay-data'
import { fetchReplayPositions, updateNewsSourceSettings, updatePaperTrading } from '@/lib/replay-data'
import { API_BASE } from '@/lib/api'
import { activateStrategy, fetchStrategies, type Strategy } from '@/lib/strategy-data'
import { ReplayStats } from './replay-stats'
import { ReplayList } from './replay-list'
import { KlineChart } from './kline-chart'
import { SignalDetail } from './signal-detail'

const TRACKS: Array<[PaperTrack, string]> = [
  ['crypto', '加密货币'],
  ['gold', '黄金'],
  ['oil', '原油'],
]

const NEWS_SOURCES: Array<[NewsSourceKey, string]> = [
  ['financialjuice', 'FJ'],
  ['tree_news', 'Tree'],
  ['techflow', '深潮'],
  ['eastmoney', '东财'],
  ['blockbeats', '律动'],
]

export function ReplayPanel() {
  const [signals, setSignals] = useState<SimSignal[]>([])
  const [strategies, setStrategies] = useState<Strategy[]>([])
  const [currentStrategyId, setCurrentStrategyId] = useState<number | null>(null)
  const [currentStrategyName, setCurrentStrategyName] = useState('加载中')
  const [currentVersion, setCurrentVersion] = useState<number | null>(null)
  const [account, setAccount] = useState<ReplayPositions['account'] | null>(null)
  const [tradingSettings, setTradingSettings] = useState<PaperTradingSettings | null>(null)
  const [market, setMarket] = useState<Pick<ReplayPositions, 'pairs' | 'market_status' | 'market_sources'> | null>(null)
  const [strategyFeedback, setStrategyFeedback] = useState<ReplayPositions['strategy_feedback'] | null>(null)
  const [llmPerformance, setLlmPerformance] = useState<ReplayPositions['llm_performance'] | null>(null)
  const [quickSim, setQuickSim] = useState<ReplayPositions['quick_sim'] | null>(null)
  const [tracks, setTracks] = useState<PaperTrack[]>([])
  const [newsSettings, setNewsSettings] = useState<NewsSourceSettings | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [refreshKey, setRefreshKey] = useState(0)
  const [loading, setLoading] = useState(false)
  const [changingTrading, setChangingTrading] = useState(false)
  const [changingNews, setChangingNews] = useState(false)
  const [selected, setSelected] = useState<SimSignal | null>(null)
  const [autoRefresh, setAutoRefresh] = useState(true)
  const tradingLock = useRef(0)
  const newsLock = useRef(0)

  const reload = useCallback(async () => {
    setLoading(true)
    try {
      const data = await fetchReplayPositions()
      setSignals(data.positions)
      setCurrentStrategyId(data.current_strategy.id)
      setCurrentStrategyName(data.current_strategy.name)
      setCurrentVersion(data.current_strategy.active_version.version)
      setAccount(data.account)
      if (tradingLock.current === 0) {
        setTradingSettings((prev) => ({
          ...data.trading_settings,
          gate_enabled: data.trading_settings.gate_enabled ?? prev?.gate_enabled ?? true,
        }))
        setTracks(data.trading_settings.tracks)
      }
      setMarket({ pairs: data.pairs, market_status: data.market_status, market_sources: data.market_sources })
      setStrategyFeedback(data.strategy_feedback)
      setLlmPerformance(data.llm_performance)
      setQuickSim(data.quick_sim ?? null)
      if (newsLock.current === 0) {
        if (data.news_settings) {
          setNewsSettings(data.news_settings)
        }
      }
      setError(null)
      setRefreshKey((value) => value + 1)
      setSelected((prev) => {
        if (!prev) return data.positions[0] ?? null
        return data.positions.find((item) => item.id === prev.id) ?? data.positions[0] ?? null
      })
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : '模拟盘接口暂时不可用')
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    void reload()
    void fetchStrategies().then(setStrategies).catch(() => setStrategies([]))
  }, [reload])

  useEffect(() => {
    if (!autoRefresh) return
    const timer = setInterval(() => void reload(), 3_000)
    return () => clearInterval(timer)
  }, [autoRefresh, reload])

  async function persistTrading(next: {
    is_running: boolean
    tracks: PaperTrack[]
    gate_enabled: boolean
  }) {
    tradingLock.current += 1
    const lock = tradingLock.current
    setChangingTrading(true)
    try {
      const saved = await updatePaperTrading(next)
      if (lock !== tradingLock.current) return
      setTradingSettings(saved)
      setTracks(saved.tracks)
      setError(null)
    } catch (cause) {
      if (lock === tradingLock.current) {
        tradingLock.current = 0
        setError(cause instanceof Error ? cause.message : '模拟操盘状态更新失败')
        await reload()
      }
    } finally {
      if (lock === tradingLock.current) {
        tradingLock.current = 0
        setChangingTrading(false)
      }
    }
  }

  async function persistNews(body: Parameters<typeof updateNewsSourceSettings>[0], optimistic: NewsSourceSettings) {
    newsLock.current += 1
    const lock = newsLock.current
    setChangingNews(true)
    setNewsSettings(optimistic)
    try {
      const saved = await updateNewsSourceSettings(body)
      if (lock !== newsLock.current) return
      setNewsSettings(saved)
      setError(null)
    } catch (cause) {
      if (lock === newsLock.current) {
        setError(cause instanceof Error ? cause.message : '新闻开关更新失败')
      }
    } finally {
      if (lock === newsLock.current) {
        newsLock.current = 0
        setChangingNews(false)
      }
    }
  }

  async function changeStrategy(id: number) {
    try {
      await activateStrategy(id)
      await reload()
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : '策略切换失败')
    }
  }

  function toggleTrack(track: PaperTrack) {
    const next = tracks.includes(track) ? tracks.filter((item) => item !== track) : [...tracks, track]
    if (next.length === 0 && tradingSettings?.is_running) {
      setError('运行中至少保留一个投资赛道')
      return
    }
    setTracks(next)
    if (!tradingSettings) return
    void persistTrading({
      is_running: tradingSettings.is_running,
      tracks: next,
      gate_enabled: tradingSettings.gate_enabled ?? true,
    })
  }

  function toggleGate() {
    if (!tradingSettings) return
    const nextGate = !(tradingSettings.gate_enabled ?? true)
    setTradingSettings({ ...tradingSettings, gate_enabled: nextGate })
    void persistTrading({
      is_running: tradingSettings.is_running,
      tracks,
      gate_enabled: nextGate,
    })
  }

  function toggleNewsMaster() {
    if (!newsSettings) return
    void persistNews({ enabled: !newsSettings.enabled }, { ...newsSettings, enabled: !newsSettings.enabled })
  }

  function toggleNewsSource(key: NewsSourceKey) {
    if (!newsSettings) return
    const nextSources = { ...newsSettings.sources, [key]: !newsSettings.sources[key] }
    void persistNews({ sources: { [key]: nextSources[key] } }, { ...newsSettings, sources: nextSources })
  }

  async function changeDailyTarget(value: number) {
    if (!newsSettings) return
    const daily_target = Math.min(2000, Math.max(1, Math.round(value)))
    void persistNews({ daily_target }, { ...newsSettings, daily_target, remaining: Math.max(0, daily_target - newsSettings.today_count) })
  }

  function toggleTrading() {
    if (!tradingSettings?.is_running && tracks.length === 0) {
      setError('请先选择至少一个投资赛道')
      return
    }
    void persistTrading({
      is_running: !tradingSettings?.is_running,
      tracks,
      gate_enabled: tradingSettings?.gate_enabled ?? true,
    }).then(() => reload())
  }

  const tracking = signals.filter((signal) => !signal.settled).length
  const livePnl = signals
    .filter((signal) => !signal.settled)
    .reduce((sum, signal) => sum + (signal.current_pnl_usdt ?? 0), 0)
  const gateOn = tradingSettings?.gate_enabled ?? true
  const newsOn = newsSettings?.enabled !== false

  return (
    <div className="flex h-full min-h-0 flex-col gap-2 overflow-hidden">
      <div className="surface-card shrink-0 space-y-2 p-2.5">
        <div className="flex flex-wrap items-center gap-2">
          <div className="min-w-[160px]">
            <div className="font-mono text-[10px] uppercase tracking-widest text-muted-foreground">当前自动模拟策略</div>
            <div className="mt-0.5 text-sm text-foreground">{currentStrategyName} · v{currentVersion ?? '—'}</div>
          </div>
          <select
            value={currentStrategyId ?? ''}
            disabled={tradingSettings?.is_running}
            onChange={(event) => void changeStrategy(Number(event.target.value))}
            className="rounded border border-border bg-card px-2 py-1.5 font-mono text-xs text-foreground disabled:cursor-not-allowed disabled:opacity-60"
          >
            {strategies.map((strategy) => (
              <option key={strategy.id} value={strategy.id}>{strategy.name} · v{strategy.latest_version?.version ?? 0}</option>
            ))}
          </select>
          <div className="flex items-center gap-1">
            {TRACKS.map(([track, label]) => (
              <button
                key={track}
                type="button"
                disabled={changingTrading}
                onClick={() => toggleTrack(track)}
                className={`rounded border px-2 py-1 font-mono text-[10px] disabled:cursor-wait disabled:opacity-70 ${tracks.includes(track) ? 'border-primary bg-primary/15 text-primary' : 'border-border text-muted-foreground'}`}
              >
                {label}
              </button>
            ))}
          </div>
          <button
            type="button"
            disabled={changingTrading}
            onClick={() => void toggleTrading()}
            className={`rounded border px-3 py-1.5 font-mono text-xs disabled:cursor-wait disabled:opacity-60 ${tradingSettings?.is_running ? 'border-short/50 bg-short/10 text-short' : 'border-primary bg-primary text-primary-foreground'}`}
          >
            {changingTrading ? '状态切换中…' : tradingSettings?.is_running ? '停止模拟操盘' : '开始模拟操盘'}
          </button>
          <button
            type="button"
            disabled={changingTrading || !tradingSettings}
            onClick={toggleGate}
            className={`rounded border px-3 py-1.5 font-mono text-xs disabled:cursor-wait disabled:opacity-60 ${gateOn ? 'border-hold/50 bg-hold/10 text-hold' : 'border-long/50 bg-long/10 text-long'}`}
          >
            {gateOn ? '闸门开启 · 严格' : '闸门关闭 · 宽松'}
          </button>
          <button
            type="button"
            disabled={!newsSettings || changingNews}
            onClick={toggleNewsMaster}
            className={`rounded border px-3 py-1.5 font-mono text-xs disabled:cursor-wait disabled:opacity-60 ${newsOn ? 'border-long/50 bg-long/10 text-long' : 'border-short/50 bg-short/10 text-short'}`}
          >
            {newsOn ? '新闻开启' : '新闻关闭'}
          </button>
          <div className="flex items-center gap-1">
            {NEWS_SOURCES.map(([key, label]) => (
              <button
                key={key}
                type="button"
                disabled={!newsSettings || changingNews || !newsOn}
                onClick={() => toggleNewsSource(key)}
                className={`rounded border px-2 py-1 font-mono text-[10px] disabled:opacity-50 ${newsSettings?.sources[key] ? 'border-primary bg-primary/15 text-primary' : 'border-border text-muted-foreground'}`}
              >
                {label}
              </button>
            ))}
          </div>
          <label className="flex items-center gap-1 font-mono text-[10px] text-muted-foreground">
            每日
            <input
              type="number"
              min={1}
              max={2000}
              defaultValue={newsSettings?.daily_target ?? 300}
              key={newsSettings?.daily_target ?? 300}
              onBlur={(event) => void changeDailyTarget(Number(event.target.value) || 300)}
              className="w-16 rounded border border-border bg-card px-1 py-0.5 text-foreground"
            />
            <span>{newsSettings ? `${newsSettings.today_count}/${newsSettings.daily_target}` : '—'}</span>
          </label>
          <span className={`rounded border px-2 py-1 font-mono text-[10px] ${tradingSettings?.is_running ? 'border-long/40 bg-long/10 text-long' : 'border-border text-muted-foreground'}`}>
            {tradingSettings?.is_running ? 'AUTO PAPER ON' : 'AUTO PAPER OFF'}
          </span>
          <span className="font-mono text-xs text-muted-foreground">持仓 {tracking}</span>
          <span className="font-mono text-xs text-foreground">权益 {account?.current_equity_usdt?.toFixed(2) ?? '—'} USDT</span>
          <span className={`font-mono text-xs ${(account?.realized_pnl_usdt ?? 0) >= 0 ? 'text-long' : 'text-short'}`}>已实现 {(account?.realized_pnl_usdt ?? 0) >= 0 ? '+' : ''}{account?.realized_pnl_usdt.toFixed(2) ?? '—'}</span>
          <span className={`font-mono text-xs ${livePnl >= 0 ? 'text-long' : 'text-short'}`}>浮盈亏 {account?.unrealized_pnl_usdt == null ? '待行情' : `${livePnl >= 0 ? '+' : ''}${livePnl.toFixed(2)} USDT`}</span>
          <span className="font-mono text-xs text-muted-foreground">可用 {account?.available_equity_usdt?.toFixed(2) ?? '—'} · 占用 {account?.used_margin_usdt.toFixed(2) ?? '—'}</span>
          <div className="ml-auto flex gap-2">
            <a href={`${API_BASE}/export/signals`} className="rounded border border-border px-2 py-1.5 text-xs text-foreground">下载八列数据</a>
            <a href="http://127.0.0.1:8501" target="_blank" rel="noreferrer" className="rounded border border-primary/40 bg-primary/10 px-2 py-1.5 text-xs text-primary">Streamlit :8501</a>
          </div>
        </div>
      </div>

      {error && (
        <button type="button" onClick={() => void reload()} className="shrink-0 rounded border border-hold/40 bg-hold/10 px-3 py-1.5 text-left text-xs text-hold">
          后端暂时不可用：{error}。点击重试，现有数据已保留。
        </button>
      )}

      <div className="thin-scroll min-h-0 flex-1 overflow-y-auto pr-0.5">
        <div className="flex min-h-full flex-col gap-2">
          {tradingSettings?.is_running && (
            <div className="thin-scroll grid shrink-0 grid-cols-1 gap-2 overflow-x-auto xl:grid-cols-4">
              <div className="surface-card min-w-[220px] p-2.5">
                <div className="flex items-center justify-between">
                  <h3 className="font-mono text-[10px] uppercase tracking-widest text-muted-foreground">实时交易对</h3>
                  <span className={`font-mono text-[10px] ${market?.market_status === 'ok' ? 'text-long' : 'text-hold'}`}>{market?.market_status?.toUpperCase() ?? 'LOADING'}</span>
                </div>
                <div className="thin-scroll mt-2 max-h-24 space-y-1 overflow-y-auto">
                  {(market?.pairs ?? []).map((pair) => (
                    <div key={pair.symbol} className="flex items-center justify-between rounded border border-border bg-secondary/40 px-2 py-1 font-mono text-[10px]">
                      <span className="text-foreground">{pair.symbol}</span>
                      <span>{pair.price == null ? '待行情' : pair.price.toLocaleString(undefined, { maximumFractionDigits: 4 })}</span>
                      <span className={pair.status === 'LIVE' ? 'text-long' : 'text-short'}>{pair.status}</span>
                    </div>
                  ))}
                </div>
              </div>
              <div className="surface-card min-w-[220px] p-2.5">
                <h3 className="font-mono text-[10px] uppercase tracking-widest text-muted-foreground">策略实时反馈</h3>
                <div className="mt-1 font-mono text-xs text-foreground">{strategyFeedback?.message ?? '正在初始化运行状态'}</div>
                <div className="mt-1 flex gap-3 font-mono text-[10px] text-muted-foreground">
                  <span>检测 {strategyFeedback?.evaluated ?? 0}</span>
                  <span className="text-long">通过 {strategyFeedback?.passed ?? 0}</span>
                  <span className="text-short">拒绝 {strategyFeedback?.rejected ?? 0}</span>
                </div>
              </div>
              <div className="surface-card min-w-[220px] p-2.5">
                <h3 className="font-mono text-[10px] uppercase tracking-widest text-muted-foreground">LLM 准确与盈利</h3>
                <div className="mt-1 grid grid-cols-2 gap-1 font-mono text-[11px]">
                  <span>准确率 <b>{llmPerformance?.accuracy_pct == null ? '样本不足' : `${llmPerformance.accuracy_pct.toFixed(1)}%`}</b></span>
                  <span>结算 <b>{llmPerformance?.settled ?? 0}</b></span>
                </div>
              </div>
              <div className="surface-card min-w-[220px] p-2.5">
                <h3 className="font-mono text-[10px] uppercase tracking-widest text-muted-foreground">快速评测</h3>
                <div className="mt-1 grid grid-cols-2 gap-1 font-mono text-[11px]">
                  <span>胜率 <b>{quickSim?.overall.winrate_pct == null ? '—' : `${quickSim.overall.winrate_pct.toFixed(1)}%`}</b></span>
                  <span>结算 <b>{quickSim?.overall.settled ?? 0}</b></span>
                </div>
              </div>
            </div>
          )}

          <div className="shrink-0"><ReplayStats refreshKey={refreshKey} /></div>

          <div className="grid min-h-[640px] flex-1 grid-cols-1 gap-2 xl:grid-cols-[minmax(260px,300px)_minmax(0,1fr)_minmax(300px,360px)]">
            <div className="surface-card flex min-h-[420px] flex-col overflow-hidden p-2.5">
              <ReplayList signals={signals} loading={loading} selectedId={selected?.id ?? null} onSelect={setSelected} onReload={reload} />
            </div>

            <div className="flex min-h-[520px] flex-col overflow-hidden">
              <div className="mb-1 flex items-center gap-2">
                <h3 className="font-mono text-xs uppercase tracking-widest text-muted-foreground">模拟盘 K 线</h3>
                <label className="ml-auto flex cursor-pointer items-center gap-1 font-mono text-[10px] text-muted-foreground">
                  <input type="checkbox" checked={autoRefresh} onChange={(event) => setAutoRefresh(event.target.checked)} className="h-3 w-3 accent-primary" />
                  <span>每 3s 实时刷新</span>
                </label>
              </div>
              <div className="min-h-0 flex-1">
                <KlineChart
                  signalId={selected?.id ?? null}
                  asset={selected?.asset ?? ''}
                  action={selected?.action ?? 'HOLD'}
                  entryPrice={selected?.entry_price ?? null}
                  exitPrice={selected?.exit_price ?? null}
                />
              </div>
            </div>

            <div className="min-h-[420px] overflow-hidden"><SignalDetail signal={selected} /></div>
          </div>
        </div>
      </div>
    </div>
  )
}
