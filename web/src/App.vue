<script setup>
import { computed, onMounted, ref, watch } from 'vue'
import {
  clearStoredCredentials,
  crawlBeer,
  fetchMetadata,
  getStoredCredentials,
  health,
  healthDetail,
  login,
  logout,
  resolveBeerName,
} from './api.js'

const beerName = ref('Russian River Pliny the Elder')
const pageUrl = ref('')
const force = ref(false)
const historyOnly = ref(false)

const busy = ref(null) // 'resolve' | 'metadata' | 'both' | 'login' | null
const error = ref('')
const pageRef = ref(null)
const metadata = ref(null)
const apiHealth = ref(null)
const authRequired = ref(true)
const authed = ref(false)
const authUser = ref('')
const loginUser = ref('')
const loginPass = ref('')
const loginError = ref('')

const options = computed(() => ({
  force: force.value,
  historyOnly: historyOnly.value,
}))

async function refreshHealth() {
  try {
    const h = await health()
    apiHealth.value = h
    authRequired.value = h.auth_required !== false
  } catch {
    apiHealth.value = null
  }
}

async function tryExistingSession() {
  const creds = getStoredCredentials()
  if (!creds) {
    authed.value = !authRequired.value
    return
  }
  try {
    const detail = await healthDetail()
    apiHealth.value = detail
    authed.value = true
    authUser.value = creds.username
  } catch (e) {
    if (e.status === 401) {
      clearStoredCredentials()
      authed.value = false
      authUser.value = ''
    }
  }
}

onMounted(async () => {
  await refreshHealth()
  if (authRequired.value) {
    await tryExistingSession()
  } else {
    authed.value = true
  }
})

async function submitLogin() {
  loginError.value = ''
  const u = loginUser.value.trim()
  const p = loginPass.value
  if (!u || !p) {
    loginError.value = 'Enter username and password.'
    return
  }
  busy.value = 'login'
  try {
    const detail = await login(u, p)
    apiHealth.value = detail
    authed.value = true
    authUser.value = u
    loginPass.value = ''
  } catch (e) {
    authed.value = false
    loginError.value = e.status === 401 ? 'Invalid username or password.' : e.message || String(e)
  } finally {
    busy.value = null
  }
}

function doLogout() {
  logout()
  authed.value = !authRequired.value
  authUser.value = ''
  pageRef.value = null
  metadata.value = null
  error.value = ''
}

watch(pageRef, (ref) => {
  if (ref?.page_url && !pageUrl.value) {
    pageUrl.value = ref.page_url
  }
})

function clearError() {
  error.value = ''
}

async function runResolve() {
  clearError()
  const q = beerName.value.trim()
  if (!q) {
    error.value = 'Enter a beer name (ideally “Brewery Beer Name”).'
    return
  }
  busy.value = 'resolve'
  try {
    pageRef.value = await resolveBeerName(q, options.value)
    pageUrl.value = pageRef.value.page_url
  } catch (e) {
    pageRef.value = null
    error.value = e.message || String(e)
  } finally {
    busy.value = null
  }
}

async function runMetadata() {
  clearError()
  const url = pageUrl.value.trim()
  if (!url) {
    error.value = 'Enter an Untappd beer page URL (/b/…).'
    return
  }
  busy.value = 'metadata'
  try {
    metadata.value = await fetchMetadata(url, options.value)
  } catch (e) {
    metadata.value = null
    error.value = e.message || String(e)
  } finally {
    busy.value = null
  }
}

/** Resolve then metadata — exposes both APIs in one click */
async function runBoth() {
  clearError()
  const q = beerName.value.trim()
  if (!q) {
    error.value = 'Enter a beer name (ideally “Brewery Beer Name”).'
    return
  }
  busy.value = 'both'
  pageRef.value = null
  metadata.value = null
  try {
    // Prefer server crawl (same policy) so history append is consistent,
    // but still surface both interface results.
    const crawled = await crawlBeer(q, options.value)
    pageRef.value = crawled.page
    metadata.value = crawled.metadata
    if (crawled.page?.page_url) pageUrl.value = crawled.page.page_url
  } catch (e) {
    if (e.status === 401) {
      error.value = 'Session expired — sign in again.'
      doLogout()
    } else {
      try {
        pageRef.value = await resolveBeerName(q, options.value)
        pageUrl.value = pageRef.value.page_url
        metadata.value = await fetchMetadata(pageRef.value.page_url, options.value)
      } catch (e2) {
        error.value = e2.message || e.message || String(e2)
        if (e2.status === 401) doLogout()
      }
    }
  } finally {
    busy.value = null
    try {
      if (authed.value) apiHealth.value = await healthDetail()
      else await refreshHealth()
    } catch {
      /* ignore */
    }
  }
}

