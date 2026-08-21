'use client'

import { useEffect, useRef, useState } from 'react'
import {
  CandlestickSeries,
  LineSeries,
  createChart,
  createSeriesMarkers,
  type IChartApi,
  type IPriceLine,
  type ISeriesApi,
  type ISeriesMarkersPluginApi,
  type Time,
} from 'lightweight-charts'
import type { ReplayKlineBar, ReplayKlineMarker } from '@/lib/replay-data'
import { fetchReplaySignalKline } from '@/lib/replay-data'
import { useIsDark } from '@/hooks/use-is-dark'

type Props = {
  signalId: number | null
  asset: string
  action?: string
  entryPrice: number | null
  exitPrice?: number | null
}

const CHART_THEME = {
  light: {
    background: '#ffffff',
    text: '#5b6472',
    grid: '#eceff3',
    border: '#dfe3e9',
    up: '#00b061',
    down: '#ef4444',
    entry: '#f0932b',
    stop: '#ef4444',
    take: '#00b061',
    exit: '#64748b',
  },
  dark: {
    background: '#16171b',
    text: '#9aa3af',
    grid: '#26282e',
    border: '#33363d',
    up: '#2ebd85',
    down: '#f6465d',
    entry: '#f7a440',
    stop: '#f6465d',
    take: '#2ebd85',
    exit: '#9aa3af',
  },
} as const

function alignMarkerTime(time: number, bars: ReplayKlineBar[]): number {
  if (!bars.length) return time
  let nearest = bars[0].time
  let best = Math.abs(bars[0].time - time)
  for (const bar of bars) {
    const delta = Math.abs(bar.time - time)
    if (delta < best) {
      best = delta
      nearest = bar.time
    }
  }
  return nearest
}

