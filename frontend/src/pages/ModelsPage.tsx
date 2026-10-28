import { useEffect, useMemo, useState } from 'react'
import {
  Bar,
  BarChart,
  CartesianGrid,
  Legend,
  ReferenceLine,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts'

import type { Curve, RunRow, TrainedModel } from '../api.ts'
import { LineSeriesChart, type SeriesSpec } from '../components/LineSeriesChart.tsx'
import { money, num, pct, signClass, when } from '../format.ts'
import { assignSlots, curveKey, mergeCurves, nameSlots, SLOTS, slotColor } from '../series.ts'
import { useFetch } from '../useFetch.ts'

type SortKey = keyof Pick<
  RunRow,
  | 'run_id'
  | 'total_return'
  | 'excess_return'
  | 'cagr'
  | 'sharpe'
  | 'sortino'
  | 'max_drawdown'
  | 'calmar'
  | 'trades_per_year'
  | 'costs_cad'
  | 'end_value_cad'
>

const COLUMNS: { key: SortKey; label: string; render: (r: RunRow) => string; signed?: boolean }[] =
  [
    { key: 'total_return', label: 'Total', render: (r) => pct(r.total_return) },
    {
      key: 'excess_return',
      label: 'vs bench',
      render: (r) => pct(r.excess_return, 1, true),
      signed: true,
    },
    { key: 'cagr', label: 'CAGR', render: (r) => pct(r.cagr) },
    { key: 'sharpe', label: 'Sharpe', render: (r) => num(r.sharpe) },
    { key: 'sortino', label: 'Sortino', render: (r) => num(r.sortino) },
    { key: 'max_drawdown', label: 'Max DD', render: (r) => pct(r.max_drawdown) },
    { key: 'calmar', label: 'Calmar', render: (r) => num(r.calmar) },
    { key: 'trades_per_year', label: 'Trades/yr', render: (r) => num(r.trades_per_year, 1) },
    { key: 'costs_cad', label: 'Costs', render: (r) => money(r.costs_cad) },
    { key: 'end_value_cad', label: 'End value', render: (r) => money(r.end_value_cad) },
  ]

/** Sorted on one metric; runs missing it always sink to the bottom. */
function sortRuns(runs: RunRow[], key: SortKey, descending: boolean): RunRow[] {
  return [...runs].sort((a, b) => {
    const x = a[key]
    const y = b[key]
    if (x === null || x === undefined) return 1
    if (y === null || y === undefined) return -1
    return descending ? y - x : x - y
  })
}

function Leaderboard({
  runs,
  slots,
  onToggle,
}: {
  runs: RunRow[]
  slots: Map<number, number>
  onToggle: (id: number) => void
}) {
  const [sort, setSort] = useState<{ key: SortKey; descending: boolean }>({
    key: 'sharpe',
    descending: true,
  })
  const sorted = useMemo(() => sortRuns(runs, sort.key, sort.descending), [runs, sort])
  const header = (key: SortKey, label: string) => (
    <th className="num" aria-sort={sort.key === key ? (sort.descending ? 'descending' : 'ascending') : 'none'}>
      <button
        type="button"
        onClick={() =>
          setSort((s) => ({ key, descending: s.key === key ? !s.descending : true }))
        }
      >
        {label}
        {sort.key === key ? (sort.descending ? ' ↓' : ' ↑') : ''}
      </button>
    </th>
  )
  const full = slots.size >= SLOTS
  return (
    <div className="table-wrap">
      <table>
        <thead>
          <tr>
            <th>Chart</th>
            {header('run_id', 'Run')}
            <th>Strategy</th>
            <th>Account</th>
            <th>Window</th>
            {COLUMNS.map((c) => header(c.key, c.label))}
          </tr>
        </thead>
        <tbody>
          {sorted.map((run) => {
            const slot = slots.get(run.run_id)
            const checked = slot !== undefined
            return (
              <tr key={run.run_id} className={checked ? 'selected' : undefined}>
                <td>
                  <input
                    type="checkbox"
                    aria-label={`chart run ${run.run_id}`}
                    checked={checked}
                    disabled={!run.has_curve || (!checked && full)}
                    title={run.has_curve ? undefined : 'stored before equity curves were persisted'}
                    onChange={() => onToggle(run.run_id)}
                  />
                  {checked && <span className="swatch" style={{ background: slotColor(slot) }} />}
                </td>
                <td className="num">#{run.run_id}</td>
                <td>{run.strategy}</td>
                <td>{run.account}</td>
                <td>{run.window}</td>
                {COLUMNS.map((c) => (
                  <td
                    key={c.key}
                    className={`num ${c.signed ? signClass(run[c.key] as number | null) : ''}`}
                  >
                    {c.render(run)}
                  </td>
                ))}
              </tr>
            )
          })}
        </tbody>
      </table>
    </div>
  )
}

function Curves({ ids, slots }: { ids: number[]; slots: Map<number, number> }) {
  const curves = useFetch<Curve[]>(ids.length ? `/api/curves?ids=${ids.join(',')}` : null)
  const list = ids.length ? (curves.data ?? []) : []
  const series: SeriesSpec[] = list.map((curve) => ({
    key: curveKey(curve),
    label: curve.kind === 'benchmark' ? `${curve.label} (benchmark)` : curve.label,
    color: curve.kind === 'benchmark' ? 'var(--benchmark)' : slotColor(slots.get(curve.run_id)),
    reference: curve.kind === 'benchmark',
  }))
  const equity = useMemo(() => mergeCurves(list, 1), [list])
  const drawdowns = useMemo(() => mergeCurves(list, 2), [list])
  if (!ids.length) {
    return <p className="empty">Tick up to {SLOTS} runs in the leaderboard to overlay their curves.</p>
  }
  if (curves.error) return <p className="error">{curves.error}</p>
  return (
    <div className={curves.loading ? 'stale' : undefined}>
      <h3 className="sub">Growth of 100 (each series rebased at its own start, CAD, after costs)</h3>
      <LineSeriesChart rows={equity} series={series} format={(v) => num(v, 0)} />
      <h3 className="sub">Drawdown from running peak</h3>
      <LineSeriesChart
        rows={drawdowns}
        series={series}
        format={(v) => pct(v, 0)}
        height={220}
        baseline={0}
      />
    </div>
  )
}

function ModelCard({ model }: { model: TrainedModel | null | undefined }) {
  if (model === undefined) return null
  if (model === null) {
    return (
      <p className="empty">
        No trained model yet. Run <code>ibkr-trader train run</code>; its walk-forward ICs show
        up here.
      </p>
    )
  }
  const names = Object.keys(model.overall_ic).sort()
  const slots = nameSlots(names)
  const rows = model.folds.map((f) => ({ fold: `fold ${f.fold}`, ...f.ic }))
  return (
    <>
      <div className="stats">
        <Stat label="Version" value={`${model.model ?? '?'} ${model.version ?? ''}`} />
        <Stat label="Trained" value={when(model.created_at)} />
        <Stat label="Label" value={model.label ?? '–'} />
        <Stat
          label="Dataset"
          value={`${num(model.dataset.rows, 0)} rows · ${num(model.dataset.symbols, 0)} names`}
        />
      </div>
      {model.dataset.survivorship === 'curated-current' && (
        <p className="note">Curated-current universe: these ICs are survivorship-biased.</p>
      )}
      <h3 className="sub">
        Walk-forward information coefficient by test fold ({model.scheme ?? '?'}, purge{' '}
        {model.purge_months ?? '?'}m)
      </h3>
      <ResponsiveContainer width="100%" height={240}>
        <BarChart data={rows} margin={{ top: 8, right: 16, bottom: 0, left: 0 }} barGap={2}>
          <CartesianGrid stroke="var(--grid)" vertical={false} />
          <XAxis dataKey="fold" stroke="var(--axis)" tick={{ fill: 'var(--muted)', fontSize: 12 }} />
          <YAxis
            stroke="var(--axis)"
            tick={{ fill: 'var(--muted)', fontSize: 12 }}
            tickFormatter={(v: number) => num(v, 2)}
            width={56}
          />
          <ReferenceLine y={0} stroke="var(--axis)" />
          <Tooltip
            cursor={{ fill: 'var(--surface-2)' }}
            formatter={(value) => (typeof value === 'number' ? num(value, 3) : String(value))}
            contentStyle={{ background: 'var(--surface)', border: '1px solid var(--border)' }}
          />
          <Legend wrapperStyle={{ fontSize: 12 }} />
          {names.map((name) => (
            <Bar
              key={name}
              dataKey={name}
              fill={slotColor(slots.get(name))}
              radius={[2, 2, 0, 0]}
              isAnimationActive={false}
            />
          ))}
        </BarChart>
      </ResponsiveContainer>
      <div className="table-wrap">
        <table>
          <thead>
            <tr>
              <th>Model</th>
              <th className="num">Mean IC</th>
              <th className="num">± std</th>
              {model.folds.map((f) => (
                <th key={String(f.fold)} className="num">
                  fold {f.fold}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {names.map((name) => (
              <tr key={name}>
                <td>
                  <span className="swatch" style={{ background: slotColor(slots.get(name)) }} />
                  {name}
                </td>
                <td className={`num ${signClass(model.overall_ic[name]?.mean)}`}>
                  {num(model.overall_ic[name]?.mean, 3)}
                </td>
                <td className="num">{num(model.overall_ic[name]?.std, 3)}</td>
                {model.folds.map((f) => (
                  <td key={String(f.fold)} className="num">
                    {num(f.ic[name], 3)}
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </>
  )
}

function Stat({ label, value }: { label: string; value: string }) {
  return (
    <div className="stat">
      <div className="label">{label}</div>
      <div className="value">{value}</div>
    </div>
  )
}

export function ModelsPage() {
  const runs = useFetch<RunRow[]>('/api/runs')
  const model = useFetch<TrainedModel | null>('/api/model')
  const [selected, setSelected] = useState<number[]>([])
  const [slots, setSlots] = useState<Map<number, number>>(new Map())
  const [preselected, setPreselected] = useState(false)

  // First answer only: chart the three best runs by Sharpe that have a curve. After
  // that an empty selection is the owner's choice and is left alone.
  useEffect(() => {
    if (!runs.data || preselected) return
    const best = sortRuns(
      runs.data.filter((r) => r.has_curve),
      'sharpe',
      true,
    )
      .slice(0, 3)
      .map((r) => r.run_id)
    setPreselected(true)
    setSelected(best)
    setSlots(assignSlots(new Map(), best))
  }, [runs.data, preselected])

  const toggle = (id: number) => {
    const next = selected.includes(id) ? selected.filter((x) => x !== id) : [...selected, id]
    setSelected(next)
    setSlots((previous) => assignSlots(previous, next))
  }

  return (
    <>
      <section className="card">
        <h2>Backtest leaderboard</h2>
        <p className="sub">
          Every persisted <code>backtest_runs</code> row. Click a column to sort; tick runs to
          compare. New runs come from the <em>Backtest: Run</em> task or{' '}
          <code>ibkr-trader backtest run</code>.
        </p>
        {runs.error && <p className="error">{runs.error}</p>}
        {runs.data?.length === 0 && (
          <p className="empty">
            No backtest runs in the database yet. Run <code>ibkr-trader backtest run</code> or{' '}
            <code>backtest lab</code>, then reload.
          </p>
        )}
        {runs.data && runs.data.length > 0 && (
          <Leaderboard runs={runs.data} slots={slots} onToggle={toggle} />
        )}
      </section>
      {runs.data && runs.data.length > 0 && (
        <section className="card">
          <h2>Equity and drawdown</h2>
          <Curves ids={selected} slots={slots} />
        </section>
      )}
      <section className="card">
        <h2>Trained model</h2>
        <p className="sub">The latest <code>models/ml_lt</code> artifact&apos;s validation.</p>
        {model.error && <p className="error">{model.error}</p>}
        <ModelCard model={model.data} />
      </section>
    </>
  )
}
