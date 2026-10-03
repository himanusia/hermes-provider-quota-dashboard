#!/usr/bin/env python3
"""quota-dash probe - read-only quota readouts for Hermes credential pools.

Ships with openai-codex, opencode-go, and commandcode (Hermes credential pool
rows + ~/.hermes/.env fallbacks), probes each provider's usage API in
parallel, and prints exactly one machine-readable line:

    @@QUOTA@@ {json}

Safety invariants:
  * read-only: no token refresh, no pool mutation, no reset-credit redemption
  * never prints tokens or full account ids (short SHA-256 fingerprints only);
    account handles only - email addresses never leave the host (masked if
    they would be the only identifier)
  * one row per unique credential, so multi-key pools show every key

Run standalone:  python3 probe.py [--provider ID] [--account PROVIDER:FP]
Overridable via env: HERMES_HOME, HERMES_AGENT_REPO.

Adding another provider is a one-function change: write probe_<id>(account)
-> row (contract in ROW_CONTRACT below) and register it in PROVIDERS. The
desktop pane renders whatever the probe returns - no UI changes needed.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import sqlite3
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from urllib.parse import quote as urlquote

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

# Router sections intentionally inspect only local SQLite state. They never
# call the router HTTP API, provider endpoints, or a router CLI command.
OMNIROUTE_HOME = os.environ.get("OMNIROUTE_HOME") or os.path.join(HOME, ".omniroute")
OMNIROUTE_DB = os.environ.get("OMNIROUTE_DB") or os.path.join(OMNIROUTE_HOME, "storage.sqlite")
ROUTER9_HOME = (
    os.environ.get("NINEROUTER_HOME")
    or os.environ.get("ROUTER9_HOME")
    or os.path.join(HOME, ".9router")
)
ROUTER9_DB = os.environ.get("NINEROUTER_DB") or os.environ.get("ROUTER9_DB") or os.path.join(ROUTER9_HOME, "db", "data.sqlite")
ROUTER_ROUTE_LIMIT = 32
ROUTER_ROUTE_PAGE_MAX = 8

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


def display_path(path: str) -> str:
    """Return a user-facing path without exposing the absolute home path."""
    path = os.path.abspath(path)
    if path == HOME or path.startswith(HOME + os.sep):
        return "~" + path[len(HOME):]
    return path


def open_sqlite_readonly(path: str):
    """Open a SQLite file without allowing writes; returns (connection, error)."""
    if not os.path.isfile(path):
        return None, "database not found"

    try:
        uri = "file:" + urlquote(os.path.abspath(path), safe="/") + "?mode=ro"
        connection = sqlite3.connect(uri, uri=True, timeout=1.0)
        connection.row_factory = sqlite3.Row
        # URI mode=ro is the important boundary. This extra connection-local
        # guard also makes accidental writes fail loudly in future edits.
        connection.execute("PRAGMA query_only=ON")
        return connection, None
    except Exception as exc:
        return None, f"read-only SQLite open failed: {type(exc).__name__}: {str(exc)[:120]}"


def sqlite_table_exists(connection, name: str) -> bool:
    row = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1", (name,)
    ).fetchone()
    return row is not None


def sqlite_table_columns(connection, name: str) -> set[str]:
    safe_name = name.replace('"', '""')
    return {str(row[1]) for row in connection.execute(f'PRAGMA table_info("{safe_name}")')}


def sqlite_table_count(connection, name: str) -> int | None:
    if not sqlite_table_exists(connection, name):
        return None
    safe_name = name.replace('"', '""')
    try:
        return int(connection.execute(f'SELECT COUNT(*) FROM "{safe_name}"').fetchone()[0])
    except Exception:
        return None


def sqlite_schema_info(connection, names: tuple[str, ...]) -> list[dict]:
    return [
        {"name": name, "present": sqlite_table_exists(connection, name), "rows": sqlite_table_count(connection, name)}
        for name in names
    ]


def safe_text(value, limit: int = 160) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    return text[:limit] + ("…" if len(text) > limit else "")


def safe_int(value, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def safe_float(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def empty_local_usage(source_table: str | None = None, available: bool = False) -> dict:
    return {
        "available": available,
        "sourceTable": source_table,
        "requests": 0,
        "inputTokens": 0,
        "outputTokens": 0,
        "estimatedCost": 0.0,
        "firstSeen": None,
        "lastSeen": None,
        "statusCounts": [],
        "byRoute": [],
    }


def usage_status_counts(connection, table: str, status_column: str) -> list[dict]:
    if not sqlite_table_exists(connection, table):
        return []
    safe_table = table.replace('"', '""')
    safe_column = status_column.replace('"', '""')
    try:
        return [
            {"status": safe_text(row[0]) or "unknown", "requests": safe_int(row[1])}
            for row in connection.execute(
                f'SELECT COALESCE("{safe_column}", \'\'), COUNT(*) FROM "{safe_table}" '
                f'GROUP BY "{safe_column}" ORDER BY COUNT(*) DESC LIMIT 16'
            )
        ]
    except Exception:
        return []


def summarize_omniroute_usage(connection) -> dict:
    """Summarize OmniRoute's local usage ledger, never its upstream quota."""
    if sqlite_table_exists(connection, "usage_history"):
        columns = sqlite_table_columns(connection, "usage_history")
        required = {"timestamp", "provider", "model", "tokens_input", "tokens_output"}
        if required.issubset(columns):
            row = connection.execute(
                "SELECT COUNT(*), COALESCE(SUM(tokens_input),0), COALESCE(SUM(tokens_output),0), "
                "MIN(timestamp), MAX(timestamp) FROM usage_history"
            ).fetchone()
            usage = empty_local_usage("usage_history", True)
            usage.update(
                {
                    "requests": safe_int(row[0]),
                    "inputTokens": safe_int(row[1]),
                    "outputTokens": safe_int(row[2]),
                    "firstSeen": safe_text(row[3]) or None,
                    "lastSeen": safe_text(row[4]) or None,
                    "statusCounts": usage_status_counts(connection, "usage_history", "status"),
                }
            )
            try:
                route_rows = connection.execute(
                    "SELECT COALESCE(provider,''), COALESCE(model,''), COALESCE(combo_strategy,''), "
                    "COUNT(*), COALESCE(SUM(tokens_input),0), COALESCE(SUM(tokens_output),0), "
                    "MIN(timestamp), MAX(timestamp) FROM usage_history "
                    "GROUP BY provider, model, combo_strategy ORDER BY COUNT(*) DESC LIMIT ?",
                    (ROUTER_ROUTE_LIMIT,),
                )
                usage["byRoute"] = [
                    {
                        "provider": safe_text(route[0]) or "unknown",
                        "model": safe_text(route[1]) or "unknown",
                        "strategy": safe_text(route[2]) or "direct",
                        "requests": safe_int(route[3]),
                        "inputTokens": safe_int(route[4]),
                        "outputTokens": safe_int(route[5]),
                        "firstSeen": safe_text(route[6]) or None,
                        "lastSeen": safe_text(route[7]) or None,
                    }
                    for route in route_rows
                ]
            except Exception:
                usage["byRoute"] = []
            return usage

    # Older or partially migrated installs may only have call_logs. Keep this
    # fallback explicitly labelled as call-log activity rather than quota data.
    if sqlite_table_exists(connection, "call_logs"):
        columns = sqlite_table_columns(connection, "call_logs")
        required = {"timestamp", "provider", "model", "tokens_in", "tokens_out"}
        if required.issubset(columns):
            where = "COALESCE(model,'') NOT IN ('connection-test','model-sync')"
            row = connection.execute(
                f"SELECT COUNT(*), COALESCE(SUM(tokens_in),0), COALESCE(SUM(tokens_out),0), "
                f"MIN(timestamp), MAX(timestamp) FROM call_logs WHERE {where}"
            ).fetchone()
            usage = empty_local_usage("call_logs", True)
            usage.update(
                {
                    "requests": safe_int(row[0]),
                    "inputTokens": safe_int(row[1]),
                    "outputTokens": safe_int(row[2]),
                    "firstSeen": safe_text(row[3]) or None,
                    "lastSeen": safe_text(row[4]) or None,
                    "statusCounts": usage_status_counts(connection, "call_logs", "status"),
                }
            )
            try:
                route_rows = connection.execute(
                    "SELECT COALESCE(provider,''), COALESCE(model,''), COALESCE(combo_name,''), "
                    "COUNT(*), COALESCE(SUM(tokens_in),0), COALESCE(SUM(tokens_out),0), "
                    "MIN(timestamp), MAX(timestamp) FROM call_logs "
                    f"WHERE {where} GROUP BY provider, model, combo_name ORDER BY COUNT(*) DESC LIMIT ?",
                    (ROUTER_ROUTE_LIMIT,),
                )
                usage["byRoute"] = [
                    {
                        "provider": safe_text(route[0]) or "unknown",
                        "model": safe_text(route[1]) or "unknown",
                        "strategy": safe_text(route[2]) or "direct",
                        "requests": safe_int(route[3]),
                        "inputTokens": safe_int(route[4]),
                        "outputTokens": safe_int(route[5]),
                        "firstSeen": safe_text(route[6]) or None,
                        "lastSeen": safe_text(route[7]) or None,
                    }
                    for route in route_rows
                ]
            except Exception:
                usage["byRoute"] = []
            return usage

    return empty_local_usage()


