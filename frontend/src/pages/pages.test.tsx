import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import type { Plan } from '../api.ts'
import { App, PageBoundary, tabFromHash } from '../App.tsx'
import { OrdersPage } from './OrdersPage.tsx'
import { PaperPage } from './PaperPage.tsx'

/** Answer each URL prefix with a body (or a `[status, detail]` failure). */
function mockApi(routes: Record<string, unknown>) {
  const calls: { url: string; init?: RequestInit }[] = []
  const fetchMock = vi.fn(async (url: string, init?: RequestInit) => {
    calls.push({ url, init })
    const key = Object.keys(routes).find((prefix) => url.startsWith(prefix))
    const body = key === undefined ? undefined : routes[`${init?.method ?? 'GET'} ${key}`] ?? routes[key]
    const headers = { 'Content-Type': 'application/json' } // as FastAPI sends
    if (Array.isArray(body) && body[0] === 'fail') {
      return new Response(JSON.stringify({ detail: body[2] }), {
        status: body[1] as number,
        headers,
      })
    }
    return new Response(JSON.stringify(body ?? null), {
      status: body === undefined ? 404 : 200,
      headers,
    })
  })
  vi.stubGlobal('fetch', fetchMock)
  return calls
}

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

const PLAN: Plan = {
  title: 'Questrade order plan · core (snapshot 2026-10-01) · 2026-10-26',
  strategy: 'core',
  snapshot_asof: '2026-10-01',
  today: '2026-10-26',
  generated_at: '2026-10-26T14:00:00+00:00',
  config: {},
  notes: ['the core snapshot is 50 days old'],
  accounts: [
    {
      account: 'TFSA ...282',
      kind: 'tfsa',
      equity_cad: 20000,
      available: { CAD: 4200 },
      holdings: 2,
      orders: [
        {
          side: 'Buy',
          symbol: 'XIC.TO',
          quantity: 40,
          limit_price: 40.95,
          currency: 'CAD',
          notional: 1638,
          good_until: '2026-11-02',
          note: 'new toward 50%',
        },
      ],
      notes: ['SPY: wants USD 5,000'],
      cash_left: { CAD: 2562 },
    },
  ],
}

describe('tabFromHash', () => {
  it('routes known tabs and falls back to the first', () => {
    expect(tabFromHash('#/paper')).toBe('paper')
    expect(tabFromHash('#orders')).toBe('orders')
    expect(tabFromHash('#/nope')).toBe('models')
    expect(tabFromHash('')).toBe('models')
  })
})

describe('OrdersPage', () => {
  it('shows the latest plan with its warnings, tickets and masked accounts', async () => {
    mockApi({ '/api/plan': { plan: PLAN, strategies: ['core'] } })
    render(<OrdersPage />)

    expect(await screen.findByText(PLAN.title)).toBeTruthy()
    expect(screen.getByText('the core snapshot is 50 days old')).toBeTruthy()
    expect(screen.getByText('XIC.TO')).toBeTruthy()
    expect(screen.getByText(/TFSA \.\.\.282/)).toBeTruthy()
    expect(screen.getByText(/Advisory only/)).toBeTruthy()
  })

  it('posts the options as JSON and shows the reason a plan failed', async () => {
    const calls = mockApi({
      '/api/plan': { plan: null, strategies: ['core', 'momentum'] },
      'POST /api/plan': ['fail', 400, 'QUESTRADE_TOKEN is not set'],
    })
    render(<OrdersPage />)
    const button = await screen.findByRole('button', { name: 'Recommend orders' })
    // happy-dom does not turn a submit button's click into a form submission; a browser does.
    fireEvent.submit(button.closest('form')!)

    expect(await screen.findByText('QUESTRADE_TOKEN is not set')).toBeTruthy()
    const post = calls.find((c) => c.init?.method === 'POST')!
    expect(new Headers(post.init?.headers).get('Content-Type')).toBe('application/json')
    expect(JSON.parse(String(post.init?.body))).toMatchObject({ strategy: 'core', max_new: 3 })
  })

  it('explains that a plan needs a forward snapshot first', async () => {
    mockApi({ '/api/plan': { plan: null, strategies: [] } })
    render(<OrdersPage />)
    expect(await screen.findByText(/No strategy has a forward snapshot yet/)).toBeTruthy()
  })
})

