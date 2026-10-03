/**
 * Hermes Provider Quota Dashboard — Hermes Desktop plugin
 * https://github.com/himanusia/hermes-provider-quota-dashboard
 *
 * A desktop pane with live quota readouts for EVERY account of OpenAI Codex
 * (credential pool), OpenCode Go, CommandCode, and the Claude Pro/Max
 * subscription (Claude Code's OAuth login), and the Antigravity / Google AI
 * subscription (agy's OAuth login) — plus read-only local
 * OmniRoute and 9router history/configuration with route explanations hidden
 * behind collapsed details.
 *
 * Data path: probe.py runs on the backend host via the gateway's `shell.exec`
 * RPC (read-only; secrets never leave the host). The probe prints one
 * `@@QUOTA@@ {json}` line which this pane renders as per-account bars.
 *
 * Install: copy this folder to ~/.hermes/desktop-plugins/quota-dash/
 * (or run install.sh). The app watches that folder and hot-reloads.
 *
 * Plain ESM, loaded uncompiled — UI is jsx() calls, not JSX syntax.
 * Only these imports resolve: @hermes/plugin-sdk, react, react/jsx-runtime.
 */

import {
  Badge,
  Button,
  Codicon,
  GlyphSpinner,
  PALETTE_AREA,
  ROUTES_AREA,
  SIDEBAR_NAV_AREA,
  Skeleton,
  STATUSBAR_AREAS,
  Switch,
  Tip,
  atom,
  cn,
  haptic,
  host,
  queryClient,
  useQuery,
  useValue
} from '@hermes/plugin-sdk'
import { useRef, useState } from 'react'
import { jsx, jsxs } from 'react/jsx-runtime'

const ID = 'quota-dash'
const QUERY_KEY = ['quota-dash', 'quotas']
const SENTINEL = '@@QUOTA@@'
const PROVIDER_IDS = ['openai-codex', 'opencode-go', 'commandcode', 'claude-subscription', 'antigravity-subscription']
const ROUTER_IDS = ['omniroute', '9router']

// --- display settings -------------------------------------------------------
// What the dashboard shows is user-configurable (⚙ in the pane header, or ⌘K
// "Quota: settings"). A hidden provider/router is not probed at all, so a
// subscription you do not have costs no request. Persisted via ctx.storage.
const SECTION_LABELS = {
  'openai-codex': 'Codex',
  'opencode-go': 'OpenCode Go',
  commandcode: 'CommandCode',
  'claude-subscription': 'Claude subscription',
  'antigravity-subscription': 'Antigravity subscription',
  omniroute: 'OmniRoute (local)',
  '9router': '9router (local)'
}
const DEFAULT_PREFS = {
  sections: Object.fromEntries([...PROVIDER_IDS, ...ROUTER_IDS].map(id => [id, true])),
  chip: true,
  notes: true,
  plan: true
}
const PREFS_KEY = 'displayPrefs'
let prefsStorage = null
const $prefs = atom(DEFAULT_PREFS)

function normalizePrefs(raw) {
  const value = raw && typeof raw === 'object' ? raw : {}
  const sections = { ...DEFAULT_PREFS.sections }

  for (const id of Object.keys(sections)) {
    if (value.sections && typeof value.sections[id] === 'boolean') {
      sections[id] = value.sections[id]
    }
  }

  const flag = key => (typeof value[key] === 'boolean' ? value[key] : DEFAULT_PREFS[key])

  return { sections, chip: flag('chip'), notes: flag('notes'), plan: flag('plan') }
}

function setPrefs(update) {
  const next = normalizePrefs(typeof update === 'function' ? update($prefs.get()) : update)
  $prefs.set(next)
  if (prefsStorage) {
    prefsStorage.set(PREFS_KEY, next)
  }
  // Newly enabled sections must load; disabled ones drop out of the poll.
  void queryClient.invalidateQueries({ queryKey: QUERY_KEY })
}

const enabledIds = (ids, prefs = $prefs.get()) => ids.filter(id => prefs.sections[id] !== false)
const ROUTE_PAGE_SIZE = 8

// Runs on the backend host. Backend env can override every path:
// HERMES_PYTHON (python), HERMES_QUOTA_PROBE (probe script), HERMES_HOME
// (default root); the defaults target a standard ~/.hermes install.
const PROBE_CMD =
  '/usr/bin/env -u PYTHONPATH "${HERMES_PYTHON:-$HOME/.hermes/hermes-agent/venv/bin/python}" "${HERMES_QUOTA_PROBE:-${HERMES_HOME:-$HOME/.hermes}/desktop-plugins/quota-dash/probe.py}"'

function buildProbeCommand({ provider, account, router, routerPart, routeOffset, routeLimit } = {}) {
  let cmd = PROBE_CMD

  if (router && /^(omniroute|9router)$/.test(router)) {
    cmd += ` --router ${router}`
    if (routerPart && /^(summary|routes)$/.test(routerPart)) {
      cmd += ` --router-part ${routerPart}`
    }
    if (Number.isInteger(routeOffset) && routeOffset >= 0) {
      cmd += ` --route-offset ${routeOffset}`
    }
    if (Number.isInteger(routeLimit) && routeLimit >= 1 && routeLimit <= ROUTE_PAGE_SIZE) {
      cmd += ` --route-limit ${routeLimit}`
    }
  } else if (account && /^[a-z0-9-]+:[0-9a-f]{6,12}$/.test(account)) {
    cmd += ` --account ${account}`
  } else if (provider && PROVIDER_IDS.includes(provider)) {
    cmd += ` --provider ${provider}`
  }

  return cmd
}

async function runProbe(filters) {
  // Each response must stay below shell.exec's 4 KB stdout cap. Router
  // summaries and route pages are separate; the full details are reassembled
  // by fetchRouter() before they reach the pane.
  // A booting backend can also hang rather than reject, so bound every RPC.
  let timer = null
  const result = await Promise.race([
    host.request('shell.exec', { command: buildProbeCommand(filters) }),
    new Promise((_, reject) => {
      timer = setTimeout(() => reject(new Error('quota probe timed out (60s)')), 60_000)
    })
  ]).finally(() => clearTimeout(timer))
  const stdout = String((result && result.stdout) || '')
  const marker = stdout.lastIndexOf(SENTINEL)

  if (marker < 0) {
    const stderr = String((result && result.stderr) || '')
      .trim()
      .split('\n')
      .slice(-3)
      .join(' ')
    const reason = stdout.length >= 3999
      ? 'probe output was truncated by shell.exec (4 KB limit)'
      : stderr || `probe produced no data (exit ${result && result.code})`
    throw new Error(reason)
  }

  const payload = JSON.parse(stdout.slice(marker + SENTINEL.length).trim().split('\n')[0])

  for (const router of payload.routers || []) {
    router.checkedAt = payload.fetchedAt || new Date().toISOString()
  }

  return payload
}

