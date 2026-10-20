export type Action = 'BUY' | 'SELL' | 'HOLD LONG' | 'HOLD SHORT' | 'EXIT' | 'WAIT'
export type Bias = 'LONG' | 'SHORT' | 'NEUTRAL'

export interface ParamSpec {
  key: string
  label: string
  kind: 'int' | 'float' | 'bool'
  default: number | boolean
  min: number | null
  max: number | null
  step: number | null
  help: string
}

export interface StrategySchema {
  id: string
  name: string
  description: string
  market: string
  timeframe_hint: string
  allows_short: boolean
  params: ParamSpec[]
  defaults: Record<string, number | boolean>
  plot_lines: { column: string; label: string; colour: string }[]
}

export interface Meta {
  app: string
  version: string
  strategies: StrategySchema[]
  timeframes: { value: string; label: string }[]
  scan_timeframes: string[]
  symbols: { symbol: string; base: string; name: string }[]
  default_watchlist: string[]
  default_strategy: string
  provider: { mode: 'live' | 'demo'; reason: string }
  settings: Settings
}

export interface Check {
  label: string
  passed: boolean
  detail: string
}

export interface Plan {
  side: Bias
  entry: number | null
  stop: number | null
  take_profits: (number | null)[]
  risk_reward: (number | null)[]
  risk_pct: number | null
  is_live: boolean
  note: string
}

export interface PaneLine {
  label: string
  colour: string
  width: number
  values: (number | null)[]
}

export interface OscillatorPane {
  title: string
  levels: number[]
  lines: Record<string, PaneLine>
  bands: { colour: string; upper: (number | null)[]; lower: (number | null)[] }[]
  markers: { index: number; side: string }[]
}

export interface ChartPayload {
  time: number[]
  series: Record<string, (number | null)[]>
  lines: Record<string, { label: string; colour: string; values: (number | null)[] }>
  markers: { index: number; side: 'LONG' | 'SHORT'; price: number }[]
  pane: OscillatorPane | null
}

export interface Setup {
  status: 'TRIGGERED' | 'ARMED' | 'FORMING' | 'IDLE'
  direction: 'LONG' | 'SHORT' | null
  level: number | null
  distance_pct: number | null
  hvb_bars_ago: number | null
  text: string
  tdi: { purple: number | null; base: number | null; slow: number | null; side: string | null } | null
}

export interface RadarCell {
  status: 'TRIGGERED' | 'ARMED' | 'FORMING' | 'IDLE' | 'ERROR'
  direction?: 'LONG' | 'SHORT' | null
  level?: number | null
  distance_pct?: number | null
  price?: number
  change_pct?: number
  tdi?: { purple: number | null; base: number | null; slow: number | null; side: string | null } | null
  text?: string
  source?: string
  error?: string
}

export interface RadarResponse {
  generated_at: number
  symbols: string[]
  timeframes: string[]
  grid: { symbol: string; cells: Record<string, RadarCell> }[]
  actionable: ({ symbol: string; timeframe: string } & RadarCell)[]
}

export interface WinRateRow {
  timeframe: string
  label: string
  bars?: number
  source?: string
  trades?: number
  wins?: number
  losses?: number
  win_rate_pct?: number | null
  profit_factor?: number | null
  total_return_pct?: number
  buy_hold_return_pct?: number
  avg_win_pct?: number
  avg_loss_pct?: number
  max_drawdown_pct?: number
  exposure_pct?: number
  avg_bars_held?: number
  error?: string
}

export interface WinRateReport {
  symbol: string
  days: number
  strategy: string
  params: Record<string, number | boolean>
  generated_at: number
  sources: string[]
  is_real_data: boolean
  rows: WinRateRow[]
  summary: { timeframes_tested: number; trades: number; wins: number; losses: number; win_rate_pct: number | null; best_timeframe: string | null }
  caveat: string
}

export interface Signal {
  setup: Setup
  symbol: string
  timeframe: string
  timeframe_label: string
  strategy: { id: string; name: string; uses_stops: boolean }
  params: Record<string, number | boolean>
  source: 'live' | 'cache' | 'demo'
  generated_at: number
  bar_time: number
  price: number
  change_pct: number
  action: Action
  action_reason: string
  bias: Bias
  confidence: number
  confidence_label: string
  confidence_notes: string[]
  readiness: number | null
  last_trigger: { side: 'LONG' | 'SHORT'; time: number; price: number; bars_ago: number } | null
  state: {
    position: 'LONG' | 'SHORT' | 'FLAT'
    since: number | null
    entry_price: number | null
    bars_in_state: number | null
    unrealized_pct: number | null
  }
  checks: Check[]
  plan: Plan
  indicators: Record<string, number>
  chart: ChartPayload
}

export interface ScanRow {
  symbol: string
  price?: number
  change_pct?: number
  action?: Action
  bias?: Bias
  confidence?: number
  state?: string
  unrealized_pct?: number | null
  distance_to_stop_pct?: number | null
  source?: string
  error?: string | null
}

export interface ScanResponse {
  timeframe: string
  strategy: string
  rows: ScanRow[]
  symbols: string[]
}

export interface Trade {
  side: 'LONG' | 'SHORT'
  entry_time: number
  exit_time: number
  entry_price: number
  exit_price: number
  qty: number
  stop: number
  target: number | null
  pnl: number
  pnl_pct: number
  r_multiple: number
  bars_held: number
  exit_reason: string
  equity_after: number
}

export interface BacktestMetrics {
  start: number
  end: number
  bars: number
  initial_equity: number
  final_equity: number
  total_return_pct: number
  buy_hold_return_pct: number
  cagr_pct: number
  max_drawdown_pct: number
  sharpe: number
  sortino: number
  trades: number
  wins: number
  losses: number
  win_rate_pct: number
  profit_factor: number | null
  expectancy_r: number
  avg_win_pct: number
  avg_loss_pct: number
  best_trade_pct: number
  worst_trade_pct: number
  avg_bars_held: number
  exposure_pct: number
  config: Record<string, number>
}

export interface BacktestResult {
  symbol: string
  timeframe: string
  source: string
  strategy: { id: string; name: string }
  params: Record<string, number | boolean>
  metrics: BacktestMetrics
  trades: Trade[]
  equity_curve: { time: number; equity: number; price: number }[]
}

export interface AlertSettings {
  enabled: boolean
  min_confidence: number
  telegram_enabled: boolean
  telegram_bot_token: string
  telegram_chat_id: string
  webhook_url: string
  telegram_configured?: boolean
}

export interface Settings {
  watchlist: string[]
  timeframe: string
  strategy: string
  refresh_seconds: number
  alerts: AlertSettings
}

export interface StoredSignal {
  id: number
  symbol: string
  timeframe: string
  strategy: string
  action: Action
  side: string
  price: number
  confidence: number
  bar_time: number
  created_at: number
  plan: Plan | null
  checks: Check[]
}

export interface NotificationRow {
  id: number
  signal_id: number | null
  channel: string
  status: string
  detail: string
  created_at: number
}

export interface SchedulerStatus {
  running: boolean
  last_run: number | null
  last_duration_ms: number | null
  scans: number
  signals_fired: number
  alerts_sent: number
  last_error: string | null
}
