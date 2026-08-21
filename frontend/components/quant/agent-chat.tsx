'use client'

import { useCallback, useEffect, useRef, useState } from 'react'
import { Bot, Loader2, Send, User, Paperclip, X, Image as ImageIcon } from 'lucide-react'
import type { ApiEvent, MarketCategory } from '@/lib/quant-data'
import { API_BASE } from '@/lib/api'

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

type ChatRole = 'user' | 'assistant' | 'system'

interface UploadedFile {
  url: string
  filename: string
  size: number
  content_type: string
  preview?: string
}

interface UploadItem {
  id: string
  file: File
  preview: string
  status: 'uploading' | 'success' | 'error'
  error?: string
}

interface ChatMessage {
  id: string
  role: ChatRole
  content: string
  timestamp: number
  /** Attached metadata from Agent — hidden behind a <details> toggle */
  meta?: {
    sql?: string
    rows?: Array<Record<string, unknown>>
  }
  /** Files attached by user */
  attachments?: UploadedFile[]
}

interface AgentChatResponse {
  reply: string
  sql?: string
  rows?: Array<Record<string, unknown>>
  error?: string
}

// ---------------------------------------------------------------------------
// Props
// ---------------------------------------------------------------------------

type Props = {
  events: ApiEvent[]
  activeMarket: MarketCategory
}

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

