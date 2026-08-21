'use client'

import { useEffect, useState } from 'react'
import {
  addStrategyVersion,
  createStrategy,
  fetchStrategies,
  fetchStrategy,
  optimizeStrategy,
  type Strategy,
  type StrategyParams,
} from '@/lib/strategy-data'

const EMPTY: StrategyParams = {
  signal_threshold: 0.5,
  notional_usdt: 30,
  leverage: 5,
  trailing_callback_rate: 0.5,
  holding_horizon_minutes: 120,
  min_event_strength: '',
  asset_filter: '',
  require_direct_catalyst: false,
}

export default function StrategiesPage() {
  const [items, setItems] = useState<Strategy[]>([])
  const [selected, setSelected] = useState<Strategy | null>(null)
  const [draft, setDraft] = useState<StrategyParams>(EMPTY)
  const [name, setName] = useState('')
  const [note, setNote] = useState('')
  const [busy, setBusy] = useState(false)
  const [message, setMessage] = useState('')

  async function reload() {
    const data = await fetchStrategies()
    setItems(data)
    setSelected((prev) => data.find((item) => item.id === prev?.id) ?? data[0] ?? null)
  }

  useEffect(() => {
    reload().catch((err) => setMessage(String(err)))
  }, [])

  useEffect(() => {
    if (selected?.latest_version) setDraft(selected.latest_version.params)
  }, [selected])

  async function onCreate() {
    if (!name.trim()) return
    setBusy(true)
    try {
      await createStrategy({ name, description: '手动入库', params: draft })
      setName('')
      setMessage('策略已入库')
      await reload()
    } catch (err) {
      setMessage(String(err))
    } finally {
      setBusy(false)
    }
  }

  async function onSaveVersion() {
    if (!selected) return
    setBusy(true)
    try {
      await addStrategyVersion(selected.id, draft, note || '手动保存参数')
      setNote('')
      setMessage(`已写入 ${selected.name} 新版本`)
      await reload()
    } catch (err) {
      setMessage(String(err))
    } finally {
      setBusy(false)
    }
  }

  async function onOptimize() {
    if (!selected) return
    setBusy(true)
    try {
      const result = await optimizeStrategy(selected.id, true)
      setMessage(`${result.mode === 'llm' ? 'LLM' : '规则'}优化完成：${result.note}`)
      await reload()
    } catch (err) {
      setMessage(String(err))
    } finally {
      setBusy(false)
    }
  }

  return (
    <main className="mx-auto grid w-full max-w-[1600px] min-h-0 flex-1 grid-cols-1 gap-4 overflow-hidden p-4 md:p-6 lg:grid-cols-[320px_1fr]">
      <section className="surface-card flex min-h-[280px] flex-col overflow-hidden">
        <div className="border-b border-border px-4 py-3 font-mono text-[11px] text-muted-foreground">策略库</div>
        <div className="thin-scroll min-h-0 flex-1 overflow-y-auto">
          {items.map((item) => (
            <button
              key={item.id}
              onClick={() => { void fetchStrategy(item.id).then(setSelected) }}
              className={`block w-full border-b border-border px-4 py-3 text-left hover:bg-secondary/60 ${selected?.id === item.id ? 'bg-primary/5' : ''}`}
            >
              <div className="text-sm text-foreground">{item.name}</div>
              <div className="mt-1 font-mono text-[10px] text-muted-foreground">
                v{item.latest_version?.version ?? 0} · {item.slug}
              </div>
            </button>
          ))}
        </div>
        <div className="space-y-2 border-t border-border p-3">
          <input value={name} onChange={(e) => setName(e.target.value)} placeholder="新策略名称" className="w-full rounded border border-border bg-card px-2 py-1.5 text-xs" />
          <button disabled={busy} onClick={() => void onCreate()} className="w-full rounded-lg border border-primary/40 bg-primary/10 px-3 py-2 text-xs text-primary disabled:opacity-50">入库新策略</button>
        </div>
      </section>

      <section className="surface-card thin-scroll min-h-0 overflow-y-auto p-4 md:p-5">
        {selected ? (
          <div className="space-y-4">
            <div>
              <h2 className="text-base text-foreground">{selected.name}</h2>
              <p className="mt-1 text-sm text-muted-foreground">{selected.description}</p>
            </div>
            <div className="grid gap-3 md:grid-cols-2">
              <Field label="信号阈值" value={draft.signal_threshold} step={0.05} onChange={(v) => setDraft({ ...draft, signal_threshold: v })} />
              <Field label="名义本金 USDT" value={draft.notional_usdt} step={5} onChange={(v) => setDraft({ ...draft, notional_usdt: v })} />
              <Field label="杠杆" value={draft.leverage} step={1} onChange={(v) => setDraft({ ...draft, leverage: v })} />
              <Field label="追踪止损 %" value={draft.trailing_callback_rate} step={0.1} onChange={(v) => setDraft({ ...draft, trailing_callback_rate: v })} />
              <Field label="持仓窗口 分钟" value={draft.holding_horizon_minutes} step={15} onChange={(v) => setDraft({ ...draft, holding_horizon_minutes: v })} />
              <label className="text-xs text-muted-foreground">
                最低事件强度
                <select value={draft.min_event_strength} onChange={(e) => setDraft({ ...draft, min_event_strength: e.target.value })} className="mt-1 w-full rounded border border-border bg-card px-2 py-1.5 text-foreground">
                  <option value="">不限</option>
                  <option value="weak">weak</option>
                  <option value="medium">medium</option>
                  <option value="strong">strong</option>
                </select>
              </label>
              <label className="text-xs text-muted-foreground">
                品种过滤
                <select value={draft.asset_filter} onChange={(e) => setDraft({ ...draft, asset_filter: e.target.value })} className="mt-1 w-full rounded border border-border bg-card px-2 py-1.5 text-foreground">
                  <option value="">全部</option>
                  {['BTC', 'ETH', 'SOL', 'XAU', 'WTI'].map((asset) => <option key={asset} value={asset}>{asset}</option>)}
                </select>
              </label>
              <label className="flex items-center gap-2 text-xs text-foreground">
                <input type="checkbox" checked={draft.require_direct_catalyst} onChange={(e) => setDraft({ ...draft, require_direct_catalyst: e.target.checked })} />
                只要直接催化剂
              </label>
            </div>
            <input value={note} onChange={(e) => setNote(e.target.value)} placeholder="版本备注" className="w-full rounded border border-border bg-card px-2 py-1.5 text-xs" />
            <div className="flex flex-wrap gap-2">
              <button disabled={busy} onClick={() => void onSaveVersion()} className="rounded-lg border border-border px-3 py-2 text-xs text-foreground disabled:opacity-50">保存为新版本</button>
              <button disabled={busy} onClick={() => void onOptimize()} className="rounded-lg border border-primary/40 bg-primary/10 px-3 py-2 text-xs text-primary disabled:opacity-50">LLM 优化并入库</button>
            </div>
            {message && <p className="text-xs text-muted-foreground">{message}</p>}
            <div>
              <div className="font-mono text-[10px] text-muted-foreground">历史版本</div>
              <div className="mt-2 space-y-2">
                {(selected.versions ?? [selected.latest_version].filter(Boolean)).map((version) => version && (
                  <div key={version.id} className="rounded border border-border px-3 py-2 text-[11px] text-muted-foreground">
                    v{version.version} · {version.source} · {version.note || '无备注'}
                  </div>
                ))}
              </div>
            </div>
          </div>
        ) : (
          <div className="text-sm text-muted-foreground">策略加载中…</div>
        )}
      </section>
    </main>
  )
}

function Field({ label, value, step, onChange }: { label: string; value: number; step: number; onChange: (value: number) => void }) {
  return (
    <label className="text-xs text-muted-foreground">
      {label}
      <input type="number" step={step} value={value} onChange={(e) => onChange(Number(e.target.value))} className="mt-1 w-full rounded border border-border bg-card px-2 py-1.5 font-mono text-foreground" />
    </label>
  )
}
