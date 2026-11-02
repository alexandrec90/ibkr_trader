import type { ReportFile } from '../api.ts'
import { when } from '../format.ts'
import { useFetch } from '../useFetch.ts'

/** The static HTML reports the existing tasks write under `reports/`, one click away. */
export function ReportsPage() {
  const reports = useFetch<ReportFile[]>('/api/reports')
  return (
    <section className="card">
      <h2>Generated reports</h2>
      <p className="sub">
        The strategy lab, factor tear sheets and the static results report, as last written under{' '}
        <code>reports/</code> by the <em>Report:</em> tasks. They open in a new tab.
      </p>
      {reports.error && <p className="error">{reports.error}</p>}
      {reports.data?.length === 0 && (
        <p className="empty">
          No reports yet. Run <em>Report: Run Strategy Lab</em> or{' '}
          <code>python scripts/report-task.py results</code>.
        </p>
      )}
      {reports.data && reports.data.length > 0 && (
        <table>
          <thead>
            <tr>
              <th>Report</th>
              <th>Written</th>
            </tr>
          </thead>
          <tbody>
            {reports.data.map((r) => (
              <tr key={r.path}>
                <td>
                  <a href={`/reports/${r.path}`} target="_blank" rel="noreferrer">
                    {r.path}
                  </a>
                </td>
                <td>{when(r.modified)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </section>
  )
}
