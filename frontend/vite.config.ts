/// <reference types="vitest" />
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

import react from '@vitejs/plugin-react'
import { defineConfig, loadEnv } from 'vite'

import { portOffset } from './src/worktreePort.ts'

const repoRoot = resolve(dirname(fileURLToPath(import.meta.url)), '..')

/**
 * Conventional bases. An ordinary checkout derives offset 0 and keeps 5173; a worktree
 * adds `portOffset` (vendored from devkit, see `src/worktreePort.ts`) so two dev servers
 * coexist, and `strictPort` turns any remaining collision into a startup error rather
 * than a silent slide onto another checkout's port.
 */
const DEV_PORT = 5173
const PREVIEW_PORT = 4173
/** `Settings.ui_port`'s default: the FastAPI app the dev server proxies to. */
const UI_PORT = 8001

/** A port from the environment, or `fallback` when it is unset or unreadable. */
export function portFrom(
  env: Record<string, string | undefined>,
  key: string,
  fallback: number,
): number {
  const raw = env[key]?.trim()
  if (!raw) return fallback
  const value = Number.parseInt(raw, 10)
  return Number.isInteger(value) && value > 0 && value < 65536 ? value : fallback
}

export default defineConfig(({ mode }) => {
  // Only these prefixes are read from the repo's .env: it holds broker credentials, and
  // nothing else in it is the dev server's business.
  const env = { ...loadEnv(mode, repoRoot, ['UI_', 'VITE_']), ...process.env }
  const offset = portOffset(repoRoot)
  const backend = `http://127.0.0.1:${portFrom(env, 'UI_PORT', UI_PORT)}`
  return {
    plugins: [react()],
    server: {
      host: '127.0.0.1',
      port: portFrom(env, 'VITE_PORT', DEV_PORT + offset),
      strictPort: true,
      // `ibkr-trader ui` serves the API; the dev server only adds hot reload. The app
      // checks the Host header, so the proxy must present the backend's own.
      proxy: {
        '/api': { target: backend, changeOrigin: true },
        '/reports': { target: backend, changeOrigin: true },
      },
    },
    preview: {
      host: '127.0.0.1',
      port: portFrom(env, 'VITE_PREVIEW_PORT', PREVIEW_PORT + offset),
      strictPort: true,
    },
    test: {
      environment: 'happy-dom',
    },
  }
})
