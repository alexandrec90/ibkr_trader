/**
 * Pure data shaping for the charts: which color an entity wears, and how per-series
 * points become the row-per-x shape recharts draws.
 */

import type { Curve, Outcome } from './api.ts'

/** The categorical palette's slot count; `styles.css` defines `--series-1..8`. */
export const SLOTS = 8

export function slotColor(slot: number | undefined): string {
  return slot === undefined || slot >= SLOTS ? 'var(--muted)' : `var(--series-${slot + 1})`
}

/**
 * Give each selected id a palette slot, keeping the slot of every id that was already
 * selected. Color follows the entity: deselecting one run must not repaint the others.
 * New ids take the lowest free slots, in selection order; past `SLOTS` they get none.
 */
export function assignSlots(previous: Map<number, number>, selected: number[]): Map<number, number> {
  const next = new Map<number, number>()
  for (const id of selected) {
    const slot = previous.get(id)
    if (slot !== undefined) next.set(id, slot)
  }
  const taken = new Set(next.values())
  for (const id of selected) {
    if (next.has(id)) continue
    let slot = 0
    while (taken.has(slot)) slot += 1
    if (slot >= SLOTS) continue
    next.set(id, slot)
    taken.add(slot)
  }
  return next
}

/** A stable slot per name: its index among the full, sorted set of names. */
export function nameSlots(names: Iterable<string>): Map<string, number> {
  return new Map([...new Set(names)].sort().map((name, index) => [name, index]))
}

export function curveKey(curve: Pick<Curve, 'kind' | 'run_id'>): string {
  return `${curve.kind === 'benchmark' ? 'b' : 's'}${curve.run_id}`
}

export type Row = { date: string } & Record<string, number | string>

/**
 * One row per date across every curve, keyed by `curveKey`. `field` 1 is growth of
 * 100, 2 is drawdown. A curve with no point on a date leaves that key absent rather
 * than zero; the chart joins its line across the missing date.
 */
export function mergeCurves(curves: Curve[], field: 1 | 2): Row[] {
  const rows = new Map<string, Row>()
  for (const curve of curves) {
    const key = curveKey(curve)
    for (const point of curve.points) {
      const date = point[0]
      let row = rows.get(date)
      if (!row) {
        row = { date }
        rows.set(date, row)
      }
      row[key] = point[field]
    }
  }
  return [...rows.values()].sort((a, b) => a.date.localeCompare(b.date))
}

/** One row per snapshot date, a key per strategy holding that snapshot's excess return. */
export function outcomeRows(outcomes: Outcome[]): Row[] {
  const rows = new Map<string, Row>()
  for (const outcome of outcomes) {
    const row = rows.get(outcome.asof) ?? { date: outcome.asof }
    row[outcome.strategy] = outcome.excess
    rows.set(outcome.asof, row)
  }
  return [...rows.values()].sort((a, b) => a.date.localeCompare(b.date))
}

export interface OutcomeSummary {
  strategy: string
  n: number
  meanExcess: number
  hitRate: number
  meanReturn: number
  meanBenchmark: number
}

/** Per-strategy mean excess and hit rate: the same numbers `snapshot report` prints. */
export function summarize(outcomes: Outcome[]): OutcomeSummary[] {
  const groups = new Map<string, Outcome[]>()
  for (const outcome of outcomes) {
    groups.set(outcome.strategy, [...(groups.get(outcome.strategy) ?? []), outcome])
  }
  const mean = (values: number[]) => values.reduce((a, b) => a + b, 0) / values.length
  return [...groups.entries()]
    .sort(([a], [b]) => a.localeCompare(b))
    .map(([strategy, rows]) => ({
      strategy,
      n: rows.length,
      meanExcess: mean(rows.map((r) => r.excess)),
      hitRate: rows.filter((r) => r.excess > 0).length / rows.length,
      meanReturn: mean(rows.map((r) => r.portfolio_return - r.cost_return)),
      meanBenchmark: mean(rows.map((r) => r.benchmark_return)),
    }))
}
