# quota-dash

Read-only quota dashboard for **Hermes Desktop**: live subscription/limit
readouts for **OpenAI Codex** (every account in the credential pool),
**OpenCode Go**, and **CommandCode** (GOAT / Pro / Max / Go / Teams) in a
single desktop pane.

> Work in progress — full README lands with the first verified release.

## How it works (short version)

- `probe.py` — a read-only Python probe. It enumerates the Hermes credential
  pools (and `.env` fallbacks) per provider, hits each provider's usage API,
  and prints one machine-readable JSON line. It never prints tokens, never
  refreshes credentials, and never redeems reset credits.
- `plugin.js` — a Hermes Desktop plugin (plain ESM, no build step). A pane
  fetches `probe.py`'s JSON through the gateway's `shell.exec` RPC and renders
  per-account bars with reset countdowns.
- `install.sh` — copies both files into
  `~/.hermes/desktop-plugins/quota-dash/`.

## License

MIT