def summarize_omniroute_cached_quotas(connection) -> list[dict]:
    """Read the newest cached OmniRoute quota snapshot per connection/window.

    This never refreshes an upstream quota. Connection IDs are used only to
    select the latest row and count sources; they are not returned.
    """
    table = "quota_snapshots"
    if not sqlite_table_exists(connection, table):
        return []

    columns = sqlite_table_columns(connection, table)
    required = {
        "provider", "connection_id", "window_key", "remaining_percentage",
        "is_exhausted", "next_reset_at", "created_at",
    }
    if not required.issubset(columns):
        return []

    tie_breaker = ", id DESC" if "id" in columns else ""
    duration_column = "window_duration_ms" if "window_duration_ms" in columns else "NULL AS window_duration_ms"
    duration_expr = "MAX(window_duration_ms)" if "window_duration_ms" in columns else "NULL"
    try:
        rows = connection.execute(
            "WITH ranked AS ("
            " SELECT provider, connection_id, window_key, remaining_percentage, is_exhausted,"
            f" next_reset_at, created_at, {duration_column},"
            " ROW_NUMBER() OVER (PARTITION BY provider, connection_id, window_key"
            f" ORDER BY created_at DESC{tie_breaker}) AS snapshot_rank"
            " FROM quota_snapshots"
            " WHERE provider IS NOT NULL AND COALESCE(window_key,'') <> ''"
            " AND remaining_percentage IS NOT NULL"
            ") SELECT provider, window_key, COUNT(*), MIN(remaining_percentage),"
            " MAX(remaining_percentage), SUM(CASE WHEN is_exhausted=1 THEN 1 ELSE 0 END),"
            " MIN(created_at), MAX(created_at), MIN(next_reset_at), MAX(next_reset_at),"
            " COUNT(DISTINCT next_reset_at),"
            " SUM(CASE WHEN next_reset_at IS NULL OR next_reset_at='' THEN 1 ELSE 0 END),"
            f" {duration_expr}"
            " FROM ranked WHERE snapshot_rank=1 GROUP BY provider, window_key"
            " ORDER BY provider, window_key"
        ).fetchall()
    except Exception:
        return []

    result = []
    for row in rows:
        low = max(0.0, min(100.0, safe_float(row[3])))
        high = max(low, min(100.0, safe_float(row[4])))
        distinct_resets = safe_int(row[10])
        missing_resets = safe_int(row[11])
        reset_at = safe_text(row[8]) or None
        result.append(
            {
                "provider": safe_text(row[0]) or "unknown",
                "window": safe_text(row[1]) or "unknown",
                "connections": safe_int(row[2]),
                "lowestRemainingPct": low,
                "highestRemainingPct": high,
                "exhaustedConnections": safe_int(row[5]),
                "oldestSnapshotAt": safe_text(row[6]) or None,
                "newestSnapshotAt": safe_text(row[7]) or None,
                "resetAt": reset_at if distinct_resets == 1 and missing_resets == 0 else None,
                "resetsVary": distinct_resets > 1 or missing_resets > 0,
                "windowDurationMs": safe_int(row[12]) if row[12] is not None else None,
            }
        )
    return result