async function fetchRouter(routerId) {
  const summaryPayload = await runProbe({ router: routerId, routerPart: 'summary' })
  const router = (summaryPayload.routers || [])[0]

  if (!router) {
    throw new Error(`no local data for ${routerId}`)
  }

  const routeCount = Number(router.usage && router.usage.routeCount) || 0
  const offsets = routeCount > 0
    ? Array.from({ length: Math.ceil(routeCount / ROUTE_PAGE_SIZE) }, (_, index) => index * ROUTE_PAGE_SIZE)
    : [0]
  const pages = await Promise.all(offsets.map(routeOffset =>
    runProbe({ router: routerId, routerPart: 'routes', routeOffset, routeLimit: ROUTE_PAGE_SIZE })
  ))
  const routes = []
  let statusCounts = []
  let notes = router.notes || []

  for (const pagePayload of pages) {
    const page = (pagePayload.routers || [])[0]

    if (!page || page.id !== routerId) {
      throw new Error(`local route details missing for ${routerId}`)
    }

    routes.push(...((page.usage && page.usage.byRoute) || []))
    if (page.routeOffset === 0) {
      if (page.source && Array.isArray(page.source.tables)) {
        router.source = { ...(router.source || {}), tables: page.source.tables }
      }
      if (Array.isArray(page.connections)) {
        router.connections = page.connections
      }
      if (Array.isArray(page.observedAliases)) {
        router.observedAliases = page.observedAliases
      }
    }
    if (!statusCounts.length) {
      statusCounts = (page.usage && page.usage.statusCounts) || []
    }
    if ((page.notes || []).length) {
      notes = page.notes
    }
  }

  if (routes.length !== routeCount) {
    throw new Error(`incomplete local route details for ${routerId} (${routes.length}/${routeCount})`)
  }

  router.usage = { ...(router.usage || {}), byRoute: routes, statusCounts }
  router.notes = notes

  return { ...summaryPayload, providers: [], routers: [router] }
}

async function fetchQuotas(filters) {
  if (filters && filters.router) {
    return fetchRouter(filters.router)
  }

  if (filters && (filters.provider || filters.account)) {
    return runProbe(filters)
  }

  const started = Date.now()
  const [providerPayloads, routerPayloads] = await Promise.all([
    Promise.all(enabledIds(PROVIDER_IDS).map(provider => runProbe({ provider }))),
    Promise.all(enabledIds(ROUTER_IDS).map(router => fetchRouter(router)))
  ])

  return {
    fetchedAt: new Date().toISOString(),
    elapsedMs: Date.now() - started,
    providers: providerPayloads.flatMap(payload => payload.providers || []),
    routers: routerPayloads.flatMap(payload => payload.routers || [])
  }
}

function mergeProvider(previous, fresh) {
  if (!previous) {
    return { providers: [fresh] }
  }

  return {
    ...previous,
    providers: previous.providers.map(provider => (provider.id === fresh.id ? fresh : provider))
  }
}

function mergeRouter(previous, fresh) {
  const routers = (previous && previous.routers) || []
  const existing = routers.some(router => router.id === fresh.id)

  return {
    ...(previous || {}),
    routers: existing
      ? routers.map(router => (router.id === fresh.id ? fresh : router))
      : [...routers, fresh]
  }
}

function resetLabel(iso) {
  if (!iso) {
    return ''
  }

  const at = new Date(iso).getTime()

  if (!Number.isFinite(at)) {
    return ''
  }

  const ms = at - Date.now()

  if (ms <= 0) {
    return 'resetting'
  }

  const minutes = Math.floor(ms / 60000)
  const days = Math.floor(minutes / 1440)
  const hours = Math.floor((minutes % 1440) / 60)

  if (days > 0) {
    return `reset ${days}d ${hours}h`
  }

  return hours > 0 ? `reset ${hours}h ${minutes % 60}m` : `reset ${minutes}m`
}

function RefreshButton({ busy, onRefresh, label }) {
  return jsx(Button, {
    variant: 'ghost',
    className: 'h-5 w-5 shrink-0 p-0 text-[0.7rem] leading-none',
    disabled: busy,
    onClick: onRefresh,
    title: label,
    children: busy ? jsx(GlyphSpinner, { className: 'size-2.5' }) : '↻'
  })
}

function WindowRow({ window: w }) {
  const pct = Math.max(0, Math.min(100, Number(w.pct) || 0))
  const shown = pct % 1 === 0 ? `${pct}` : pct.toFixed(1)
  const meta = [w.note, resetLabel(w.reset)].filter(Boolean).join(' · ')

  return jsxs('div', {
    className: 'flex flex-col gap-1',
    children: [
      jsxs('div', {
        className: 'flex items-baseline justify-between gap-2 text-[0.6875rem]',
        children: [
          jsx('span', { className: 'truncate text-(--ui-text-tertiary)', children: w.k }),
          jsxs('span', {
            className: 'shrink-0 tabular-nums text-(--ui-text-secondary)',
            children: [jsx('span', { children: `${shown}%` }), meta ? jsx('span', { className: 'text-(--ui-text-quaternary)', children: ` · ${meta}` }) : null]
          })
        ]
      }),
      jsx('div', {
        className: 'h-1 w-full overflow-hidden rounded-full bg-(--ui-stroke-secondary)',
        children: jsx('div', {
          className: 'h-full rounded-full bg-(--ui-accent)',
          style: { width: `${pct}%` }
        })
      })
    ]
  })
}

function AccountCard({ account, sharedCreds, busy, onRefresh }) {
  const prefs = useValue($prefs)
  const notes = prefs.notes ? account.notes || [] : []
  const windows = account.windows || []
  const creds = sharedCreds || []
  const merged = creds.length > 1
  const title = merged ? creds.map(cred => cred.label).join(' · ') : account.label

  return jsxs('div', {
    className: 'flex flex-col gap-2 rounded-md border border-(--ui-stroke-secondary) p-2',
    children: [
      jsxs('div', {
        className: 'flex items-start gap-1.5',
        children: [
          jsx('div', {
            className: cn('min-w-0 flex-1 text-xs font-medium', merged ? 'break-words leading-snug' : 'truncate'),
            children: title
          }),
          account.plan && prefs.plan ? jsx(Badge, { variant: 'muted', size: 'xs', children: account.plan }) : null,
          jsx(RefreshButton, { busy, onRefresh, label: merged ? 'Refresh all credentials' : `Refresh ${account.label}` })
        ]
      }),
      account.sub
        ? jsx('div', { className: 'truncate text-[0.65rem] text-(--ui-text-quaternary)', children: account.sub })
        : null,
      windows.length
        ? jsxs('div', { className: 'flex flex-col gap-2', children: windows.map(w => jsx(WindowRow, { window: w, key: w.k })) })
        : null,
      merged
        ? jsx('div', {
            className: 'text-[0.65rem] text-(--ui-text-quaternary)',
            children: `${creds.length} credentials share one account's quota`
          })
        : null,
      creds
        .filter(cred => cred.error)
        .map((cred, index) =>
          jsx('div', { className: 'text-[0.65rem] text-(--ui-text-secondary)', children: `⚠ ${cred.label}: ${cred.error}` }, `cred-error-${index}`)
        ),
      notes.map((note, index) =>
        jsx('div', { className: 'text-[0.65rem] text-(--ui-text-tertiary)', children: note }, `note-${index}`)
      ),
      account.error
        ? jsx('div', { className: 'text-[0.65rem] text-(--ui-text-secondary)', children: `⚠ ${account.error}` })
        : null
    ]
  })
}

