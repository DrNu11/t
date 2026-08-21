import { API_BASE } from '@/lib/api'

export type InternalNews = {
  id: number
  timestamp: string
  source: string
  news_text: string
  analysis_status: string
  action: 'BUY' | 'SELL' | 'HOLD'
  score: number
}

export type TechFlowItem = {
  id: string
  title: string
  summary: string
  url: string
  published_at: string
  source: 'TechFlow 深潮'
}

export type Factor = { score: number; explanation: string }
export type EvidenceItem = { evidence_score: number; raw_score: number; source: string }
export type Significance = {
  chi_square: number
  p_value: number
  sample_size: number
  wins: number
  losses: number
  significant: boolean
  sufficient_sample: boolean
  note: string
  scope?: 'asset' | 'lane' | 'market'
}
export type Contradiction = { type: string; detail: string }
export type ConfidenceDetail = {
  confidence: number
  base: number
  significance_weight: number
  contradiction_penalty: number
  gate: number
  passed_gate: boolean
}
export type NewsAnalysis = {
  news: { id: number; source: string; content: string; published_at: string }
  decision: null | {
    decision_id: number
    created_at: string
    sentiment_score: number
    suggested_action: 'BUY' | 'SELL' | 'HOLD'
    reasoning: string
    reasoning_path: string
    market_category: string
    target_asset: string
    market_confirmation: string
  }
  analysis: {
    factors: Record<string, Factor>
    weights: Record<string, number>
    final_score: number
    raw_action: 'BUY' | 'SELL' | 'HOLD'
    action: 'BUY' | 'SELL' | 'HOLD'
    confidence: number
    evidence: Record<string, EvidenceItem>
    significance: Significance
    contradictions: Contradiction[]
    confidence_detail: ConfidenceDetail
    verdict: string
  }
  strategy: {
    action: string
    raw_action: string
    confidence: number
    passed_gate: boolean
    verdict: string
    note: string
  }
}

export type StrategyAdvice = {
  mode: 'llm' | 'rules'
  applied: false
  current_values: Record<string, number>
  advice: {
    signal_threshold: number
    notional_multiplier: number
    trailing_callback_rate: number
    holding_horizon_minutes: number
    reason: string
    risk_notes: string
    rollback_condition: string
  }
  bounds: Record<string, { min: number; max: number }>
  validation: {
    confidence: number
    significance: Significance
    contradictions: Contradiction[]
    gated_action: 'BUY' | 'SELL' | 'HOLD'
    verdict: string
  }
}

async function json<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`, init)
  if (!response.ok) throw new Error(`${path} ${response.status}`)
  return response.json() as Promise<T>
}

export const fetchInternalNews = () => json<InternalNews[]>('/events')
export const fetchTechFlowNews = () => json<{ status: string; items: TechFlowItem[]; error: string }>('/news/techflow')
export const fetchNewsAnalysis = (id: number) => json<NewsAnalysis>(`/news/${id}/analysis`)
export const fetchStrategyAdvice = (id: number) => json<StrategyAdvice>(`/news/${id}/strategy-advice`, { method: 'POST' })
