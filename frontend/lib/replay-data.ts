import { API_BASE } from '@/lib/api'

// Replay data types — simulated/paper-trading view of EXECUTED signals

export type SimSignal = {
  id: number
  news_id: number
  news_time: string
  source: string
  news_text: string
  asset: string
  action: 'BUY' | 'SELL' | 'HOLD'
  score: number
  reasoning: string | null
  reasoning_path: string | null
  market_category: string
  event_strength: string | null
  direct_catalyst: number | null
  prediction_type: string | null
  market_confirmation: string | null
  event_phase: string | null
  expected_horizon: string | null
  invalidation_condition: string | null
  decision_context: string | null
  entry_price: number | null
  exit_price: number | null
  exit_time: string | null
  exit_reason: string | null
  max_price: number | null
  min_price: number | null
  max_price_time: number | null
  min_price_time: number | null
  entry_time: string | null
  is_correct: 'WIN' | 'LOSS' | 'HOLD' | 'BREAKEVEN' | '' | null
  settled: number
  mfe_pct: number | null
  mae_pct: number | null
  forward_pnl: number | null
  mfe_time_mins: number | null
  analysis_type: 'conflict' | 'trend' | string | null
  bullish_probability: number | null
  bearish_probability: number | null
  uncertainty: number | null
  bullish_force: number | null
  bearish_force: number | null
  impact_horizon: 'short' | 'medium' | 'long' | string | null
  impact_window: Record<string, unknown> | string | null
  entry_zone: string | null
  take_profit_pct: number | null
  stop_loss_pct: number | null
  exit_policy: string | null
  dual_side_candidate: number | null
  extra_models_consensus: Record<string, unknown> | string | null
  doubao_action: 'BUY' | 'SELL' | 'HOLD' | null
  doubao_reasoning: string | null
  cluster_size: number | null
  timeframe_match: string | null
  created_at: string | null
  strategy_id: number | null
  strategy_version_id: number | null
  strategy_name: string | null
  strategy_version: number | null
  strategy_params: Record<string, number | string | boolean> | null
  quality_status?: string | null
  decision_eligible?: boolean
  recorded_is_correct?: 'WIN' | 'LOSS' | 'HOLD' | 'BREAKEVEN' | '' | null
  tracking_quality?: 'SETTLED' | 'TRACKABLE' | 'RESEARCH_EXCLUDED' | 'LEGACY_UNTRACKABLE' | string
  current_price?: number | null
  current_pnl_pct?: number | null
  current_pnl_usdt?: number | null
  notional_usdt?: number | null
  leverage?: number | null
  margin_usdt?: number | null
  gross_pnl_usdt?: number | null
  fees_usdt?: number | null
  slippage_usdt?: number | null
  hold_minutes?: number | null
  live_mfe_pct?: number
  live_mae_pct?: number
  pricing_status?: 'LIVE' | 'UNAVAILABLE'
  paper_status?: 'TRACKING' | 'SETTLED' | 'RESEARCH_EXCLUDED' | 'LEGACY_UNTRACKABLE' | string
}

export type PaperTrack = 'crypto' | 'gold' | 'oil'

export type PaperTradingSettings = {
  is_running: boolean
  tracks: PaperTrack[]
  gate_enabled?: boolean
  gate_locked?: boolean
  started_at: string | null
  active_run_id: number | null
  active_run: null | {
    id: number
    strategy_id: number
    strategy_version_id: number
    strategy_name: string
    strategy_version: number
    agent_version: string
    model_id: string
    spec_version: string
    activation_reason: string
    status: 'RUNNING' | 'STOPPED'
  }
  updated_at: string | null
}

export type QuickSimProfitability = {
  settled: number
  wins: number
  losses: number
  breakeven: number
  winrate_pct: number | null
  avg_pnl_pct: number | null
  total_pnl_pct: number
  total_pnl_usdt: number
  return_on_notional_pct: number | null
  profitable: boolean | null
}