def normalize_router_provider(value) -> str:
    """Avoid surfacing opaque connection UUIDs as if they were provider names."""
    text = safe_text(value)
    if not text:
        return "unknown"
    if text.startswith("openai-compatible"):
        return "openai-compatible"
    return text


def summarize_9router_usage(connection) -> dict:
    """Summarize 9router's local usageHistory table, not upstream quota."""
    if not sqlite_table_exists(connection, "usageHistory"):
        return empty_local_usage()

    columns = sqlite_table_columns(connection, "usageHistory")
    required = {"timestamp", "provider", "model", "promptTokens", "completionTokens"}
    if not required.issubset(columns):
        return empty_local_usage()

    cost_sum = "COALESCE(SUM(cost),0)" if "cost" in columns else "NULL"
    row = connection.execute(
        "SELECT COUNT(*), COALESCE(SUM(promptTokens),0), COALESCE(SUM(completionTokens),0), "
        f"{cost_sum}, MIN(timestamp), MAX(timestamp) FROM usageHistory"
    ).fetchone()
    usage = empty_local_usage("usageHistory", True)
    usage.update(
        {
            "requests": safe_int(row[0]),
            "inputTokens": safe_int(row[1]),
            "outputTokens": safe_int(row[2]),
            "estimatedCost": round(safe_float(row[3]), 6) if "cost" in columns else None,
            "firstSeen": safe_text(row[4]) or None,
            "lastSeen": safe_text(row[5]) or None,
            "statusCounts": usage_status_counts(connection, "usageHistory", "status"),
        }
    )
    try:
        route_cost_sum = "COALESCE(SUM(cost),0)" if "cost" in columns else "NULL"
        route_rows = connection.execute(
            "SELECT COALESCE(provider,''), COALESCE(model,''), COUNT(*), "
            "COALESCE(SUM(promptTokens),0), COALESCE(SUM(completionTokens),0), "
            f"{route_cost_sum}, MIN(timestamp), MAX(timestamp) FROM usageHistory "
            "GROUP BY provider, model ORDER BY COUNT(*) DESC LIMIT ?",
            (ROUTER_ROUTE_LIMIT,),
        )
        usage["byRoute"] = [
            {
                "provider": normalize_router_provider(route[0]),
                "model": safe_text(route[1]) or "unknown",
                "strategy": "direct",
                "requests": safe_int(route[2]),
                "inputTokens": safe_int(route[3]),
                "outputTokens": safe_int(route[4]),
                "estimatedCost": round(safe_float(route[5]), 6) if "cost" in columns else None,
                "firstSeen": safe_text(route[6]) or None,
                "lastSeen": safe_text(route[7]) or None,
            }
            for route in route_rows
        ]
    except Exception:
        usage["byRoute"] = []
    return usage


