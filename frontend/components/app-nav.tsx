'use client'

import Link from 'next/link'
import { usePathname } from 'next/navigation'

const TABS = [
  { href: '/', label: '实时信号台' },
  { href: '/replay', label: '模拟盘 · 复盘看板', badge: 'PAPER' },
  { href: '/news', label: '新闻策略' },
  { href: '/dashboard', label: '后台看板' },
  { href: '/strategies', label: '策略库' },
  { href: '/backtest', label: '策略回测' },
] as const

function isActive(pathname: string, href: string) {
  if (href === '/') return pathname === '/'
  return pathname === href || pathname.startsWith(`${href}/`)
}

export function AppNav() {
  const pathname = usePathname() || '/'
  return (
    <nav className="border-b border-border bg-card" aria-label="主视图">
      <div className="thin-scroll mx-auto flex w-full max-w-[1600px] items-center gap-1 overflow-x-auto px-3 md:px-6">
        {TABS.map((tab) => {
          const active = isActive(pathname, tab.href)
          return (
            <Link
              key={tab.href}
              href={tab.href}
              prefetch={false}
              className={`flex shrink-0 items-center gap-2 border-b-2 px-3 py-3 font-mono text-xs uppercase tracking-wider transition-colors sm:px-4 sm:tracking-widest ${
                active
                  ? 'border-primary text-primary'
                  : 'border-transparent text-muted-foreground hover:text-foreground'
              }`}
            >
              <span>{tab.label}</span>
              {'badge' in tab && tab.badge ? (
                <span className={`rounded-md border px-1.5 py-0.5 font-mono text-[9px] font-bold ${
                  active
                    ? 'border-hold/40 bg-hold/10 text-hold'
                    : 'border-border bg-secondary text-muted-foreground'
                }`}>
                  {tab.badge}
                </span>
              ) : null}
            </Link>
          )
        })}
      </div>
    </nav>
  )
}
