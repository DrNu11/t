'use client'

import { useEffect, useState } from 'react'
import { Search, Settings, Triangle } from 'lucide-react'
import { ThemeToggle } from '@/components/theme-toggle'
import { API_BASE } from '@/lib/api'

type AIModel = {
  id: string
  label: string
}

type AIModelsResponse = {
  models: AIModel[]
  selected: string
}

export function DashboardHeader() {
  const [models, setModels] = useState<AIModel[]>([])
  const [selected, setSelected] = useState('')
  const [saving, setSaving] = useState(false)
  const [status, setStatus] = useState('加载模型列表')

  useEffect(() => {
    let active = true

    fetch(`${API_BASE}/ai/models`)
      .then(async (response) => {
        if (!response.ok) throw new Error('加载失败')
        return response.json() as Promise<AIModelsResponse>
      })
      .then((data) => {
        if (!active) return
        setModels(data.models)
        setSelected(data.selected)
        setStatus('选择 AI 模型')
      })
      .catch(() => {
        if (active) setStatus('模型列表加载失败')
      })

    return () => {
      active = false
    }
  }, [])

  async function handleModelChange(modelId: string) {
    const previous = selected
    setSelected(modelId)
    setSaving(true)
    setStatus('正在切换模型')

    try {
      const response = await fetch(`${API_BASE}/ai/models`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ model_id: modelId }),
      })
      if (!response.ok) throw new Error('保存失败')
      const data = await response.json() as AIModelsResponse
      setModels(data.models)
      setSelected(data.selected)
      setStatus('模型已切换')
    } catch {
      setSelected(previous)
      setStatus('模型切换失败，已回滚')
    } finally {
      setSaving(false)
    }
  }

  return (
    <header className="top-nav sticky top-0 z-20 flex h-16 items-center justify-between px-3 md:px-6">
      <div className="flex shrink-0 items-center gap-2 md:gap-3">
        <div className="flex h-9 w-9 items-center justify-center rounded-lg border border-primary/25 bg-primary/10 shadow-sm">
          <Triangle className="h-5 w-5 fill-primary text-primary" aria-hidden="true" />
        </div>
        <div className="hidden leading-tight sm:block">
          <h1 className="font-mono text-lg font-bold tracking-tight text-foreground">
            Trident
          </h1>
          <p className="text-[10px] uppercase tracking-[0.2em] text-muted-foreground">
            量化交易平台
          </p>
        </div>
      </div>

      <div className="hidden items-center gap-2 rounded-lg border border-border bg-card px-3 py-1.5 shadow-sm lg:flex">
        <Search className="h-3.5 w-3.5 text-muted-foreground" aria-hidden="true" />
        <span className="font-mono text-xs text-muted-foreground">
          BTC / USDT · Perpetual
        </span>
      </div>

      <div className="flex min-w-0 items-center gap-1.5 sm:gap-2 md:gap-3">
        <select
          value={selected}
          onChange={(event) => void handleModelChange(event.target.value)}
          disabled={saving || models.length === 0}
          aria-label="选择 AI 模型"
          title={status}
          className="h-9 min-w-0 max-w-[8.5rem] truncate rounded-lg border border-border bg-card px-2 font-mono text-[11px] text-foreground shadow-sm transition-colors hover:border-primary/40 disabled:cursor-wait disabled:opacity-60 sm:max-w-[15rem] md:text-xs"
        >
          {models.length === 0 ? <option value="">模型</option> : null}
          {models.map((model) => (
            <option key={model.id} value={model.id}>{model.label}</option>
          ))}
        </select>
        <ThemeToggle />
        <button
          type="button"
          className="flex h-9 w-9 shrink-0 items-center justify-center rounded-lg border border-border bg-card text-muted-foreground shadow-sm transition-colors hover:border-primary/40 hover:bg-accent hover:text-foreground"
          aria-label="设置"
        >
          <Settings className="h-4 w-4" aria-hidden="true" />
        </button>
      </div>
    </header>
  )
}