function ProviderSection({ provider, busy, busyAccounts, onRefreshProvider, onRefreshAccount }) {
  // Credentials that resolve to the same account share one quota — collapse
  // them into a single card; the card lists the credentials that share it.
  const groups = []

  for (const account of provider.accounts || []) {
    const key = account.acct ? `acct:${account.acct}` : `cred:${account.fp || account.label}`
    let group = groups.find(candidate => candidate.key === key)

    if (!group) {
      group = { key, account, creds: [] }
      groups.push(group)
    } else if (group.account.error && !account.error) {
      group.account = account
    }

    group.creds.push({ label: account.label, fp: account.fp, error: account.error })
  }

  return jsxs('div', {
    className: 'flex flex-col gap-2',
    children: [
      jsxs('div', {
        className: 'flex items-center gap-1.5',
        children: [
          jsx('div', { className: 'min-w-0 flex-1 truncate text-xs font-medium', children: provider.name }),
          jsx(Badge, { variant: 'outline', size: 'xs', children: String(groups.length) }),
          jsx(RefreshButton, { busy, onRefresh: onRefreshProvider, label: `Refresh ${provider.name}` })
        ]
      }),
      groups.length === 0
        ? jsx('div', { className: 'text-[0.6875rem] text-(--ui-text-quaternary)', children: 'no credentials found' })
        : groups.map(group =>
            jsx(
              AccountCard,
              {
                account: group.account,
                sharedCreds: group.creds,
                busy: group.creds.some(cred => Boolean(busyAccounts[`${provider.id}:${cred.fp}`])),
                onRefresh: () => group.creds.filter(cred => cred.fp).forEach(cred => onRefreshAccount(provider.id, cred.fp, cred.label))
              },
              `${provider.id}|${group.key}`
            )
          )
    ]
  })
}

function formatRouterCount(value) {
  if (value === null || value === undefined || value === '') {
    return '—'
  }

  const number = Number(value)

  if (!Number.isFinite(number)) {
    return '—'
  }

  return new Intl.NumberFormat(undefined, { notation: 'compact', maximumFractionDigits: 1 }).format(number)
}

function formatLocalCost(value) {
  const number = Number(value)

  return Number.isFinite(number) && number > 0 ? `$${number.toFixed(2)}` : null
}

