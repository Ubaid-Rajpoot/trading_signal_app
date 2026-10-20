import { useEffect, useMemo, useRef, useState } from 'react'
import type { OscillatorPane } from '../types'

const PAD = { top: 10, right: 62, bottom: 18, left: 10 }

const fmt = (v: number) => v.toFixed(1)

/**
 * Oscillator sub-pane drawn under the price chart (TDI for the shift strategy).
 * Shares the same bar spacing as the price chart so candles and crosses line up.
 */
export default function OscillatorChart({
  pane,
  bars,
  height = 168,
}: {
  pane: OscillatorPane
  bars: number
  height?: number
}) {
  const ref = useRef<HTMLDivElement | null>(null)
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

  const plotW = Math.max(width - PAD.left - PAD.right, 120)
  const plotH = height - PAD.top - PAD.bottom
  const step = plotW / Math.max(bars, 1)

  const { min, max } = useMemo(() => {
    const values: number[] = [...pane.levels]
    Object.values(pane.lines).forEach((line) => line.values.forEach((v) => v != null && values.push(v)))
    pane.bands.forEach((band) => {
      band.upper.forEach((v) => v != null && values.push(v))
      band.lower.forEach((v) => v != null && values.push(v))
    })
    if (!values.length) return { min: 0, max: 100 }
    const lo = Math.min(...values)
    const hi = Math.max(...values)
    const pad = (hi - lo) * 0.06 || 1
    return { min: Math.max(0, lo - pad), max: Math.min(100, hi + pad) }
  }, [pane])

  const x = (i: number) => PAD.left + i * step + step / 2
  const y = (v: number) => PAD.top + plotH - ((v - min) / (max - min || 1)) * plotH

  const path = (values: (number | null)[]) =>
    values
      .map((v, i) => (v == null ? null : `${x(i).toFixed(2)},${y(v).toFixed(2)}`))
      .filter(Boolean)
      .join(' ')

  const bandArea = (band: { upper: (number | null)[]; lower: (number | null)[] }) => {
    const top = band.upper.map((v, i) => (v == null ? null : `${x(i).toFixed(2)},${y(v).toFixed(2)}`)).filter(Boolean)
    const bottom = band.lower
      .map((v, i) => (v == null ? null : `${x(i).toFixed(2)},${y(v).toFixed(2)}`))
      .filter(Boolean)
      .reverse()
    return [...top, ...bottom].join(' ')
  }

  return (
    <div ref={ref} style={{ width: '100%' }}>
      <div className="legend" style={{ paddingBottom: 2 }}>
        <span className="legend-item" style={{ color: 'var(--muted-2)' }}>
          {pane.title} · purple crosses the base line with speed = HVB
        </span>
        {Object.entries(pane.lines).map(([key, line]) => (
          <span className="legend-item" key={key}>
            <span className="legend-swatch" style={{ background: line.colour, height: line.width > 1.5 ? 3 : 2 }} />
            {line.label}
          </span>
        ))}
        <span className="legend-item" style={{ marginLeft: 'auto' }}>
          <span className="legend-swatch" style={{ background: '#facc15' }} />HVB
        </span>
      </div>
      <svg className="chart-svg" viewBox={`0 0 ${width} ${height}`} height={height}>
        {pane.levels.map((level) => (
          <g key={level}>
            <line
              x1={PAD.left}
              x2={PAD.left + plotW}
              y1={y(level)}
              y2={y(level)}
              stroke={level === 50 ? 'rgba(255,255,255,0.16)' : 'rgba(255,255,255,0.06)'}
              strokeDasharray="3 4"
            />
            <text x={PAD.left + plotW + 8} y={y(level) + 3.5} fill="#607088" fontSize="10" fontFamily="var(--mono)">
              {level}
            </text>
          </g>
        ))}

        {pane.bands.map((band, i) => (
          <g key={i}>
            <polygon points={bandArea(band)} fill={band.colour} opacity={0.07} />
            <polyline points={path(band.upper)} fill="none" stroke={band.colour} strokeWidth={1} opacity={0.65} />
            <polyline points={path(band.lower)} fill="none" stroke={band.colour} strokeWidth={1} opacity={0.65} />
          </g>
        ))}

        {Object.entries(pane.lines).map(([key, line]) => (
          <polyline
            key={key}
            points={path(line.values)}
            fill="none"
            stroke={line.colour}
            strokeWidth={line.width}
            strokeLinejoin="round"
            opacity={0.95}
          />
        ))}

        {pane.markers.map((marker, i) => {
          const cx = x(marker.index)
          const long = marker.side === 'LONG'
          return (
            <rect
              key={i}
              x={cx - Math.max(step * 0.45, 2)}
              y={PAD.top + 2}
              width={Math.max(step * 0.9, 4)}
              height={plotH - 4}
              fill="#facc15"
              opacity={0.16}
              rx={2}
            />
          )
        })}
      </svg>
    </div>
  )
}
