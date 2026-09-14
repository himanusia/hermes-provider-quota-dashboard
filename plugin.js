/**
 * Hermes Provider Quota Dashboard — Hermes Desktop plugin
 * https://github.com/himanusia/hermes-provider-quota-dashboard
 *
 * A desktop pane with live quota readouts for EVERY account of OpenAI Codex
 * (credential pool), OpenCode Go, and CommandCode — with refresh controls at
 * global, per-provider, and per-account granularity.
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

// Runs on the backend host. Backend env can override every path:
// HERMES_PYTHON (python), HERMES_QUOTA_PROBE (probe script), HERMES_HOME
// (default root); the defaults target a standard ~/.hermes install.
const PROBE_CMD =
  '/usr/bin/env -u PYTHONPATH "${HERMES_PYTHON:-$HOME/.hermes/hermes-agent/venv/bin/python}" "${HERMES_QUOTA_PROBE:-${HERMES_HOME:-$HOME/.hermes}/desktop-plugins/quota-dash/probe.py}"'

function buildProbeCommand({ provider, account } = {}) {
  let cmd = PROBE_CMD

  if (account && /^[a-z0-9-]+:[0-9a-f]{6,12}$/.test(account)) {
    cmd += ` --account ${account}`
  } else if (provider && /^[a-z0-9-]+$/.test(provider)) {
    cmd += ` --provider ${provider}`
  }

  return cmd
}

async function fetchQuotas(filters) {
  const result = await host.request('shell.exec', { command: buildProbeCommand(filters) })
  const stdout = String((result && result.stdout) || '')
  const marker = stdout.lastIndexOf(SENTINEL)

  if (marker < 0) {
    const stderr = String((result && result.stderr) || '')
      .trim()
      .split('\n')
      .slice(-3)
      .join(' ')
    throw new Error(stderr || `probe produced no data (exit ${result && result.code})`)
  }

  return JSON.parse(stdout.slice(marker + SENTINEL.length).trim().split('\n')[0])
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
  const notes = account.notes || []
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
          account.plan ? jsx(Badge, { variant: 'muted', size: 'xs', children: account.plan }) : null,
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

function QuotaPane() {
  const [busyProviders, setBusyProviders] = useState({})
  const [busyAccounts, setBusyAccounts] = useState({})
  // Per-scope refresh generations: a slow in-flight response must never
  // clobber a newer refresh's result (last click wins).
  const refreshSeq = useRef({})
  const beginRefresh = scope => {
    const token = (refreshSeq.current[scope] || 0) + 1
    refreshSeq.current[scope] = token
    return token
  }
  const isCurrent = (scope, token) => refreshSeq.current[scope] === token

  const query = useQuery({
    queryKey: QUERY_KEY,
    queryFn: () => fetchQuotas(),
    refetchInterval: 60_000,
    staleTime: 30_000,
    retry: 1
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

  const fetchedAt = query.data && query.data.fetchedAt ? new Date(query.data.fetchedAt) : null

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
                className: 'h-5 px-1 text-[0.7rem]',
                onClick: refreshAll,
                disabled: query.isFetching,
                children: query.isFetching ? jsx(GlyphSpinner, { className: 'size-2.5' }) : 'Refresh all'
              })
            ]
          })
        ]
      }),
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
                ...((query.data && query.data.providers) || []).map(provider =>
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
                )
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

const PROVIDER_LABELS = { 'openai-codex': 'Codex', 'opencode-go': 'OpenCode Go', commandcode: 'CommandCode' }
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
  'commandcode-anthropic': 'commandcode'
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

  return null
}

/** The most-used 5h window of a provider (worst case across its accounts);
 * falls back to any window when none is labelled 5h. */
function fiveHourWindow(provider) {
  if (!provider) {
    return null
  }

  const windows = (provider.accounts || []).flatMap(account => account.windows || [])
  const fives = windows.filter(w => String(w.k || '').toLowerCase().includes('5h'))
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

  // Follow the FOCUSED chat, not the primary-only globals: the same
  // `model.options` read the composer menu uses — the live agent owns its
  // provider/model, so the chip tracks the user between tiles. Drafts and
  // unspawned sessions answer with ''; the main model covers those.
  const focusQuery = useQuery({
    queryKey: ['quota-dash', 'focused', focusedRuntimeId, mainModel],
    enabled: Boolean(focusedRuntimeId),
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
    refetchInterval: 60_000,
    staleTime: 30_000,
    retry: 1
  })

  const providers = (query.data && query.data.providers) || []
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
        keywords: ['quota', 'usage', 'limits', 'codex', 'opencode', 'commandcode'],
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
