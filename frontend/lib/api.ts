export const API_BASE = '/api'

export async function apiGet<T>(path: string): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`)
  if (!response.ok) throw new Error(`${path} ${response.status}`)
  return response.json() as Promise<T>
}

export async function apiSend<T>(path: string, method: 'POST' | 'PUT', body?: unknown): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`, {
    method,
    headers: { 'Content-Type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
  })
  if (!response.ok) {
    const detail = await response.text()
    throw new Error(detail || `${path} ${response.status}`)
  }
  return response.json() as Promise<T>
}