export type QuickSimEvaluation = {
  enabled: boolean
  horizon_minutes: number
  notional_usdt: number
  open_trades: number
  overall: QuickSimProfitability
  gate_passed: QuickSimProfitability
  gate_rejected: QuickSimProfitability
  by_asset: Array<QuickSimProfitability & { asset: string }>
  open: Array<{
    id: number
    decision_id: number
    asset: string
    action: 'BUY' | 'SELL'
    entry_price: number
    entry_ts: number
    horizon_minutes: number
    gate_passed: number
    gate_reason: string
  }>
  recent: Array<{
    id: number
    decision_id: number
    asset: string
    action: 'BUY' | 'SELL'
    entry_price: number
    exit_price: number | null
    pnl_pct: number | null
    pnl_usdt: number | null
    verdict: string
    gate_passed: number
    gate_reason: string
    entry_ts: number
    exit_ts: number | null
    settled: number
  }>
  method: string
}

export type ReplayPositions = {
  current_strategy: {
    id: number
    name: string
    active_version: {
      id: number
      version: number
      params: Record<string, number | string | boolean>
    }
  }
  trading_settings: PaperTradingSettings
  news_settings?: NewsSourceSettings
  pairs: Array<{
    asset: string
    symbol: string
    track: PaperTrack
    price: number | null
    change24h: number | null
    source: string
    source_count: number
    status: 'LIVE' | 'OBSERVATION_ONLY' | 'UNAVAILABLE'
    quality_status: string
    quality_reason: string
    decision_eligible: boolean
  }>
  market_status: 'ok' | 'partial' | 'unavailable'
  market_sources: Record<string, { status: string; assets: string[]; error: string }>
  strategy_feedback: {
    stage: 'STOPPED' | 'WAITING_NEWS' | 'SIGNAL_REJECTED' | 'POSITION_OPEN'
    message: string
    evaluated: number
    passed: number
    rejected: number
    latest: Array<{
      id: number
      target_asset: string
      suggested_action: string
      evidence_confidence: number | null
      evidence_action: string
      trade_gate_reason: string
      entry_price: number | null
      created_at: string
    }>
  }
  llm_performance: {
    model_id: string
    settled: number
    wins: number
    losses: number
    holds: number
    accuracy_pct: number | null
    avg_pnl_pct: number | null
    total_pnl_pct: number
    profitable: boolean | null
    verdict: string
    method: string
  }
  quick_sim: QuickSimEvaluation
  positions: SimSignal[]
  account: {
    initial_equity_usdt: number
    realized_pnl_usdt: number
    unrealized_pnl_usdt: number | null
    current_equity_usdt: number | null
    used_margin_usdt: number
    available_equity_usdt: number | null
    unpriced_positions: number
    fees_usdt?: number
    slippage_usdt?: number
    trade_count?: number
    research_positions_excluded?: number
    legacy_untrackable_excluded?: number
  }
  updated_at_ms: number
  pricing_note: string
}

export type ReplayKlineBar = {
  time: number
  open: number
  high: number
  low: number
  close: number
  volume: number
}

export type ReplayKlineMarker = {
  time: number
  position: 'aboveBar' | 'belowBar' | 'inBar'
  color: string
  shape: 'arrowUp' | 'arrowDown' | 'circle' | 'square'
  text: string
  kind: 'entry' | 'exit'
}

export type ReplayKlineResponse = {
  signal_id: number
  asset: string
  action?: 'BUY' | 'SELL' | 'HOLD'
  entry_price: number
  entry_time: number
  exit_price?: number | null
  stop_loss?: number | null
  take_profit?: number | null
  trailing_callback_rate?: number
  invalidation_condition?: string
  settled?: number
  is_paper_trading: boolean
  markers?: ReplayKlineMarker[]
  klines: ReplayKlineBar[]
}

export type NewsSourceKey = 'financialjuice' | 'tree_news' | 'techflow' | 'eastmoney' | 'blockbeats' | 'jin10'

export type NewsSourceSettings = {
  enabled: boolean
  daily_target: number
  today_count: number
  remaining: number
  sources: Record<NewsSourceKey, boolean>
}

export type ReplayStatsSummary = {
    total: number
    tracking: number
    legacy_untrackable?: number
    settled: number
    wins: number
    losses: number
    holds: number
    avg_pnl: number | null
    avg_mfe: number | null
    avg_mae: number | null
    best_trade: number | null
    worst_trade: number | null
    winrate: number
}

