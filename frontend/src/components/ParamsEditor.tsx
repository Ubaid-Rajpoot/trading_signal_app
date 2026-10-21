import type { StrategySchema } from '../types'

interface Props {
  schema: StrategySchema
  values: Record<string, number | boolean | string>
  onChange: (next: Record<string, number | boolean | string>) => void
  compact?: boolean
}

export default function ParamsEditor({ schema, values, onChange, compact = false }: Props) {
  const set = (key: string, value: number | boolean | string) => onChange({ ...values, [key]: value })
  const reset = () => onChange({ ...schema.defaults } as Record<string, number | boolean | string>)

  return (
    <div className="stack" style={{ gap: 10 }}>
      <div className="row-between">
        <span className="tiny muted">Parameters override the strategy defaults for this run only.</span>
        <button className="btn sm ghost" onClick={reset}>Reset</button>
      </div>
      <div className={compact ? 'grid-3' : 'grid-2'} style={{ gap: 10 }}>
        {schema.params.map((param) => {
          const value = values[param.key] ?? param.default
          if (param.kind === 'select') {
            return (
              <div className="field" key={param.key} title={param.help}>
                <label>{param.label}</label>
                <select className="select" value={String(value)} onChange={(e) => set(param.key, e.target.value)}>
                  {(param.options ?? []).map((opt) => (
                    <option key={opt} value={opt}>{opt}</option>
                  ))}
                </select>
              </div>
            )
          }
          if (param.kind === 'bool') {
            return (
              <div key={param.key} className="field" style={{ justifyContent: 'center' }}>
                <label>{param.label}</label>
                <label className="hstack" style={{ cursor: 'pointer' }}>
                  <span className="switch">
                    <input type="checkbox" checked={Boolean(value)} onChange={(e) => set(param.key, e.target.checked)} />
                    <span />
                  </span>
                  <span className="small muted">{value ? 'On' : 'Off'}</span>
                </label>
              </div>
            )
          }
          return (
            <div className="field" key={param.key} title={param.help}>
              <label>{param.label}</label>
              <input
                className="input sm mono"
                type="number"
                value={Number(value)}
                min={param.min ?? undefined}
                max={param.max ?? undefined}
                step={param.step ?? (param.kind === 'int' ? 1 : 0.1)}
                onChange={(e) => {
                  const next = Number(e.target.value)
                  if (!Number.isNaN(next)) set(param.key, param.kind === 'int' ? Math.round(next) : next)
                }}
              />
            </div>
          )
        })}
      </div>
    </div>
  )
}
