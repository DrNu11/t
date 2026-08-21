'use client'

import { useEffect, useState } from 'react'
import { API_BASE } from '@/lib/api'
import { fetchDashboardOverview, fmtRate, type DashboardOverview } from '@/lib/strategy-data'

export default function DashboardPage() {
  const [data, setData] = useState<DashboardOverview | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    fetchDashboardOverview().then(setData).catch((err) => setError(String(err)))
  }, [])

  const overall = data?.replay.overall

  return (
    <main className="thin-scroll mx-auto w-full max-w-[1600px] flex-1 overflow-y-auto p-4 md:p-6">
      <div className="mb-4">
        <h2 className="font-mono text-sm uppercase tracking-widest text-muted-foreground">后台看板</h2>
        <p className="mt-1 text-sm text-muted-foreground">对应 dashboard/app.py 的离线复盘能力，数据来自本平台已结算信号，不读 Excel 模拟盘。</p>
      </div>
      {error && <div className="mb-4 rounded border border-short/40 bg-short/10 px-3 py-2 text-xs text-short">{error}</div>}
      <div className="grid gap-3 md:grid-cols-4">
        <Stat label="系统" value={data?.health.status ?? '…'} hint={data?.health.db_exists ? '本地库已挂载' : '数据库未就绪'} />
        <Stat label="已结算信号" value={String(overall?.total ?? '—')} hint={`胜率 ${fmtRate(overall?.winrate ?? null)}`} />
        <Stat label="平均 forward PnL" value={overall?.avg_pnl == null ? '—' : `${overall.avg_pnl.toFixed(3)}%`} hint={`${overall?.wins ?? 0} 胜 / ${overall?.losses ?? 0} 负`} />
        <Stat label="策略库" value={String(data?.strategies.length ?? '—')} hint={`${data?.recent_runs.length ?? 0} 条最近回测`} />
      </div>

      <div className="mt-4 grid gap-4 lg:grid-cols-[1.2fr_0.8fr]">
        <section className="surface-card overflow-hidden">
          <div className="border-b border-border px-4 py-3 font-mono text-[11px] text-muted-foreground">最近已结算信号</div>
          <div className="thin-scroll max-h-[420px] overflow-y-auto">
            {(data?.recent_signals ?? []).map((item) => (
              <div key={item.id} className="grid grid-cols-[72px_56px_1fr_90px] items-center gap-2 border-b border-border px-4 py-2 text-xs">
                <span className="font-mono text-foreground">{item.asset}</span>
                <span className={item.action === 'BUY' ? 'text-long' : item.action === 'SELL' ? 'text-short' : 'text-muted-foreground'}>{item.action}</span>
                <span className="truncate text-muted-foreground">{item.news_text}</span>
                <span className={`text-right font-mono ${Number(item.forward_pnl) >= 0 ? 'text-long' : 'text-short'}`}>
                  {item.forward_pnl == null ? '—' : `${item.forward_pnl.toFixed(2)}%`}
                </span>
              </div>
            ))}
            {!data?.recent_signals.length && <div className="p-4 text-xs text-muted-foreground">暂无已结算信号</div>}
          </div>
        </section>
        <section className="surface-card space-y-3 p-4">
          <div className="font-mono text-[11px] text-muted-foreground">离线 Streamlit 看板</div>
          <p className="text-sm leading-6 text-foreground">
            {data?.streamlit.source} 仍可用于上传 Excel 做客户演示。本页已把同一套复盘统计接到平台库，避免两套口径。
          </p>
          <div className="flex flex-wrap gap-2">
            <a href={data?.streamlit.url || 'http://127.0.0.1:8501'} target="_blank" rel="noreferrer" className="rounded-lg border border-primary/40 bg-primary/10 px-3 py-2 text-xs text-primary">
              打开 Streamlit :8501
            </a>
            <a href={`${API_BASE.replace(/\/api$/, '')}${data?.exports.signals_xlsx || '/api/export/signals'}`} className="rounded-lg border border-border px-3 py-2 text-xs text-foreground">
              导出 signals.xlsx
            </a>
          </div>
          <div className="font-mono text-[10px] text-muted-foreground">SSE 客户端 {data?.health.sse_clients ?? 0}</div>
        </section>
      </div>
    </main>
  )
}

function Stat({ label, value, hint }: { label: string; value: string; hint: string }) {
  return (
    <div className="surface-card p-4">
      <div className="font-mono text-[10px] uppercase tracking-wider text-muted-foreground">{label}</div>
      <div className="mt-2 font-mono text-xl text-foreground">{value}</div>
      <div className="mt-1 text-[11px] text-muted-foreground">{hint}</div>
    </div>
  )
}