def connection_counts(connection, table: str, provider_column: str, active_column: str) -> list[dict]:
    if not sqlite_table_exists(connection, table):
        return []
    columns = sqlite_table_columns(connection, table)
    if provider_column not in columns or active_column not in columns:
        return []
    safe_table = table.replace('"', '""')
    safe_provider = provider_column.replace('"', '""')
    safe_active = active_column.replace('"', '""')
    try:
        rows = connection.execute(
            f'SELECT COALESCE("{safe_provider}",\'\'), COUNT(*), '
            f'COALESCE(SUM("{safe_active}"),0) FROM "{safe_table}" '
            f'GROUP BY "{safe_provider}" ORDER BY COUNT(*) DESC LIMIT 32'
        )
        groups = {}
        for row in rows:
            provider = normalize_router_provider(row[0])
            group = groups.setdefault(provider, {"provider": provider, "connections": 0, "active": 0})
            group["connections"] += safe_int(row[1])
            group["active"] += safe_int(row[2])
        return sorted(groups.values(), key=lambda item: (-item["connections"], item["provider"]))
    except Exception:
        return []


def models_from_combo(raw_models) -> list[dict]:
    try:
        models = json.loads(raw_models) if isinstance(raw_models, str) else raw_models
    except Exception:
        models = []
    if isinstance(models, dict):
        models = models.get("models") or models.get("routes") or []
    if not isinstance(models, list):
        return []

    result = []
    for item in models[:ROUTER_ROUTE_LIMIT]:
        if isinstance(item, str):
            model = safe_text(item)
            provider = ""
            weight = None
        elif isinstance(item, dict):
            model = safe_text(item.get("model") or item.get("modelId") or item.get("name"))
            provider = safe_text(item.get("providerId") or item.get("provider"))
            weight = item.get("weight") if isinstance(item.get("weight"), (int, float)) else None
        else:
            continue
        if model:
            result.append({"provider": provider or None, "model": model, "weight": weight})
    return result


