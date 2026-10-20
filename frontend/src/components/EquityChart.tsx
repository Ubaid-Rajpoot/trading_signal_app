import { useEffect, useMemo, useRef, useState } from 'react'
import type { BacktestResult } from '../types'

const PAD = { top: 16, right: 64, bottom: 24, left: 10 }

export default function EquityChart({ result, height = 300 }: { result: BacktestResult; height?: number }) {
  const ref = useRef<HTMLDivElement | null>(null)
  const [width, setWidth] = useState(900)
  useEffect(() => {
    const el = ref.current
    if (!el) return
    const update = () => setWidth(el.clientWidth || 900)
    update()
    const obs = new ResizeObserver(update)
    obs.observe(el)
    return () => obs.disconnect()
  }, [])

  const { equity, benchmark, start, end } = useMemo(() => {
    const curve = result.equity_curve
    const eq = curve.map((p) => p.equity)
    const initial = eq[0] || 1
    const bench = curve.map((p) => (p.price / (curve[0]?.price || 1)) * initial)
    return { equity: eq, benchmark: bench, start: curve[0]?.time ?? 0, end: curve[curve.length - 1]?.time ?? 0 }
  }, [result])

  const plotW = Math.max(width - PAD.left - PAD.right, 120)
  const plotH = height - PAD.top - PAD.bottom
  const n = equity.length

  const values = [...equity, ...benchmark]
  const lo = Math.min(...values)
  const hi = Math.max(...values)
  const pad = (hi - lo) * 0.08 || hi * 0.02 || 1
  const min = lo - pad
  const max = hi + pad

  const x = (i: number) => PAD.left + (i / Math.max(n - 1, 1)) * plotW
  const y = (v: number) => PAD.top + plotH - ((v - min) / (max - min || 1)) * plotH

  const line = (arr: number[]) => arr.map((v, i) => `${x(i).toFixed(2)},${y(v).toFixed(2)}`).join(' ')
  const area = `${PAD.left},${PAD.top + plotH} ${line(equity)} ${PAD.left + plotW},${PAD.top + plotH}`
  const ticks = [0, 0.25, 0.5, 0.75, 1].map((f) => min + (max - min) * f)
  const positive = equity[n - 1] >= equity[0]

  const fmt = (v: number) => `$${Math.round(v).toLocaleString()}`

  return (
    <div ref={ref} style={{ width: '100%' }}>
      <div className="legend">
        <span className="legend-item"><span className="legend-swatch" style={{ background: 'var(--green)' }} />Strategy equity</span>
        <span className="legend-item"><span className="legend-swatch" style={{ background: '#4b5a70' }} />Buy &amp; hold</span>
        <span className="legend-item" style={{ marginLeft: 'auto' }}>
          {new Date(start).toLocaleDateString()} → {new Date(end).toLocaleDateString()}
        </span>
      </div>
      <svg className="chart-svg" viewBox={`0 0 ${width} ${height}`} height={height}>
        <defs>
          <linearGradient id="equityFill" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stopColor={positive ? 'rgba(47,214,163,0.30)' : 'rgba(249,112,122,0.30)'} />
            <stop offset="100%" stopColor="rgba(47,214,163,0.01)" />
          </linearGradient>
        </defs>
        {ticks.map((tick) => (
          <g key={tick}>
            <line x1={PAD.left} x2={PAD.left + plotW} y1={y(tick)} y2={y(tick)} stroke="rgba(255,255,255,0.045)" strokeDasharray="2 4" />
            <text x={PAD.left + plotW + 8} y={y(tick) + 3.5} fill="#607088" fontSize="10.5" fontFamily="var(--mono)">{fmt(tick)}</text>
          </g>
        ))}
        <polygon points={area} fill="url(#equityFill)" />
        <polyline points={line(benchmark)} fill="none" stroke="#4b5a70" strokeWidth={1.3} strokeDasharray="4 4" />
        <polyline points={line(equity)} fill="none" stroke={positive ? '#2fd6a3' : '#f9707a'} strokeWidth={1.9} strokeLinejoin="round" />
        <line x1={PAD.left} x2={PAD.left + plotW} y1={y(equity[0])} y2={y(equity[0])} stroke="rgba(255,255,255,0.12)" strokeDasharray="3 4" />
      </svg>
    </div>
  )
}
