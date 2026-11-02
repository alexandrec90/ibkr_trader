/**
 * The JSON the FastAPI app (src/ibkr_trader/web/) serves, typed. Shapes mirror
 * `web/views.py`; a field added there is added here.
 */

export interface RunRow {
  run_id: number
  strategy: string
  account: string
  window: string
  created_at: string
  model_version: string | null
  universe_n: number | null
  benchmark: string | null
  has_curve: boolean
  total_return: number | null
  benchmark_total_return: number | null
  excess_return: number | null
  cagr: number | null
  sharpe: number | null
  sortino: number | null
  max_drawdown: number | null
  calmar: number | null
  n_trades: number | null
  trades_per_year: number | null
  costs_cad: number | null
  tax_cad: number | null
  end_value_cad: number | null
}

/** `[date, growth of 100, drawdown fraction <= 0]` per point. */
export type CurvePoint = [string, number, number]

export interface Curve {
  run_id: number
  label: string
  kind: 'strategy' | 'benchmark'
  points: CurvePoint[]
}

export interface Fold {
  fold: number | string
  train: [string, string] | null
  test: [string, string] | null
  ic: Record<string, number | null>
}

export interface TrainedModel {
  model: string | null
  version: string | null
  created_at: string | null
  feature_set_version: string | null
  label: string | null
  dataset: {
    rows: number | null
    dates: number | null
    span: [string, string] | null
    symbols: number | null
    survivorship: string
  }
  scheme: string | null
  purge_months: number | null
  folds: Fold[]
  overall_ic: Record<string, { mean: number | null; std: number | null }>
}

export interface Holding {
  symbol: string
  weight: number
}

export interface ShadowBook {
  strategy: string
  history: {
    asof: string
    model_version: string
    names: number
    invested: number
    turnover: number | null
  }[]
  latest: {
    asof: string
    model_version: string
    bar_asof: string | null
    holdings: Holding[]
  }
}

export interface Outcome {
  strategy: string
  asof: string
  horizon_end: string
  portfolio_return: number
  cost_return: number
  benchmark_return: number
  excess: number
}

export interface Shadow {
  books: ShadowBook[]
  performance: {
    asof: string
    horizons: { months: number; outcomes: Outcome[] }[]
    error: string | null
  }
}

export interface PaperOrder {
  id: number
  environment: string
  symbol: string
  side: string
  quantity: number
  order_type: string
  limit_price: number | null
  status: string
  placed_at: string | null
  avg_fill_price: number | null
  filled_quantity: number | null
  fills: number
  commission: number | null
}

export interface Ticket {
  side: string
  symbol: string
  quantity: number
  limit_price: number
  currency: string
  notional: number
  good_until: string
  note: string
}

export interface AccountPlan {
  account: string
  kind: string
  equity_cad: number
  available: Record<string, number>
  holdings: number
  orders: Ticket[]
  notes: string[]
  cash_left: Record<string, number>
}

export interface Plan {
  title: string
  strategy: string
  snapshot_asof: string
  today: string
  generated_at: string
  config: Record<string, unknown>
  notes: string[]
  accounts: AccountPlan[]
}

export interface PlanOptions {
  strategy: string
  min_order: number
  max_position: number
  max_new: number
  exits: boolean
}

export interface ReportFile {
  path: string
  modified: string
}

/** Fetch JSON, turning a non-2xx answer into an Error carrying the server's `detail`. */
export async function fetchJson<T>(url: string, init?: RequestInit): Promise<T> {
  const response = await fetch(url, init)
  if (!response.ok) {
    // The app's errors are JSON with a `detail`; anything else (a proxy's HTML page)
    // is reported by its status line.
    const json = response.headers.get('content-type')?.includes('application/json')
    const body = json ? ((await response.json()) as { detail?: unknown }) : undefined
    throw new Error(
      typeof body?.detail === 'string' ? body.detail : `${response.status} ${response.statusText}`,
    )
  }
  return (await response.json()) as T
}

export function postPlan(options: PlanOptions): Promise<{ plan: Plan }> {
  return fetchJson('/api/plan', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(options),
  })
}