export type ReplayStats = {
  overall: ReplayStatsSummary
  strategy_id: number
  strategy_version_id: number
  by_asset: Array<{ asset: string; total: number; wins: number; losses: number; avg_pnl: number | null }>
  by_action: Array<{ action: string; total: number; wins: number; avg_pnl: number | null }>
  research_excluded?: ReplayStatsSummary
  is_paper_trading: boolean
}

export type ReplayReflection = {
  sample: number
  wins: number
  losses: number
  winrate: number
  by_analysis_type: Array<{ key: string; sample: number; wins: number; losses: number; winrate: number; avg_forward_pnl: number | null }>
  by_horizon: Array<{ key: string; sample: number; wins: number; losses: number; winrate: number; avg_forward_pnl: number | null }>
  by_asset: Array<{ key: string; sample: number; wins: number; losses: number; winrate: number; avg_forward_pnl: number | null }>
  failure_patterns: Record<string, number>
  failure_diagnostics: Array<{
    key: string
    label: string
    count: number
    share: number | null
    assessment: 'observed' | 'candidate' | 'not_observed' | 'insufficient_sample' | 'not_assessable' | string
    evidence: string[]
    sample_signal_ids: number[]
  }>
  observable_loss_coverage: number
  recommendations: string[]
  research_excluded?: { sample: number; wins: number; losses: number }
  method: string
}

export async function fetchReplaySignals(params?: {
  asset?: string
  action?: string
  settled?: number
  limit?: number
}): Promise<SimSignal[]> {
  const sp = new URLSearchParams()
  if (params?.asset) sp.set('asset', params.asset)
  if (params?.action) sp.set('action', params.action)
  if (params?.settled !== undefined) sp.set('settled', String(params.settled))
  if (params?.limit) sp.set('limit', String(params.limit))
  const url = `${API_BASE}/replay/signals?${sp.toString()}`
  const r = await fetch(url)
  if (!r.ok) throw new Error(`fetchReplaySignals ${r.status}`)
  return (await r.json()) as SimSignal[]
}

export async function fetchReplayPositions(): Promise<ReplayPositions> {
  const r = await fetch(`${API_BASE}/replay/positions`, { cache: 'no-store' })
  if (!r.ok) throw new Error(`fetchReplayPositions ${r.status}`)
  return (await r.json()) as ReplayPositions
}

function asTradingSettings(payload: Record<string, unknown>): PaperTradingSettings {
  return {
    is_running: Boolean(payload.is_running),
    tracks: Array.isArray(payload.tracks) ? payload.tracks as PaperTrack[] : [],
    gate_enabled: payload.gate_enabled === undefined ? undefined : Boolean(payload.gate_enabled),
    gate_locked: payload.gate_locked === undefined ? undefined : Boolean(payload.gate_locked),
    started_at: (payload.started_at as string | null) ?? null,
    active_run_id: (payload.active_run_id as number | null) ?? null,
    active_run: (payload.active_run as PaperTradingSettings['active_run']) ?? null,
    updated_at: (payload.updated_at as string | null) ?? null,
  }
}

function asNewsSettings(payload: Record<string, unknown> | null | undefined): NewsSourceSettings | null {
  if (!payload) return null
  const nested = payload.news_settings
  const source = nested && typeof nested === 'object' ? nested as Record<string, unknown> : payload
  if (typeof source.enabled !== 'boolean' || !source.sources || typeof source.sources !== 'object') {
    return null
  }
  return source as unknown as NewsSourceSettings
}

export async function updatePaperTrading(settings: Pick<PaperTradingSettings, 'is_running' | 'tracks'> & { gate_enabled?: boolean }): Promise<PaperTradingSettings> {
  const r = await fetch(`${API_BASE}/replay/settings`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(settings),
  })
  if (!r.ok) throw new Error((await r.text()) || `updatePaperTrading ${r.status}`)
  const payload = await r.json() as Record<string, unknown>
  const saved = asTradingSettings(payload)
  if (settings.gate_enabled !== undefined && saved.gate_enabled === undefined) {
    saved.gate_enabled = settings.gate_enabled
  }
  return saved
}

