import { useEffect, useMemo, useRef, useState } from 'react'
import type { ChartPayload } from '../types'

const PAD = { top: 14, right: 62, bottom: 24, left: 10 }

function useElementWidth<T extends HTMLElement>() {
  const ref = useRef<T | null>(null)
  const [width, setWidth] = useState(900)
  useEffect(() => {
    const el = ref.current
    if (!el) return
    const update = () => setWidth(el.clientWidth || 900)
    update()
    const observer = new ResizeObserver(update)
    observer.observe(el)
    return () => observer.disconnect()
  }, [])
  return { ref, width }
}

const fmtPrice = (value: number) => {
  if (value >= 1000) return value.toLocaleString(undefined, { maximumFractionDigits: 0 })
  if (value >= 10) return value.toFixed(2)
  if (value >= 1) return value.toFixed(3)
  return value.toFixed(5)
}

const fmtTime = (ms: number, withDate = false) => {
  const d = new Date(ms)
  const time = d.toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' })
  if (!withDate) return time
  return `${d.toLocaleDateString(undefined, { month: 'short', day: 'numeric' })} ${time}`
}

export default function CandleChart({ chart, height = 430 }: { chart: ChartPayload; height?: number }) {
  const { ref, width } = useElementWidth<HTMLDivElement>()
  const [hover, setHover] = useState<number | null>(null)

  const { series, lines, time } = chart
  const n = time.length
  const plotW = Math.max(width - PAD.left - PAD.right, 120)
  const plotH = height - PAD.top - PAD.bottom
  const volH = Math.min(64, plotH * 0.22)
  const priceH = plotH - volH - 8

  const { min, max, ticks, step } = useMemo(() => {
    const values: number[] = []
    const push = (arr?: (number | null)[]) => arr?.forEach((v) => v != null && values.push(v))
    push(series.high)
    push(series.low)
    Object.values(lines).forEach((l) => push(l.values))
    if (!values.length) return { min: 0, max: 1, ticks: [0, 1], step: plotW / Math.max(n, 1) }
    let lo = Math.min(...values)
    let hi = Math.max(...values)
    const pad = (hi - lo) * 0.06 || hi * 0.01 || 1
    lo -= pad
    hi += pad
    const count = 4
    const raw = (hi - lo) / count
    const mag = Math.pow(10, Math.floor(Math.log10(raw || 1)))
    const norm = raw / mag
    const niceStep = (norm >= 5 ? 10 : norm >= 2 ? 5 : norm >= 1 ? 2 : 1) * mag
    const first = Math.ceil(lo / niceStep) * niceStep
    const out: number[] = []
    for (let v = first; v <= hi; v += niceStep) out.push(Number(v.toFixed(10)))
    return { min: lo, max: hi, ticks: out, step: plotW / Math.max(n, 1) }
  }, [series, lines, n, plotW])

  const x = (i: number) => PAD.left + i * step + step / 2
  const y = (price: number) => PAD.top + priceH - ((price - min) / (max - min || 1)) * priceH

  const volumes = series.volume ?? []
  const maxVol = Math.max(...volumes.map((v) => v ?? 0), 1)
  const volTop = PAD.top + priceH + 8

  const timeTicks = useMemo(() => {
    const target = Math.max(3, Math.floor(plotW / 110))
    const stride = Math.max(1, Math.floor(n / target))
    const out: number[] = []
    for (let i = n - 1; i >= 0; i -= stride) out.push(i)
    return out.reverse()
  }, [n, plotW])

  const onMove = (event: React.MouseEvent<SVGSVGElement>) => {
    const rect = event.currentTarget.getBoundingClientRect()
    const localX = ((event.clientX - rect.left) / rect.width) * width
    const index = Math.round((localX - PAD.left - step / 2) / step)
    setHover(index >= 0 && index < n ? index : null)
  }

  const hoverCandle = hover != null && series.close ? {
    open: series.open?.[hover],
    high: series.high?.[hover],
    low: series.low?.[hover],
    close: series.close[hover],
    volume: series.volume?.[hover],
    time: time[hover],
  } : null

  const last = series.close?.[n - 1]
  const previous = series.close?.[n - 2]
  const up = last != null && previous != null && last >= previous

  return (
    <div className="chart-wrap" ref={ref}>
      <div className="legend">
        {Object.entries(lines).map(([key, line]) => (
          <span className="legend-item" key={key}>
            <span className="legend-swatch" style={{ background: line.colour }} />
            {line.label}
          </span>
        ))}
        <span className="legend-item" style={{ marginLeft: 'auto' }}>
          <span className="legend-swatch" style={{ background: 'var(--green)' }} />Long trigger
          <span className="legend-swatch" style={{ background: 'var(--red)', marginLeft: 10 }} />Short trigger
        </span>
      </div>
      <svg
        className="chart-svg"
        viewBox={`0 0 ${width} ${height}`}
        height={height}
        onMouseMove={onMove}
        onMouseLeave={() => setHover(null)}
      >
        <defs>
          <linearGradient id="volUp" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stopColor="rgba(47,214,163,0.42)" />
            <stop offset="100%" stopColor="rgba(47,214,163,0.05)" />
          </linearGradient>
          <linearGradient id="volDown" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stopColor="rgba(249,112,122,0.42)" />
            <stop offset="100%" stopColor="rgba(249,112,122,0.05)" />
          </linearGradient>
        </defs>

        {ticks.map((tick) => (
          <g key={tick}>
            <line x1={PAD.left} x2={PAD.left + plotW} y1={y(tick)} y2={y(tick)} stroke="rgba(255,255,255,0.045)" strokeDasharray="2 4" />
            <text x={PAD.left + plotW + 8} y={y(tick) + 3.5} fill="#607088" fontSize="10.5" fontFamily="var(--mono)">
              {fmtPrice(tick)}
            </text>
          </g>
        ))}

        {timeTicks.map((i) => (
          <text key={i} x={x(i)} y={height - 6} fill="#607088" fontSize="10.5" textAnchor="middle" fontFamily="var(--mono)">
            {fmtTime(time[i], i === timeTicks[0])}
          </text>
        ))}

        {series.volume && volumes.map((v, i) => {
          const h = ((v ?? 0) / maxVol) * volH
          const rising = (series.close?.[i] ?? 0) >= (series.open?.[i] ?? 0)
          return (
            <rect
              key={i}
              x={x(i) - Math.max(step * 0.32, 0.8)}
              y={volTop + volH - h}
              width={Math.max(step * 0.64, 1.4)}
              height={Math.max(h, 0.5)}
              fill={rising ? 'url(#volUp)' : 'url(#volDown)'}
            />
          )
        })}

        {(series.open && series.high && series.low && series.close) && series.open.map((open, i) => {
          const close = series.close![i]
          const high = series.high![i]
          const low = series.low![i]
          if (open == null || close == null || high == null || low == null) return null
          const rising = close >= open
          const colour = rising ? '#2fd6a3' : '#f9707a'
          const bodyTop = y(Math.max(open, close))
          const bodyBottom = y(Math.min(open, close))
          const bodyW = Math.max(step * 0.62, 1.2)
          return (
            <g key={i}>
              <line x1={x(i)} x2={x(i)} y1={y(high)} y2={y(low)} stroke={colour} strokeWidth={Math.max(step * 0.09, 0.6)} opacity={0.85} />
              <rect
                x={x(i) - bodyW / 2}
                y={bodyTop}
                width={bodyW}
                height={Math.max(bodyBottom - bodyTop, 1)}
                fill={rising ? colour : colour}
                opacity={rising ? 0.9 : 0.85}
                rx={step > 5 ? 1 : 0}
              />
            </g>
          )
        })}

        {Object.entries(lines).map(([key, line]) => {
          const points = line.values
            .map((v, i) => (v == null ? null : `${x(i)},${y(v)}`))
            .filter(Boolean)
            .join(' ')
          return <polyline key={key} points={points} fill="none" stroke={line.colour} strokeWidth={1.4} opacity={0.9} strokeLinejoin="round" />
        })}

        {chart.markers.map((marker, i) => {
          const cx = x(marker.index)
          const long = marker.side === 'LONG'
          const cy = long ? y(marker.price) + 12 : y(marker.price) - 12
          return (
            <g key={`${marker.index}-${i}`}>
              <path
                d={long ? `M ${cx} ${cy - 7} L ${cx + 5} ${cy} L ${cx - 5} ${cy} Z` : `M ${cx} ${cy + 7} L ${cx + 5} ${cy} L ${cx - 5} ${cy} Z`}
                fill={long ? '#2fd6a3' : '#f9707a'}
                opacity={0.95}
              />
            </g>
          )
        })}

        {last != null && (
          <g>
            <line x1={PAD.left} x2={PAD.left + plotW} y1={y(last)} y2={y(last)} stroke={up ? 'rgba(47,214,163,0.45)' : 'rgba(249,112,122,0.45)'} strokeDasharray="3 3" />
            <rect x={PAD.left + plotW + 2} y={y(last) - 9} width={PAD.right - 6} height={18} rx={4} fill={up ? '#186b51' : '#7a2b32'} />
            <text x={PAD.left + plotW + 6} y={y(last) + 4} fill="#eaf5ff" fontSize="10.5" fontFamily="var(--mono)">
              {fmtPrice(last)}
            </text>
          </g>
        )}

        {hover != null && (
          <g>
            <line x1={x(hover)} x2={x(hover)} y1={PAD.top} y2={PAD.top + plotH} stroke="rgba(255,255,255,0.18)" strokeDasharray="3 3" />
          </g>
        )}
      </svg>

      {hoverCandle && (
        <div
          className="tooltip"
          style={{
            left: Math.min(Math.max(x(hover!) + 14, 8), Math.max(width - 170, 8)),
            top: 30,
          }}
        >
          <div className="row muted tiny" style={{ marginBottom: 4 }}><span>{fmtTime(hoverCandle.time, true)}</span></div>
          <div className="row"><span className="muted">O</span><span className="mono">{hoverCandle.open != null ? fmtPrice(hoverCandle.open) : '—'}</span></div>
          <div className="row"><span className="muted">H</span><span className="mono">{hoverCandle.high != null ? fmtPrice(hoverCandle.high) : '—'}</span></div>
          <div className="row"><span className="muted">L</span><span className="mono">{hoverCandle.low != null ? fmtPrice(hoverCandle.low) : '—'}</span></div>
          <div className="row"><span className="muted">C</span><span className="mono">{hoverCandle.close != null ? fmtPrice(hoverCandle.close) : '—'}</span></div>
          {hoverCandle.volume != null && (
            <div className="row"><span className="muted">Vol</span><span className="mono">{Math.round(hoverCandle.volume).toLocaleString()}</span></div>
          )}
        </div>
      )}
    </div>
  )
}
