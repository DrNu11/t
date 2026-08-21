'use client'

import { useEffect, useState } from 'react'

/** Tracks the `dark` class on <html> so canvas/iframe charts can restyle themselves. */
export function useIsDark(): boolean {
  const [dark, setDark] = useState(false)

  useEffect(() => {
    const root = document.documentElement
    const sync = () => setDark(root.classList.contains('dark'))
    sync()
    const observer = new MutationObserver(sync)
    observer.observe(root, { attributes: true, attributeFilter: ['class'] })
    return () => observer.disconnect()
  }, [])

  return dark
}
