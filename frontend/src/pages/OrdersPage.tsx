import { type FormEvent, useState } from 'react'

import { type AccountPlan, type Plan, type PlanOptions, postPlan } from '../api.ts'
import { cash, money, num, when } from '../format.ts'
import { useFetch } from '../useFetch.ts'

interface LatestPlan {
  plan: Plan | null
  strategies: string[]
}

const DEFAULTS: Omit<PlanOptions, 'strategy'> = {
  min_order: 1000,
  max_position: 0.2,
  max_new: 3,
  exits: false,
}

function AccountCard({ account }: { account: AccountPlan }) {
  return (
    <section className="card">
      <h2>
        {account.account} <span className="badge">{account.kind.toUpperCase()}</span>
      </h2>
      <p className="sub">
        Equity {money(account.equity_cad)} · available {cash(account.available)} ·{' '}
        {account.holdings} holding(s)
      </p>
      {account.orders.length === 0 ? (
        <p className="empty">No orders for this account.</p>
      ) : (
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Side</th>
                <th>Symbol</th>
                <th className="num">Qty</th>
                <th className="num">Limit</th>
                <th className="num">Notional</th>
                <th>Good until</th>
                <th>Why</th>
              </tr>
            </thead>
            <tbody>
              {account.orders.map((o, i) => (
                <tr key={`${o.symbol}-${o.side}-${i}`}>
                  <td className={o.side.toLowerCase() === 'buy' ? 'pos' : 'neg'}>{o.side}</td>
                  <td>{o.symbol}</td>
                  <td className="num">{o.quantity}</td>
                  <td className="num">
                    {o.currency} {num(o.limit_price, 2)}
                  </td>
                  <td className="num">{money(o.notional, o.currency, 2)}</td>
                  <td>{o.good_until}</td>
                  <td>{o.note}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {account.notes.map((note) => (
        <p key={note} className="sub">
          – {note}
        </p>
      ))}
      {account.orders.length > 0 && (
        <p className="sub">Cash left once every buy fills: {cash(account.cash_left)}</p>
      )}
    </section>
  )
}

function PlanForm({
  strategies,
  initial,
  onPlanned,
}: {
  strategies: string[]
  initial: string | undefined
  onPlanned: (plan: Plan) => void
}) {
  // Mounted only once the strategy list has arrived, so the default can be chosen here.
  const [options, setOptions] = useState<PlanOptions>(() => ({
    strategy: initial && strategies.includes(initial) ? initial : (strategies[0] ?? ''),
    ...DEFAULTS,
  }))
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string>()

  const submit = async (event: FormEvent) => {
    event.preventDefault()
    setBusy(true)
    setError(undefined)
    try {
      onPlanned((await postPlan(options)).plan)
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason))
    } finally {
      setBusy(false)
    }
  }

  const set = <K extends keyof PlanOptions>(key: K, value: PlanOptions[K]) =>
    setOptions((o) => ({ ...o, [key]: value }))

  if (!strategies.length) {
    return (
      <p className="empty">
        No strategy has a forward snapshot yet, so there is no target book to plan from. Run the{' '}
        <em>Snapshot: Run Monthly</em> task (<code>ibkr-trader snapshot run</code>) first.
      </p>
    )
  }
  return (
    <form className="plan" onSubmit={submit}>
      <label>
        Strategy
        <select value={options.strategy} onChange={(e) => set('strategy', e.target.value)}>
          {strategies.map((s) => (
            <option key={s}>{s}</option>
          ))}
        </select>
      </label>
      <label>
        Min order (CAD)
        <input
          type="number"
          min={0}
          step={100}
          value={options.min_order}
          onChange={(e) => set('min_order', Number(e.target.value))}
        />
      </label>
      <label>
        Max position
        <input
          type="number"
          min={0.01}
          max={1}
          step={0.01}
          value={options.max_position}
          onChange={(e) => set('max_position', Number(e.target.value))}
        />
      </label>
      <label>
        Max new names
        <input
          type="number"
          min={0}
          step={1}
          value={options.max_new}
          onChange={(e) => set('max_new', Number(e.target.value))}
        />
      </label>
      <label>
        <span>
          <input
            type="checkbox"
            checked={options.exits}
            onChange={(e) => set('exits', e.target.checked)}
          />{' '}
          Sell holdings outside the strategy
        </span>
      </label>
      <button className="primary" type="submit" disabled={busy || !options.strategy}>
        {busy ? 'Asking Questrade…' : 'Recommend orders'}
      </button>
      {error && <p className="error">{error}</p>}
    </form>
  )
}

export function OrdersPage() {
  const latest = useFetch<LatestPlan>('/api/plan')
  const [fresh, setFresh] = useState<Plan>()
  const plan = fresh ?? latest.data?.plan ?? undefined

  return (
    <>
      <section className="card">
        <h2>Questrade order recommendations</h2>
        <p className="note">
          Advisory only: nothing is sent to Questrade or any broker. Enter each ticket yourself as a
          Limit order, Good Till Date. Recommending reads your accounts with the single-use{' '}
          <code>QUESTRADE_TOKEN</code> in <code>.env</code> and writes its replacement back there.
        </p>
        {latest.error && <p className="error">{latest.error}</p>}
        {latest.data && (
          <PlanForm
            strategies={latest.data.strategies}
            initial={plan?.strategy}
            onPlanned={setFresh}
          />
        )}
      </section>
      {plan ? (
        <>
          <section className="card">
            <h2>{plan.title}</h2>
            <p className="sub">
              Generated {when(plan.generated_at)} · tickets also in{' '}
              <code>reports/questrade-plan.csv</code>
            </p>
            {plan.notes.map((note) => (
              <p key={note} className="note">
                {note}
              </p>
            ))}
          </section>
          {plan.accounts.map((account) => (
            <AccountCard key={account.account} account={account} />
          ))}
        </>
      ) : (
        latest.data && (
          <p className="empty">
            No plan yet. Recommend one above, or run the <em>Questrade: Recommend Orders</em> task.
          </p>
        )
      )}
    </>
  )
}