def combo_model_label(item: dict) -> str:
    provider = safe_text(item.get("provider"))
    model = safe_text(item.get("model")) or "unknown"
    if provider and not (model == provider or model.startswith(provider + "/")):
        return f"{provider}/{model}"
    return model


def combo_explanation(name: str, strategy: str, models: list[dict]) -> str:
    name = safe_text(name) or "unnamed route"
    strategy = safe_text(strategy) or "unknown"
    if name == "coding-safe":
        intro = "A local route alias intended for coding requests; the name is not a sandbox or safety guarantee. Priority tries candidates in stored order, with later entries as fallbacks."
    elif strategy.lower() == "priority":
        intro = "A local priority route alias; models are tried in stored order, with later entries as fallbacks."
    elif strategy.lower() in {"round-robin", "round_robin"}:
        intro = "A local round-robin route alias; the router rotates candidates according to its configuration."
    elif strategy.lower() in {"weighted", "weight"}:
        intro = "A local weighted route alias; the configured weights determine candidate selection."
    else:
        intro = "A local router alias; its stored strategy is shown below."
    if models:
        order = " → ".join(combo_model_label(item) for item in models)
        return f"{intro} It is not an upstream subscription or quota. Stored models: {order}."
    return f"{intro} No model list was available in the local metadata; upstream quota was not queried."


def make_combo(name: str, strategy: str, models: list[dict], observed_requests: int | None = None) -> dict:
    combo = {
        "name": safe_text(name) or "unnamed route",
        "strategy": safe_text(strategy) or "unknown",
        "models": models,
        "explanation": combo_explanation(name, strategy, models),
    }
    if observed_requests is not None:
        combo["observedRequests"] = safe_int(observed_requests)
    return combo


def omniroute_combos(connection) -> list[dict]:
    if not sqlite_table_exists(connection, "combos"):
        return []
    result = []
    try:
        for row in connection.execute("SELECT name, data FROM combos ORDER BY sort_order, name"):
            try:
                data = json.loads(row[1]) if isinstance(row[1], str) else row[1]
            except Exception:
                data = {}
            data = data if isinstance(data, dict) else {}
            result.append(
                make_combo(
                    safe_text(row[0]) or safe_text(data.get("name")),
                    safe_text(data.get("strategy")),
                    models_from_combo(data.get("models") or data.get("routes") or []),
                )
            )
    except Exception:
        return []
    return result


def router9_combos(connection) -> list[dict]:
    if not sqlite_table_exists(connection, "combos"):
        return []
    result = []
    try:
        for row in connection.execute("SELECT name, kind, models FROM combos ORDER BY name"):
            result.append(make_combo(safe_text(row[0]), safe_text(row[1]) or "configured", models_from_combo(row[2])))
    except Exception:
        return []
    return result


def observed_omniroute_combos(connection) -> list[dict]:
    if not sqlite_table_exists(connection, "call_logs"):
        return []
    columns = sqlite_table_columns(connection, "call_logs")
    if not {"combo_name", "model"}.issubset(columns):
        return []
    try:
        rows = connection.execute(
            "SELECT combo_name, COUNT(*) FROM call_logs "
            "WHERE COALESCE(combo_name,'') <> '' AND COALESCE(model,'') NOT IN ('connection-test','model-sync') "
            "GROUP BY combo_name ORDER BY COUNT(*) DESC LIMIT 32"
        )
        return [{"name": safe_text(row[0]), "requests": safe_int(row[1])} for row in rows if safe_text(row[0])]
    except Exception:
        return []


def merge_observed_combos(configured: list[dict], observed: list[dict]) -> list[dict]:
    by_name = {combo["name"]: combo for combo in configured}
    for item in observed:
        name = item["name"]
        if name in by_name:
            by_name[name]["observedRequests"] = item["requests"]
        else:
            configured.append(
                make_combo(
                    name,
                    "observed",
                    [],
                    item["requests"],
                )
            )
            configured[-1]["explanation"] = (
                "Observed in local router logs, but no matching combo metadata was found; "
                "the dashboard does not infer its candidate list or upstream quota."
            )
    return configured


def router_summary(status: str, reason: str | None, usage: dict) -> str:
    if status == "unavailable":
        return reason or "local database unavailable"
    if usage.get("available"):
        return f"{safe_int(usage.get('requests'))} local requests · upstream quota not queried"
    return "configured locally · no local usage rows · upstream quota not queried"