export function AgentChat({ events, activeMarket }: Props) {
  const [messages, setMessages] = useState<ChatMessage[]>([
    {
      id: 'welcome',
      role: 'assistant',
      content:
        '你好，我是 Trident Data Copilot。\n\n我可以帮你：\n• 查询最近的交易信号和盈亏\n• 分析某个品种的仓位变化\n• 统计 DeepSeek V4 Flash 等模型的投票分歧\n• 检索特定新闻触发的决策\n\n直接问我问题就好。支持拖拽或点击回形针上传附件（图片/文档，最大 5MB）。',
      timestamp: Date.now(),
    },
  ])
  const [input, setInput] = useState('')
  const [loading, setLoading] = useState(false)
  const [uploadQueue, setUploadQueue] = useState<UploadItem[]>([])
  const scrollRef = useRef<HTMLDivElement>(null)
  const inputRef = useRef<HTMLTextAreaElement>(null)
  const fileInputRef = useRef<HTMLInputElement>(null)
  const dragOverlayRef = useRef<HTMLDivElement>(null)
  const [isDragging, setIsDragging] = useState(false)

  // ------------------------------------------------------------------
  // Auto-scroll
  // ------------------------------------------------------------------

  useEffect(() => {
    if (scrollRef.current) {
      scrollRef.current.scrollTop = scrollRef.current.scrollHeight
    }
  }, [messages])

  // ------------------------------------------------------------------
  // File upload helpers
  // ------------------------------------------------------------------

  const MAX_FILE_SIZE = 5 * 1024 * 1024 // 5 MB
  const ALLOWED_TYPES = new Set([
    'image/jpeg', 'image/png', 'image/gif', 'image/webp',
    'application/pdf',
    'text/plain', 'text/csv', 'text/html',
  ])

  const processFiles = useCallback((files: FileList | File[]) => {
    const items: UploadItem[] = []
    Array.from(files).forEach((file) => {
      if (!ALLOWED_TYPES.has(file.type)) {
        items.push({
          id: `upload-${Date.now()}-${Math.random().toString(36).slice(2)}`,
          file,
          preview: '',
          status: 'error',
          error: `不支持的文件类型: ${file.type}`,
        })
        return
      }
      if (file.size > MAX_FILE_SIZE) {
        items.push({
          id: `upload-${Date.now()}-${Math.random().toString(36).slice(2)}`,
          file,
          preview: '',
          status: 'error',
          error: `文件过大 (${(file.size / 1024 / 1024).toFixed(1)}MB，最大 5MB)`,
        })
        return
      }
      // Create preview for images
      const preview = file.type.startsWith('image/') ? URL.createObjectURL(file) : ''
      items.push({
        id: `upload-${Date.now()}-${Math.random().toString(36).slice(2)}`,
        file,
        preview,
        status: 'uploading',
      })
    })
    setUploadQueue((prev) => [...prev, ...items])

    // Upload each item
    items.forEach(async (item) => {
      if (item.status === 'error') return
      try {
        const formData = new FormData()
        formData.append('file', item.file)
        const res = await fetch(`${API_BASE}/agent_chat/upload`, {
          method: 'POST',
          body: formData,
        })
        if (!res.ok) {
          const text = await res.text()
          throw new Error(`HTTP ${res.status}: ${text}`)
        }
        const data = await res.json()
        setUploadQueue((prev) =>
          prev.map((u) =>
            u.id === item.id ? { ...u, status: 'success', preview: data.url } : u,
          ),
        )
      } catch (err: unknown) {
        const msg = err instanceof Error ? err.message : String(err)
        setUploadQueue((prev) =>
          prev.map((u) =>
            u.id === item.id ? { ...u, status: 'error', error: msg } : u,
          ),
        )
      }
    })
  }, [])

  const handleFileSelect = useCallback(
    (e: React.ChangeEvent<HTMLInputElement>) => {
      if (e.target.files) {
        processFiles(e.target.files)
        e.target.value = '' // reset so same file can be selected again
      }
    },
    [processFiles],
  )

  const removeUploadItem = useCallback((id: string) => {
    setUploadQueue((prev) => {
      const item = prev.find((u) => u.id === id)
      if (item?.preview && !item.preview.startsWith('/uploads/')) {
        URL.revokeObjectURL(item.preview)
      }
      return prev.filter((u) => u.id !== id)
    })
  }, [])

  const clearUploadedUrls = useCallback((): UploadedFile[] => {
    const urls: UploadedFile[] = []
    uploadQueue.forEach((item) => {
      if (item.status === 'success') {
        urls.push({
          url: item.preview,
          filename: item.file.name,
          size: item.file.size,
          content_type: item.file.type,
        })
      }
    })
    setUploadQueue([])
    return urls
  }, [uploadQueue])

  // Drag-and-drop handlers
  const handleDragOver = useCallback((e: React.DragEvent) => {
    e.preventDefault()
    e.stopPropagation()
    setIsDragging(true)
  }, [])

  const handleDragLeave = useCallback((e: React.DragEvent) => {
    e.preventDefault()
    e.stopPropagation()
    setIsDragging(false)
  }, [])

  const handleDrop = useCallback(
    (e: React.DragEvent) => {
      e.preventDefault()
      e.stopPropagation()
      setIsDragging(false)
      if (e.dataTransfer.files.length > 0) {
        processFiles(e.dataTransfer.files)
      }
    },
    [processFiles],
  )

  // ------------------------------------------------------------------
  // Call backend
  // ------------------------------------------------------------------

  const callAgent = useCallback(
    async (userMessage: string, attachments?: UploadedFile[]): Promise<AgentChatResponse> => {
      const res = await fetch(`${API_BASE}/agent_chat`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          user_message: userMessage,
          active_market: activeMarket,
          attachments: attachments || [],
          context: {
            event_count: events.length,
            market_counts: events.reduce<Record<string, number>>((acc, e) => {
              acc[e.market_category] = (acc[e.market_category] ?? 0) + 1
              return acc
            }, {}),
          },
        }),
      })

      if (!res.ok) {
        const text = await res.text()
        throw new Error(`HTTP ${res.status}: ${text}`)
      }

      return res.json()
    },
    [activeMarket, events],
  )

  // ------------------------------------------------------------------
  // Send handler
  // ------------------------------------------------------------------

  const handleSend = useCallback(async () => {
    const text = input.trim()
    if (!text && uploadQueue.length === 0 || loading) return

    // Collect uploaded file URLs
    const attachments = clearUploadedUrls()

    const userMsg: ChatMessage = {
      id: `u-${Date.now()}`,
      role: 'user',
      content: text,
      timestamp: Date.now(),
      ...(attachments.length > 0 ? { attachments } : {}),
    }
    setMessages((prev) => [...prev, userMsg])
    setInput('')
    setLoading(true)

    try {
      const data = await callAgent(text, attachments)

      // ── Natural language reply (shown by default) ──
      let reply = data.reply
      if (data.error) {
        reply += `\n\n❌ 错误：${data.error}`
      }

      // ── Metadata: SQL + rows (hidden behind <details> toggle) ──
      const hasMeta = (data.sql && data.sql.trim()) || (data.rows && data.rows.length > 0)
      let meta: ChatMessage['meta'] = undefined
      if (hasMeta) {
        meta = {
          sql: data.sql || '',
          rows: data.rows || [],
        }
      }

      const botMsg: ChatMessage = {
        id: `b-${Date.now()}`,
        role: 'assistant',
        content: reply,
        timestamp: Date.now(),
        meta,
      }
      setMessages((prev) => [...prev, botMsg])
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : String(err)
      setMessages((prev) => [
        ...prev,
        {
          id: `b-${Date.now()}`,
          role: 'assistant',
          content: `❌ 请求失败：${msg}`,
          timestamp: Date.now(),
        },
      ])
    } finally {
      setLoading(false)
      // 把焦点还给输入框
      setTimeout(() => inputRef.current?.focus(), 50)
    }
  }, [input, loading, callAgent])

  // ------------------------------------------------------------------
  // Keyboard shortcut
  // ------------------------------------------------------------------

  const handleKeyDown = useCallback(
    (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
      if (e.key === 'Enter' && !e.shiftKey) {
        e.preventDefault()
        handleSend()
      }
    },
    [handleSend],
  )

  // ------------------------------------------------------------------
  // Render helpers
  // ------------------------------------------------------------------

  const renderMarkdown = (content: string) => {
    // 把 ``` 代码块 → <pre><code>
    const parts = content.split(/(```[\s\S]*?```)/g)
    return parts.map((part, i) => {
      const codeMatch = part.match(/^```(\w*)\n?([\s\S]*?)```$/)
      if (codeMatch) {
        return (
          <pre
            key={i}
            className="my-2 overflow-x-auto rounded border border-border bg-secondary/50 p-3 font-mono text-[11px] leading-relaxed text-foreground/90"
          >
            <code>{codeMatch[2].trim()}</code>
          </pre>
        )
      }
      // 简单处理 inline code `` 和表格 |
      const lines = part.split('\n')
      return (
        <span key={i}>
          {lines.map((line, j) => {
            // 表格行
            if (line.startsWith('|') && line.endsWith('|')) {
              const cells = line.split('|').filter(Boolean)
              const isHeader = lines[j + 1]?.includes('---')
              const Tag = isHeader ? 'th' : 'td'
              return (
                <tr key={j} className={isHeader ? 'border-b border-border' : ''}>
                  {cells.map((cell, k) => (
                    <Tag
                      key={k}
                      className="px-2 py-1 font-mono text-[11px] text-muted-foreground"
                    >
                      {cell.trim()}
                    </Tag>
                  ))}
                </tr>
              )
            }
            // 引用行
            if (line.startsWith('>')) {
              return (
                <span
                  key={j}
                  className="block border-l-2 border-primary/40 pl-2 italic text-muted-foreground"
                >
                  {line.replace(/^> ?/, '')}
                </span>
              )
            }
            return (
              <span key={j}>
                {line}
                {j < lines.length - 1 && <br />}
              </span>
            )
          })}
        </span>
      )
    })
  }

  // ------------------------------------------------------------------
  // UI
  // ------------------------------------------------------------------

  const hasAttachments = uploadQueue.some((u) => u.status === 'success')

  return (
    <section className="glass relative flex h-full flex-col rounded-lg border border-border">
      {/* Drag overlay */}
      {isDragging && (
        <div
          ref={dragOverlayRef}
          onDragOver={handleDragOver}
          onDragLeave={handleDragLeave}
          onDrop={handleDrop}
          className="absolute inset-0 z-50 flex items-center justify-center rounded-lg border-2 border-dashed border-primary/60 bg-primary/10 backdrop-blur-sm"
        >
          <div className="flex flex-col items-center gap-2 text-primary">
            <ImageIcon className="h-10 w-10" />
            <span className="text-sm font-medium">拖放文件到此处上传</span>
          </div>
        </div>
      )}

      {/* Header */}
      <div className="flex items-center gap-2 border-b border-border px-4 py-3">
        <Bot className="h-4 w-4 text-primary" aria-hidden="true" />
        <h2 className="text-sm font-semibold text-foreground">
          Trident Data Copilot
        </h2>
        <span className="ml-auto rounded-full bg-primary/10 px-2 py-0.5 text-[10px] font-medium text-primary">
          AI Agent
        </span>
      </div>

      {/* Messages */}
      <div
        ref={scrollRef}
        className="min-h-0 flex-1 space-y-3 overflow-y-auto overscroll-contain scroll-smooth px-4 py-3"
      >
        {messages.map((msg) => (
          <div key={msg.id}>
            <div
              className={`flex gap-2.5 ${
                msg.role === 'user' ? 'flex-row-reverse' : ''
              }`}
            >
              {/* Avatar */}
              <div
                className={`mt-0.5 flex h-7 w-7 shrink-0 items-center justify-center rounded-full ${
                  msg.role === 'user'
                    ? 'bg-primary text-primary-foreground'
                    : 'bg-secondary text-muted-foreground'
                }`}
              >
                {msg.role === 'user' ? (
                  <User className="h-3.5 w-3.5" />
                ) : (
                  <Bot className="h-3.5 w-3.5" />
                )}
              </div>

              {/* Bubble */}
              <div
                className={`max-w-[85%] rounded-lg px-3.5 py-2.5 text-sm leading-relaxed ${
                  msg.role === 'user'
                    ? 'bg-primary text-primary-foreground'
                    : 'bg-secondary text-secondary-foreground'
                }`}
              >
                {/* Attachments preview (user messages only) */}
                {msg.role === 'user' && msg.attachments && msg.attachments.length > 0 && (
                  <div className="mb-2 flex flex-wrap gap-2">
                    {msg.attachments.map((att, i) =>
                      att.content_type?.startsWith('image/') ? (
                        <img
                          key={i}
                          src={att.url}
                          alt={att.filename}
                          className="max-h-32 max-w-[200px] rounded-md object-cover"
                        />
                      ) : (
                        <div
                          key={i}
                          className="flex items-center gap-1.5 rounded-md bg-white/20 px-2 py-1 text-xs"
                        >
                          <span className="opacity-70">📎</span>
                          <span className="truncate max-w-[150px]">{att.filename}</span>
                          <span className="opacity-50">({(att.size / 1024).toFixed(0)}KB)</span>
                        </div>
                      ),
                    )}
                  </div>
                )}
                <div className="whitespace-pre-wrap break-words">
                  {renderMarkdown(msg.content)}
                </div>
              </div>
            </div>

            {/* Collapsible: SQL + data table (only for assistant with metadata) */}
            {msg.role === 'assistant' && msg.meta && (
              <details className="mt-1.5 ml-9 group">
                <summary className="cursor-pointer text-[11px] text-muted-foreground/50 hover:text-muted-foreground/80 transition-colors select-none">
                  ⚙️ 查看底层数据与查询逻辑
                </summary>
                <div className="mt-2 space-y-2">
                  {/* SQL */}
                  {msg.meta.sql && (
                    <pre className="overflow-x-auto rounded border border-border/50 bg-secondary/30 p-2.5 font-mono text-[10px] leading-relaxed text-muted-foreground/70">
                      <code>{msg.meta.sql.trim()}</code>
                    </pre>
                  )}
                  {/* Data table */}
                  {msg.meta.rows && msg.meta.rows.length > 0 && (
                    <div className="overflow-x-auto rounded border border-border/50">
                      <table className="w-full text-[10px]">
                        <thead>
                          <tr className="border-b border-border/50 bg-secondary/30">
                            {Object.keys(msg.meta.rows[0]).map((col) => (
                              <th
                                key={col}
                                className="whitespace-nowrap px-2 py-1.5 text-left font-medium text-muted-foreground/60"
                              >
                                {col}
                              </th>
                            ))}
                          </tr>
                        </thead>
                        <tbody className="divide-y divide-border/30">
                          {msg.meta.rows.slice(0, 20).map((row, ri) => (
                            <tr key={ri}>
                              {Object.keys(msg.meta!.rows![0]).map((col) => (
                                <td
                                  key={col}
                                  className="whitespace-nowrap px-2 py-1 text-muted-foreground/50 font-mono"
                                >
                                  {String(row[col] ?? '—').slice(0, 60)}
                                </td>
                              ))}
                            </tr>
                          ))}
                        </tbody>
                      </table>
                      {msg.meta.rows.length > 20 && (
                        <div className="px-2 py-1.5 text-[10px] text-muted-foreground/40">
                          仅显示前 20 行，共 {msg.meta.rows.length} 行
                        </div>
                      )}
                    </div>
                  )}
                </div>
              </details>
            )}
          </div>
        ))}

        {/* Loading indicator */}
        {loading && (
          <div className="flex items-center gap-2 px-2 text-xs text-muted-foreground">
            <Loader2 className="h-3.5 w-3.5 animate-spin" />
            Agent 正在查询数据库...
          </div>
        )}
      </div>

      {/* Upload queue */}
      {uploadQueue.length > 0 && (
        <div className="border-t border-border px-3 py-2">
          <div className="flex flex-wrap gap-2">
            {uploadQueue.map((item) => (
              <div
                key={item.id}
                className={`group relative flex items-center gap-1.5 rounded-md border px-2 py-1 text-xs ${
                  item.status === 'error'
                    ? 'border-short/30 bg-short/10 text-short'
                    : item.status === 'uploading'
                      ? 'border-primary/30 bg-primary/10 text-primary'
                      : 'border-long/30 bg-long/10 text-long'
                }`}
              >
                {item.status === 'uploading' && (
                  <Loader2 className="h-3 w-3 animate-spin" />
                )}
                {item.status === 'success' && item.preview.startsWith('/uploads/') && (
                  item.file.type.startsWith('image/') ? (
                    <img
                      src={item.preview}
                      alt={item.file.name}
                      className="h-5 w-5 rounded object-cover"
                    />
                  ) : (
                    <span>📎</span>
                  )
                )}
                {item.status === 'success' && !item.preview.startsWith('/uploads/') && (
                  <ImageIcon className="h-3 w-3" />
                )}
                <span className="max-w-[120px] truncate">{item.file.name}</span>
                {item.error && <span className="text-[10px] opacity-70">{item.error}</span>}
                <button
                  onClick={() => removeUploadItem(item.id)}
                  className="ml-0.5 opacity-50 hover:opacity-100 transition-opacity"
                  aria-label="移除"
                >
                  <X className="h-3 w-3" />
                </button>
              </div>
            ))}
          </div>
        </div>
      )}

      {/* Input */}
      <div className="border-t border-border p-3">
        <div className="flex items-end gap-2">
          <textarea
            ref={inputRef}
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={handleKeyDown}
            placeholder="问我任何关于交易数据的问题..."
            rows={2}
            className="min-h-[44px] flex-1 resize-none rounded-lg border border-border bg-background px-3 py-2.5 text-sm text-foreground placeholder:text-muted-foreground/60 focus:border-primary/50 focus:outline-none focus:ring-1 focus:ring-primary/30"
            disabled={loading}
          />
          {/* File upload button */}
          <button
            type="button"
            onClick={() => fileInputRef.current?.click()}
            disabled={loading}
            className="flex h-10 w-10 shrink-0 items-center justify-center rounded-lg border border-border bg-background text-muted-foreground transition-colors hover:bg-secondary hover:text-foreground disabled:cursor-not-allowed disabled:opacity-40"
            aria-label="上传附件"
            title="上传图片或文档（最大 5MB）"
          >
            <Paperclip className="h-4 w-4" />
          </button>
          <input
            ref={fileInputRef}
            type="file"
            accept="image/jpeg,image/png,image/gif,image/webp,application/pdf,text/plain,text/csv,text/html"
            multiple
            onChange={handleFileSelect}
            className="hidden"
          />
          <button
            onClick={handleSend}
            disabled={(!input.trim() && !hasAttachments) || loading}
            className="flex h-10 w-10 shrink-0 items-center justify-center rounded-lg bg-primary text-primary-foreground transition-opacity hover:opacity-90 disabled:cursor-not-allowed disabled:opacity-40"
            aria-label="发送消息"
          >
            {loading ? (
              <Loader2 className="h-4 w-4 animate-spin" />
            ) : (
              <Send className="h-4 w-4" />
            )}
          </button>
        </div>
      </div>
    </section>
  )
}
