#!/usr/bin/env python3
"""quota-dash probe - read-only quota readouts for Hermes credential pools.

Enumerates every credential for openai-codex, opencode-go, and commandcode
(Hermes credential pool rows + ~/.hermes/.env fallbacks), probes each
provider's usage API in parallel, and prints exactly one machine-readable
line:

    @@QUOTA@@ {json}

Safety invariants:
  * read-only: no token refresh, no pool mutation, no reset-credit redemption
  * never prints tokens or full account ids (short SHA-256 fingerprints only)
  * one row per unique credential, so multi-key pools show every key

Run standalone:  python3 probe.py
Overridable via env: HERMES_HOME, HERMES_AGENT_REPO.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

HOME = os.path.expanduser("~")
HERMES_HOME = os.environ.get("HERMES_HOME") or os.path.join(HOME, ".hermes")
REPO = os.environ.get("HERMES_AGENT_REPO") or os.path.join(HERMES_HOME, "hermes-agent")
if REPO not in sys.path:
    sys.path.insert(0, REPO)

import httpx  # noqa: E402  (provided by the Hermes venv)

SENTINEL = "@@QUOTA@@"
TIMEOUT = 15.0

CODEX_DEFAULT_BASE = "https://chatgpt.com/backend-api/codex"
OPENCODE_DEFAULT_BASE = "https://opencode.ai/zen/go/v1"
COMMANDCODE_DEFAULT_BASE = "https://api.commandcode.ai"

COMMANDCODE_PLANS = {
    "individual-go": ("Go", 10),
    "individual-goat": ("GOAT", 70),
    "individual-pro": ("Pro", 30),
    "individual-pro-v1": ("Pro", 80),
    "individual-provider": ("Provider", 15),
    "individual-max": ("Max", 150),
    "individual-ultra": ("Ultra", 300),
    "teams-pro": ("Teams Pro", 40),
}


def sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8", "replace")).hexdigest()


def env_map() -> dict:
    out: dict[str, str] = {}
    try:
        with open(os.path.join(HERMES_HOME, ".env"), encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                out[key.strip()] = value.strip().strip('"').strip("'")
    except OSError:
        pass
    return out


ENV = env_map()


def resolve_ref(value: str) -> str:
    """Resolve an `env:NAME` pool reference; plain tokens pass through."""
    value = str(value or "").strip()
    if value.startswith("env:"):
        name = value[4:]
        return os.environ.get(name) or ENV.get(name) or ""
    return value


def pool_rows(provider: str) -> list:
    """(label, token, base_url) for every pool row; read-only, never refreshes."""
    rows: list = []
    try:
        from agent.credential_pool import PooledCredential, read_credential_pool

        for index, raw_item in enumerate(read_credential_pool(provider) or [], 1):
            try:
                raw = json.loads(raw_item) if isinstance(raw_item, str) else dict(raw_item)
            except Exception:
                continue
            try:
                entry = PooledCredential.from_dict(provider, raw)
            except Exception:
                entry = None
            token = resolve_ref(
                getattr(entry, "runtime_api_key", "")
                or raw.get("api_key", "")
                or raw.get("access_token", "")
            )
            base = str(getattr(entry, "runtime_base_url", "") or raw.get("base_url") or "")
            label = str(getattr(entry, "label", "") or raw.get("label") or f"{provider}-{index}")
            if token:
                rows.append((label, token, base))
    except Exception:
        pass
    return rows


def accounts_for(provider: str, env_var: str, default_base: str) -> list:
    """Every unique credential for a provider: pool rows first, then the env var."""
    seen: set = set()
    accounts: list = []

    def add(label: str, token: str, base: str) -> None:
        token = resolve_ref(token)
        if not token:
            return
        fp = sha(token)[:12]
        if fp in seen:
            return
        seen.add(fp)
        accounts.append({"label": label, "token": token, "base": base or default_base, "fp": fp})

    for label, token, base in pool_rows(provider):
        add(label, token, base)
    if env_var:
        env_token = os.environ.get(env_var) or ENV.get(env_var) or ""
        if env_token:
            add(f"{env_var} (env)", env_token, default_base)
    return accounts


def jwt_claims(token: str) -> dict:
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(payload))
    except Exception:
        return {}


def local_iso(value) -> str | None:
    """epoch seconds / epoch ms / ISO string -> local ISO, minute precision."""
    try:
        if value is None:
            return None
        if isinstance(value, (int, float)):
            seconds = value / 1000 if value > 10**11 else value
            dt = datetime.fromtimestamp(seconds, tz=timezone.utc)
        else:
            text = str(value)
            dt = datetime.fromisoformat(text[:-1] + "+00:00" if text.endswith("Z") else text)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone().isoformat(timespec="minutes")
    except Exception:
        return None


def probe_codex(account: dict) -> dict:
    token = account["token"]
    base = (account["base"] or CODEX_DEFAULT_BASE).rstrip("/")
    root = base.removesuffix("/codex")
    prefix = root + ("/wham" if "/backend-api" in root else "/api/codex")
    claims = jwt_claims(token)
    acct = (claims.get("https://api.openai.com/auth") or {}).get("chatgpt_account_id")
    exp = claims.get("exp")

    bits = [f"fp {sha(token)[:6]}"]
    if acct:
        bits.append(f"acct {sha(str(acct))[:6]}")
    if exp:
        bits.append("token exp " + datetime.fromtimestamp(exp).strftime("%d %b %H:%M"))

    row = {"label": account["label"], "sub": " - ".join(bits), "plan": None,
           "acct": sha(str(acct))[:6] if acct else None,
           "windows": [], "notes": [], "error": None}
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json", "User-Agent": "codex-cli"}
    if acct:
        headers["ChatGPT-Account-Id"] = str(acct)
    try:
        response = httpx.get(prefix + "/usage", headers=headers, timeout=TIMEOUT)
        if response.status_code == 200:
            data = response.json() or {}
            plan = str(data.get("plan_type") or "").strip()
            row["plan"] = plan.title() if plan else None
            limits = data.get("rate_limit") or {}
            for key, label in (("primary_window", "session (5h)"), ("secondary_window", "weekly")):
                window = limits.get(key) or {}
                used = window.get("used_percent")
                if used is not None:
                    row["windows"].append({"k": label, "pct": float(used),
                                           "reset": local_iso(window.get("reset_at"))})
            resets = (data.get("rate_limit_reset_credits") or {}).get("available_count")
            if isinstance(resets, (int, float)) and resets > 0:
                row["notes"].append(f"{int(resets)} banked reset (redeem: /usage reset)")
            credits = data.get("credits") or {}
            if credits.get("has_credits"):
                balance = credits.get("balance")
                if credits.get("unlimited"):
                    row["notes"].append("credits: unlimited")
                elif isinstance(balance, (int, float)):
                    row["notes"].append(f"credits: ${float(balance):.2f}")
                else:
                    row["notes"].append("credits available")
        elif response.status_code in (401, 403):
            row["error"] = f"HTTP {response.status_code} - token expired; use this account once (or re-auth) to refresh"
        else:
            row["error"] = f"HTTP {response.status_code}"
    except Exception as exc:
        row["error"] = f"{type(exc).__name__}: {str(exc)[:80]}"
    return row


def probe_opencode(account: dict) -> dict:
    token = account["token"]
    base = (account["base"] or OPENCODE_DEFAULT_BASE).rstrip("/")
    row = {"label": account["label"], "sub": f"fp {sha(token)[:6]}", "plan": None,
           "windows": [], "notes": [], "error": None}
    try:
        response = httpx.get(base + "/usage",
                             headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
                             timeout=TIMEOUT)
        if response.status_code == 200:
            usage = (response.json() or {}).get("usage") or {}
            for key, label in (("rolling", "5h"), ("weekly", "weekly"), ("monthly", "monthly")):
                window = usage.get(key) or {}
                percent = window.get("percent")
                if percent is not None:
                    row["windows"].append({"k": label, "pct": float(percent),
                                           "reset": local_iso(window.get("resetsAt"))})
                status = str(window.get("status") or "").strip()
                if status and status.lower() != "ok":
                    row["notes"].append(f"{label}: {status}")
        elif response.status_code in (401, 403):
            row["error"] = f"HTTP {response.status_code} - API key rejected"
        else:
            row["error"] = f"HTTP {response.status_code}"
    except Exception as exc:
        row["error"] = f"{type(exc).__name__}: {str(exc)[:80]}"
    return row


def probe_commandcode(account: dict) -> dict:
    token = account["token"]
    base = (account["base"] or COMMANDCODE_DEFAULT_BASE).rstrip("/")
    row = {"label": account["label"], "sub": f"fp {sha(token)[:6]}", "plan": None,
           "windows": [], "notes": [], "error": None}
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}

    def get(path: str) -> dict:
        response = httpx.get(base + path, headers=headers, timeout=TIMEOUT)
        if response.status_code != 200:
            raise RuntimeError(f"{path} HTTP {response.status_code}")
        return response.json() or {}

    try:
        whoami = get("/alpha/whoami")
        credits = get("/alpha/billing/credits")
        subscription = get("/alpha/billing/subscriptions")
        summary = get("/alpha/usage/summary")

        user = whoami.get("user") or {}
        who = " - ".join(p for p in (user.get("userName") or user.get("name"), user.get("email")) if p)
        if who:
            row["sub"] = f"{who} - fp {sha(token)[:6]}"

        sub = subscription.get("data") or {}
        plan_id = str(sub.get("planId") or "")
        label, total = COMMANDCODE_PLANS.get(plan_id, (plan_id or None, None))
        row["plan"] = label

        windows = credits.get("windowLimits") or {}
        for key, wlabel in (("fiveHour", "5h"), ("weekly", "weekly")):
            window = windows.get(key) or {}
            cap = window.get("cap")
            used = window.get("used") or 0
            if isinstance(cap, (int, float)) and float(cap) > 0:
                row["windows"].append({
                    "k": wlabel,
                    "pct": round((float(used) / float(cap)) * 100, 1),
                    "reset": local_iso(window.get("resetAt")),
                    "note": f"${float(used):.2f} / ${float(cap):.0f}",
                })

        balances = credits.get("credits") or {}
        monthly = balances.get("monthlyCredits")
        if isinstance(monthly, (int, float)):
            suffix = f" of ${total}" if isinstance(total, (int, float)) else ""
            row["notes"].append(f"credit balance: ${float(monthly):.2f}{suffix} (this cycle)")
        for key, note_label in (("purchasedCredits", "purchased"), ("freeCredits", "free")):
            value = balances.get(key)
            if isinstance(value, (int, float)) and float(value) > 0:
                row["notes"].append(f"{note_label}: ${float(value):.2f}")
        period_end = str(sub.get("currentPeriodEnd") or "")
        if period_end:
            row["notes"].append("renews " + period_end[:10])
        if summary.get("totalCount") is not None:
            row["notes"].append(
                "this period: {count} requests - ${cost:.2f} - {success}% ok - {tokens:.1f}M tokens".format(
                    count=int(summary.get("totalCount") or 0),
                    cost=float(summary.get("totalCost") or 0),
                    success=summary.get("successRate"),
                    tokens=float(summary.get("totalTokens") or 0) / 1e6,
                )
            )
    except Exception as exc:
        row["error"] = str(exc)[:120]
    return row


PROVIDERS = (
    {"id": "openai-codex", "name": "Codex (ChatGPT)", "env_var": "", "base": CODEX_DEFAULT_BASE,
     "probe": probe_codex},
    {"id": "opencode-go", "name": "OpenCode Go", "env_var": "OPENCODE_GO_API_KEY",
     "base": OPENCODE_DEFAULT_BASE, "probe": probe_opencode},
    {"id": "commandcode", "name": "CommandCode", "env_var": "COMMANDCODE_API_KEY",
     "base": COMMANDCODE_DEFAULT_BASE, "probe": probe_commandcode},
)


def main() -> int:
    started = time.time()
    jobs = []
    per_provider = {}
    for provider in PROVIDERS:
        accounts = accounts_for(provider["id"], provider["env_var"], provider["base"])
        per_provider[provider["id"]] = accounts
        for account in accounts:
            jobs.append((provider, account))

    results = {}
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = {pool.submit(provider["probe"], account): (provider["id"], account["fp"])
                   for provider, account in jobs}
        for future, key in futures.items():
            try:
                results[key] = future.result()
            except Exception as exc:  # defensive: probe fns guard, this is a backstop
                results[key] = {"label": "?", "sub": "", "plan": None, "windows": [],
                                "notes": [], "error": f"{type(exc).__name__}: {exc}"}

    payload = {
        "fetchedAt": datetime.now().astimezone().isoformat(timespec="seconds"),
        "elapsedMs": int((time.time() - started) * 1000),
        "providers": [
            {
                "id": provider["id"],
                "name": provider["name"],
                "accounts": [results[(provider["id"], account["fp"])]
                             for account in per_provider[provider["id"]]],
            }
            for provider in PROVIDERS
        ],
    }
    print(SENTINEL + " " + json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    sys.exit(main())
