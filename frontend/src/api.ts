import type {
  BacktestResult,
  Meta,
  NotificationRow,
  ScanResponse,
  SchedulerStatus,
  Settings,
  Signal,
  StoredSignal,
} from './types'

const BASE = '/api'

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${BASE}${path}`, {
    headers: { 'Content-Type': 'application/json' },
    ...init,
  })
  if (!res.ok) {
    let message = `Request failed (${res.status})`
    try {
      const body = await res.json()
      if (body?.detail) message = typeof body.detail === 'string' ? body.detail : JSON.stringify(body.detail)
    } catch {
      /* keep the status message */
    }
    throw new Error(message)
  }
  return res.json() as Promise<T>
}

const qs = (params: Record<string, string | number | undefined>) => {
  const search = new URLSearchParams()
  Object.entries(params).forEach(([key, value]) => {
    if (value !== undefined && value !== '') search.set(key, String(value))
  })
  const out = search.toString()
  return out ? `?${out}` : ''
}

export const api = {
  meta: () => request<Meta>('/meta'),
  health: () => request<{ status: string; provider: { mode: string; reason: string }; stats: Record<string, unknown> }>('/health'),
  signal: (symbol: string, timeframe: string, strategy: string, params?: Record<string, unknown>) =>
    request<Signal>(`/signal${qs({ symbol, timeframe, strategy, params: params && Object.keys(params).length ? JSON.stringify(params) : undefined })}`),
  scan: (symbols: string[], timeframe: string, strategy: string, params?: Record<string, unknown>) =>
    request<ScanResponse>(
      `/scan${qs({
        symbols: symbols.join(','),
        timeframe,
        strategy,
        params: params && Object.keys(params).length ? JSON.stringify(params) : undefined,
      })}`,
    ),
  backtest: (body: Record<string, unknown>) =>
    request<BacktestResult>('/backtest', { method: 'POST', body: JSON.stringify(body) }),
  settings: () => request<Settings>('/settings'),
  saveSettings: (patch: Partial<Settings>) =>
    request<Settings>('/settings', { method: 'PUT', body: JSON.stringify(patch) }),
  history: (limit = 100) => request<{ items: StoredSignal[] }>(`/signals${qs({ limit })}`),
  alerts: () => request<{ items: NotificationRow[]; scheduler: SchedulerStatus }>('/alerts'),
  testAlert: (channel: 'telegram' | 'webhook') =>
    request<{ channel: string; status: string; detail: string }>(`/alerts/test${qs({ channel })}`, { method: 'POST' }),
}
