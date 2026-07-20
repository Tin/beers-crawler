/**
 * Thin client for the two beers-crawler interfaces:
 *  1. resolve  — beer name → Untappd page URL
 *  2. metadata — Untappd page URL → beer metadata (rating_score primary)
 *
 * Production under /beers/rating/: VITE_API_BASE=/beers/rating/api
 * Auth: HTTP Basic — credentials in sessionStorage only (never in source).
 */

const CRED_KEY = 'beers_crawler_basic'

function defaultApiBase() {
  if (import.meta.env.VITE_API_BASE) {
    return String(import.meta.env.VITE_API_BASE).replace(/\/$/, '')
  }
  const base = import.meta.env.BASE_URL || '/'
  if (base.includes('/beers/rating')) {
    return '/beers/rating/api'
  }
  return ''
}

const BASE = defaultApiBase()

export function getStoredCredentials() {
  try {
    const raw = sessionStorage.getItem(CRED_KEY)
    if (!raw) return null
    const parsed = JSON.parse(raw)
    if (parsed && parsed.username != null && parsed.password != null) {
      return { username: String(parsed.username), password: String(parsed.password) }
    }
  } catch {
    /* ignore */
  }
  return null
}

export function setStoredCredentials(username, password) {
  sessionStorage.setItem(
    CRED_KEY,
    JSON.stringify({ username, password }),
  )
}

export function clearStoredCredentials() {
  sessionStorage.removeItem(CRED_KEY)
}

function authHeader() {
  const creds = getStoredCredentials()
  if (!creds) return {}
  const token = btoa(`${creds.username}:${creds.password}`)
  return { Authorization: `Basic ${token}` }
}

async function request(path, options = {}) {
  const url = `${BASE}${path}`
  const res = await fetch(url, {
    ...options,
    headers: {
      Accept: 'application/json',
      ...authHeader(),
      ...(options.headers || {}),
    },
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

/** Public liveness (no auth). */
export function health() {
  return request('/health')
}

/** Authenticated health + stats. */
export function healthDetail() {
  return request('/health/detail')
}

/** Verify credentials work (stores on success). */
export async function login(username, password) {
  setStoredCredentials(username, password)
  try {
    return await healthDetail()
  } catch (e) {
    clearStoredCredentials()
    throw e
  }
}

export function logout() {
  clearStoredCredentials()
}

export function resolveBeerName(query, { historyOnly = false, force = false } = {}) {
  const params = new URLSearchParams({ q: query })
  if (historyOnly) params.set('history_only', 'true')
  if (force) params.set('force', 'true')
  return request(`/v1/resolve?${params}`)
}

export function fetchMetadata(pageUrl, { historyOnly = false, force = false } = {}) {
  const params = new URLSearchParams({ url: pageUrl })
  if (historyOnly) params.set('history_only', 'true')
  if (force) params.set('force', 'true')
  return request(`/v1/metadata?${params}`)
}

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
