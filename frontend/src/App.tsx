import { Component, lazy, type ReactNode, Suspense, useEffect, useState } from 'react'

// One chunk per page, so the chart library loads with the pages that draw charts.
const ModelsPage = lazy(() => import('./pages/ModelsPage.tsx').then((m) => ({ default: m.ModelsPage })))
const OrdersPage = lazy(() => import('./pages/OrdersPage.tsx').then((m) => ({ default: m.OrdersPage })))
const PaperPage = lazy(() => import('./pages/PaperPage.tsx').then((m) => ({ default: m.PaperPage })))
const ReportsPage = lazy(() =>
  import('./pages/ReportsPage.tsx').then((m) => ({ default: m.ReportsPage })),
)

export const TABS = [
  { id: 'models', label: 'Models', Page: ModelsPage },
  { id: 'orders', label: 'Order recommendations', Page: OrdersPage },
  { id: 'paper', label: 'Paper trading', Page: PaperPage },
  { id: 'reports', label: 'Reports', Page: ReportsPage },
] as const

type TabId = (typeof TABS)[number]['id']

/** `#/paper` -> "paper"; anything unknown is the first tab. */
export function tabFromHash(hash: string): TabId {
  const id = hash.replace(/^#\/?/, '')
  return TABS.find((t) => t.id === id)?.id ?? TABS[0].id
}

/**
 * Keeps one page's crash from blanking the whole app. The likely one is a rebuild under
 * an open tab: `ui-task.py` rebuilds when sources change, the old page then asks for a
 * chunk whose hashed name no longer exists, and only a reload fetches the new index.
 */
export class PageBoundary extends Component<{ children: ReactNode }, { error?: Error }> {
  state: { error?: Error } = {}

  static getDerivedStateFromError(error: Error) {
    return { error }
  }

  render() {
    const { error } = this.state
    if (!error) return this.props.children
    const stale = /dynamically imported module|Importing a module script failed/i.test(error.message)
    return (
      <section className="card">
        <p className="error">
          {stale ? 'The UI was rebuilt since this tab opened.' : `This page failed: ${error.message}`}
        </p>
        <button type="button" onClick={() => window.location.reload()}>
          Reload
        </button>
      </section>
    )
  }
}

type Theme = 'light' | 'dark' | 'system'
const THEME_KEY = 'ibkr-trader.theme'

/**
 * Storage blocked by the browser (private window, cleared site data) throws a
 * DOMException; the theme then lasts for this page view only. Anything else is a bug.
 */
function storageBlocked(error: unknown): boolean {
  if (error instanceof DOMException) return true
  throw error
}

function storedTheme(): Theme {
  try {
    const value = localStorage.getItem(THEME_KEY)
    return value === 'light' || value === 'dark' ? value : 'system'
  } catch (error) {
    storageBlocked(error)
    return 'system'
  }
}

function applyTheme(theme: Theme) {
  if (theme === 'system') document.documentElement.removeAttribute('data-theme')
  else document.documentElement.setAttribute('data-theme', theme)
  try {
    if (theme === 'system') localStorage.removeItem(THEME_KEY)
    else localStorage.setItem(THEME_KEY, theme)
  } catch (error) {
    storageBlocked(error)
  }
}

export function App() {
  const [tab, setTab] = useState<TabId>(() => tabFromHash(window.location.hash))
  const [theme, setTheme] = useState<Theme>(storedTheme)

  useEffect(() => {
    const onHash = () => setTab(tabFromHash(window.location.hash))
    window.addEventListener('hashchange', onHash)
    return () => window.removeEventListener('hashchange', onHash)
  }, [])
  useEffect(() => applyTheme(theme), [theme])

  const { Page } = TABS.find((t) => t.id === tab) ?? TABS[0]
  return (
    <>
      <header className="top">
        <h1>ibkr-trader</h1>
        <nav className="tabs">
          {TABS.map((t) => (
            <a key={t.id} href={`#/${t.id}`} aria-current={t.id === tab ? 'page' : undefined}>
              {t.label}
            </a>
          ))}
        </nav>
        <span className="badge" title="ENVIRONMENT=paper is the ceiling; this UI places no orders">
          paper · read-only
        </span>
        <select
          aria-label="theme"
          value={theme}
          onChange={(e) => setTheme(e.target.value as Theme)}
        >
          <option value="system">System theme</option>
          <option value="light">Light</option>
          <option value="dark">Dark</option>
        </select>
      </header>
      <main>
        <PageBoundary key={tab}>
          <Suspense fallback={<p className="sub">Loading…</p>}>
            <Page />
          </Suspense>
        </PageBoundary>
      </main>
    </>
  )
}
