import type { Action, Settings } from '../types'

export const fmtPrice = (value: number | null | undefined): string => {
  if (value == null || Number.isNaN(value)) return '—'
  if (Math.abs(value) >= 1000) return value.toLocaleString(undefined, { maximumFractionDigits: 2 })
  if (Math.abs(value) >= 10) return value.toFixed(3)
  if (Math.abs(value) >= 1) return value.toFixed(4)
  return value.toFixed(6)
}

export const fmtPct = (value: number | null | undefined, digits = 2): string => {
  if (value == null || Number.isNaN(value)) return '—'
  const sign = value > 0 ? '+' : ''
  return `${sign}${value.toFixed(digits)}%`
}

export const fmtTime = (ms: number | null | undefined): string => {
  if (!ms) return '—'
  const d = new Date(ms)
  return d.toLocaleString(undefined, {
    month: 'short',
    day: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
  })
}

export const fmtTimeShort = (ms: number | null | undefined): string => {
  if (!ms) return '—'
  const d = new Date(ms)
  return d.toLocaleString(undefined, { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' })
}

export const fmtAgo = (ms: number | null | undefined): string => {
  if (!ms) return '—'
  const seconds = Math.max(0, Math.round((Date.now() - ms) / 1000))
  if (seconds < 60) return `${seconds}s ago`
  const minutes = Math.round(seconds / 60)
  if (minutes < 60) return `${minutes}m ago`
  const hours = Math.round(minutes / 60)
  if (hours < 24) return `${hours}h ago`
  return `${Math.round(hours / 24)}d ago`
}

export type ChipTone = 'buy' | 'sell' | 'hold' | 'wait' | 'exit'

export const actionTone = (action: Action | string | undefined): ChipTone => {
  switch (action) {
    case 'BUY': return 'buy'
    case 'SELL': return 'sell'
    case 'HOLD LONG':
    case 'HOLD SHORT': return 'hold'
    case 'EXIT': return 'exit'
    default: return 'wait'
  }
}

export function ActionChip({ action, large = false }: { action: Action | string; large?: boolean }) {
  return (
    <span className={`chip ${actionTone(action)}`} style={large ? { fontSize: 12, padding: '4px 10px' } : undefined}>
      {action}
    </span>
  )
}

export function Spinner() {
  return <span className="spinner" />
}

export function StatTile({ label, value, sub, tone }: { label: string; value: string; sub?: string; tone?: 'pos' | 'neg' }) {
  return (
    <div className="stat">
      <div className="label">{label}</div>
      <div className={`value ${tone ?? ''}`}>{value}</div>
      {sub && <div className="sub">{sub}</div>}
    </div>
  )
}

export function Ring({ value, label, tone = 'var(--green)' }: { value: number; label: string; tone?: string }) {
  const size = 62
  const stroke = 5
  const radius = (size - stroke) / 2
  const circumference = 2 * Math.PI * radius
  const offset = circumference * (1 - Math.max(0, Math.min(100, value)) / 100)
  return (
    <div className="hstack" style={{ gap: 12 }}>
      <svg width={size} height={size} viewBox={`0 0 ${size} ${size}`} style={{ transform: 'rotate(-90deg)' }}>
        <circle cx={size / 2} cy={size / 2} r={radius} fill="none" stroke="rgba(255,255,255,0.08)" strokeWidth={stroke} />
        <circle
          cx={size / 2}
          cy={size / 2}
          r={radius}
          fill="none"
          stroke={tone}
          strokeWidth={stroke}
          strokeLinecap="round"
          strokeDasharray={circumference}
          strokeDashoffset={offset}
          style={{ transition: 'stroke-dashoffset 0.5s ease' }}
        />
        <text
          x="50%"
          y="52%"
          textAnchor="middle"
          dominantBaseline="middle"
          fill="var(--text)"
          fontSize="15"
          fontWeight="650"
          fontFamily="var(--mono)"
          style={{ transform: 'rotate(90deg)', transformOrigin: 'center' }}
        >
          {value}
        </text>
      </svg>
      <div>
        <div className="ring-label">{label}</div>
        <div className="ring-value muted">{value}%</div>
      </div>
    </div>
  )
}

export function ErrorBox({ message, onRetry }: { message: string; onRetry?: () => void }) {
  return (
    <div className="error-box row-between">
      <span>{message}</span>
      {onRetry && <button className="btn sm ghost" onClick={onRetry}>Retry</button>}
    </div>
  )
}

export function Field({ label, hint, children }: { label: string; hint?: string; children: React.ReactNode }) {
  return (
    <div className="field">
      <label>{label}</label>
      {children}
      {hint && <span className="hint">{hint}</span>}
    </div>
  )
}

export function Switch({ checked, onChange, label }: { checked: boolean; onChange: (v: boolean) => void; label?: string }) {
  return (
    <label className="hstack" style={{ cursor: 'pointer', gap: 9 }}>
      <span className="switch">
        <input type="checkbox" checked={checked} onChange={(e) => onChange(e.target.checked)} />
        <span />
      </span>
      {label && <span className="small">{label}</span>}
    </label>
  )
}

export function Section({ title, right, children, bodyStyle }: { title: string; right?: React.ReactNode; children: React.ReactNode; bodyStyle?: React.CSSProperties }) {
  return (
    <section className="panel">
      <header className="panel-head">
        <span className="panel-title">{title}</span>
        {right && <span className="right">{right}</span>}
      </header>
      <div className="panel-body" style={bodyStyle}>{children}</div>
    </section>
  )
}

export type { Settings }