describe('PaperPage', () => {
  it('says what to run when there are no snapshots or paper orders', async () => {
    mockApi({
      '/api/shadow': {
        books: [],
        performance: { asof: '2026-10-26', horizons: [], error: null },
      },
      '/api/paper': { orders: [] },
    })
    render(<PaperPage />)

    expect(await screen.findByText(/No forward snapshots yet/)).toBeTruthy()
    expect(await screen.findByText(/No IBKR paper orders recorded/)).toBeTruthy()
  })

  it('shows a strategy book and its scored outcomes', async () => {
    mockApi({
      '/api/shadow': {
        books: [
          {
            strategy: 'core',
            history: [
              { asof: '2026-09-01', model_version: 'v1', names: 2, invested: 1, turnover: null },
            ],
            latest: {
              asof: '2026-09-01',
              model_version: 'v1',
              bar_asof: '2026-08-29',
              holdings: [{ symbol: 'XIC', weight: 0.6 }],
            },
          },
        ],
        performance: {
          asof: '2026-10-26',
          error: null,
          horizons: [
            {
              months: 1,
              outcomes: [
                {
                  strategy: 'core',
                  asof: '2026-09-01',
                  horizon_end: '2026-10-01',
                  portfolio_return: 0.03,
                  cost_return: 0.001,
                  benchmark_return: 0.01,
                  excess: 0.019,
                },
              ],
            },
          ],
        },
      },
      '/api/paper': { orders: [] },
    })
    render(<PaperPage />)

    expect(await screen.findByText('XIC')).toBeTruthy()
    expect(screen.getByText('40.0%')).toBeTruthy() // the uninvested remainder, as cash
    expect(screen.getAllByText('+1.9%').length).toBeGreaterThan(0) // mean excess in the table
  })

  it('reports an unscoreable shadow instead of an empty chart', async () => {
    mockApi({
      '/api/shadow': {
        books: [
          {
            strategy: 'core',
            history: [],
            latest: { asof: '2026-09-01', model_version: 'v1', bar_asof: null, holdings: [] },
          },
        ],
        performance: { asof: '2026-10-26', horizons: [], error: 'XEQT daily bars are required' },
      },
      '/api/paper': { orders: [] },
    })
    render(<PaperPage />)
    expect(await screen.findByText('XEQT daily bars are required')).toBeTruthy()
  })
})

describe('PageBoundary', () => {
  function Throws({ message }: { message: string }): never {
    throw new Error(message)
  }

  it('turns a chunk lost to a rebuild into a reload prompt, not a blank page', () => {
    vi.spyOn(console, 'error').mockImplementation(() => {})
    render(
      <PageBoundary>
        <Throws message="Failed to fetch dynamically imported module: /assets/OrdersPage-x.js" />
      </PageBoundary>,
    )
    expect(screen.getByText('The UI was rebuilt since this tab opened.')).toBeTruthy()
    expect(screen.getByRole('button', { name: 'Reload' })).toBeTruthy()
  })

  it('names any other failure', () => {
    vi.spyOn(console, 'error').mockImplementation(() => {})
    render(
      <PageBoundary>
        <Throws message="boom" />
      </PageBoundary>,
    )
    expect(screen.getByText('This page failed: boom')).toBeTruthy()
  })
})

describe('App', () => {
  it('renders the tab named by the hash', async () => {
    window.location.hash = '#/reports'
    mockApi({ '/api/reports': [] })
    render(<App />)
    expect(await screen.findByText(/No reports yet/)).toBeTruthy()
    expect(screen.getByRole('link', { name: 'Reports' }).getAttribute('aria-current')).toBe('page')
  })
})
