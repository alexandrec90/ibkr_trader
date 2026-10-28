import {
  CartesianGrid,
  Legend,
  Line,
  LineChart,
  ReferenceLine,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts'

import type { Row } from '../series.ts'

export interface SeriesSpec {
  key: string
  label: string
  color: string
  /** A reference series (a benchmark): thinner, and listed after the real ones. */
  reference?: boolean
}

interface Props {
  rows: Row[]
  series: SeriesSpec[]
  format: (value: number) => string
  height?: number
  /** Draw a baseline at this y (0 for returns and drawdowns). */
  baseline?: number
  /** Markers on every point: for sparse series such as monthly snapshots. */
  dots?: boolean
}

function yearTick(date: string): string {
  return date.slice(0, 7)
}

/** Crosshair tooltip: every series' value at the hovered date, in text ink. */
function ChartTooltip({
  active,
  payload,
  label,
  series,
  format,
}: {
  active?: boolean
  payload?: { dataKey?: unknown; value?: unknown }[]
  label?: unknown
  series: SeriesSpec[]
  format: (value: number) => string
}) {
  if (!active || !payload?.length) return null
  const byKey = new Map(series.map((s) => [s.key, s]))
  return (
    <div className="tooltip">
      <div className="when">{String(label)}</div>
      <table>
        <tbody>
          {payload.map((entry) => {
            const spec = byKey.get(String(entry.dataKey))
            if (!spec || typeof entry.value !== 'number') return null
            return (
              <tr key={spec.key}>
                <td>
                  <span className="swatch" style={{ background: spec.color }} />
                  {spec.label}
                </td>
                <td className="num">{format(entry.value)}</td>
              </tr>
            )
          })}
        </tbody>
      </table>
    </div>
  )
}

/** One y-axis, thin lines, hairline grid, legend below: the shape every line chart here takes. */
export function LineSeriesChart({ rows, series, format, height = 320, baseline, dots }: Props) {
  const ordered = [...series.filter((s) => !s.reference), ...series.filter((s) => s.reference)]
  return (
    <ResponsiveContainer width="100%" height={height}>
      <LineChart data={rows} margin={{ top: 8, right: 16, bottom: 0, left: 0 }}>
        <CartesianGrid stroke="var(--grid)" vertical={false} />
        <XAxis
          dataKey="date"
          tickFormatter={yearTick}
          stroke="var(--axis)"
          tick={{ fill: 'var(--muted)', fontSize: 12 }}
          minTickGap={48}
        />
        <YAxis
          tickFormatter={format}
          stroke="var(--axis)"
          tick={{ fill: 'var(--muted)', fontSize: 12 }}
          width={64}
          domain={['auto', 'auto']}
        />
        {baseline !== undefined && <ReferenceLine y={baseline} stroke="var(--axis)" />}
        <Tooltip
          content={<ChartTooltip series={ordered} format={format} />}
          cursor={{ stroke: 'var(--axis)' }}
        />
        <Legend
          wrapperStyle={{ fontSize: 12, color: 'var(--ink-2)' }}
          formatter={(value) => <span style={{ color: 'var(--ink-2)' }}>{value}</span>}
        />
        {ordered.map((s) => (
          <Line
            key={s.key}
            dataKey={s.key}
            name={s.label}
            stroke={s.color}
            strokeWidth={s.reference ? 1.5 : 2}
            dot={dots ? { r: 4, strokeWidth: 2, stroke: 'var(--surface)', fill: s.color } : false}
            activeDot={{ r: 4, strokeWidth: 2, stroke: 'var(--surface)' }}
            connectNulls
            isAnimationActive={false}
          />
        ))}
      </LineChart>
    </ResponsiveContainer>
  )
}
