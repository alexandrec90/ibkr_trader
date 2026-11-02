/** Display formatting. Every helper renders a missing value as an en dash, never "NaN". */

const DASH = '–'

export function pct(value: number | null | undefined, digits = 1, signed = false): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return DASH
  const text = `${(value * 100).toFixed(digits)}%`
  return signed && value > 0 ? `+${text}` : text
}

export function num(value: number | null | undefined, digits = 2): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return DASH
  return value.toLocaleString('en-CA', {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  })
}

export function money(value: number | null | undefined, currency = 'CAD', digits = 0): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return DASH
  return `${currency} ${num(value, digits)}`
}

/** `{CAD: 4200, USD: 0}` as "CAD 4,200.00", skipping empty currencies like the terminal. */
export function cash(amounts: Record<string, number>): string {
  const shown = Object.entries(amounts)
    .filter(([, value]) => value >= 0.005)
    .sort(([a], [b]) => a.localeCompare(b))
    .map(([ccy, value]) => money(value, ccy, 2))
  return shown.length ? shown.join(' · ') : 'none'
}

/** An ISO timestamp as a local "2026-10-26 14:05". */
export function when(iso: string | null | undefined): string {
  if (!iso) return DASH
  const date = new Date(iso)
  if (Number.isNaN(date.getTime())) return iso
  const pad = (n: number) => String(n).padStart(2, '0')
  return (
    `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ` +
    `${pad(date.getHours())}:${pad(date.getMinutes())}`
  )
}

/** Sign class for a return: green when positive, red when negative. */
export function signClass(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value) || value === 0) return ''
  return value > 0 ? 'pos' : 'neg'
}
