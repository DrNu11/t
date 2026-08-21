'use client'

import { useEffect, useState } from 'react'
import { Moon, Sun } from 'lucide-react'

export function ThemeToggle() {
  const [mounted, setMounted] = useState(false)
  const [dark, setDark] = useState(false)

  useEffect(() => {
    setDark(document.documentElement.classList.contains('dark'))
    setMounted(true)
  }, [])

  function toggleTheme() {
    const next = !dark
    document.documentElement.classList.toggle('dark', next)
    localStorage.setItem('trident-theme', next ? 'dark' : 'light')
    setDark(next)
  }

  const label = mounted && dark ? '切换到日间模式' : '切换到夜间模式'

  return (
    <button
      type="button"
      onClick={toggleTheme}
      className="flex h-9 w-9 shrink-0 items-center justify-center rounded-lg border border-border bg-card text-muted-foreground transition-colors hover:border-primary/40 hover:bg-accent hover:text-foreground"
      aria-label={label}
      title={label}
    >
      {mounted && dark ? <Sun className="h-4 w-4" aria-hidden="true" /> : <Moon className="h-4 w-4" aria-hidden="true" />}
    </button>
  )
}
