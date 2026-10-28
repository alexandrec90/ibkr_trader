import { describe, expect, it } from 'vitest'

import { cash, money, num, pct, signClass, when } from './format.ts'

describe('format', () => {
  it('renders missing and non-finite values as a dash, never NaN', () => {
    for (const fn of [pct, num, money]) {
      expect(fn(null)).toBe('–')
      expect(fn(undefined)).toBe('–')
      expect(fn(Number.NaN)).toBe('–')
    }
    expect(when(null)).toBe('–')
  })

  it('formats percentages, optionally signed', () => {
    expect(pct(0.1234)).toBe('12.3%')
    expect(pct(0.05, 1, true)).toBe('+5.0%')
    expect(pct(-0.05, 1, true)).toBe('-5.0%')
  })

  it('formats money with its currency and thousands separators', () => {
    expect(money(1638)).toBe('CAD 1,638')
    expect(money(12.5, 'USD', 2)).toBe('USD 12.50')
  })

  it('shows cash like the terminal: non-empty currencies, sorted', () => {
    expect(cash({ USD: 10, CAD: 4200 })).toBe('CAD 4,200.00 · USD 10.00')
    expect(cash({ CAD: 0, USD: 0.001 })).toBe('none')
  })

  it('classes returns by sign', () => {
    expect(signClass(0.1)).toBe('pos')
    expect(signClass(-0.1)).toBe('neg')
    expect(signClass(0)).toBe('')
    expect(signClass(null)).toBe('')
  })

  it('passes an unparseable timestamp through', () => {
    expect(when('not a date')).toBe('not a date')
  })
})