function formatScore(score) {
  if (score == null || Number.isNaN(Number(score))) return '—'
  return Number(score).toFixed(2)
}

function formatCount(n) {
  if (n == null) return '—'
  return Number(n).toLocaleString()
}

function formatWhen(iso) {
  if (!iso) return '—'
  try {
    return new Date(iso).toLocaleString()
  } catch {
    return iso
  }
}

function originLabel(fromHistory) {
  return fromHistory ? 'history' : 'live'
}

const ratingPercent = computed(() => {
  const s = metadata.value?.rating_score
  if (s == null) return 0
  return Math.max(0, Math.min(100, (Number(s) / 5) * 100))
})
</script>

<template>
  <div class="page">
    <header class="hero">
      <div class="brand">
        <span class="mark" aria-hidden="true">🍺</span>
        <div>
          <h1>beers-crawler</h1>
          <p class="tagline">Untappd resolve + metadata</p>
        </div>
      </div>
      <div class="health" :class="apiHealth ? 'ok' : 'down'">
        <span class="dot" />
        <template v-if="apiHealth">
          API {{ apiHealth.status }}
          <template v-if="apiHealth.auth_required">
            <span class="sep">·</span>
            auth
          </template>
          <template v-if="authed && apiHealth.stats">
            <span class="sep">·</span>
            {{ apiHealth.stats?.with_rating_score ?? 0 }} scored
            <span class="sep">·</span>
            refresh {{ Math.round((apiHealth.min_refresh_seconds || 0) / 3600) }}h
          </template>
          <template v-if="authUser">
            <span class="sep">·</span>
            {{ authUser }}
          </template>
        </template>
        <template v-else>
          API offline — start <code>beers-crawler serve</code>
        </template>
      </div>
      <button
        v-if="authed && authRequired"
        type="button"
        class="ghost logout"
        @click="doLogout"
      >
        Log out
      </button>
    </header>

    <section v-if="authRequired && !authed" class="panel login-panel">
      <h2>Sign in</h2>
      <p class="hint">This API requires a username and password.</p>
      <form class="login-form" @submit.prevent="submitLogin">
        <label class="field">
          <span>Username</span>
          <input v-model="loginUser" type="text" autocomplete="username" required />
        </label>
        <label class="field">
          <span>Password</span>
          <input v-model="loginPass" type="password" autocomplete="current-password" required />
        </label>
        <p v-if="loginError" class="error" role="alert">{{ loginError }}</p>
        <button type="submit" class="primary" :disabled="busy === 'login'">
          {{ busy === 'login' ? 'Signing in…' : 'Sign in' }}
        </button>
      </form>
    </section>

    <main v-else class="grid">
      <section class="panel">
        <h2>1 · Resolve</h2>
        <p class="hint">Beer name → Untappd page URL</p>
        <label class="field">
          <span>Beer name</span>
          <input
            v-model="beerName"
            type="search"
            placeholder="Brewery Beer Name"
            autocomplete="off"
            @keydown.enter.prevent="runResolve"
          />
        </label>

        <div class="toggles">
          <label class="check">
            <input v-model="historyOnly" type="checkbox" />
            History only
          </label>
          <label class="check">
            <input v-model="force" type="checkbox" />
            Force live
          </label>
        </div>

        <div class="actions">
          <button type="button" class="primary" :disabled="!!busy" @click="runBoth">
            {{ busy === 'both' ? 'Looking up…' : 'Look up beer' }}
          </button>
          <button type="button" class="ghost" :disabled="!!busy" @click="runResolve">
            {{ busy === 'resolve' ? 'Resolving…' : 'Resolve only' }}
          </button>
        </div>

        <h2 class="spaced">2 · Metadata</h2>
        <p class="hint">Untappd page URL → rating &amp; details</p>
        <label class="field">
          <span>Page URL</span>
          <input
            v-model="pageUrl"
            type="url"
            placeholder="https://untappd.com/b/…/…"
            autocomplete="off"
            @keydown.enter.prevent="runMetadata"
          />
        </label>
        <div class="actions">
          <button type="button" class="ghost" :disabled="!!busy" @click="runMetadata">
            {{ busy === 'metadata' ? 'Fetching…' : 'Fetch metadata' }}
          </button>
        </div>

        <p v-if="error" class="error" role="alert">{{ error }}</p>
      </section>

      <section class="panel results">
        <h2>Result</h2>

        <div v-if="!pageRef && !metadata && !busy" class="empty">
          Look up a beer to see rating, brewery, style, and page link.
        </div>

        <div v-if="busy" class="empty pulse">Working…</div>

        <article v-if="metadata || pageRef" class="beer-card">
          <header class="beer-head">
            <div>
              <p class="brewery">{{ metadata?.brewery || '—' }}</p>
              <h3>{{ metadata?.name || pageRef?.query || 'Beer' }}</h3>
              <p v-if="metadata?.style" class="style">{{ metadata.style }}</p>
            </div>
            <div class="score-block" :title="metadata?.rating_score != null ? `${metadata.rating_score} / 5` : 'No score'">
              <div class="score-num">{{ formatScore(metadata?.rating_score) }}</div>
              <div class="score-max">/ 5</div>
              <div class="score-bar" aria-hidden="true">
                <div class="score-fill" :style="{ width: ratingPercent + '%' }" />
              </div>
              <div class="score-count">{{ formatCount(metadata?.rating_count) }} ratings</div>
            </div>
          </header>

          <dl class="facts">
            <div>
              <dt>ABV</dt>
              <dd>{{ metadata?.abv != null ? `${metadata.abv}%` : '—' }}</dd>
            </div>
            <div>
              <dt>IBU</dt>
              <dd>{{ metadata?.ibu != null ? metadata.ibu : '—' }}</dd>
            </div>
            <div>
              <dt>Beer ID</dt>
              <dd>{{ metadata?.beer_id || pageRef?.beer_id || '—' }}</dd>
            </div>
            <div>
              <dt>Source</dt>
              <dd>
                <span v-if="metadata" class="pill" :class="metadata.from_history ? 'hist' : 'live'">
                  meta · {{ originLabel(metadata.from_history) }}
                </span>
                <span v-if="pageRef" class="pill" :class="pageRef.from_history ? 'hist' : 'live'">
                  resolve · {{ originLabel(pageRef.from_history) }}
                </span>
              </dd>
            </div>
            <div class="wide">
              <dt>Scraped</dt>
              <dd>{{ formatWhen(metadata?.scraped_at) }}</dd>
            </div>
            <div class="wide">
              <dt>Page</dt>
              <dd>
                <a
                  v-if="metadata?.page_url || pageRef?.page_url"
                  :href="metadata?.page_url || pageRef?.page_url"
                  target="_blank"
                  rel="noopener noreferrer"
                >
                  {{ metadata?.page_url || pageRef?.page_url }}
                </a>
                <span v-else>—</span>
              </dd>
            </div>
          </dl>

          <p v-if="metadata?.description" class="desc">{{ metadata.description }}</p>

          <details v-if="pageRef" class="raw">
            <summary>Resolve details</summary>
            <dl class="mini">
              <div><dt>Query</dt><dd>{{ pageRef.query }}</dd></div>
              <div><dt>Match</dt><dd>{{ Number(pageRef.match_score).toFixed(2) }}</dd></div>
              <div><dt>Slug</dt><dd class="mono">{{ pageRef.slug || '—' }}</dd></div>
              <div><dt>Source</dt><dd>{{ pageRef.source }}</dd></div>
            </dl>
          </details>
        </article>
      </section>
    </main>

    <footer class="foot">
      Interfaces:
      <code>GET /v1/resolve?q=</code>
      ·
      <code>GET /v1/metadata?url=</code>
      · combined
      <code>POST /v1/crawl</code>
    </footer>
  </div>
