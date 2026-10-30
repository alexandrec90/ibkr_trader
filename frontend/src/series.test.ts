import { describe, expect, it } from 'vitest'

import type { Curve, Outcome } from './api.ts'
import {
  assignSlots,
  curveKey,
  mergeCurves,
  nameSlots,
  outcomeRows,
  SLOTS,
  slotColor,
  summarize,
} from './series.ts'

describe('assignSlots', () => {
  it('gives new ids the lowest free slots in selection order', () => {
    expect([...assignSlots(new Map(), [7, 3, 9])]).toEqual([
      [7, 0],
      [3, 1],
      [9, 2],
    ])
  })

  it('keeps every survivor on its slot when one run is deselected', () => {
    const first = assignSlots(new Map(), [7, 3, 9])
    const second = assignSlots(first, [7, 9])
    expect(second.get(9)).toBe(2) // not repainted to slot 1
    expect(assignSlots(second, [7, 9, 4]).get(4)).toBe(1) // the freed slot is reused
  })

  it('gives nothing past the palette', () => {
    const ids = Array.from({ length: SLOTS + 1 }, (_, i) => i)
    const slots = assignSlots(new Map(), ids)
    expect(slots.size).toBe(SLOTS)
    expect(slots.has(SLOTS)).toBe(false)
  })
})

describe('slotColor and nameSlots', () => {
  it('maps slots onto the CSS palette and anything else onto muted', () => {
    expect(slotColor(0)).toBe('var(--series-1)')
    expect(slotColor(SLOTS)).toBe('var(--muted)')
    expect(slotColor(undefined)).toBe('var(--muted)')
  })

  it('slots names by sorted position, so filtering never recolors', () => {
    expect([...nameSlots(['momentum', 'core', 'momentum'])]).toEqual([
      ['core', 0],
      ['momentum', 1],
    ])
  })
})

const curve = (run_id: number, kind: Curve['kind'], points: Curve['points']): Curve => ({
  run_id,
  kind,
  label: `#${run_id}`,
  points,
})

describe('mergeCurves', () => {
  it('builds one row per date, keyed per curve, sorted by date', () => {
    const rows = mergeCurves(
      [
        curve(1, 'strategy', [
          ['2010-01-05', 110, -0.01],
          ['2010-01-04', 100, 0],
        ]),
        curve(1, 'benchmark', [['2010-01-05', 101, 0]]),
      ],
      1,
    )
    expect(rows).toEqual([
      { date: '2010-01-04', s1: 100 },
      { date: '2010-01-05', s1: 110, b1: 101 },
    ])
  })

  it('reads the drawdown field when asked', () => {
    expect(mergeCurves([curve(2, 'strategy', [['2010-01-04', 100, -0.2]])], 2)).toEqual([
      { date: '2010-01-04', s2: -0.2 },
    ])
  })

  it('keys strategy and benchmark curves apart', () => {
    expect(curveKey({ kind: 'strategy', run_id: 3 })).toBe('s3')
    expect(curveKey({ kind: 'benchmark', run_id: 3 })).toBe('b3')
  })
})

const outcome = (strategy: string, asof: string, excess: number): Outcome => ({
  strategy,
  asof,
  horizon_end: asof,
  portfolio_return: excess + 0.01,
  cost_return: 0.01,
  benchmark_return: 0,
  excess,
})

describe('outcomes', () => {
  it('summarizes mean excess and hit rate per strategy, sorted', () => {
    const summary = summarize([
      outcome('momentum', '2026-01-01', 0.04),
      outcome('core', '2026-01-01', -0.02),
      outcome('momentum', '2026-02-01', -0.02),
    ])
    expect(summary.map((s) => s.strategy)).toEqual(['core', 'momentum'])
    const momentum = summary[1]!
    expect(momentum.n).toBe(2)
    expect(momentum.meanExcess).toBeCloseTo(0.01)
    expect(momentum.hitRate).toBe(0.5)
    expect(momentum.meanReturn).toBeCloseTo(0.01) // net of the 1% cost
  })

  it('summarizes nothing to nothing', () => {
    expect(summarize([])).toEqual([])
  })

  it('pivots outcomes to one row per snapshot date', () => {
    expect(
      outcomeRows([outcome('a', '2026-02-01', 0.1), outcome('b', '2026-01-01', 0.2)]),
    ).toEqual([
      { date: '2026-01-01', b: 0.2 },
      { date: '2026-02-01', a: 0.1 },
    ])
  })
})
