# Hermes Provider Quota Dashboard

**A [Hermes Desktop](https://hermes-agent.nousresearch.com/docs/developer-guide/desktop-plugin-sdk) plugin**
(`himanusia/hermes-provider-quota-dashboard`) that puts a live quota dashboard in
a side pane: subscription limits for **OpenAI Codex**, **OpenCode Go**, and
**CommandCode** — for *every* account in your Hermes credential pools, not just
the one currently selected.

Built with the Hermes Desktop Plugin SDK (`@hermes/plugin-sdk`) — plain ESM, no
build step. Drop it in the desktop plugin door and the app hot-loads it.

![Quota Dashboard pane in Hermes Desktop](docs/screenshot.png)

*The pane in a live Hermes Desktop session — the Codex card folds the pool's
credentials that share one account into a single readout, next to OpenCode Go
and CommandCode, each with its own refresh. (Account email redacted in this
screenshot.)*

## What it shows

| Provider | Per-account readout |
|---|---|
| **OpenAI Codex** | every pool credential: plan, 5h session + weekly windows (% used, reset time), banked resets, credit balance, token expiry, and short credential/account fingerprints. Credentials that resolve to the same account share one quota, so they collapse into a single card listing the credentials behind it |
| **OpenCode Go** | rolling (5h) / weekly / monthly usage % of each window's cap, with reset times |
| **CommandCode** | plan (GOAT / Pro / Max / Go / Teams), 5h + weekly rate-limit windows ($ used / cap) and the monthly cycle budget (spend / plan total, reset at cycle end), remaining credit balance, cycle end date, and the current billing period's request/token totals |
| **Claude subscription** | Claude Pro/Max via Claude Code's own login (what `claude-subscription-directsdk-experimental` drives): plan, 5h session + weekly windows (% used, reset time), per-model weekly caps when the plan has them, extra-usage monthly spend when enabled, and the weekly surface mix |
| **Antigravity subscription** | Google AI Pro/Ultra via agy's own login (what `antigravity-subscription-directsdk` drives): plan, and both quota groups (Gemini; Claude + GPT-OSS), each with its own 5h and weekly window (% used, reset time) |
| **OmniRoute** | local SQLite metadata and local usage history: configured route aliases (including `coding-safe`), stored model order, local request/token totals, and configured-connection counts |
| **9router** | local SQLite metadata and local usage history: observed models, local request/token totals, and configured-connection counts; the local database currently has no combo rows |

Ways to open it: a **status-bar chip** that follows the FOCUSED chat — it reads
the focused session's live provider/model (the same `model.options` read the
composer menu uses) and shows that provider's 5h usage (e.g. `OpenCode Go 5h 6%`;
a Codex-backed chat shows `Codex session (5h) 0%`) — and toggles the pane open /
closed (highlighted while the pane is open); a
**Quota Dashboard** row in
the left sidebar (opens the full-page view), and ⌘K palette commands:
**Quota: refresh dashboard**, **Quota: open in main workspace**,
**Quota: open as page**, **Quota: hide pane**, **Quota: show pane**,
**Quota: show/hide &lt;provider&gt;**, **Quota: toggle statusbar chip**.

Choose what it shows with **⚙** in the pane header: one switch per provider
and local router, plus the statusbar chip, plan badges, and account notes.
Choices persist per profile; a hidden provider or router is not probed at all.

Refresh at three granularities: **Refresh all** in the pane header, a **↻ button
per provider**, and a **↻ button on every account card**. A per-provider or
per-account refresh only re-probes that slice (`probe.py --provider <id>` /
`probe.py --account <provider>:<fp>`), then merges the fresh numbers into the
view — so you can re-check one Codex key without re-hitting the others. On a
merged account card (several credentials sharing one account), ↻ re-probes
every credential behind it.

## Install