export function KlineChart({ signalId, asset, action, entryPrice, exitPrice }: Props) {
  const isDark = useIsDark()
  const containerRef = useRef<HTMLDivElement>(null)
  const chartRef = useRef<IChartApi | null>(null)
  const candleRef = useRef<ISeriesApi<'Candlestick'> | null>(null)
  const markersRef = useRef<ISeriesMarkersPluginApi<Time> | null>(null)
  const extraLinesRef = useRef<IPriceLine[]>([])
  const [chartReady, setChartReady] = useState(false)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [meta, setMeta] = useState<{
    action: string
    stop_loss: number | null
    take_profit: number | null
    trailing: number
    invalidation: string
    is_paper_trading: boolean
  } | null>(null)

  useEffect(() => {
    const el = containerRef.current
    if (!el) return
    const palette = isDark ? CHART_THEME.dark : CHART_THEME.light
    const chart = createChart(el, {
      width: Math.max(el.clientWidth, 1),
      height: Math.max(el.clientHeight, 320),
      layout: {
        background: { color: palette.background },
        textColor: palette.text,
        fontFamily: 'var(--font-stack)',
        fontSize: 11,
      },
      grid: {
        vertLines: { color: palette.grid },
        horzLines: { color: palette.grid },
      },
      timeScale: {
        timeVisible: true,
        secondsVisible: false,
        borderColor: palette.border,
      },
      rightPriceScale: {
        borderColor: palette.border,
        scaleMargins: { top: 0.1, bottom: 0.1 },
      },
      crosshair: { mode: 1 },
      autoSize: false,
    })

    const candle = chart.addSeries(CandlestickSeries, {
      upColor: palette.up,
      downColor: palette.down,
      borderUpColor: palette.up,
      borderDownColor: palette.down,
      wickUpColor: palette.up,
      wickDownColor: palette.down,
    })
    markersRef.current = createSeriesMarkers(candle, [])
    chart.addSeries(LineSeries, {
      color: palette.entry,
      lineWidth: 2,
      lineStyle: 2,
      priceLineVisible: false,
      lastValueVisible: false,
      title: 'Entry',
      visible: false,
    })

    chartRef.current = chart
    candleRef.current = candle
    extraLinesRef.current = []
    setChartReady(true)

    const resize = () => {
      if (!el.clientWidth || !el.clientHeight) return
      chart.applyOptions({ width: el.clientWidth, height: el.clientHeight })
    }
    const observer = new ResizeObserver(resize)
    observer.observe(el)
    resize()

    return () => {
      observer.disconnect()
      setChartReady(false)
      extraLinesRef.current = []
      markersRef.current?.detach()
      markersRef.current = null
      chart.remove()
      chartRef.current = null
      candleRef.current = null
    }
  }, [isDark])

  useEffect(() => {
    if (!signalId || !chartReady || !candleRef.current) return
    let cancelled = false
    setLoading(true)
    setError(null)
    fetchReplaySignalKline(signalId)
      .then((resp) => {
        if (cancelled || !candleRef.current) return
        const bars: ReplayKlineBar[] = [...resp.klines]
        if (!bars.length) {
          setError('K 线数据为空')
          return
        }
        bars.sort((a, b) => a.time - b.time)
        candleRef.current.setData(
          bars.map((bar) => ({
            time: bar.time as Time,
            open: bar.open,
            high: bar.high,
            low: bar.low,
            close: bar.close,
          })),
        )
        extraLinesRef.current.forEach((line) => {
          try { candleRef.current?.removePriceLine(line) } catch { /* already removed */ }
        })
        extraLinesRef.current = []
        const palette = isDark ? CHART_THEME.dark : CHART_THEME.light
        extraLinesRef.current.push(candleRef.current.createPriceLine({
          price: resp.entry_price,
          color: palette.entry,
          lineWidth: 2,
          lineStyle: 2,
          axisLabelVisible: true,
          title: `${resp.action || action || 'ENTRY'} @ ${resp.entry_price}`,
        }))
        if (resp.stop_loss) {
          extraLinesRef.current.push(candleRef.current.createPriceLine({
            price: resp.stop_loss,
            color: palette.stop,
            lineWidth: 2,
            lineStyle: 1,
            axisLabelVisible: true,
            title: `止损 ${resp.stop_loss}`,
          }))
        }
        if (resp.take_profit) {
          extraLinesRef.current.push(candleRef.current.createPriceLine({
            price: resp.take_profit,
            color: palette.take,
            lineWidth: 1,
            lineStyle: 3,
            axisLabelVisible: true,
            title: `追踪高/低 ${resp.take_profit}`,
          }))
        }
        if (resp.exit_price) {
          extraLinesRef.current.push(candleRef.current.createPriceLine({
            price: resp.exit_price,
            color: palette.exit,
            lineWidth: 1,
            lineStyle: 2,
            axisLabelVisible: true,
            title: `出场 ${resp.exit_price}`,
          }))
        }
        const markers: ReplayKlineMarker[] = resp.markers?.length
          ? resp.markers
          : [{
              time: resp.entry_time,
              position: (resp.action || action) === 'SELL' ? 'aboveBar' : 'belowBar',
              color: (resp.action || action) === 'SELL' ? palette.stop : palette.take,
              shape: (resp.action || action) === 'SELL' ? 'arrowDown' : 'arrowUp',
              text: `${resp.action || action || 'ENTRY'}`,
              kind: 'entry',
            }]
        markersRef.current?.setMarkers(markers.map((marker) => ({
          time: alignMarkerTime(marker.time, bars) as Time,
          position: marker.position,
          color: marker.color,
          shape: marker.shape,
          text: marker.text,
        })))
        chartRef.current?.timeScale().fitContent()
        setMeta({
          action: resp.action || action || '',
          stop_loss: resp.stop_loss ?? null,
          take_profit: resp.take_profit ?? null,
          trailing: resp.trailing_callback_rate ?? 0,
          invalidation: resp.invalidation_condition || '',
          is_paper_trading: resp.is_paper_trading,
        })
      })
      .catch((err) => {
        if (!cancelled) setError(String(err))
      })
      .finally(() => {
        if (!cancelled) setLoading(false)
      })
    return () => {
      cancelled = true
    }
  }, [signalId, isDark, chartReady, action])

  return (
    <div className="relative h-full min-h-[360px] w-full overflow-hidden rounded-md border border-border bg-card">
      <div className="absolute left-2 top-2 z-10 flex max-w-[92%] flex-wrap items-center gap-2 font-mono text-[10px] text-muted-foreground">
        <span className="rounded border border-hold/50 bg-hold/10 px-2 py-0.5 text-hold">
          PAPER · 模拟盘
        </span>
        <span className="text-foreground">
          {asset || '—'} · {meta?.action || action || '—'} · entry {entryPrice ?? '—'}
          {exitPrice != null ? ` · exit ${exitPrice}` : ''}
        </span>
        {meta?.stop_loss != null && (
          <span className="text-short">止损 {meta.stop_loss}{meta.trailing ? ` · 回撤 ${meta.trailing}%` : ''}</span>
        )}
        {meta?.invalidation && (
          <span className="max-w-[240px] truncate" title={meta.invalidation}>失效：{meta.invalidation}</span>
        )}
        {meta?.is_paper_trading && (
          <span className="text-muted-foreground">（基于 entry/max/min 反推，非真实盘口）</span>
        )}
      </div>
      {loading && (
        <div className="absolute right-2 top-2 z-10 font-mono text-[10px] text-muted-foreground">
          加载 K 线…
        </div>
      )}
      {error && (
        <div className="absolute inset-x-2 bottom-2 z-10 rounded border border-short/50 bg-short/10 px-2 py-1 font-mono text-[10px] text-short">
          {error}
        </div>
      )}
      {!signalId && (
        <div className="pointer-events-none absolute inset-0 z-10 flex items-center justify-center">
          <span className="font-mono text-xs text-muted-foreground">← 在左侧选择一个信号查看 K 线</span>
        </div>
      )}
      <div ref={containerRef} className="h-full min-h-[520px] w-full" />
    </div>
  )
}
