'use client'

import { useCallback, useEffect, useRef, useState } from 'react'
import { TrendingUp } from 'lucide-react'
import { useIsDark } from '@/hooks/use-is-dark'

type Props = {
  symbol: string
  interval?: string
  title?: string
}

const SYMBOL_LABELS: Record<string, string> = {
  'BINANCE:BTCUSDT': 'BTC/USDT',
  'OANDA:XAUUSD': 'XAU/USD',
  'OANDA:WTICOUSD': 'WTI/USD',
}

const INTERVAL_LABELS: Record<string, string> = {
  '60': '1H',
  '240': '4H',
  'D': '日线',
}

/** Generate a stable DOM id from the symbol, avoiding React's useId drift. */
function symbolToContainerId(symbol: string): string {
  return `tv-${symbol.replace(/[^a-zA-Z0-9]/g, '_')}`
}

/**
 * TradingView Advanced Chart Widget
 *
 * Key design decisions to avoid DOM reconciliation errors:
 * 1. The chart container div is NOT managed by React — no JSX children inside it.
 *    TradingView's injected iframe/DOM nodes never collide with React's Virtual DOM.
 * 2. A stable container id derived from `symbol` so TradingView reuses it correctly.
 * 3. On unmount, we explicitly remove the injected script + clear the container
 *    via raw DOM, which is safe because React does not own those nodes.
 * 4. The parent page.tsx passes `key={activeMarket}` on the wrapping div,
 *    forcing React to destroy and recreate the entire section on tab switch.
 */
export function TradingViewChart({
  symbol,
  interval = '60',
  title,
}: Props) {
  const isDark = useIsDark()
  const containerRef = useRef<HTMLDivElement>(null)
  const scriptRef = useRef<HTMLScriptElement | null>(null)
  const mountedRef = useRef(true)
  const [phase, setPhase] = useState<'loading' | 'loaded' | 'error'>('loading')

  const containerId = symbolToContainerId(symbol)
  const label = SYMBOL_LABELS[symbol] ?? symbol
  const intervalLabel = INTERVAL_LABELS[interval] ?? interval
  const displayTitle = title ?? `${label} · ${intervalLabel} K线`

  const bootWidget = useCallback(() => {
    const container = containerRef.current
    if (!container) return

    // ── Safe cleanup of any prior widget (in case of hot-reload) ──
    // Remove previous script if still attached
    if (scriptRef.current && scriptRef.current.parentNode) {
      scriptRef.current.parentNode.removeChild(scriptRef.current)
      scriptRef.current = null
    }
    // Clear container safely — only remove children that actually belong to it
    while (container.firstChild) {
      container.removeChild(container.firstChild)
    }

    // Create inner widget div (TradingView will place the iframe here)
    const widgetDiv = document.createElement('div')
    widgetDiv.className = 'tradingview-widget-container__widget'
    widgetDiv.style.height = '100%'
    widgetDiv.style.width = '100%'
    container.appendChild(widgetDiv)

    // Inject TradingView embed script
    const script = document.createElement('script')
    script.src = 'https://s3.tradingview.com/external-embedding/embed-widget-advanced-chart.js'
    script.type = 'text/javascript'
    script.async = true
    script.onload = () => {
      if (mountedRef.current) setPhase('loaded')
    }
    script.onerror = () => {
      if (mountedRef.current) setPhase('error')
    }

    const surface = isDark ? '#16171b' : '#ffffff'
    const upColor = isDark ? '#2ebd85' : '#00b061'
    const downColor = isDark ? '#f6465d' : '#ef4444'

    const widgetConfig: Record<string, unknown> = {
      autosize: true,
      symbol,
      interval,
      timezone: 'Asia/Shanghai',
      theme: isDark ? 'dark' : 'light',
      style: '1',
      locale: 'zh_CN',
      toolbar_bg: surface,
      enable_publishing: false,
      hide_top_toolbar: false,
      hide_legend: false,
      hide_side_toolbar: false,
      allow_symbol_change: false,
      save_image: false,
      container_id: containerId,
      studies: [
        'MASimple@tv-basicstudies',
      ],
      show_popup_button: false,
      popup_width: '1000',
      popup_height: '650',
      disabled_features: [
        'header_symbol_search',
        'header_compare',
        'header_saveload',
        'use_localstorage_for_settings',
        'right_bar_stays_on_scroll',
      ],
      enabled_features: [],
      overrides: {
        'paneProperties.background': surface,
        'paneProperties.backgroundType': 'solid',
        'paneProperties.backgroundGradientStart': surface,
        'paneProperties.backgroundGradientEnd': surface,
        'mainSeriesProperties.candleStyle.upColor': upColor,
        'mainSeriesProperties.candleStyle.downColor': downColor,
        'mainSeriesProperties.candleStyle.wickUpColor': upColor,
        'mainSeriesProperties.candleStyle.wickDownColor': downColor,
        'mainSeriesProperties.candleStyle.borderUpColor': upColor,
        'mainSeriesProperties.candleStyle.borderDownColor': downColor,
      },
      loading_screen: {
        backgroundColor: surface,
        foregroundColor: isDark ? '#9aa3af' : '#6b7280',
      },
    }

    script.textContent = JSON.stringify(widgetConfig)
    container.appendChild(script)
    scriptRef.current = script
  }, [symbol, interval, isDark, containerId])

  useEffect(() => {
    mountedRef.current = true
    setPhase('loading')
    // Small delay so the DOM has rendered the container div before we inject
    const t = setTimeout(bootWidget, 0)
    return () => {
      mountedRef.current = false
      clearTimeout(t)
      // ── Safe unmount cleanup ──
      // Remove the injected script from DOM (if still attached)
      if (scriptRef.current && scriptRef.current.parentNode) {
        scriptRef.current.parentNode.removeChild(scriptRef.current)
        scriptRef.current = null
      }
      // Clear the container — only its own direct children, which we created
      const container = containerRef.current
      if (container) {
        while (container.firstChild) {
          container.removeChild(container.firstChild)
        }
      }
    }
  }, [bootWidget])

  return (
    <section className="glass flex h-full flex-col overflow-hidden rounded-lg border border-border">
      {/* Header bar */}
      <div className="flex shrink-0 items-center gap-2 border-b border-border px-4 py-2.5">
        <TrendingUp className="h-4 w-4 text-primary" aria-hidden="true" />
        <h2 className="text-sm font-semibold text-foreground">
          {displayTitle}
        </h2>
        <span className="ml-auto inline-flex items-center rounded-full bg-primary/10 px-2 py-0.5 font-mono text-[10px] text-primary">
          {label}
        </span>
      </div>

      {/* Chart area — NO React children inside this div (React must not own its internals) */}
      <div className="relative min-h-0 flex-1">
        {phase === 'error' ? (
          <div className="flex h-full items-center justify-center text-sm text-muted-foreground">
            TradingView 图表加载失败 · 请检查网络连接
          </div>
        ) : (
          <>
            <div
              id={containerId}
              ref={containerRef}
              className="tradingview-widget-container"
              style={{ height: '100%', width: '100%' }}
            />
            {/* Spinner: rendered by React *outside* the widget container */}
            {phase === 'loading' && (
              <div className="pointer-events-none absolute inset-0 flex items-center justify-center">
                <div className="flex items-center gap-3 rounded-lg bg-background/80 px-4 py-3 backdrop-blur-sm">
                  <div className="h-4 w-4 animate-spin rounded-full border-2 border-primary border-t-transparent" />
                  <span className="text-sm text-muted-foreground">加载 K 线中...</span>
                </div>
              </div>
            )}
          </>
        )}
      </div>
    </section>
  )
}