export async function fetchReplayStats(strategyId?: number, versionId?: number): Promise<ReplayStats> {
  const sp = new URLSearchParams()
  if (strategyId) sp.set('strategy_id', String(strategyId))
  if (versionId) sp.set('version_id', String(versionId))
  const r = await fetch(`${API_BASE}/replay/stats?${sp.toString()}`, { cache: 'no-store' })
  if (!r.ok) throw new Error(`fetchReplayStats ${r.status}`)
  return (await r.json()) as ReplayStats
}

export async function fetchReplayReflection(limit = 500): Promise<ReplayReflection> {
  const r = await fetch(`${API_BASE}/replay/reflection?limit=${Math.min(Math.max(limit, 20), 500)}`, { cache: 'no-store' })
  if (!r.ok) throw new Error(`fetchReplayReflection ${r.status}`)
  return (await r.json()) as ReplayReflection
}

const DEFAULT_NEWS_SETTINGS: NewsSourceSettings = {
  enabled: true,
  daily_target: 300,
  today_count: 0,
  remaining: 300,
  sources: {
    financialjuice: true,
    tree_news: true,
    techflow: true,
    eastmoney: true,
    blockbeats: true,
    jin10: true,
  },
}

export async function fetchNewsSourceSettings(): Promise<NewsSourceSettings> {
  const replay = await fetch(`${API_BASE}/replay/settings`, { cache: 'no-store' })
  if (replay.ok) {
    const mapped = asNewsSettings(await replay.json() as Record<string, unknown>)
    if (mapped) return mapped
  }
  return DEFAULT_NEWS_SETTINGS
}

export async function updateNewsSourceSettings(body: {
  enabled?: boolean
  daily_target?: number
  sources?: Partial<Record<NewsSourceKey, boolean>>
}): Promise<NewsSourceSettings> {
  const r = await fetch(`${API_BASE}/replay/settings`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      news_enabled: body.enabled,
      news_daily_target: body.daily_target,
      news_sources: body.sources,
    }),
  })
  if (!r.ok) throw new Error((await r.text()) || `updateNewsSourceSettings ${r.status}`)
  return asNewsSettings(await r.json() as Record<string, unknown>) ?? DEFAULT_NEWS_SETTINGS
}

export async function fetchReplaySignalKline(
  signalId: number
): Promise<ReplayKlineResponse> {
  const r = await fetch(`${API_BASE}/replay/signal/${signalId}/kline`)
  if (!r.ok) throw new Error(`fetchReplaySignalKline ${r.status}`)
  return (await r.json()) as ReplayKlineResponse
}

export function fmtPct(v: number | null | undefined, digits = 2): string {
  if (v === null || v === undefined || Number.isNaN(v)) return '—'
  const sign = v > 0 ? '+' : ''
  return `${sign}${v.toFixed(digits)}%`
}

export function fmtPrice(v: number | null | undefined, asset = ''): string {
  if (v === null || v === undefined || Number.isNaN(v)) return '—'
  const upper = asset.toUpperCase()
  const decimals = upper === 'BTC' || upper === 'ETH' ? 2 : upper === 'XAU' || upper === 'GOLD' ? 2 : 4
  return v.toLocaleString('en-US', { minimumFractionDigits: decimals, maximumFractionDigits: decimals })
}

export function fmtTime(iso: string | null | undefined): string {
  if (!iso) return '—'
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return iso
  return d.toLocaleString('zh-CN', {
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
  })
}

export function actionColor(action: string): string {
  switch (action.toUpperCase()) {
    case 'BUY':
      return 'text-long border-long/40 bg-long/10'
    case 'SELL':
      return 'text-short border-short/40 bg-short/10'
    default:
      return 'text-muted-foreground border-border bg-secondary'
  }
}

export function verdictColor(verdict: string | null | undefined): string {
  switch ((verdict || '').toUpperCase()) {
    case 'WIN':
      return 'text-long border-long/50 bg-long/15'
    case 'LOSS':
      return 'text-short border-short/50 bg-short/15'
    case 'HOLD':
    case 'BREAKEVEN':
      return 'text-hold border-hold/50 bg-hold/15'
    default:
      return 'text-muted-foreground border-border bg-secondary'
  }
}
