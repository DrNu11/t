import { apiGet, apiSend } from '@/lib/api'

export type StrategyParams = {
  signal_threshold: number
  notional_usdt: number
  leverage: number
  trailing_callback_rate: number
  holding_horizon_minutes: number
  min_event_strength: string
  asset_filter: string
  require_direct_catalyst: boolean
}

export type StrategyVersion = {
  id: number
  version: number
  params: StrategyParams
  source: string
  note: string
  created_at: string
}

export type Strategy = {
  id: number
  slug: string
  name: string
  description: string
  status: string
  created_at: string
  updated_at: string
  latest_version: StrategyVersion | null
  versions?: StrategyVersion[]
}

export type BacktestReport = {
  data_source: string
  honest: boolean
  note: string
  params: StrategyParams
  sample_size: number
  taken: number
  skipped: number
  wins: number
  losses: number
  flats: number
  winrate: number | null
  total_pnl_usdt: number
  avg_pnl_usdt: number
  avg_win_usdt: number
  avg_loss_usdt: number
  profit_factor: number | null
  max_drawdown_usdt: number
  insufficient_sample: boolean
  by_asset: Array<{ asset: string; taken: number; wins: number; pnl_usdt: number; winrate: number | null }>
  equity_curve: Array<{ t: string; equity: number; id: number }>
  trades: Array<{
    id: number
    asset: string
    action: string
    entry_time: string
    entry_price: number
    exit_price: number | null
    sentiment_score: number
    forward_pnl_pct: number
    pnl_usdt: number
    is_correct: string
    equity_usdt: number
  }>
}

export type BacktestRun = {
  run_id: number | null
  strategy: { id: number; name: string; slug: string }
  version: { id: number; version: number }
  report: BacktestReport
}

export type DashboardOverview = {
  health: { status: string; db_exists: boolean; sse_clients: number }
  replay: {
    overall: {
      total: number
      wins: number
      losses: number
      winrate: number
      avg_pnl: number | null
    }
    is_paper_trading: boolean
  }
  recent_signals: Array<{
    id: number
    asset: string
    action: string
    news_text: string
    is_correct: string | null
    forward_pnl: number | null
    entry_price: number | null
    entry_time: string | null
  }>
  strategies: Strategy[]
  recent_runs: Array<{
    id: number
    strategy_id: number
    winrate: number | null
    total_pnl_usdt: number | null
    taken: number
    created_at: string
  }>
  streamlit: { url: string; source: string }
  exports: { signals_xlsx: string }
}

export function fetchStrategies() {
  return apiGet<Strategy[]>('/strategies')
}

export function activateStrategy(id: number, versionId?: number) {
  return apiSend<Strategy & { active_version: StrategyVersion }>(`/strategies/${id}/activate`, 'POST', {
    version_id: versionId,
  })
}

export function fetchStrategy(id: number) {
  return apiGet<Strategy>(`/strategies/${id}`)
}

export function createStrategy(payload: { name: string; description: string; params?: Partial<StrategyParams> }) {
  return apiSend<Strategy>('/strategies', 'POST', payload)
}

export function addStrategyVersion(id: number, params: StrategyParams, note: string) {
  return apiSend<StrategyVersion>(`/strategies/${id}/versions`, 'POST', { params, note, source: 'manual' })
}

export function runBacktest(payload: { strategy_id: number; version_id?: number; asset?: string; persist?: boolean }) {
  return apiSend<BacktestRun>('/backtest/run', 'POST', payload)
}

export function optimizeStrategy(strategyId: number, useLlm = true) {
  return apiSend<{
    mode: string
    note: string
    version: StrategyVersion
    baseline_report: {
      sample_size: number
      taken: number
      winrate: number | null
      total_pnl_usdt: number
      max_drawdown_usdt: number
      insufficient_sample: boolean
    }
  }>('/backtest/optimize', 'POST', { strategy_id: strategyId, use_llm: useLlm })
}

export function fetchDashboardOverview() {
  return apiGet<DashboardOverview>('/dashboard/overview')
}

export function fmtUsd(value: number | null | undefined) {
  if (value === null || value === undefined || Number.isNaN(value)) return '—'
  const sign = value > 0 ? '+' : ''
  return `${sign}${value.toFixed(2)} USDT`
}

export function fmtRate(value: number | null | undefined) {
  if (value === null || value === undefined || Number.isNaN(value)) return '—'
  return `${(value * 100).toFixed(1)}%`
}