def unavailable_router(router_id: str, name: str, path: str, reason: str, connection=None) -> dict:
    schema_names = {
        "omniroute": ("usage_history", "call_logs", "combos", "model_combo_mappings", "provider_connections", "quota_snapshots"),
        "9router": ("usageHistory", "usageDaily", "combos", "providerConnections", "settings", "quota_snapshots"),
    }.get(router_id, ())
    try:
        tables = sqlite_schema_info(connection, schema_names) if connection is not None else [
            {"name": item, "present": False, "rows": None} for item in schema_names
        ]
    except Exception:
        tables = [{"name": item, "present": False, "rows": None} for item in schema_names]
    usage = empty_local_usage()
    return {
        "id": router_id,
        "name": name,
        "status": "unavailable",
        "readOnly": True,
        "scope": "local-router",
        "summary": router_summary("unavailable", reason, usage),
        "source": {"kind": "sqlite", "path": display_path(path), "readOnly": True, "tables": tables},
        "usage": usage,
        "cachedQuotas": [],
        "aliases": [],
        "observedAliases": [],
        "connections": [],
        "notes": [reason, "No router metrics are claimed; upstream provider quota was not queried."],
    }


def probe_omniroute() -> dict:
    path = OMNIROUTE_DB
    connection, error = open_sqlite_readonly(path)
    if connection is None:
        return unavailable_router("omniroute", "OmniRoute", path, error or "database unavailable")
    try:
        table_names = ("usage_history", "call_logs", "combos", "model_combo_mappings", "provider_connections", "quota_snapshots")
        usage = summarize_omniroute_usage(connection)
        cached_quotas = summarize_omniroute_cached_quotas(connection)
        aliases = omniroute_combos(connection)
        observed = observed_omniroute_combos(connection)
        aliases = merge_observed_combos(aliases, observed)
        connections = connection_counts(connection, "provider_connections", "provider", "is_active")
        status = "available" if usage.get("requests") or aliases or connections or cached_quotas else "configured"
        notes = [
            "Read-only local SQLite inspection. Local router accounting is not an upstream subscription quota.",
            "The probe does not call OmniRoute, provider APIs, or any route/model.",
            "Quota snapshots are cached local values; the dashboard does not refresh them.",
        ]
        if not usage.get("available"):
            notes.append("No compatible local usage table was found; only configuration metadata is shown.")
        return {
            "id": "omniroute",
            "name": "OmniRoute",
            "status": status,
            "readOnly": True,
            "scope": "local-router",
            "summary": router_summary(status, None, usage),
            "source": {"kind": "sqlite", "path": display_path(path), "readOnly": True, "tables": sqlite_schema_info(connection, table_names)},
            "usage": usage,
            "cachedQuotas": cached_quotas,
            "aliases": aliases,
            "observedAliases": observed,
            "connections": connections,
            "notes": notes,
        }
    except Exception as exc:
        return unavailable_router(
            "omniroute",
            "OmniRoute",
            path,
            f"local database could not be summarized: {type(exc).__name__}: {str(exc)[:120]}",
            connection=connection,
        )
    finally:
        connection.close()


def probe_9router() -> dict:
    path = ROUTER9_DB
    connection, error = open_sqlite_readonly(path)
    if connection is None:
        return unavailable_router("9router", "9router", path, error or "database unavailable")
    try:
        table_names = ("usageHistory", "usageDaily", "combos", "providerConnections", "settings", "quota_snapshots")
        usage = summarize_9router_usage(connection)
        aliases = router9_combos(connection)
        connections = connection_counts(connection, "providerConnections", "provider", "isActive")
        status = "available" if usage.get("requests") or aliases or connections else "configured"
        notes = [
            "Read-only local SQLite inspection. Local router accounting is not an upstream subscription quota.",
            "The probe does not call 9router, provider APIs, or any route/model.",
        ]
        if not aliases:
            notes.append("No configured combo rows were found in the local 9router database.")
        if not usage.get("available"):
            notes.append("No compatible local usage table was found; only configuration metadata is shown.")
        if sqlite_table_exists(connection, "quota_snapshots"):
            notes.append("A local quota_snapshots table exists, but this probe does not interpret its schema.")
        else:
            notes.append("No cached upstream quota snapshot table is present in this 9router database.")
        return {
            "id": "9router",
            "name": "9router",
            "status": status,
            "readOnly": True,
            "scope": "local-router",
            "summary": router_summary(status, None, usage),
            "source": {"kind": "sqlite", "path": display_path(path), "readOnly": True, "tables": sqlite_schema_info(connection, table_names)},
            "usage": usage,
            "cachedQuotas": [],
            "aliases": aliases,
            "observedAliases": [],
            "connections": connections,
            "notes": notes,
        }
    except Exception as exc:
        return unavailable_router(
            "9router",
            "9router",
            path,
            f"local database could not be summarized: {type(exc).__name__}: {str(exc)[:120]}",
            connection=connection,
        )
    finally:
        connection.close()