Requires [Hermes Desktop](https://github.com/NousResearch/hermes-agent) with
the desktop plugin door (`~/.hermes/desktop-plugins/`).

```bash
git clone https://github.com/himanusia/hermes-provider-quota-dashboard.git
cd hermes-provider-quota-dashboard
./install.sh
```

`install.sh` copies `plugin.js` + `probe.py` into
`~/.hermes/desktop-plugins/quota-dash/`. The app watches that folder and loads
the plugin within seconds; if the pane does not appear, run
**⌘K → Reload desktop plugins**. The pane (`quotas`) docks into the right
panel — drag it wherever you like afterwards.

Manual install is the same two files:

```bash
mkdir -p ~/.hermes/desktop-plugins/quota-dash
cp plugin.js probe.py ~/.hermes/desktop-plugins/quota-dash/
```

Verify the probe standalone (uses the Hermes venv Python):

```bash
~/.hermes/hermes-agent/venv/bin/python \
  ~/.hermes/desktop-plugins/quota-dash/probe.py
```

## How it works

```
plugin.js (desktop pane)
   └─ host.request('shell.exec')            ← gateway JSON-RPC
        └─ probe.py  (backend host)         ← read-only probe
             ├─ Hermes credential pool      (agent.credential_pool)
             ├─ ~/.hermes/.env fallbacks
             └─ provider usage APIs         → @@QUOTA@@ {json}
```

`probe.py` enumerates every credential for each provider (pool rows first,
then `.env` fallbacks), probes the provider APIs in parallel, and prints a
single `@@QUOTA@@ {json}` line. `plugin.js` fetches that line through the
gateway's `shell.exec` RPC, parses it, and renders the bars — refreshed every
60 s and on demand.

### Local router sections

The OmniRoute and 9router sections use **only read-only local SQLite files**:

| Router | Local source | Read-only tables |
|---|---|---|
| **OmniRoute** | `~/.omniroute/storage.sqlite` | `usage_history`, `call_logs`, `combos`, `model_combo_mappings`, `provider_connections`, and cached `quota_snapshots` |
| **9router** | `~/.9router/db/data.sqlite` | `usageHistory`, `usageDaily`, `combos`, `providerConnections`, and `settings` |

The probe opens these databases with SQLite `mode=ro` plus a connection-local
`query_only` guard. It never invokes `omniroute`/`9router`, sends an HTTP request
to either router, tests a configured provider, refreshes a credential, or
changes router state. Missing or incompatible databases render as
**unavailable/configured** with no invented metrics.

Local request/token totals and the optional local cost ledger are router
history, not upstream subscription quota. The main cards visualize that history
as compact request/token tiles and route-mix bars; cached OmniRoute snapshots
are shown as used-quota meters (converted from the stored remaining%).
Last local activity and database-check time are
shown separately. Route explanations and source metadata stay collapsed by
default. Expanded details include exact snapshot/reset metadata. Those values are
read from local storage, not refreshed by the dashboard. The current 9router
schema has no cached upstream quota snapshot table, so it shows local request
history only and a no-cache state rather than invented quota.

For the installed local state, OmniRoute's `coding-safe` alias is intended for
coding requests (the name is not a sandbox or safety guarantee) and uses a
priority route with this model order: `command-code/deepseek/deepseek-v4.1-flash`
→ `opencode-go/deepseek-v4.1-flash` → `codex/gpt-5.6-luna`. Later entries are
fallback candidates in the stored order; this is a local routing preference,
not proof of provider availability or quota. Other aliases are explained from
stored metadata when present; an alias observed only in local logs (for example
`auto/fast`) is explicitly marked as observed rather than assigned an inferred
candidate list. The current 9router database has no configured combo rows, so
its section shows observed models only.

To verify the router path without querying any external provider usage API:

```bash
~/.hermes/hermes-agent/venv/bin/python probe.py \
  --router omniroute --router 9router
```

The `--router` mode intentionally emits no provider rows and is used by the
hermetic tests. The desktop transport fetches providers and routers separately;
OmniRoute model history is paged in 8-row chunks. This keeps each sentinel
response under the gateway `shell.exec` 4 KB output cap, then reassembles the
full router details before rendering.

### Data endpoints

| Provider | Endpoint | Credential |
|---|---|---|
| Codex | `chatgpt.com/backend-api/wham/usage` (or `/api/codex/usage`) | ChatGPT OAuth token per pool entry |
| OpenCode Go | `https://opencode.ai/zen/go/v1/usage` | `OPENCODE_GO_API_KEY` |
| CommandCode | `https://api.commandcode.ai/alpha/{whoami,billing/credits,billing/subscriptions,usage/summary}` | `COMMANDCODE_API_KEY` |
| Antigravity subscription | `https://daily-cloudcode-pa.googleapis.com/v1internal:{loadCodeAssist,retrieveUserQuotaSummary}` (the host agy itself reads; the plain `cloudcode-pa` host answers 200 but reports stale zeros for the Claude/GPT group, so it is only a fallback) (`groups[].buckets[]`: `window` 5h/weekly, `remainingFraction`) | agy's OAuth login: macOS Keychain item `gemini` / `antigravity`, else the token file under `ANTIGRAVITY_CONFIG_DIR` / `~/.gemini/antigravity-cli` (row `default`), plus each rotation account in `~/.hermes/antigravity-accounts.json` (row named by its label). The account the provider recorded as `serving` is the active row |
| Claude subscription | `https://api.anthropic.com/api/oauth/usage` (`anthropic-beta: oauth-2025-04-20`) | Claude Code's OAuth login: `CLAUDE_CODE_OAUTH_TOKEN`, else the macOS Keychain item `Claude Code-credentials`, else `.credentials.json` under `CLAUDE_SUBSCRIPTION_DIRECTSDK_CONFIG_DIR` / `CLAUDE_CONFIG_DIR` / `~/.claude` |

## Adding another provider

Pull requests adding providers are welcome — the seam is one function.

1. Write `probe_<id>(account) -> row` in `probe.py` next to the others.
   `account` is `{"label", "token", "base", "fp"}` (pool rows first, `.env`
   fallbacks second); return the row contract below.
2. Append one entry to `PROVIDERS` (id, display name, env-var fallback,
   default base URL, your probe function).
3. Test standalone: `python3 probe.py --provider <id>` — expect a single
   `@@QUOTA@@ {json}` line containing your provider's rows.

Row contract (only `label` is required; `normalize_row` fills the rest):

```python
{
  "label": "credential label",            # card title
  "sub":   "fp a1b2c3 - extra detail",    # muted detail line
  "plan":  "Plus",                        # badge, or None
  "acct":  "a1b2c3",                      # account-id hash — rows with the
                                          #   same value merge into one card
  "windows": [                            # one labelled bar each
    {"k": "5h", "pct": 42.0, "reset": "2026-09-12T23:26+07:00", "note": "$2 / $14"}
  ],
  "notes": ["any extra fact"],            # small muted lines
  "error": None                           # string -> warning line
}
```

The desktop pane renders whatever the probe returns — no UI changes needed.

Rules: probes are read-only (never refresh tokens, never mutate the pool,
never redeem credits), never print secrets (short SHA-256 fingerprints only),
one row per credential — the UI collapses rows that share an `acct`.

## Security & safety

- **Read-only by design.** The probe never refreshes tokens, never mutates
  credential pools, never redeems reset credits, and never re-authenticates.
  An expired token shows an `HTTP 401` hint instead of a silent refresh.
- **Secrets never leave the host.** The pane receives numbers, account
  handles, and short SHA-256 fingerprints only — no tokens, no full account
  ids, no email addresses (an email-only account shows a masked fallback).
- **No credentials in this repo.** Keys are read at runtime from the Hermes
  credential pool / `~/.hermes/.env` on your machine.

## Verification

The repository includes a hermetic local-router harness. It creates temporary
SQLite fixtures, asserts the `mode=ro` probe leaves them unchanged, checks
missing-database behavior, and proves `--router` mode does not invoke provider
probes:

```bash
~/.hermes/hermes-agent/venv/bin/python -m unittest -v test_probe.py
node --check plugin.js
~/.hermes/hermes-agent/venv/bin/python -m py_compile probe.py
```

## Status

End-to-end verified **2026-09-12** on macOS (Hermes Desktop, local backend):
every Codex pool entry, OpenCode Go's windows, and CommandCode's GOAT plan all
returned and rendered in the pane. It is a dated check, not a permanent claim —
re-verify anytime with the standalone probe above.

## Limitations

- Codex access tokens expire; this tool reports the 401 rather than refreshing
  (use the account, or re-auth, then refresh the pane).
- CommandCode support uses the same `/alpha/…` endpoints its own CLI uses for
  `/usage` — undocumented, and may change without notice.
- Claude subscription usage comes from `/api/oauth/usage`, the endpoint Claude
  Code's own `/usage` reads — undocumented. The probe never refreshes the OAuth
  token: an expired one is reported, and the next Claude turn refreshes it.
- That endpoint rate-limits hard (HTTP 429). Claude and Antigravity readings are
  therefore shared across the pane, the chip, and the TUI line through a
  numbers-only snapshot in `~/.hermes/cache/quota-dash/` (no tokens): at most
  one request per account every 180 s (`HERMES_QUOTA_MIN_INTERVAL`). If a
  request fails, the last good reading is shown with a `last reading HH:MM
  (HTTP 429)` note instead of an empty card.
- Antigravity quota uses the Cloud Code `v1internal` endpoints agy itself
  calls — undocumented. Models share limits per group (Gemini; Claude +
  GPT-OSS), and each group has a 5h and a weekly bucket. The statusbar chip
  meters the group the focused chat's model belongs to.
- Anthropic's usage endpoint publishes no rate-limit headers (no
  `ratelimit-*`, and `retry-after: 0` even on 429), so the 180 s interval is an
  observed safe value, not a documented limit.