</template>

<style scoped>
.page {
  max-width: 1040px;
  margin: 0 auto;
  padding: 2rem 1.25rem 3rem;
}

.hero {
  display: flex;
  flex-wrap: wrap;
  align-items: flex-end;
  justify-content: space-between;
  gap: 1rem;
  margin-bottom: 1.75rem;
}

.logout {
  margin-left: auto;
}

.login-panel {
  max-width: 420px;
  margin: 0 auto 2rem;
}

.login-form .primary {
  width: 100%;
  margin-top: 0.5rem;
}

.brand {
  display: flex;
  gap: 0.85rem;
  align-items: center;
}

.mark {
  font-size: 2rem;
  filter: saturate(0.9);
}

h1 {
  margin: 0;
  font-family: var(--font);
  font-weight: 600;
  font-size: 1.85rem;
  letter-spacing: 0.02em;
}

.tagline {
  margin: 0.15rem 0 0;
  color: var(--muted);
  font-size: 0.95rem;
}

.health {
  display: inline-flex;
  align-items: center;
  gap: 0.45rem;
  padding: 0.4rem 0.75rem;
  border-radius: 999px;
  border: 1px solid var(--border);
  background: var(--bg-elevated);
  color: var(--muted);
  font-size: 0.82rem;
}

.health.ok {
  color: var(--good);
  border-color: #2f4a32;
}