ROUTERS = (
    {"id": "omniroute", "probe": probe_omniroute},
    {"id": "9router", "probe": probe_9router},
)


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
        name = str(user.get("userName") or user.get("name") or "").strip()
        email = str(user.get("email") or "").strip()
        # The account's email is personal data - keep it out of the payload.
        # Show the handle only; if there is no handle, a masked fallback.
        if not name and email:
            name = email[:2] + "***@***"
        if name:
            row["sub"] = f"{name} - fp {sha(token)[:6]}"

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

        # The monthly allowance is a CREDIT BUDGET ($), not a rate-limit window:
        # commandcode publishes only fiveHour/weekly under windowLimits, while the
        # cycle figure lives in usage/summary (periodBasis "billing-period") and
        # the plan total in the subscription. Synthesize the window so every
        # surface meters it like any other window; skip it when the plan is not
        # in the map, so an unknown cap renders as a note instead of a fake %.
        period_end = str(sub.get("currentPeriodEnd") or "")
        spent = summary.get("totalMonthlyCredits")
        if not isinstance(spent, (int, float)):
            spent = summary.get("totalCost")
        if isinstance(spent, (int, float)) and isinstance(total, (int, float)) and float(total) > 0:
            row["windows"].append({
                "k": "monthly",
                "pct": round((float(spent) / float(total)) * 100, 1),
                "reset": local_iso(period_end) if period_end else None,
                "note": f"${float(spent):.2f} / ${float(total):.0f}",
            })

        balances = credits.get("credits") or {}
        monthly = balances.get("monthlyCredits")
        if isinstance(monthly, (int, float)):
            suffix = f" of ${total}" if isinstance(total, (int, float)) else ""
            row["notes"].append(f"credit balance: ${float(monthly):.2f}{suffix} left (this cycle)")
        for key, note_label in (("purchasedCredits", "purchased"), ("freeCredits", "free")):
            value = balances.get(key)
            if isinstance(value, (int, float)) and float(value) > 0:
                row["notes"].append(f"{note_label}: ${float(value):.2f}")
        if period_end:
            # Neutral wording: the subscription carries canceledAt, so claiming a
            # renewal would be wrong - the date is when the cycle ends.
            row["notes"].append("cycle ends " + period_end[:10])
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


ROW_CONTRACT = {"label": "?", "sub": "", "plan": None, "acct": None,
                "windows": [], "notes": [], "error": None}


def normalize_row(row) -> dict:
    """Fill a probe row's defaults - a backstop so third-party probe functions
    returning partial rows still render. Window entries:
    {"k": str, "pct": float, "reset": iso|None, "note": str|None}."""
    merged = dict(ROW_CONTRACT)
    if isinstance(row, dict):
        merged.update(row)
    for key in ("windows", "notes"):
        if not isinstance(merged.get(key), list):
            merged[key] = []
    return merged


# To add a provider: write `def probe_<id>(account) -> row` (contract above,
# guide in README "Adding another provider" - PRs welcome) and append one
# entry here. `account` = {"label", "token", "base", "fp"}; set "env_var" to
# the .env var supplying a fallback credential when the pool is empty ("" =
# pool only).
PROVIDERS = (
    {"id": "openai-codex", "name": "Codex (ChatGPT)", "env_var": "", "base": CODEX_DEFAULT_BASE,
     "probe": probe_codex},
    {"id": "opencode-go", "name": "OpenCode Go", "env_var": "OPENCODE_GO_API_KEY",
     "base": OPENCODE_DEFAULT_BASE, "probe": probe_opencode},
    {"id": "commandcode", "name": "CommandCode", "env_var": "COMMANDCODE_API_KEY",
     "base": COMMANDCODE_DEFAULT_BASE, "probe": probe_commandcode},
)


