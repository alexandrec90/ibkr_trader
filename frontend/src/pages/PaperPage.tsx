import { useMemo, useState } from 'react'

import type { PaperOrder, Shadow, ShadowBook } from '../api.ts'
import { LineSeriesChart, type SeriesSpec } from '../components/LineSeriesChart.tsx'
import { num, pct, signClass, when } from '../format.ts'
import { nameSlots, outcomeRows, slotColor, summarize } from '../series.ts'
import { useFetch } from '../useFetch.ts'

function Performance({ shadow, slots }: { shadow: Shadow; slots: Map<string, number> }) {
  const horizons = shadow.performance.horizons
  const [months, setMonths] = useState(horizons[0]?.months ?? 1)
  const outcomes = useMemo(
    () => horizons.find((h) => h.months === months)?.outcomes ?? [],
    [horizons, months],
  )
  const summary = useMemo(() => summarize(outcomes), [outcomes])
  const rows = useMemo(() => outcomeRows(outcomes), [outcomes])
  const series: SeriesSpec[] = summary.map((s) => ({
    key: s.strategy,
    label: s.strategy,
    color: slotColor(slots.get(s.strategy)),
  }))

  if (shadow.performance.error) return <p className="error">{shadow.performance.error}</p>
  return (
    <>
      <div className="row">
        <span className="sub">Realization horizon</span>
        <div className="segmented" role="group" aria-label="horizon">
          {horizons.map((h) => (
            <button
              key={h.months}
              type="button"
              aria-pressed={h.months === months}
              onClick={() => setMonths(h.months)}
            >
              {h.months}m
            </button>
          ))}
        </div>
      </div>
      {outcomes.length === 0 ? (
        <p className="empty">
          No snapshot is {months} month(s) old yet: a forward decision is scored only once its
          horizon has fully elapsed.
        </p>
      ) : (
        <>
          <h3 className="sub">
            Excess return of each snapshot over XEQT, {months}-month horizon, after turnover costs
          </h3>
          <LineSeriesChart
            rows={rows}
            series={series}
            format={(v) => pct(v, 1, true)}
            baseline={0}
            dots
            height={260}
          />
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Strategy</th>
                  <th className="num">Snapshots</th>
                  <th className="num">Mean return</th>
                  <th className="num">Mean XEQT</th>
                  <th className="num">Mean excess</th>
                  <th className="num">Hit rate</th>
                </tr>
              </thead>
              <tbody>
                {summary.map((s) => (
                  <tr key={s.strategy}>
                    <td>
                      <span
                        className="swatch"
                        style={{ background: slotColor(slots.get(s.strategy)) }}
                      />
                      {s.strategy}
                    </td>
                    <td className="num">{s.n}</td>
                    <td className="num">{pct(s.meanReturn, 1, true)}</td>
                    <td className="num">{pct(s.meanBenchmark, 1, true)}</td>
                    <td className={`num ${signClass(s.meanExcess)}`}>
                      {pct(s.meanExcess, 1, true)}
                    </td>
                    <td className="num">{pct(s.hitRate, 0)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </>
      )}
    </>
  )
}

function Book({ book }: { book: ShadowBook }) {
  const top = book.latest.holdings[0]?.weight ?? 1
  const cash = 1 - book.latest.holdings.reduce((sum, h) => sum + h.weight, 0)
  return (
    <div className="grid-2">
      <div>
        <h3 className="sub">
          Latest book · {book.latest.asof} · model {book.latest.model_version} · bars to{' '}
          {book.latest.bar_asof ?? '?'}
        </h3>
        {book.latest.holdings.length === 0 ? (
          <p className="empty">All cash.</p>
        ) : (
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Symbol</th>
                  <th>Target weight</th>
                </tr>
              </thead>
              <tbody>
                {book.latest.holdings.map((h) => (
                  <tr key={h.symbol}>
                    <td>{h.symbol}</td>
                    <td>
                      <span className="weight-bar" style={{ width: `${(h.weight / top) * 120}px` }} />
                      {pct(h.weight)}
                    </td>
                  </tr>
                ))}
                {cash > 0.0005 && (
                  <tr>
                    <td>cash</td>
                    <td>{pct(cash)}</td>
                  </tr>
                )}
              </tbody>
            </table>
          </div>
        )}
      </div>
      <div>
        <h3 className="sub">Decision history</h3>
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Snapshot</th>
                <th>Model</th>
                <th className="num">Names</th>
                <th className="num">Invested</th>
                <th className="num">Turnover</th>
              </tr>
            </thead>
            <tbody>
              {[...book.history].reverse().map((h) => (
                <tr key={h.asof}>
                  <td>{h.asof}</td>
                  <td>{h.model_version}</td>
                  <td className="num">{h.names}</td>
                  <td className="num">{pct(h.invested, 0)}</td>
                  <td className="num">{pct(h.turnover, 0)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  )
}

function Books({ books, slots }: { books: ShadowBook[]; slots: Map<string, number> }) {
  const [chosen, setChosen] = useState<string>()
  const book = books.find((b) => b.strategy === chosen) ?? books[0]
  if (!book) return null
  return (
    <>
      <div className="row">
        <label className="sub">
          Strategy{' '}
          <select value={book.strategy} onChange={(e) => setChosen(e.target.value)}>
            {books.map((b) => (
              <option key={b.strategy}>{b.strategy}</option>
            ))}
          </select>
        </label>
        <span className="swatch" style={{ background: slotColor(slots.get(book.strategy)) }} />
      </div>
      <Book book={book} />
    </>
  )
}

function Orders({ orders }: { orders: PaperOrder[] }) {
  if (!orders.length) {
    return (
      <p className="empty">
        No IBKR paper orders recorded. The paper loop (<code>ibkr-trader paper</code>, or the{' '}
        <code>serve</code> job once <code>PAPER_TRADING_ENABLED=true</code>) has not placed an
        order yet; until it does, the forward shadow above is the paper record.
      </p>
    )
  }
  return (
    <div className="table-wrap">
      <table>
        <thead>
          <tr>
            <th>Placed</th>
            <th>Env</th>
            <th>Side</th>
            <th>Symbol</th>
            <th className="num">Qty</th>
            <th>Type</th>
            <th className="num">Limit</th>
            <th>Status</th>
            <th className="num">Filled</th>
            <th className="num">Avg fill</th>
            <th className="num">Commission</th>
          </tr>
        </thead>
        <tbody>
          {orders.map((o) => (
            <tr key={o.id}>
              <td>{when(o.placed_at)}</td>
              <td>{o.environment}</td>
              <td>{o.side}</td>
              <td>{o.symbol}</td>
              <td className="num">{num(o.quantity, 0)}</td>
              <td>{o.order_type}</td>
              <td className="num">{num(o.limit_price)}</td>
              <td>{o.status}</td>
              <td className="num">{num(o.filled_quantity, 0)}</td>
              <td className="num">{num(o.avg_fill_price)}</td>
              <td className="num">{num(o.commission)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

export function PaperPage() {
  const shadow = useFetch<Shadow>('/api/shadow')
  const paper = useFetch<{ orders: PaperOrder[] }>('/api/paper')
  const slots = useMemo(() => nameSlots((shadow.data?.books ?? []).map((b) => b.strategy)), [shadow.data])

  return (
    <>
      <section className="card">
        <h2>Forward shadow performance</h2>
        <p className="sub">
          Each monthly snapshot records what a strategy would hold, before the future bars exist;
          it is scored here only once its horizon has passed. Scored at{' '}
          {shadow.data?.performance.asof ?? '…'}.
        </p>
        {shadow.error && <p className="error">{shadow.error}</p>}
        {shadow.data &&
          (shadow.data.books.length === 0 ? (
            <p className="empty">
              No forward snapshots yet. Run the <em>Snapshot: Run Monthly</em> task (
              <code>ibkr-trader snapshot run</code>) to record the first decisions.
            </p>
          ) : (
            <Performance shadow={shadow.data} slots={slots} />
          ))}
      </section>
      {shadow.data && shadow.data.books.length > 0 && (
        <section className="card">
          <h2>Paper decisions</h2>
          <Books books={shadow.data.books} slots={slots} />
        </section>
      )}
      <section className="card">
        <h2>IBKR paper orders</h2>
        {paper.error && <p className="error">{paper.error}</p>}
        {paper.data && <Orders orders={paper.data.orders} />}
      </section>
    </>
  )
}