.health.down {
  color: var(--bad);
  border-color: #5a3030;
}

.health .dot {
  width: 0.5rem;
  height: 0.5rem;
  border-radius: 50%;
  background: currentColor;
  box-shadow: 0 0 8px currentColor;
}

.health .sep {
  opacity: 0.5;
}

.health code {
  font-family: var(--mono);
  font-size: 0.78rem;
}

.grid {
  display: grid;
  grid-template-columns: 1fr 1.15fr;
  gap: 1.25rem;
}

@media (max-width: 820px) {
  .grid {
    grid-template-columns: 1fr;
  }
}

.panel {
  background: linear-gradient(180deg, var(--bg-card), var(--bg-elevated));
  border: 1px solid var(--border);
  border-radius: var(--radius);
  padding: 1.25rem 1.35rem 1.4rem;
  box-shadow: 0 12px 40px #00000055;
}

.panel h2 {
  margin: 0;
  font-family: var(--font);
  font-size: 1.2rem;
  font-weight: 600;
}

.panel h2.spaced {
  margin-top: 1.5rem;
  padding-top: 1.25rem;
  border-top: 1px solid var(--border);
}

.hint {
  margin: 0.25rem 0 1rem;
  color: var(--muted);
  font-size: 0.88rem;
}

.field {
  display: flex;
  flex-direction: column;
  gap: 0.35rem;
  margin-bottom: 0.85rem;
}

.field span {
  font-size: 0.78rem;
  text-transform: uppercase;
  letter-spacing: 0.06em;
  color: var(--muted);
}

.field input {
  width: 100%;
  padding: 0.7rem 0.85rem;
  border-radius: 8px;
  border: 1px solid var(--border);
  background: #100e0c;
  color: var(--text);
  outline: none;
}

.field input:focus {
  border-color: var(--accent-dim);
  box-shadow: 0 0 0 3px var(--focus);
}

.toggles {
  display: flex;
  flex-wrap: wrap;
  gap: 1rem;
  margin-bottom: 1rem;
}

.check {
  display: inline-flex;
  align-items: center;
  gap: 0.4rem;
  color: var(--muted);
  font-size: 0.9rem;
  cursor: pointer;
}

.actions {
  display: flex;
  flex-wrap: wrap;
  gap: 0.6rem;
}

button {
  border-radius: 8px;
  border: 1px solid transparent;
  padding: 0.65rem 1rem;
  cursor: pointer;
  transition: background 0.15s, border-color 0.15s, opacity 0.15s;
}

button:disabled {
  opacity: 0.55;
  cursor: wait;
}