def parse_filters(argv):
    """Parse provider/account filters and local-router-only inspection options."""
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--provider", action="append", default=[])
    parser.add_argument("--account", action="append", default=[])
    parser.add_argument("--router", action="append", default=[])
    parser.add_argument("--router-part", choices=("all", "summary", "routes"), default="all")
    parser.add_argument("--route-offset", type=int, default=0)
    parser.add_argument("--route-limit", type=int, default=12)
    args, _ = parser.parse_known_args(argv)
    providers = {p.strip() for p in args.provider if p.strip()}
    accounts: dict = {}
    for entry in args.account:
        provider, _, fp = entry.strip().partition(":")
        if provider and fp:
            accounts.setdefault(provider, set()).add(fp.lower())
    routers = {router.strip().lower() for router in args.router if router.strip()}
    route_offset = max(0, args.route_offset)
    route_limit = max(1, min(ROUTER_ROUTE_PAGE_MAX, args.route_limit))
    return providers, accounts, routers, args.router_part, route_offset, route_limit


def main() -> int:
    started = time.time()
    only_providers, account_filters, only_routers, router_part, route_offset, route_limit = parse_filters(sys.argv[1:])
    wanted = set(only_providers) | set(account_filters)
    router_only = bool(only_routers)

    jobs = []
    per_provider = {}
    for provider in PROVIDERS:
        pid = provider["id"]
        if router_only:
            continue
        if wanted and pid not in wanted:
            continue
        accounts = accounts_for(pid, provider["env_var"], provider["base"])
        if pid in account_filters:
            prefixes = account_filters[pid]
            accounts = [a for a in accounts if any(a["fp"].startswith(p) for p in prefixes)]
        per_provider[pid] = accounts
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

    router_results = []
    # A provider/account refresh only needs provider data. The initial load,
    # and an explicit --router probe, read the local router databases too.
    if router_only or (not only_providers and not account_filters):
        for router in ROUTERS:
            if only_routers and router["id"] not in only_routers:
                continue
            try:
                result = router["probe"]()
                usage = result.get("usage") or {}
                if router_part == "summary":
                    route_rows = usage.pop("byRoute", []) or []
                    usage.pop("statusCounts", None)
                    usage["routeCount"] = len(route_rows)
                    if router["id"] == "omniroute":
                        result["notes"] = []
                        (result.get("source") or {}).pop("tables", None)
                        result["connections"] = []
                        result["observedAliases"] = []
                    router_results.append(result)
                elif router_part == "routes":
                    route_rows = usage.get("byRoute") or []
                    router_results.append(
                        {
                            "id": router["id"],
                            "routeOffset": route_offset,
                            "source": {"tables": (result.get("source") or {}).get("tables", [])} if route_offset == 0 else {},
                            "connections": result.get("connections", []) if route_offset == 0 else [],
                            "observedAliases": result.get("observedAliases", []) if route_offset == 0 else [],
                            "usage": {
                                "routeCount": len(route_rows),
                                "byRoute": route_rows[route_offset:route_offset + route_limit],
                                "statusCounts": usage.get("statusCounts", []) if route_offset == 0 else [],
                            },
                            "notes": result.get("notes", []) if route_offset == 0 else [],
                        }
                    )
                else:
                    router_results.append(result)
            except Exception as exc:  # defensive: local inspection must not break the provider pane
                path = OMNIROUTE_DB if router["id"] == "omniroute" else ROUTER9_DB
                name = "OmniRoute" if router["id"] == "omniroute" else "9router"
                router_results.append(
                    unavailable_router(
                        router["id"],
                        name,
                        path,
                        f"local probe failed: {type(exc).__name__}: {str(exc)[:120]}",
                    )
                )

    payload = {
        "fetchedAt": datetime.now().astimezone().isoformat(timespec="seconds"),
        "elapsedMs": int((time.time() - started) * 1000),
        "providers": [
            {
                "id": provider["id"],
                "name": provider["name"],
                "accounts": [
                    {**normalize_row(results.get((provider["id"], account["fp"]))), "fp": account["fp"]}
                    for account in per_provider[provider["id"]]
                ],
            }
            for provider in PROVIDERS
            if provider["id"] in per_provider
        ],
        "routers": router_results,
    }
    print(SENTINEL + " " + json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    sys.exit(main())