function formatRouterTimestamp(value) {
  if (!value) {
    return null
  }

  const date = new Date(value)

  return Number.isFinite(date.getTime())
    ? date.toLocaleString(undefined, { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' })
    : String(value)
}

function routerModelLabel(provider, model) {
  const name = String(model || 'unknown')
  const source = String(provider || '').trim()

  if (!source || name === source || name.startsWith(`${source}/`)) {
    return name
  }

  return `${source} / ${name}`
}

function routerDefaultSummary(router) {
  const usage = router.usage || {}
  const cachedQuotas = router.cachedQuotas || []

  if (router.status === 'unavailable') {
    return router.summary || 'local database unavailable'
  }

  const parts = []

  if (usage.available) {
    parts.push(`${formatRouterCount(usage.requests)} local requests`)
    const input = Number(usage.inputTokens) || 0
    const output = Number(usage.outputTokens) || 0
    const cost = formatLocalCost(usage.estimatedCost)

    if (input || output) {
      parts.push(`${formatRouterCount(input)} in · ${formatRouterCount(output)} out`)
    }

    if (cost) {
      parts.push(`local ledger ${cost}`)
    }
  } else {
    parts.push('configured locally · no local usage rows')
  }

  const lastActivity = formatRouterTimestamp(usage.lastSeen)

  if (lastActivity) {
    parts.push(`last activity ${lastActivity}`)
  }

  if (cachedQuotas.length) {
    parts.push(`${cachedQuotas.length} cached quota windows`)
    parts.push('cached only · not refreshed')
  } else {
    parts.push('upstream quota not queried')
  }

  return parts.join(' · ')
}

function RouterAliasCard({ alias }) {
  const models = alias.models || []
  const observed = Number(alias.observedRequests)

  return jsxs('div', {
    className: 'flex flex-col gap-1.5 rounded-md border border-(--ui-stroke-secondary) p-2',
    children: [
      jsxs('div', {
        className: 'flex items-center gap-1.5',
        children: [
          jsx('div', { className: 'min-w-0 flex-1 break-words text-xs font-medium', children: alias.name }),
          jsx(Badge, { variant: 'muted', size: 'xs', children: alias.strategy || 'unknown' }),
          Number.isFinite(observed) ? jsx(Badge, { variant: 'outline', size: 'xs', children: `${formatRouterCount(observed)} seen` }) : null
        ]
      }),
      jsx('div', { className: 'text-[0.6rem] leading-relaxed text-(--ui-text-tertiary)', children: alias.explanation }),
      models.length
        ? jsxs('div', {
            className: 'flex flex-col gap-1',
            children: [
              jsx('div', { className: 'text-[0.65rem] font-medium text-(--ui-text-secondary)', children: 'Stored model order' }),
              ...models.map((model, index) =>
                jsxs(
                  'div',
                  {
                    className: 'flex items-baseline justify-between gap-2 text-[0.65rem]',
                    children: [
                      jsx('span', {
                        className: 'min-w-0 break-words text-(--ui-text-tertiary)',
                        children: routerModelLabel(model.provider, model.model)
                      }),
                      model.weight !== null && model.weight !== undefined
                        ? jsx('span', { className: 'shrink-0 text-(--ui-text-quaternary)', children: `weight ${model.weight}` })
                        : null
                    ]
                  },
                  `${alias.name}|${index}`
                )
              )
            ]
          })
        : jsx('div', { className: 'text-[0.65rem] text-(--ui-text-quaternary)', children: 'No model list in local metadata.' })
    ]
  })
}

function RouterUsageVisual({ router }) {
  const usage = router.usage || {}
  const input = Number(usage.inputTokens) || 0
  const output = Number(usage.outputTokens) || 0
  const metrics = [
    { label: 'Requests', value: formatRouterCount(usage.requests) },
    { label: 'Input tokens', value: formatRouterCount(input) },
    { label: 'Output tokens', value: formatRouterCount(output) },
    ...(formatLocalCost(usage.estimatedCost)
      ? [{ label: 'Local cost', value: formatLocalCost(usage.estimatedCost) }]
      : [])
  ]

  return jsxs('div', {
    className: 'flex flex-col gap-2',
    children: [
      jsxs('div', {
        className: 'grid grid-cols-2 gap-1.5',
        children: metrics.map(metric =>
          jsxs('div', {
            className: `flex min-w-0 flex-col gap-0.5 rounded border border-(--ui-stroke-secondary) px-1.5 py-1 ${metrics.length === 3 && metric.label === 'Output tokens' ? 'col-span-2' : ''}`,
            children: [
              jsx('div', { className: 'truncate text-base font-semibold tabular-nums text-(--ui-text-primary)', children: metric.value }),
              jsx('div', { className: 'truncate text-(--ui-text-quaternary)', style: { fontSize: '0.6rem' }, children: metric.label })
            ]
          }, metric.label)
        )
      })
    ]
  })
}

function RouterCallsDetails({ router }) {
  const usage = router.usage || {}
  const totalRequests = Math.max(0, Number(usage.requests) || 0)
  const routes = [...(usage.byRoute || [])].sort((left, right) => Number(right.requests) - Number(left.requests))
  const topRoutes = routes.slice(0, 4)
  const topRequests = topRoutes.reduce((total, route) => total + (Number(route.requests) || 0), 0)
  const otherRequests = Math.max(0, totalRequests - topRequests)
  const chartRoutes = otherRequests > 0
    ? [...topRoutes, { other: true, requests: otherRequests }]
    : topRoutes

  return jsx('details', {
    className: 'text-[0.58rem]',
    children: [
      jsx('summary', {
        className: 'cursor-pointer select-none text-[0.58rem] text-(--ui-text-secondary)',
        children: 'Calls per model'
      }),
      totalRequests > 0 && chartRoutes.length
        ? jsxs('div', {
            className: 'mt-2 flex flex-col gap-1.5',
            children: [
              jsx('div', { className: 'text-[0.55rem] text-(--ui-text-quaternary)', children: 'Share of local requests' }),
              ...chartRoutes.map((route, index) => {
                const requestCount = Math.max(0, Number(route.requests) || 0)
                const sharePct = Math.max(0, Math.min(100, requestCount / totalRequests * 100))
                const label = route.other ? 'Other' : routerModelLabel(route.provider, route.model)

                return jsxs('div', {
                  className: 'flex flex-col gap-0.5',
                  children: [
                    jsxs('div', {
                      className: 'flex items-start justify-between gap-2 text-[0.58rem]',
                      children: [
                        jsx('span', { className: 'min-w-0 flex-1 break-words text-(--ui-text-tertiary)', title: label, children: label }),
                        jsx('span', {
                          className: 'shrink-0 tabular-nums text-(--ui-text-secondary)',
                          children: `${formatRouterCount(requestCount)} · ${formatPct(sharePct)}%`
                        })
                      ]
                    }),
                    jsx('div', {
                      className: 'h-1.5 w-full overflow-hidden rounded-full bg-(--ui-stroke-secondary)',
                      role: 'meter',
                      'aria-label': `${label} share of local requests`,
                      'aria-valuemin': 0,
                      'aria-valuemax': 100,
                      'aria-valuenow': sharePct,
                      title: `${formatRouterCount(requestCount)} of ${formatRouterCount(totalRequests)} local requests`,
                      children: jsx('div', {
                        className: 'route-meter-fill h-full rounded-full bg-(--ui-accent)',
                        style: { width: `${sharePct}%`, opacity: Math.max(0.45, 1 - index * 0.12) }
                      })
                    })
                  ]
                }, `${router.id}|route-share|${index}`)
              })
            ]
          })
        : jsx('div', { className: 'pt-2 text-[0.55rem] text-(--ui-text-quaternary)', children: 'No local request-share data' }),
      jsx('details', {
        className: 'mt-3 text-[0.55rem]',
        children: [
          jsx('summary', { className: 'cursor-pointer select-none text-(--ui-text-quaternary)', children: 'More router details' }),
          jsx(RouterDetails, { router })
        ]
      })
    ]
  })
}

function quotaPctOrNull(value) {
  if (value === null || value === undefined || value === '') {
    return null
  }

  const number = Number(value)
  return Number.isFinite(number) ? number : null
}

// The probe reports cached snapshots as remaining%; every other quota readout
// in this pane is used%. Convert at render so both read the same direction;
// the complement swaps the aggregates (the most-used connection has the
// LOWEST remaining).
function usedPctFromRemaining(value) {
  const remaining = quotaPctOrNull(value)
  return remaining === null ? null : Math.max(0, Math.min(100, 100 - remaining))
}

function RouterQuotaMeter({ snapshot }) {
  const usedHigh = usedPctFromRemaining(snapshot.lowestRemainingPct)
  const usedLow = usedPctFromRemaining(snapshot.highestRemainingPct)

  if (usedHigh === null) {
    return null
  }

  const pct = usedHigh
  const shown = usedLow !== null && usedLow !== usedHigh
    ? `${formatPct(usedLow)}–${formatPct(usedHigh)}%`
    : `${formatPct(usedHigh)}%`
  const label = `${snapshot.provider} · ${snapshot.window}`

  return jsxs('div', {
    className: 'flex flex-col gap-1',
    children: [
      jsxs('div', {
        className: 'flex items-baseline justify-between gap-2 text-[0.58rem]',
        children: [
          jsx('span', { className: 'min-w-0 truncate text-(--ui-text-tertiary)', children: label }),
          jsx('span', { className: 'shrink-0 tabular-nums text-(--ui-text-secondary)', children: shown })
        ]
      }),
      jsx('div', {
        className: 'h-1.5 w-full overflow-hidden rounded-full bg-(--ui-stroke-secondary)',
        role: 'meter',
        'aria-label': `${label} quota used`,
        'aria-valuemin': 0,
        'aria-valuemax': 100,
        'aria-valuenow': pct,
        title: `highest used across ${formatRouterCount(snapshot.connections)} connections`,
        children: jsx('div', {
          className: 'quota-meter-fill h-full rounded-full bg-(--ui-accent)',
          style: { width: `${pct}%` }
        })
      })
    ]
  })
}

function RouterQuotaCard({ snapshot }) {
  const usedHigh = usedPctFromRemaining(snapshot.lowestRemainingPct)
  const usedLow = usedPctFromRemaining(snapshot.highestRemainingPct)
  const used = usedLow !== null && usedHigh !== null
    ? `${formatPct(usedLow)}–${formatPct(usedHigh)}% used`
    : 'used percentage unavailable'
  const observed = [
    formatRouterTimestamp(snapshot.oldestSnapshotAt),
    formatRouterTimestamp(snapshot.newestSnapshotAt)
  ].filter(Boolean)
  const observedLabel = observed.length === 2 && observed[0] !== observed[1]
    ? `${observed[0]} – ${observed[1]}`
    : observed[0] || 'unknown'
  const resetLabel = snapshot.resetAt
    ? `reset ${formatRouterTimestamp(snapshot.resetAt)}`
    : snapshot.resetsVary
      ? 'reset times vary or are unavailable'
      : 'reset time unavailable'
  const connectionCount = Number(snapshot.connections)
  const connectionNoun = connectionCount === 1 ? 'connection' : 'connections'

  return jsxs('div', {
    className: 'flex flex-col gap-0.5 rounded border border-(--ui-stroke-secondary) px-2 py-1.5 text-[0.65rem]',
    children: [
      jsx('div', { className: 'font-medium text-(--ui-text-tertiary)', children: `${snapshot.provider} · ${snapshot.window}` }),
      jsx('div', {
        className: 'text-(--ui-text-quaternary)',
        children: `${used} across ${formatRouterCount(snapshot.connections)} ${connectionNoun} · ${formatRouterTimestamp(snapshot.newestSnapshotAt) || 'snapshot time unknown'}`
      }),
      jsx('div', { className: 'text-(--ui-text-quaternary)', children: `${resetLabel} · ${formatRouterCount(snapshot.exhaustedConnections)} exhausted` }),
      observedLabel !== (formatRouterTimestamp(snapshot.newestSnapshotAt) || 'unknown')
        ? jsx('div', { className: 'text-(--ui-text-quaternary)', children: `snapshot range ${observedLabel}` })
        : null
    ]
  })
}

function RouterDetails({ router }) {
  const source = router.source || {}
  const usage = router.usage || {}
  const aliases = router.aliases || []
  const cachedQuotas = router.cachedQuotas || []
  const routes = usage.byRoute || []
  const connections = router.connections || []
  const statuses = usage.statusCounts || []
  const tables = (source.tables || []).filter(table => table.present)

  return jsxs('div', {
    className: 'flex flex-col gap-3 pt-2',
    children: [
      jsx('div', {
        className: 'text-[0.58rem] leading-relaxed text-(--ui-text-tertiary)',
        children: 'This section is local router data only. It does not fetch upstream subscription quota, test a provider, send a model request, or change router state.'
      }),
      (router.notes || []).map((note, index) =>
        jsx('div', { className: 'text-[0.65rem] text-(--ui-text-quaternary)', children: note }, `router-note-${router.id}-${index}`)
      ),
      jsxs('div', {
        className: 'flex flex-col gap-1',
        children: [
          jsx('div', { className: 'text-[0.65rem] font-medium text-(--ui-text-secondary)', children: 'Local data source' }),
          jsx('div', { className: 'break-all text-[0.65rem] text-(--ui-text-tertiary)', children: `${source.path || 'unknown'} · ${source.readOnly ? 'read-only SQLite' : 'read mode not confirmed'}` }),
          jsx('div', {
            className: 'text-[0.65rem] text-(--ui-text-quaternary)',
            children: tables.length ? `tables: ${tables.map(table => `${table.name} (${formatRouterCount(table.rows)} rows)`).join(' · ')}` : 'expected local tables were not found'
          })
        ]
      }),
      cachedQuotas.length
        ? jsxs('div', {
            className: 'flex flex-col gap-1.5',
            children: [
              jsx('div', { className: 'text-[0.65rem] font-medium text-(--ui-text-secondary)', children: 'Cached upstream quota snapshots' }),
              jsx('div', {
                className: 'text-[0.65rem] leading-relaxed text-(--ui-text-quaternary)',
                children: 'Used percentages are aggregated across connections. These cached values are read-only and are not refreshed by this dashboard.'
              }),
              ...cachedQuotas.map((snapshot, index) => jsx(RouterQuotaCard, { snapshot }, `${router.id}|cached-quota|${index}`))
            ]
          })
        : null,
      aliases.length
        ? jsxs('div', {
            className: 'flex flex-col gap-2',
            children: [
              jsx('div', { className: 'text-[0.65rem] font-medium text-(--ui-text-secondary)', children: 'Route aliases and explanations' }),
              ...aliases.map(alias => jsx(RouterAliasCard, { alias }, `${router.id}|alias|${alias.name}`))
            ]
          })
        : jsx('div', { className: 'text-[0.65rem] text-(--ui-text-quaternary)', children: 'No configured route aliases were found in local metadata.' }),
      routes.length
        ? jsxs('div', {
            className: 'flex flex-col gap-1.5',
            children: [
              jsx('div', { className: 'text-[0.65rem] font-medium text-(--ui-text-secondary)', children: 'Observed local models' }),
              ...routes.map((route, index) =>
                jsxs(
                  'div',
                  {
                    className: 'flex flex-col gap-0.5 rounded border border-(--ui-stroke-secondary) px-2 py-1.5 text-[0.65rem]',
                    children: [
                      jsx('div', { className: 'break-words text-(--ui-text-tertiary)', children: routerModelLabel(route.provider, route.model) }),
                      jsx('div', {
                        className: 'text-(--ui-text-quaternary)',
                        children: `${formatRouterCount(route.requests)} local requests · ${formatRouterCount(route.inputTokens)} in · ${formatRouterCount(route.outputTokens)} out · ${route.strategy || 'direct'}`
                      }),
                      route.estimatedCost ? jsx('div', { className: 'text-(--ui-text-quaternary)', children: `local ledger ${formatLocalCost(route.estimatedCost)}` }) : null
                    ]
                  },
                  `${router.id}|route|${index}`
                )
              )
            ]
          })
        : null,
      connections.length
        ? jsxs('div', {
            className: 'flex flex-col gap-1',
            children: [
              jsx('div', { className: 'text-[0.65rem] font-medium text-(--ui-text-secondary)', children: 'Configured connections (metadata only)' }),
              ...connections.map((connection, index) =>
                jsx(
                  'div',
                  {
                    className: 'text-[0.65rem] text-(--ui-text-quaternary)',
                    children: `${connection.provider || 'unknown'} · ${formatRouterCount(connection.active)} active / ${formatRouterCount(connection.connections)} total`
                  },
                  `${router.id}|connection|${index}`
                )
              )
            ]
          })
        : null,
      statuses.length
        ? jsxs('div', {
            className: 'flex flex-col gap-1',
            children: [
              jsx('div', { className: 'text-[0.65rem] font-medium text-(--ui-text-secondary)', children: 'Local record statuses' }),
              jsx('div', { className: 'text-[0.65rem] text-(--ui-text-quaternary)', children: statuses.map(item => `${item.status}: ${formatRouterCount(item.requests)}`).join(' · ') })
            ]
          })
        : null
    ]
  })
}

function LocalRouterSection({ router, busy, onRefresh }) {
  const status = router.status === 'available' ? 'local data' : router.status === 'unavailable' ? 'unavailable' : router.status || 'unknown'
  const available = router.status === 'available'
  const usageAvailable = Boolean(router.usage && router.usage.available)
  const cachedQuotas = router.cachedQuotas || []

  return jsxs('div', {
    className: 'flex flex-col gap-2 rounded-md border border-(--ui-stroke-secondary) p-2',
    children: [
      jsxs('div', {
        className: 'flex items-center gap-1.5',
        children: [
          jsx('div', { className: 'min-w-0 flex-1 truncate text-xs font-medium', children: router.name }),
          jsx(Badge, { variant: router.status === 'unavailable' ? 'muted' : 'outline', size: 'xs', children: status }),
          jsx(RefreshButton, { busy, onRefresh, label: `Refresh ${router.name} local data` })
        ]
      }),
      usageAvailable
        ? jsx(RouterUsageVisual, { router })
        : jsx('div', {
            className: 'text-[0.6rem] leading-relaxed text-(--ui-text-tertiary)',
            children: routerDefaultSummary(router)
          }),
      cachedQuotas.length
        ? jsxs('div', {
            className: 'flex flex-col gap-1.5',
            children: [
              jsx('div', { className: 'text-[0.58rem] font-medium text-(--ui-text-secondary)', children: 'Cached quota · used' }),
              ...cachedQuotas.map((snapshot, index) => jsx(RouterQuotaMeter, { snapshot }, `${router.id}|quota-meter|${index}`))
            ]
          })
        : available
          ? jsxs('div', {
              className: 'flex items-center gap-1.5 text-[0.58rem] text-(--ui-text-quaternary)',
              children: [
                jsx('span', { className: 'inline-flex size-5 items-center justify-center rounded-full border border-dashed border-(--ui-stroke-secondary)', children: '—' }),
                jsx('span', { children: 'No cached upstream quota' })
              ]
            })
          : null,
      usageAvailable ? jsx(RouterCallsDetails, { router }) : null
    ]
  })
}

function SettingRow({ label, checked, onChange }) {
  return jsxs('label', {
    className: 'flex cursor-pointer items-center justify-between gap-2 text-[0.6875rem] text-(--ui-text-secondary)',
    children: [
      jsx('span', { className: 'truncate', children: label }),
      jsx(Switch, { size: 'xs', checked, onCheckedChange: value => onChange(Boolean(value)) })
    ]
  })
}

function QuotaSettings() {
  const prefs = useValue($prefs)
  const section = id =>
    jsx(SettingRow, {
      label: SECTION_LABELS[id] || id,
      checked: prefs.sections[id] !== false,
      onChange: value => setPrefs(current => ({ ...current, sections: { ...current.sections, [id]: value } }))
    }, id)
  const flag = (key, label) =>
    jsx(SettingRow, { label, checked: prefs[key], onChange: value => setPrefs(current => ({ ...current, [key]: value })) }, key)
  const heading = text => jsx('div', { className: 'pt-1 text-[0.65rem] uppercase tracking-wide text-(--ui-text-quaternary)', children: text })

  return jsxs('div', {
    className: 'flex flex-col gap-1.5 rounded-md border border-(--ui-stroke-secondary) p-2',
    children: [
      heading('Providers'),
      ...PROVIDER_IDS.map(section),
      heading('Local routers'),
      ...ROUTER_IDS.map(section),
      heading('Display'),
      flag('chip', 'Statusbar chip'),
      flag('plan', 'Plan badge'),
      flag('notes', 'Account notes')
    ]
  })
}

function QuotaPane() {
  const prefs = useValue($prefs)
  const [settingsOpen, setSettingsOpen] = useState(false)
  const [busyProviders, setBusyProviders] = useState({})
  const [busyAccounts, setBusyAccounts] = useState({})
  const [busyRouters, setBusyRouters] = useState({})
  // Per-scope refresh generations: a slow in-flight response must never
  // clobber a newer refresh's result (last click wins).
  const refreshSeq = useRef({})
  const beginRefresh = scope => {
    const token = (refreshSeq.current[scope] || 0) + 1
    refreshSeq.current[scope] = token
    return token
  }
  const isCurrent = (scope, token) => refreshSeq.current[scope] === token

  // A fresh app open must load itself: hold the first fetch until the gateway
  // socket is open (shell.exec would just burn a failure during backend boot),
  // then heal fast after any failure instead of waiting a full minute.
  const gatewayReady = useValue(host.state.gateway) === 'open'
  const query = useQuery({
    queryKey: QUERY_KEY,
    queryFn: () => fetchQuotas(),
    enabled: gatewayReady,
    refetchInterval: q => (q.state.error ? 15_000 : 60_000),
    staleTime: 30_000,
    retry: 3,
    retryDelay: attempt => Math.min(1_000 * 2 ** attempt, 10_000)
  })

  const failure = err =>
    host.notify({ kind: 'error', message: `quota refresh failed: ${(err && err.message) || err}` })

  const refreshAll = () => {
    haptic('tap')
    void queryClient.invalidateQueries({ queryKey: QUERY_KEY })
  }

  const refreshProvider = providerId => {
    haptic('tap')
    const scope = `provider:${providerId}`
    const token = beginRefresh(scope)
    setBusyProviders(previous => ({ ...previous, [providerId]: true }))
    fetchQuotas({ provider: providerId })
      .then(payload => {
        if (!isCurrent(scope, token)) {
          return
        }

        const fresh = (payload.providers || [])[0]

        if (!fresh) {
          throw new Error(`no data for ${providerId}`)
        }

        queryClient.setQueryData(QUERY_KEY, previous => mergeProvider(previous, fresh))
      })
      .catch(err => {
        if (isCurrent(scope, token)) {
          failure(err)
        }
      })
      .finally(() => {
        if (isCurrent(scope, token)) {
          setBusyProviders(previous => ({ ...previous, [providerId]: false }))
        }
      })
  }

  const refreshAccount = (providerId, fp, label) => {
    haptic('tap')
    const key = `${providerId}:${fp}`
    const scope = `account:${key}`
    const token = beginRefresh(scope)
    setBusyAccounts(previous => ({ ...previous, [key]: true }))
    fetchQuotas({ account: key })
      .then(payload => {
        if (!isCurrent(scope, token)) {
          return
        }

        const fresh = (payload.providers || [])[0] && (payload.providers[0].accounts || [])[0]

        if (!fresh || !fresh.fp) {
          throw new Error(`no data for ${label}`)
        }

        queryClient.setQueryData(QUERY_KEY, previous => {
          if (!previous) {
            return previous
          }

          return {
            ...previous,
            providers: previous.providers.map(provider =>
              provider.id === providerId
                ? {
                    ...provider,
                    accounts: provider.accounts.map(account =>
                      // Match by fingerprint only. The label fallback could
                      // collide when two rows share a label and would clobber
                      // the wrong account; fp is always present and unique.
                      account.fp && fresh.fp && account.fp === fresh.fp ? fresh : account
                    )
                  }
                : provider
            )
          }
        })
      })
      .catch(err => {
        if (isCurrent(scope, token)) {
          failure(err)
        }
      })
      .finally(() => {
        if (isCurrent(scope, token)) {
          setBusyAccounts(previous => ({ ...previous, [key]: false }))
        }
      })
  }

  const refreshRouter = routerId => {
    haptic('tap')
    const scope = `router:${routerId}`
    const token = beginRefresh(scope)
    setBusyRouters(previous => ({ ...previous, [routerId]: true }))
    fetchQuotas({ router: routerId })
      .then(payload => {
        if (!isCurrent(scope, token)) {
          return
        }

        const fresh = (payload.routers || [])[0]

        if (!fresh) {
          throw new Error(`no local data for ${routerId}`)
        }

        queryClient.setQueryData(QUERY_KEY, previous => mergeRouter(previous, fresh))
      })
      .catch(err => {
        if (isCurrent(scope, token)) {
          failure(err)
        }
      })
      .finally(() => {
        if (isCurrent(scope, token)) {
          setBusyRouters(previous => ({ ...previous, [routerId]: false }))
        }
      })
  }

  const fetchedAt = query.data && query.data.fetchedAt ? new Date(query.data.fetchedAt) : null
  // Filter at render too: a cached payload from before a toggle must not
  // keep showing a section the user just hid.
  const routers = ((query.data && query.data.routers) || []).filter(router => prefs.sections[router.id] !== false)
  const shownProviders = ((query.data && query.data.providers) || []).filter(provider => prefs.sections[provider.id] !== false)

  return jsxs('div', {
    className: 'flex h-full flex-col gap-3 overflow-y-auto p-3 text-sm',
    children: [
      jsxs('div', {
        className: 'flex items-center justify-between gap-2',
        children: [
          jsx('div', { className: 'font-medium', children: 'Quota Dashboard' }),
          jsxs('div', {
            className: 'flex items-center gap-1',
            children: [
              fetchedAt
                ? jsx('span', {
                    className: 'text-[0.65rem] tabular-nums text-(--ui-text-quaternary)',
                    children: fetchedAt.toLocaleTimeString()
                  })
                : null,
              jsx(Button, {
                variant: 'ghost',
                className: cn('h-5 w-5 p-0', settingsOpen && 'text-foreground'),
                title: settingsOpen ? 'Close settings' : 'Choose what the dashboard shows',
                onClick: () => setSettingsOpen(open => !open),
                children: jsx(Codicon, { name: 'settings-gear', size: '0.75rem' })
              }),
              jsx(Button, {
                variant: 'ghost',
                className: 'h-5 px-1 text-[0.7rem]',
                onClick: refreshAll,
                disabled: query.isFetching,
                children: query.isFetching ? jsx(GlyphSpinner, { className: 'size-2.5' }) : 'Refresh all'
              })
            ]
          })
        ]
      }),
      settingsOpen ? jsx(QuotaSettings, {}) : null,
      query.isLoading
        ? jsxs('div', {
            className: 'flex flex-col gap-2',
            children: [
              jsx(Skeleton, { className: 'h-4 w-24' }),
              jsx(Skeleton, { className: 'h-20 w-full' }),
              jsx(Skeleton, { className: 'h-20 w-full' })
            ]
          })
        : query.isError
          ? jsxs('div', {
              className: 'flex flex-col gap-1 rounded-md border border-(--ui-stroke-secondary) p-2 text-[0.6875rem]',
              children: [
                jsx('div', { className: 'font-medium', children: 'probe failed' }),
                jsx('div', {
                  className: 'text-(--ui-text-tertiary)',
                  children: String((query.error && query.error.message) || query.error)
                }),
                jsxs('div', {
                  className: 'text-(--ui-text-quaternary)',
                  children: [
                    'manual run: ',
                    jsx('code', { children: '~/.hermes/hermes-agent/venv/bin/python ~/.hermes/desktop-plugins/quota-dash/probe.py' })
                  ]
                }),
                jsx(Button, { variant: 'outline', className: 'mt-1 w-fit h-5 px-1 text-[0.7rem]', onClick: refreshAll, children: 'Retry' })
              ]
            })
          : jsxs('div', {
              className: 'flex flex-col gap-3',
              children: [
                ...shownProviders.map(provider =>
                  jsx(
                    ProviderSection,
                    {
                      provider,
                      busy: Boolean(busyProviders[provider.id]),
                      busyAccounts,
                      onRefreshProvider: () => refreshProvider(provider.id),
                      onRefreshAccount: refreshAccount
                    },
                    provider.id
                  )
                ),
                ...(!shownProviders.length && !routers.length && !query.isFetching
                  ? [jsx('div', { className: 'text-[0.6875rem] text-(--ui-text-tertiary)', children: 'Nothing selected — open ⚙ to choose providers.' }, 'empty')]
                  : []),
                ...(routers.length
                  ? [
                      jsx('div', { className: 'pt-1 text-xs font-medium text-(--ui-text-secondary)', children: 'Local routers' }),
                      ...routers.map(router =>
                        jsx(
                          LocalRouterSection,
                          {
                            router,
                            busy: Boolean(busyRouters[router.id]),
                            onRefresh: () => refreshRouter(router.id)
                          },
                          router.id
                        )
                      )
                    ]
                  : [])
              ]
            })
    ]
  })
}

function paneContribution() {
  return {
    id: 'pane',
    area: 'panes',
    title: 'quotas',
    data: { placement: 'right', width: '300px' },
    render: () => jsx(QuotaPane, {})
  }
}

// Pane registration state at module scope so the statusbar chip, the palette
// commands, and the plugin lifecycle all drive the same toggle.
let paneCtx = null
let paneDisposer = null
const $paneOpen = atom(true)

function showPane() {
  if (!paneDisposer && paneCtx) {
    paneDisposer = paneCtx.register(paneContribution())
  }

  $paneOpen.set(true)
}

function hidePane() {
  if (paneDisposer) {
    paneDisposer()
    paneDisposer = null
  }

  $paneOpen.set(false)
}

function togglePane() {
  haptic('tap')

  // Deterministic toggle: registered → hide, else → show. (A visibility atom
  // can read false while the pane is still registered — e.g. tabbed behind in
  // its group — and an "already registered but not visible → resurface" branch
  // on that reading turns the chip into a re-register loop that never hides.)
  if (paneDisposer) {
    hidePane()
  } else {
    showPane()
  }
}

// --- statusbar chip: the live session's provider + its 5h limit ---------------

const PROVIDER_LABELS = {
  'openai-codex': 'Codex',
  'opencode-go': 'OpenCode Go',
  commandcode: 'CommandCode',
  'claude-subscription': 'Claude',
  'antigravity-subscription': 'Antigravity'
}
// One plugin icon, not per-provider glyphs: the chip is the dashboard's
// toggle, so it wears the same codicon as the sidebar row and the page.
const CHIP_ICON = 'dashboard'
const PROVIDER_ALIASES = {
  'openai-codex': 'openai-codex',
  codex: 'openai-codex',
  'opencode-go': 'opencode-go',
  'opencode-go-sub': 'opencode-go',
  go: 'opencode-go',
  zen: 'opencode-go',
  commandcode: 'commandcode',
  'commandcode-chat': 'commandcode',
  'commandcode-claude': 'commandcode',
  'commandcode-anthropic': 'commandcode',
  'claude-subscription': 'claude-subscription',
  'claude-subscription-directsdk-experimental': 'claude-subscription',
  'claude-subscription-directsdk': 'claude-subscription',
  'claude-code': 'claude-subscription',
  'antigravity-subscription': 'antigravity-subscription',
  'antigravity-subscription-directsdk': 'antigravity-subscription',
  antigravity: 'antigravity-subscription'
}

/** Best-effort map from a model slug (`host.model`) to one of the quota
 * providers. Provider-prefixed slugs ("opencode-go/…") resolve exactly; bare
 * slugs use a small keyword table. Unmapped → null; the chip then falls back
 * to the tightest 5h window across all providers, label included. */
function providerForModel(slug) {
  const text = String(slug || '').toLowerCase().trim()

  if (!text) {
    return null
  }

  const prefix = text.includes('/') ? text.split('/')[0] : null

  if (prefix && PROVIDER_ALIASES[prefix]) {
    return PROVIDER_ALIASES[prefix]
  }

  if (text.includes('commandcode') || text.includes('goat')) {
    return 'commandcode'
  }

  if (/deepseek|kimi|qwen|glm|minimax|grok|hy3/.test(text)) {
    return 'opencode-go'
  }

  if (/luna|sol|terra|codex|gpt-5|(^|\/)o[34]/.test(text)) {
    return 'openai-codex'
  }

  if (/^claude-(opus|sonnet|haiku|fable)/.test(text)) {
    return 'claude-subscription'
  }

  return null
}

/** The most-used 5h window of a provider (worst case across its accounts);
 * falls back to any window when none is labelled 5h. */
function fiveHourWindow(provider) {
  if (!provider) {
    return null
  }

  const windows = (provider.accounts || []).flatMap(account => account.windows || [])
  const fives = windows.filter(w => /5h|session/i.test(String(w.k || '')))
  const pool = fives.length ? fives : windows

  return pool.length ? pool.reduce((worst, w) => (w.pct > worst.pct ? w : worst)) : null
}

function formatPct(pct) {
  const value = Math.round(Number(pct) * 10) / 10

  return Number.isInteger(value) ? String(value) : value.toFixed(1)
}

function QuotaChip() {
  const open = useValue($paneOpen)
  const focusedRuntimeId = useValue(host.state.focusedSessionId)
  const mainModel = useValue(host.state.model)
  const gatewayReady = useValue(host.state.gateway) === 'open'

  // Follow the FOCUSED chat, not the primary-only globals: the same
  // `model.options` read the composer menu uses — the live agent owns its
  // provider/model, so the chip tracks the user between tiles. Drafts and
  // unspawned sessions answer with ''; the main model covers those.
  const focusQuery = useQuery({
    queryKey: ['quota-dash', 'focused', focusedRuntimeId, mainModel],
    enabled: Boolean(focusedRuntimeId) && gatewayReady,
    staleTime: 30_000,
    retry: 1,
    queryFn: async () => {
      const res = await host.request('model.options', {
        session_id: focusedRuntimeId,
        explicit_only: true
      })

      return { model: String((res && res.model) || ''), provider: String((res && res.provider) || '') }
    }
  })

  const focused = focusQuery.data || null
  const model = (focused && focused.model) || mainModel || ''

  const query = useQuery({
    queryKey: QUERY_KEY,
    queryFn: () => fetchQuotas(),
    enabled: gatewayReady,
    refetchInterval: q => (q.state.error ? 15_000 : 60_000),
    staleTime: 30_000,
    retry: 3,
    retryDelay: attempt => Math.min(1_000 * 2 ** attempt, 10_000)
  })

  const prefs = useValue($prefs)
  const providers = ((query.data && query.data.providers) || []).filter(provider => prefs.sections[provider.id] !== false)
  const providerId =
    PROVIDER_ALIASES[String((focused && focused.provider) || '').toLowerCase()] || providerForModel(model)
  let active = providers.find(provider => provider.id === providerId) || null
  let five = active ? fiveHourWindow(active) : null

  if (!five) {
    active = null

    for (const provider of providers) {
      const candidate = fiveHourWindow(provider)

      if (candidate && (!five || candidate.pct > five.pct)) {
        five = candidate
        active = provider
      }
    }
  }

  const label = (active && PROVIDER_LABELS[active.id]) || 'quota'
  const value = five ? `${five.k} ${formatPct(five.pct)}%` : null
  const icon = CHIP_ICON
  const tooltip = [
    open ? 'Quota Dashboard — click to close' : 'Quota Dashboard — click to open',
    model ? `session: ${model}` : null,
    active ? `${active.name}${value ? ` · ${value}` : ''}` : null
  ]
    .filter(Boolean)
    .join(' · ')

  if (!prefs.chip) {
    return null
  }

  return jsx(Tip, {
    label: tooltip,
    children: jsxs('button', {
      type: 'button',
      // Match the app's standard statusbar action chrome (STATUSBAR_ACTION_CLASS):
      // one muted tone with hover states — the accent glow is the update pill's
      // colour, not a readout's.
      className: cn(
        'inline-flex h-full items-center gap-1 rounded-none px-1.5 text-[0.6875rem] text-(--ui-text-tertiary) transition-colors hover:bg-(--chrome-action-hover) hover:text-foreground'
      ),
      onClick: togglePane,
      children: [
        jsx(Codicon, { name: icon, size: '0.75rem' }),
        jsx('span', { className: 'tabular-nums', children: value ? `${label} ${value}` : label })
      ]
    })
  })
}

function QuotaPage() {
  return jsx('div', {
    className: 'h-full overflow-y-auto',
    children: jsx('div', {
      className: 'mx-auto flex w-full max-w-3xl flex-col p-4',
      children: jsx(QuotaPane, {})
    })
  })
}

export default {
  id: ID,
  name: 'Provider Quota Dashboard',
  register(ctx) {
    paneCtx = ctx
    prefsStorage = ctx.storage
    $prefs.set(normalizePrefs(ctx.storage.get(PREFS_KEY, DEFAULT_PREFS)))
    paneDisposer = ctx.register(paneContribution())
    $paneOpen.set(true)

    ctx.registerMany([
      {
        id: 'chip',
        area: STATUSBAR_AREAS.right,
        order: 10,
        render: () => jsx(QuotaChip, {})
      },
      {
        id: 'page',
        area: ROUTES_AREA,
        data: { path: '/quota-dashboard' },
        render: () => jsx(QuotaPage, {})
      },
      {
        id: 'nav',
        area: SIDEBAR_NAV_AREA,
        data: { path: '/quota-dashboard', label: 'Quota Dashboard', codicon: 'dashboard' }
      }
    ])

    ctx.register({
      id: 'refresh',
      area: PALETTE_AREA,
      data: {
        id: 'quota-dash.refresh',
        label: 'Quota: refresh dashboard',
        keywords: ['quota', 'usage', 'limits', 'codex', 'opencode', 'commandcode', 'claude', 'antigravity'],
        run: () => {
          haptic('tap')
          void queryClient.invalidateQueries({ queryKey: QUERY_KEY })
        }
      }
    })

    ctx.register({
      id: 'open',
      area: PALETTE_AREA,
      data: {
        id: 'quota-dash.open',
        label: 'Quota: open in main workspace',
        keywords: ['quota', 'usage', 'limits', 'dashboard'],
        run: () => {
          if (typeof host.openWorkspace === 'function') {
            host.openWorkspace('quota-dash', {
              render: () => jsx(QuotaPane, {}),
              title: 'Quotas',
              minWidth: 280
            })
          } else {
            host.notify({ kind: 'info', message: 'The quota pane lives in the right panel (tab: quotas).' })
          }
        }
      }
    })

    ctx.register({
      id: 'hide',
      area: PALETTE_AREA,
      data: {
        id: 'quota-dash.hide',
        label: 'Quota: hide pane',
        keywords: ['quota', 'hide', 'close', 'pane'],
        run: () => {
          if (paneDisposer) {
            hidePane()
            host.notify({ kind: 'info', message: 'Quota pane hidden — restore from the statusbar "quota" chip or ⌘K "Quota: show pane".' })
          }
        }
      }
    })

    ctx.register({
      id: 'show',
      area: PALETTE_AREA,
      data: {
        id: 'quota-dash.show',
        label: 'Quota: show pane',
        keywords: ['quota', 'show', 'open', 'pane'],
        run: () => {
          showPane()
          host.notify({ kind: 'info', message: 'Quota pane restored.' })
        }
      }
    })

    ctx.register({
      id: 'toggle-chip',
      area: PALETTE_AREA,
      data: {
        id: 'quota-dash.toggle-chip',
        label: 'Quota: toggle statusbar chip',
        keywords: ['quota', 'chip', 'statusbar', 'settings'],
        run: () => setPrefs(current => ({ ...current, chip: !current.chip }))
      }
    })

    for (const sectionId of [...PROVIDER_IDS, ...ROUTER_IDS]) {
      ctx.register({
        id: `toggle-${sectionId}`,
        area: PALETTE_AREA,
        data: {
          id: `quota-dash.toggle.${sectionId}`,
          label: `Quota: show/hide ${SECTION_LABELS[sectionId]}`,
          keywords: ['quota', 'settings', 'show', 'hide', sectionId],
          run: () => {
            const next = !($prefs.get().sections[sectionId] !== false)
            setPrefs(current => ({ ...current, sections: { ...current.sections, [sectionId]: next } }))
            host.notify({ kind: 'info', message: `${SECTION_LABELS[sectionId]} ${next ? 'shown' : 'hidden'} in Quota Dashboard.` })
          }
        }
      })
    }

    ctx.register({
      id: 'page-open',
      area: PALETTE_AREA,
      data: {
        id: 'quota-dash.page',
        label: 'Quota: open as page',
        keywords: ['quota', 'page', 'dashboard', 'open'],
        run: () => host.navigate('/quota-dashboard')
      }
    })
  }
}