button.primary {
  background: linear-gradient(180deg, #e8a54b, #c07a28);
  color: #1a1208;
  font-weight: 600;
}

button.primary:hover:not(:disabled) {
  filter: brightness(1.06);
}

button.ghost {
  background: transparent;
  border-color: var(--border);
  color: var(--text);
}

button.ghost:hover:not(:disabled) {
  border-color: var(--accent-dim);
  color: var(--accent);
}

.error {
  margin: 1rem 0 0;
  padding: 0.75rem 0.85rem;
  border-radius: 8px;
  background: #3a1c1c;
  border: 1px solid #6a3030;
  color: #f0c0c0;
  font-size: 0.9rem;
}

.empty {
  color: var(--muted);
  padding: 2rem 0.5rem;
  text-align: center;
}

.pulse {
  animation: pulse 1.2s ease-in-out infinite;
}

@keyframes pulse {
  50% {
    opacity: 0.45;
  }
}

.beer-card {
  display: flex;
  flex-direction: column;
  gap: 1.1rem;
}

.beer-head {
  display: flex;
  justify-content: space-between;
  gap: 1rem;
  align-items: flex-start;
}

.brewery {
  margin: 0;
  color: var(--accent);
  font-size: 0.92rem;
  letter-spacing: 0.02em;
}

.beer-head h3 {
  margin: 0.15rem 0 0;
  font-family: var(--font);
  font-size: 1.55rem;
  font-weight: 600;
  line-height: 1.2;
}

.style {
  margin: 0.35rem 0 0;
  color: var(--muted);
  font-size: 0.95rem;
}

.score-block {
  min-width: 7.5rem;
  text-align: right;
  flex-shrink: 0;
}

.score-num {
  display: inline;
  font-family: var(--font);
  font-size: 2.4rem;
  font-weight: 600;
  color: var(--accent);
  line-height: 1;
}

.score-max {
  display: inline;
  color: var(--muted);
  margin-left: 0.15rem;
}

.score-bar {
  margin-top: 0.45rem;
  height: 6px;
  border-radius: 999px;
  background: #3a3228;
  overflow: hidden;
}

.score-fill {
  height: 100%;
  background: linear-gradient(90deg, #c07a28, #e8a54b);
  border-radius: inherit;
}

.score-count {
  margin-top: 0.35rem;
  font-size: 0.78rem;
  color: var(--muted);
}

.facts {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 0.75rem 1rem;
  margin: 0;
  padding: 1rem 0 0;
  border-top: 1px solid var(--border);
}

.facts .wide {
  grid-column: 1 / -1;
}

.facts dt {
  font-size: 0.72rem;
  text-transform: uppercase;
  letter-spacing: 0.06em;
  color: var(--muted);
  margin-bottom: 0.15rem;
}

.facts dd {
  margin: 0;
  word-break: break-word;
}

.pill {
  display: inline-block;
  margin-right: 0.35rem;
  margin-top: 0.15rem;
  padding: 0.15rem 0.5rem;
  border-radius: 999px;
  font-size: 0.75rem;
  border: 1px solid var(--border);
}

.pill.live {
  color: var(--good);
  border-color: #2f4a32;
  background: #1a2a1c;
}

.pill.hist {
  color: var(--warn);
  border-color: #5a4a18;
  background: #2a2410;
}

.desc {
  margin: 0;
  padding: 0.85rem 1rem;
  border-radius: 8px;
  background: #181410;
  border: 1px solid var(--border);
  color: #d8cbb8;
  font-size: 0.92rem;
}

.raw {
  border-top: 1px solid var(--border);
  padding-top: 0.75rem;
  color: var(--muted);
  font-size: 0.88rem;
}

.raw summary {
  cursor: pointer;
  user-select: none;
}

.mini {
  display: grid;
  gap: 0.45rem;
  margin: 0.75rem 0 0;
}

.mini div {
  display: grid;
  grid-template-columns: 5rem 1fr;
  gap: 0.5rem;
}

.mini dt {
  color: var(--muted);
}

.mini dd {
  margin: 0;
}

.mono {
  font-family: var(--mono);
  font-size: 0.85rem;
}

.foot {
  margin-top: 1.75rem;
  color: var(--muted);
  font-size: 0.8rem;
  text-align: center;
}

.foot code {
  font-family: var(--mono);
  font-size: 0.75rem;
  color: #cbb89a;
}
</style>
