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
import { useState } from 'react'
import { jsx, jsxs } from 'react/jsx-runtime'

const ID = 'quota-dash'
const QUERY_KEY = ['quota-dash', 'quotas']
const SENTINEL = '@@QUOTA@@'

// Runs on the backend host. Edit the paths here if you installed elsewhere;
// `HERMES_PYTHON` / `HERMES_QUOTA_PROBE` env vars on the backend override them.
const PROBE_CMD =
  '/usr/bin/env -u PYTHONPATH "$HOME/.hermes/hermes-agent/venv/bin/python" "$HOME/.hermes/desktop-plugins/quota-dash/probe.py"'

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
    setBusyProviders(previous => ({ ...previous, [providerId]: true }))
    fetchQuotas({ provider: providerId })
      .then(payload => {
        const fresh = (payload.providers || [])[0]

        if (!fresh) {
          throw new Error(`no data for ${providerId}`)
        }

        queryClient.setQueryData(QUERY_KEY, previous => mergeProvider(previous, fresh))
      })
      .catch(failure)
      .finally(() => setBusyProviders(previous => ({ ...previous, [providerId]: false })))
  }

  const refreshAccount = (providerId, fp, label) => {
    haptic('tap')
    const key = `${providerId}:${fp}`
    setBusyAccounts(previous => ({ ...previous, [key]: true }))
    fetchQuotas({ account: key })
      .then(payload => {
        const fresh = (payload.providers || [])[0] && (payload.providers[0].accounts || [])[0]

        if (!fresh) {
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
                      account.fp === fresh.fp || account.label === fresh.label ? fresh : account
                    )
                  }
                : provider
            )
          }
        })
      })
      .catch(failure)
      .finally(() => setBusyAccounts(previous => ({ ...previous, [key]: false })))
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
                ),
                jsx('div', {
                  className: 'text-[0.65rem] text-(--ui-text-quaternary)',
                  children: 'read-only probe · ↻ per provider/account · secrets never leave the host'
                })
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

function QuotaChip() {
  const open = useValue($paneOpen)

  return jsx(Tip, {
    label: open ? 'Quota Dashboard — click to close the pane' : 'Quota Dashboard — click to open the pane',
    children: jsx('button', {
      type: 'button',
      className: cn(
        'inline-flex h-full items-center gap-1 px-1.5 text-[0.6875rem] transition-colors',
        open
          ? 'text-(--ui-accent)'
          : 'text-(--ui-text-tertiary) hover:bg-(--chrome-action-hover) hover:text-foreground'
      ),
      onClick: togglePane,
      children: 'quota'
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
