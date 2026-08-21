import { Analytics } from '@vercel/analytics/next'
import type { Metadata, Viewport } from 'next'
import { Inter, JetBrains_Mono } from 'next/font/google'
import { DashboardHeader } from '@/components/quant/dashboard-header'
import { AppNav } from '@/components/app-nav'
import './globals.css'

const geistSans = Inter({
  subsets: ['latin'],
  variable: '--font-geist-sans',
})
const geistMono = JetBrains_Mono({
  subsets: ['latin'],
  variable: '--font-geist-mono',
})

export const metadata: Metadata = {
  title: 'Trident Quant Platform',
  description:
    'Institutional-grade AI quantitative trading dashboard with real-time event bus, live signals, and AI sentiment analytics.',
  generator: 'v0.app',
}

export const viewport: Viewport = {
  colorScheme: 'light dark',
  themeColor: [
    { media: '(prefers-color-scheme: light)', color: '#f5f7fa' },
    { media: '(prefers-color-scheme: dark)', color: '#151619' },
  ],
}

const themeScript = `(function(){try{var t=localStorage.getItem('trident-theme');var d=t?t==='dark':matchMedia('(prefers-color-scheme: dark)').matches;document.documentElement.classList.toggle('dark',d)}catch(e){}})()`

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode
}>) {
  return (
    <html
      lang="zh-CN"
      suppressHydrationWarning
      className={`bg-background ${geistSans.variable} ${geistMono.variable}`}
    >
      <head>
        <script dangerouslySetInnerHTML={{ __html: themeScript }} />
      </head>
      <body className="bg-background font-sans antialiased">
        <div className="app-shell flex h-screen flex-col overflow-x-hidden">
          <DashboardHeader />
          <AppNav />
          {children}
        </div>
        {process.env.NODE_ENV === 'production' && <Analytics />}
      </body>
    </html>
  )
}
