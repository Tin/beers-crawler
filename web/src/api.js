/**
 * Thin client for the two beers-crawler interfaces:
 *  1. resolve  — beer name → Untappd page URL
 *  2. metadata — Untappd page URL → beer metadata (rating_score primary)
 *
 * Production under /beers/rating/: VITE_API_BASE=/beers/rating/api
 * Local Vite dev: same base (proxied) or empty for bare /v1
 */

function defaultApiBase() {
  if (import.meta.env.VITE_API_BASE) {
    return String(import.meta.env.VITE_API_BASE).replace(/\/$/, '')
  }
  // When app is served under /beers/rating/, talk to sibling /api
  const base = import.meta.env.BASE_URL || '/'
  if (base.includes('/beers/rating')) {
    return '/beers/rating/api'
  }
  return ''
}

const BASE = defaultApiBase()

async function request(path, options = {}) {
  const url = `${BASE}${path}`
  const res = await fetch(url, {
    headers: { Accept: 'application/json', ...(options.headers || {}) },
    ...options,
  })
  let body = null
  const text = await res.text()
  if (text) {
    try {
      body = JSON.parse(text)
    } catch {
      body = { detail: text }
    }
  }
  if (!res.ok) {
    const detail =
      (body && (body.detail || body.message)) ||
      res.statusText ||
      `HTTP ${res.status}`
    const err = new Error(typeof detail === 'string' ? detail : JSON.stringify(detail))
    err.status = res.status
    err.body = body
    throw err
  }
  return body
}

/** Interface 1: beer name → Untappd page ref */
export function resolveBeerName(query, { historyOnly = false, force = false } = {}) {
  const params = new URLSearchParams({ q: query })
  if (historyOnly) params.set('history_only', 'true')
  if (force) params.set('force', 'true')
  return request(`/v1/resolve?${params}`)
}

/** Interface 2: Untappd page URL → metadata */
export function fetchMetadata(pageUrl, { historyOnly = false, force = false } = {}) {
  const params = new URLSearchParams({ url: pageUrl })
  if (historyOnly) params.set('history_only', 'true')
  if (force) params.set('force', 'true')
  return request(`/v1/metadata?${params}`)
}

/** Convenience: name → resolve → metadata (server-side combined) */
export function crawlBeer(name, { historyOnly = false, force = false } = {}) {
  return request('/v1/crawl', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      name,
      history_only: historyOnly,
      force,
    }),
  })
}

export function health() {
  return request('/health')
}