- CommandCode publishes no monthly *rate-limit* window: `windowLimits` carries
  only the 5h and weekly caps. The monthly figure is a **$ credit budget**
  spread over three endpoints — the plan total from
  `billing/subscriptions`, spend from `usage/summary` (`periodBasis:
  "billing-period"`), and the remaining balance from `billing/credits` — so the
  probe synthesizes one `monthly` window (`spend / plan total`, reset = cycle
  end). An unrecognized `planId` has no cap, so that account keeps its balance
  note and no monthly bar rather than a made-up percentage.
- OpenCode Go percentages are the provider's own usage % of each window cap
  (5-hour = 20% of the monthly limit, weekly = 50%; see opencode.ai/docs/go).
  Numbers are shown exactly as returned; nothing is recomputed locally.
- The status-bar chip derives its provider from the session's model slug:
  provider-prefixed slugs (`opencode-go/…`) resolve exactly; bare slugs use a
  small keyword table (`deepseek/kimi/qwen/glm/… → OpenCode Go`,
  `luna/sol/terra/gpt-5… → Codex`, `…goat… → CommandCode`). Unmapped models
  fall back to the tightest 5h window across all providers — the provider tag
  still shows which one.
- The pane needs the backend host to have the Hermes venv
  (`~/.hermes/hermes-agent/venv`) and `~/.hermes/.env` in the usual place.
  Set `HERMES_PYTHON` / `HERMES_QUOTA_PROBE` / `HERMES_HOME` in the backend
  environment (or edit `PROBE_CMD` in `plugin.js`) if your install differs.

## License

MIT
