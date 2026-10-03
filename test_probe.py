import base64
import contextlib
import hashlib
import io
import json
import os
import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import probe


class LocalRouterProbeTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.omni_db = self.root / "omniroute" / "storage.sqlite"
        self.router9_db = self.root / "9router" / "db" / "data.sqlite"
        self.omni_db.parent.mkdir(parents=True)
        self.router9_db.parent.mkdir(parents=True)
        self.old_omni_db = probe.OMNIROUTE_DB
        self.old_router9_db = probe.ROUTER9_DB
        probe.OMNIROUTE_DB = str(self.omni_db)
        probe.ROUTER9_DB = str(self.router9_db)

    def tearDown(self):
        probe.OMNIROUTE_DB = self.old_omni_db
        probe.ROUTER9_DB = self.old_router9_db
        self.tempdir.cleanup()

    @staticmethod
    def digest(path):
        return hashlib.sha256(path.read_bytes()).hexdigest()

    def create_omniroute_db(self):
        connection = sqlite3.connect(self.omni_db)
        connection.executescript(
            """
            CREATE TABLE usage_history (
                id INTEGER PRIMARY KEY,
                provider TEXT,
                model TEXT,
                tokens_input INTEGER DEFAULT 0,
                tokens_output INTEGER DEFAULT 0,
                status TEXT,
                combo_strategy TEXT,
                timestamp TEXT NOT NULL
            );
            CREATE TABLE call_logs (
                id TEXT PRIMARY KEY,
                timestamp TEXT NOT NULL,
                model TEXT,
                combo_name TEXT
            );
            CREATE TABLE combos (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                data TEXT NOT NULL,
                sort_order INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE provider_connections (
                id TEXT PRIMARY KEY,
                provider TEXT NOT NULL,
                is_active INTEGER DEFAULT 1
            );
            CREATE TABLE quota_snapshots (
                id INTEGER PRIMARY KEY,
                provider TEXT NOT NULL,
                connection_id TEXT NOT NULL,
                window_key TEXT NOT NULL,
                remaining_percentage REAL,
                is_exhausted INTEGER,
                next_reset_at TEXT,
                window_duration_ms INTEGER,
                created_at TEXT NOT NULL
            );
            INSERT INTO usage_history VALUES
                (1, 'command-code', 'deepseek/v4', 100, 25, '200', 'priority', '2026-09-24T00:00:00Z'),
                (2, 'codex', 'gpt-test', 50, 10, '200', 'direct', '2026-09-24T00:01:00Z');
            INSERT INTO call_logs VALUES
                ('a', '2026-09-24T00:00:00Z', 'deepseek/v4', 'coding-safe');
            INSERT INTO combos VALUES
                ('combo-1', 'coding-safe', '{"strategy":"priority","models":[{"providerId":"command-code","model":"command-code/deepseek/v4"},{"providerId":"codex","model":"codex/gpt-test"}]}', 1);
            INSERT INTO provider_connections VALUES ('connection-1', 'command-code', 1);
            INSERT INTO quota_snapshots VALUES
                (1, 'codex', 'credential-id-1', 'session', 40, 0, '2026-09-24T01:00:00Z', 18000000, '2026-09-24T00:00:00Z'),
                (2, 'codex', 'credential-id-1', 'session', 90, 0, '2026-09-24T01:00:00Z', 18000000, '2026-09-24T00:01:00Z'),
                (3, 'codex', 'credential-id-2', 'session', 70, 0, '2026-09-24T01:00:00Z', 18000000, '2026-09-24T00:02:00Z');
            """
        )
        connection.commit()
        connection.close()

    def create_9router_db(self, include_cost=True):
        connection = sqlite3.connect(self.router9_db)
        schema = """
            CREATE TABLE usageHistory (
                id INTEGER PRIMARY KEY,
                timestamp TEXT NOT NULL,
                provider TEXT,
                model TEXT,
                promptTokens INTEGER DEFAULT 0,
                completionTokens INTEGER DEFAULT 0,
                cost REAL DEFAULT 0,
                status TEXT
            );
            CREATE TABLE usageDaily (dateKey TEXT PRIMARY KEY, data TEXT NOT NULL);
            CREATE TABLE combos (id TEXT PRIMARY KEY, name TEXT, kind TEXT, models TEXT NOT NULL);
            CREATE TABLE providerConnections (id TEXT PRIMARY KEY, provider TEXT NOT NULL, isActive INTEGER DEFAULT 1);
            CREATE TABLE settings (id INTEGER PRIMARY KEY, data TEXT NOT NULL);
            INSERT INTO usageHistory VALUES
                (1, '2026-09-24T00:00:00Z', 'openai-compatible-chat-test', 'deepseek/v4', 200, 30, 0.25, 'ok');
            INSERT INTO usageDaily VALUES ('2026-09-24', '{}');
            INSERT INTO providerConnections VALUES ('connection-1', 'openai-compatible-chat-test', 1);
            INSERT INTO settings VALUES (1, '{"providerStrategies":{}}');
            """
        if not include_cost:
            schema = schema.replace("                cost REAL DEFAULT 0,\n", "")
            schema = schema.replace(", 200, 30, 0.25, 'ok');", ", 200, 30, 'ok');")
        connection.executescript(schema)
        connection.commit()
        connection.close()

    def test_omniroute_probe_is_local_read_only_and_explains_combo(self):
        self.create_omniroute_db()
        before = self.digest(self.omni_db)

        result = probe.probe_omniroute()

        self.assertEqual(result["status"], "available")
        self.assertTrue(result["readOnly"])
        self.assertEqual(result["usage"]["requests"], 2)
        self.assertEqual(result["usage"]["inputTokens"], 150)
        self.assertEqual(result["aliases"][0]["name"], "coding-safe")
        self.assertIn("priority", result["aliases"][0]["explanation"].lower())
        self.assertIn("coding requests", result["aliases"][0]["explanation"])
        self.assertIn("not a sandbox", result["aliases"][0]["explanation"])
        self.assertIn("fallbacks", result["aliases"][0]["explanation"])
        self.assertIn("upstream subscription", result["aliases"][0]["explanation"])
        self.assertEqual(result["connections"][0]["active"], 1)
        self.assertEqual(len(result["cachedQuotas"]), 1)
        cached = result["cachedQuotas"][0]
        self.assertEqual(cached["lowestRemainingPct"], 70)
        self.assertEqual(cached["highestRemainingPct"], 90)
        self.assertEqual(cached["connections"], 2)
        self.assertEqual(cached["resetAt"], "2026-09-24T01:00:00Z")
        self.assertNotIn("credential-id-1", json.dumps(result))
        self.assertEqual(before, self.digest(self.omni_db))

    def test_9router_probe_reports_local_usage_without_combo_or_quota_claim(self):
        self.create_9router_db()
        before = self.digest(self.router9_db)

        result = probe.probe_9router()

        self.assertEqual(result["status"], "available")
        self.assertTrue(result["readOnly"])
        self.assertEqual(result["usage"]["requests"], 1)
        self.assertEqual(result["usage"]["inputTokens"], 200)
        self.assertEqual(result["aliases"], [])
        self.assertTrue(any("No configured combo" in note for note in result["notes"]))
        self.assertTrue(any("No cached upstream quota snapshot" in note for note in result["notes"]))
        self.assertEqual(result["cachedQuotas"], [])
        self.assertEqual(before, self.digest(self.router9_db))

    def test_9router_without_optional_cost_column_still_reports_usage(self):
        self.create_9router_db(include_cost=False)

        result = probe.probe_9router()

        self.assertTrue(result["usage"]["available"])
        self.assertEqual(result["usage"]["requests"], 1)
        self.assertIsNone(result["usage"]["estimatedCost"])
        self.assertIsNone(result["usage"]["byRoute"][0]["estimatedCost"])

    def test_sqlite_readonly_connection_rejects_writes(self):
        self.create_9router_db()
        before = self.digest(self.router9_db)
        connection, error = probe.open_sqlite_readonly(str(self.router9_db))
        self.assertIsNone(error)
        if connection is None:
            self.fail("read-only SQLite connection did not open")

        with self.assertRaises(sqlite3.OperationalError):
            connection.execute("UPDATE usageHistory SET status='changed'")

        connection.close()
        self.assertEqual(before, self.digest(self.router9_db))

    def test_missing_database_is_explicitly_unavailable(self):
        result = probe.probe_omniroute()

        self.assertEqual(result["status"], "unavailable")
        self.assertFalse(result["usage"]["available"])
        self.assertEqual(result["usage"]["requests"], 0)
        self.assertIn("database not found", result["summary"])

    def test_plugin_keeps_route_explanations_collapsed(self):
        plugin_source = (Path(__file__).parent / "plugin.js").read_text(encoding="utf-8")
        visual_source = plugin_source.partition("function RouterUsageVisual({ router })")[2].partition("function RouterCallsDetails({ router })")[0]
        calls_source = plugin_source.partition("function RouterCallsDetails({ router })")[2].partition("function quotaPctOrNull")[0]
        section_source = plugin_source.partition("function LocalRouterSection({ router, busy, onRefresh })")[2].partition("function QuotaPane")[0]

        self.assertNotIn("Calls per model", visual_source)
        self.assertIn("function RouterCallsDetails({ router })", plugin_source)
        self.assertIn("jsx('details'", calls_source)
        self.assertIn("jsx('summary'", calls_source)
        self.assertIn("Calls per model", calls_source)
        self.assertNotIn("open:", calls_source)
        self.assertIn("More router details", calls_source)
        self.assertLess(section_source.index("cachedQuotas.length"), section_source.index("RouterCallsDetails"))
        self.assertIn("const totalRequests", calls_source)
        self.assertIn("sharePct", calls_source)
        self.assertIn("formatPct(sharePct)", calls_source)
        self.assertIn("of local requests", calls_source)
        self.assertIn("flex-1 break-words", calls_source)
        self.assertNotIn("flex-1 truncate", calls_source)
        self.assertIn("'aria-valuemax': 100", calls_source)
        self.assertIn("'aria-valuenow': sharePct", calls_source)
        self.assertIn("Cached upstream quota snapshots", plugin_source)
        self.assertIn("Route aliases and explanations", plugin_source)
        self.assertIn("jsx(RouterUsageVisual, { router })", plugin_source)
        self.assertNotIn("Local route mix", plugin_source)
        self.assertNotIn("Panggilan per model", plugin_source)
        self.assertIn("Input tokens", visual_source)
        self.assertIn("Output tokens", visual_source)
        self.assertIn("grid grid-cols-2 gap-1.5", visual_source)
        self.assertIn("col-span-2", visual_source)
        self.assertIn("text-base font-semibold", visual_source)
        self.assertIn("style: { fontSize: '0.6rem' }", visual_source)
        self.assertIn("quota-meter-fill", plugin_source)
        self.assertIn("function quotaPctOrNull(value)", plugin_source)
        self.assertIn("const usedHigh = usedPctFromRemaining(snapshot.lowestRemainingPct)", plugin_source)
        self.assertIn("enabledIds(PROVIDER_IDS).map(provider => runProbe({ provider }))", plugin_source)
        self.assertIn("routerPart: 'summary'", plugin_source)
        self.assertIn("routerPart: 'routes'", plugin_source)
        self.assertNotIn("open: true", plugin_source)

    def test_router_only_main_does_not_invoke_provider_probes(self):
        self.create_omniroute_db()
        output = io.StringIO()
        provider_probe = {"id": "should-not-run", "env_var": "", "base": "", "probe": lambda account: self.fail("provider probe called")}

        with patch.object(probe, "PROVIDERS", (provider_probe,)), patch.object(
            sys, "argv", ["probe.py", "--router", "omniroute"]
        ), contextlib.redirect_stdout(output):
            exit_code = probe.main()

        self.assertEqual(exit_code, 0)
        payload_line = next(line for line in output.getvalue().splitlines() if line.startswith(probe.SENTINEL + " "))
        payload = json.loads(payload_line.split(" ", 1)[1])
        self.assertEqual(payload["providers"], [])
        self.assertEqual([router["id"] for router in payload["routers"]], ["omniroute"])

    def test_router_output_pages_fit_shell_exec_capture_limit(self):
        self.create_omniroute_db()
        routes = [
            {
                "provider": f"provider-{index % 3}",
                "model": f"sample-model-{index:02d}",
                "strategy": "direct",
                "requests": index + 1,
                "inputTokens": 100,
                "outputTokens": 50,
                "firstSeen": "2026-09-01T00:00:00Z",
                "lastSeen": "2026-09-24T00:00:00Z",
            }
            for index in range(25)
        ]
        usage = probe.empty_local_usage("usage_history", True)
        usage.update(
            {
                "requests": 25,
                "inputTokens": 2500,
                "outputTokens": 1250,
                "byRoute": routes,
                "statusCounts": [{"status": "200", "requests": 25}],
            }
        )

        def synthetic_usage(_connection):
            return {
                **usage,
                "byRoute": routes,
                "statusCounts": [{"status": "200", "requests": 25}],
            }

        with patch.object(probe, "summarize_omniroute_usage", side_effect=synthetic_usage):
            def run_part(part, offset=None):
                output = io.StringIO()
                argv = ["probe.py", "--router", "omniroute", "--router-part", part]
                if offset is not None:
                    argv.extend(["--route-offset", str(offset), "--route-limit", "8"])
                with patch.object(sys, "argv", argv), contextlib.redirect_stdout(output):
                    self.assertEqual(probe.main(), 0)
                line = next(item for item in output.getvalue().splitlines() if item.startswith(probe.SENTINEL + " "))
                self.assertLessEqual(len(line), 3500, "router chunks should leave headroom under shell.exec's 4 KB cap")
                return json.loads(line.split(" ", 1)[1])["routers"][0]

            summary = run_part("summary")
            self.assertEqual(summary["usage"]["routeCount"], 25)
            self.assertNotIn("byRoute", summary["usage"])
            rows = []
            status_counts = []
            for offset in (0, 8, 16, 24):
                page = run_part("routes", offset)
                rows.extend(page["usage"]["byRoute"])
                if offset == 0:
                    status_counts = page["usage"]["statusCounts"]
            self.assertEqual(len(rows), 25)
            self.assertEqual(status_counts, [{"status": "200", "requests": 25}])


class CommandCodeProbeTests(unittest.TestCase):
    """CommandCode publishes no monthly *rate-limit* window - the cycle figure is
    a $ credit budget split across three endpoints. The probe synthesizes the
    window so every surface meters it; these tests pin that, plus the refusal to
    invent a percentage when the plan is unknown."""

    class Response:
        def __init__(self, status_code, payload):
            self.status_code = status_code
            self._payload = payload

        def json(self):
            return self._payload

    def payloads(self, plan_id="individual-goat", spent=56.144070094,
                 remaining=13.172665233, period_end="2026-10-11T12:03:13.000Z"):
        return {
            "/alpha/whoami": {"user": {"userName": "sample", "email": "sample@example.com"}},
            "/alpha/billing/credits": {
                "credits": {"monthlyCredits": remaining, "purchasedCredits": 0},
                "windowLimits": {
                    "fiveHour": {"used": 0.773947405, "cap": 14, "resetAt": 1790885723288},
                    "weekly": {"used": 16.082088414, "cap": 35, "resetAt": 1790943522086},
                },
            },
            "/alpha/billing/subscriptions": {
                "data": {"planId": plan_id, "currentPeriodEnd": period_end}
            },
            "/alpha/usage/summary": {
                "totalMonthlyCredits": spent, "totalCost": spent, "totalCount": 23804,
                "successRate": 100, "totalTokens": 2_356_600_000,
            },
        }

    def fake_get(self, payloads, status_code=200):
        def get(url, headers=None, timeout=None):
            path = url.split("api.commandcode.ai", 1)[-1]
            if path not in payloads:
                return self.Response(404, {"error": "unknown route"})
            return self.Response(status_code, payloads[path])
        return get

    def row(self, payloads, status_code=200):
        with patch.object(probe.httpx, "get", self.fake_get(payloads, status_code)):
            return probe.probe_commandcode({"label": "COMMANDCODE_API_KEY (env)", "token": "sample-token",
                                            "base": "", "fp": "deadbeef"})

    def test_monthly_window_is_synthesized_from_the_plan_total(self):
        row = self.row(self.payloads())
        self.assertIsNone(row["error"])
        self.assertEqual([w["k"] for w in row["windows"]], ["5h", "weekly", "monthly"])
        monthly = row["windows"][2]
        self.assertEqual(monthly["pct"], 80.2)
        self.assertEqual(monthly["note"], "$56.14 / $70")
        self.assertTrue(monthly["reset"].startswith("2026-10-11"), monthly["reset"])
        self.assertEqual(row["plan"], "GOAT")

    def test_unknown_plan_never_invents_a_monthly_percentage(self):
        row = self.row(self.payloads(plan_id="individual-mystery"))
        self.assertEqual([w["k"] for w in row["windows"]], ["5h", "weekly"])
        self.assertEqual(row["plan"], "individual-mystery")
        self.assertTrue(any(n.startswith("credit balance:") for n in row["notes"]))

    def test_the_cycle_date_is_not_claimed_as_a_renewal(self):
        row = self.row(self.payloads())
        self.assertIn("cycle ends 2026-10-11", row["notes"])
        self.assertFalse(any("renews" in note for note in row["notes"]))

    def test_the_account_email_never_reaches_the_payload(self):
        row = self.row(self.payloads())
        self.assertNotIn("@", json.dumps(row))
        self.assertTrue(row["sub"].startswith("sample - fp "), row["sub"])

    def test_an_endpoint_failure_is_reported_not_swallowed(self):
        row = self.row(self.payloads(), status_code=500)
        self.assertIn("HTTP 500", row["error"] or "")
        self.assertEqual(row["windows"], [])


class ClaudeSubscriptionProbeTests(unittest.TestCase):
    def setUp(self):
        # Never touch the real ~/.hermes/cache snapshot store.
        self.tmp = tempfile.TemporaryDirectory()
        self.snap = patch.object(probe, "SNAPSHOT_DIR", self.tmp.name)
        self.snap.start()

    def tearDown(self):
        self.snap.stop()
        self.tmp.cleanup()

    class Response:
        def __init__(self, status_code, payload):
            self.status_code = status_code
            self._payload = payload

        def json(self):
            return self._payload

    USAGE = {
        "five_hour": {"utilization": 13.0, "resets_at": "2026-10-03T13:49:59+00:00", "locked_reason": None},
        "seven_day": {"utilization": 2.0, "resets_at": "2026-10-03T13:59:59+00:00"},
        "seven_day_opus": None,
        "seven_day_sonnet": {"utilization": 40.0, "resets_at": "2026-10-05T00:00:00+00:00"},
        "iguana_necktie": {"utilization": 99.0},
        "extra_usage": {"is_enabled": True, "monthly_limit": 5000, "used_credits": 1250,
                        "currency": "usd", "decimal_places": 2, "spend_limit_reached": False},
        "seven_day_breakdown": {"rows": [{"display_name": "Claude Code", "percent": 100},
                                         {"display_name": "Chats", "percent": 0}]},
    }

    def account(self, **extra):
        return {"label": "Claude Code login (keychain)", "token": "sample-oauth-token", "base": "",
                "fp": "deadbeef", "plan": "pro", "tier": "default_claude_ai",
                "expires": (time.time() + 3600) * 1000, **extra}

    def run_probe(self, payload, status_code=200, **extra):
        seen = {}

        def get(url, headers=None, timeout=None):
            seen["url"], seen["headers"] = url, headers
            return self.Response(status_code, payload)

        with patch.object(probe.httpx, "get", get):
            return probe.probe_claude(self.account(**extra)), seen

    def test_windows_map_to_the_shared_row_contract(self):
        row, seen = self.run_probe(self.USAGE)
        self.assertTrue(seen["url"].endswith("/api/oauth/usage"))
        self.assertEqual(seen["headers"]["anthropic-beta"], "oauth-2025-04-20")
        self.assertIsNone(row["error"])
        self.assertEqual(row["plan"], "Pro")
        self.assertEqual([w["k"] for w in row["windows"]],
                         ["session (5h)", "weekly", "weekly sonnet", "extra usage (monthly)"])
        self.assertEqual(row["windows"][0]["pct"], 13.0)
        self.assertEqual(row["windows"][3]["pct"], 25.0)
        self.assertEqual(row["windows"][3]["note"], "12.50 / 50 USD")
        self.assertIn("weekly mix: Claude Code 100%", row["notes"])

    def test_codenamed_and_null_windows_are_never_guessed(self):
        row, _ = self.run_probe(self.USAGE)
        self.assertFalse(any("iguana" in w["k"] or "opus" in w["k"] for w in row["windows"]))

    def test_the_token_never_reaches_the_payload(self):
        row, _ = self.run_probe(self.USAGE)
        self.assertNotIn("sample-oauth-token", json.dumps(row))

    def test_an_expired_token_is_reported_without_a_request_or_refresh(self):
        calls = []
        with patch.object(probe.httpx, "get", lambda *a, **k: calls.append(a)):
            row = probe.probe_claude(self.account(expires=(time.time() - 60) * 1000))
        self.assertEqual(calls, [])
        self.assertIn("expired", row["error"])

    def test_a_rejected_login_is_reported(self):
        row, _ = self.run_probe({}, status_code=401)
        self.assertIn("HTTP 401", row["error"])
        self.assertEqual(row["windows"], [])

    def test_credentials_file_is_read_and_deduplicated(self):
        with tempfile.TemporaryDirectory() as tmp:
            creds = {"claudeAiOauth": {"accessToken": "file-token", "subscriptionType": "max",
                                       "expiresAt": 1}}
            Path(tmp, ".credentials.json").write_text(json.dumps(creds))
            env = {"CLAUDE_CONFIG_DIR": tmp, "HERMES_QUOTA_NO_KEYCHAIN": "1"}
            with patch.dict(probe.os.environ, env, clear=False), \
                    patch.object(probe, "ENV", {}), patch.object(probe, "HOME", tmp):
                probe.os.environ.pop("CLAUDE_CODE_OAUTH_TOKEN", None)
                probe.os.environ.pop("CLAUDE_SUBSCRIPTION_DIRECTSDK_CONFIG_DIR", None)
                accounts = probe.claude_accounts()
        self.assertEqual(len(accounts), 1)
        self.assertEqual(accounts[0]["plan"], "max")
        self.assertEqual(accounts[0]["fp"], probe.sha("file-token")[:12])

    def test_provider_is_registered_with_its_own_account_resolver(self):
        entry = next(p for p in probe.PROVIDERS if p["id"] == "claude-subscription")
        self.assertIs(entry["accounts"], probe.claude_accounts)
        self.assertIs(entry["probe"], probe.probe_claude)

    def test_plugin_exposes_display_settings(self):
        source = (Path(__file__).parent / "plugin.js").read_text(encoding="utf-8")
        self.assertIn("'claude-subscription'", source)
        self.assertIn("'claude-subscription-directsdk-experimental': 'claude-subscription'", source)
        self.assertIn("ctx.storage.get(PREFS_KEY", source)
        self.assertIn("enabledIds(PROVIDER_IDS)", source)

    def test_plugin_surfaces_the_active_account(self):
        source = (Path(__file__).parent / "plugin.js").read_text(encoding="utf-8")
        # The list keeps every credential and flags the live one; the bottom
        # panel is the "which account is in use" summary the dock cannot fit.
        self.assertIn("account.is_active", source)
        self.assertIn("active_account", source)
        self.assertIn("ActiveAccountPanel", source)
        # A credential the pool benches and a pool with nothing servable both
        # have to be visible, not silently rendered as healthy quota.
        self.assertIn("model_benched", source)
        self.assertIn("no credential", source)


class SnapshotAndAntigravityTests(unittest.TestCase):
    class Response:
        def __init__(self, status_code, payload):
            self.status_code = status_code
            self._payload = payload

        def json(self):
            return self._payload

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.patches = [patch.object(probe, "SNAPSHOT_DIR", self.tmp.name),
                        patch.object(probe, "SNAPSHOT_MIN_INTERVAL", 180)]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    def claude_account(self):
        return {"label": "Claude", "token": "sample-oauth-token", "base": "", "fp": "deadbeef0000",
                "plan": "pro", "expires": (time.time() + 3600) * 1000}

    def test_a_429_falls_back_to_the_last_reading_labelled_with_its_age(self):
        good = {"five_hour": {"utilization": 30.0, "resets_at": "2026-10-03T13:49:59+00:00"},
                "seven_day": {"utilization": 4.0}}
        with patch.object(probe.httpx, "get", lambda *a, **k: self.Response(200, good)):
            first = probe.probe_claude(self.claude_account())
        self.assertEqual([w["pct"] for w in first["windows"]], [30.0, 4.0])
        path = probe._snapshot_path("claude-subscription", "deadbeef0000")
        data = json.loads(Path(path).read_text())
        data["at"] -= 600  # stale: forces a live request
        Path(path).write_text(json.dumps(data))
        self.assertNotIn("sample-oauth-token", Path(path).read_text())
        with patch.object(probe.httpx, "get", lambda *a, **k: self.Response(429, {})):
            row = probe.probe_claude(self.claude_account())
        self.assertIsNone(row["error"])
        self.assertEqual([w["pct"] for w in row["windows"]], [30.0, 4.0])
        self.assertTrue(row["notes"][0].startswith("last reading "), row["notes"])
        self.assertIn("HTTP 429", row["notes"][0])

    def test_a_fresh_snapshot_serves_without_a_request(self):
        good = {"five_hour": {"utilization": 30.0}, "seven_day": {"utilization": 4.0}}
        with patch.object(probe.httpx, "get", lambda *a, **k: self.Response(200, good)):
            probe.probe_claude(self.claude_account())
        calls = []
        with patch.object(probe.httpx, "get", lambda *a, **k: calls.append(a)):
            row = probe.probe_claude(self.claude_account())
        self.assertEqual(calls, [])
        self.assertEqual([w["pct"] for w in row["windows"]], [30.0, 4.0])

    def test_a_429_without_any_reading_stays_an_error(self):
        with patch.object(probe.httpx, "get", lambda *a, **k: self.Response(429, {})):
            row = probe.probe_claude(self.claude_account())
        self.assertIn("HTTP 429", row["error"])

    def test_antigravity_meters_each_group_5h_and_weekly(self):
        summary = {"groups": [
            {"displayName": "Gemini Models", "description": "Models within this group: Gemini Flash, Gemini Pro",
             "buckets": [
                 {"bucketId": "gemini-weekly", "window": "weekly", "remainingFraction": 0.9,
                  "resetTime": "2026-10-10T11:25:13Z"},
                 {"bucketId": "gemini-5h", "window": "5h", "remainingFraction": 0.5,
                  "resetTime": "2026-10-03T16:25:13Z"}]},
            {"displayName": "Claude and GPT models", "description": "Models within this group: Claude Opus, GPT-OSS",
             "buckets": [
                 {"bucketId": "3p-weekly", "window": "weekly", "remainingFraction": 0.75},
                 {"bucketId": "3p-5h", "window": "5h", "remainingFraction": 0.2}]},
        ]}
        load = {"paidTier": {"name": "Google AI Pro"}, "cloudaicompanionProject": "p-1"}
        seen = []

        urls = []

        def post(url, headers=None, json=None, timeout=None):
            seen.append(url.rsplit(":", 1)[-1])
            urls.append(url)
            return self.Response(200, load if url.endswith(":loadCodeAssist") else summary)

        account = {"label": "agy", "token": "agy-token", "base": "", "fp": "cafe00000000",
                   "expires": "2099-01-01T00:00:00+00:00"}
        with patch.object(probe.httpx, "post", post):
            row = probe.probe_antigravity(account)
        self.assertEqual(seen, ["loadCodeAssist", "retrieveUserQuotaSummary"])
        # The daily host is what agy itself reads; the plain host reports stale
        # zeros for the Claude/GPT group.
        self.assertTrue(all(u.startswith("https://daily-cloudcode-pa.googleapis.com/") for u in urls), urls)
        self.assertIsNone(row["error"])
        self.assertEqual(row["plan"], "Google AI Pro")
        self.assertEqual([(w["k"], w["group"], w["pct"]) for w in row["windows"]], [
            ("Gemini 5h", "Gemini", 50.0), ("Gemini weekly", "Gemini", 10.0),
            ("Claude/GPT 5h", "Claude/GPT", 80.0), ("Claude/GPT weekly", "Claude/GPT", 25.0)])
        self.assertEqual(row["windows"][0]["note"], "Gemini Flash, Gemini Pro")
        self.assertNotIn("agy-token", json.dumps(row))

    def test_antigravity_falls_back_host_with_a_note(self):
        summary = {"groups": [{"displayName": "Claude and GPT models", "buckets": [
            {"window": "5h", "remainingFraction": 0.1}, {"window": "weekly", "remainingFraction": 0.6}]}]}
        load = {"paidTier": {"name": "Google AI Pro"}}

        def post(url, headers=None, json=None, timeout=None):
            if "daily-" in url:
                raise probe.httpx.ConnectError("daily host down")
            return self.Response(200, load if url.endswith(":loadCodeAssist") else summary)

        account = {"label": "agy", "token": "t", "base": "", "fp": "f00d00000000",
                   "expires": "2099-01-01T00:00:00+00:00"}
        with patch.object(probe.httpx, "post", post):
            row = probe.probe_antigravity(account)
        self.assertIsNone(row["error"])
        self.assertEqual([w["pct"] for w in row["windows"]], [90.0, 40.0])
        self.assertTrue(any(n.startswith("quota host fallback:") for n in row["notes"]), row["notes"])

    def test_antigravity_expired_token_is_never_refreshed(self):
        calls = []
        with patch.object(probe.httpx, "post", lambda *a, **k: calls.append(a)):
            row = probe.probe_antigravity({"label": "agy", "token": "t", "base": "", "fp": "beef00000000",
                                           "expires": "2000-01-01T00:00:00+00:00"})
        self.assertEqual(calls, [])
        self.assertIn("expired", row["error"])

    def test_antigravity_keychain_value_is_decoded(self):
        blob = base64.b64encode(json.dumps({"token": {"access_token": "kc-token",
                                                      "expiry": "2099-01-01T00:00:00Z"}}).encode()).decode()

        class Out:
            returncode = 0
            stdout = "go-keyring-base64:" + blob

        with patch.object(probe.sys, "platform", "darwin"), \
                patch.object(probe.subprocess, "run", lambda *a, **k: Out()), \
                patch.object(probe, "HOME", self.tmp.name), patch.object(probe, "ENV", {}), \
                patch.dict(probe.os.environ, {}, clear=False):
            probe.os.environ.pop("ANTIGRAVITY_CONFIG_DIR", None)
            probe.os.environ.pop("HERMES_QUOTA_NO_KEYCHAIN", None)
            accounts = probe.antigravity_accounts()
        self.assertEqual([a["token"] for a in accounts], ["kc-token"])


class FakePool:
    """Stand-in for ``CredentialPool`` recording what the probe asked of it."""

    def __init__(self, entries, available, pick):
        self._entries = entries
        self._available = available
        self._pick = pick
        self.persisted = False

    def _persist(self, *args, **kwargs):  # must be replaced by the read-only guard
        self.persisted = True

    def _available_entries(self, **kwargs):
        self.asked = kwargs
        return self._available, []

    def _select_unlocked(self, **kwargs):
        self.select_kwargs = kwargs
        return self._pick, []

    def _is_sole_credential(self):
        return len(self._entries) == 1


def fake_entry(entry_id, token, status="ok"):
    return type("E", (), {"id": entry_id, "last_status": status, "runtime_api_key": token})()


class ActiveAccountTests(unittest.TestCase):
    """The active account is what the pool's own selector would serve."""

    def load(self, pool, rows=None):
        """Wire pool_selection to *pool* without touching the real auth store."""
        raw = rows if rows is not None else [{"id": "x"}]
        for target, value in (
            (patch("agent.credential_pool.read_credential_pool", return_value=raw), None),
            (patch("agent.credential_pool.PooledCredential.from_dict",
                   new=lambda provider, payload: payload), None),
            (patch("agent.credential_pool.CredentialPool",
                   new=lambda provider, entries: pool), None),
        ):
            target.start()
            self.addCleanup(target.stop)
        return pool

    def test_pool_less_provider_returns_none(self):
        with patch("agent.credential_pool.read_credential_pool", return_value=[]):
            self.assertIsNone(probe.pool_selection("claude-subscription", None))

    def test_selection_is_read_only(self):
        entries = [fake_entry("a", "tok-a")]
        pool = self.load(FakePool(entries, entries, entries[0]))
        selection = probe.pool_selection("openai-codex", "gpt-6-luna-900k")
        self.assertFalse(pool.persisted, "selection must not write auth.json")
        self.assertFalse(pool.select_kwargs.get("count"), "selection must not bump request_count")
        self.assertFalse(pool.asked.get("refresh"), "selection must not refresh tokens")
        self.assertEqual(selection["_active_fp"], probe.sha("tok-a")[:12])

    def test_pick_comes_from_the_pool_not_from_priority(self):
        # least_used: the pool picked the low-request_count row even though a
        # higher-priority sibling exists — priority ranking would be wrong here.
        rows = [fake_entry("p0", "tok-primary"), fake_entry("p1", "tok-live")]
        pool = self.load(FakePool(rows, rows, rows[1]))
        selection = probe.pool_selection("openai-codex", "gpt-6-luna-900k")
        self.assertEqual(selection["_active_fp"], probe.sha("tok-live")[:12])
        self.assertEqual(selection["state"], "ok")
        self.assertEqual(selection["available"], 2)

    def test_empty_pool_for_a_benched_model_claims_no_active_account(self):
        rows = [fake_entry("a", "tok-a"), fake_entry("b", "tok-b")]
        pool = self.load(FakePool(rows, [], None))  # available=[] -> no pick
        with patch("agent.credential_pool.model_cooldown_until", return_value=1893456000.0):
            selection = probe.pool_selection("openai-codex", "coding-safe")
        self.assertIsNone(selection["_active_fp"])
        self.assertEqual(selection["state"], "empty")
        self.assertEqual({v["verdict"] for v in selection["rows"].values()}, {"model_benched"})
        self.assertTrue(all(v["until"] for v in selection["rows"].values()))

    def test_verdicts_distinguish_dead_and_exhausted(self):
        rows = [fake_entry("a", "tok-a"), fake_entry("d", "tok-d", "dead"), fake_entry("e", "tok-e", "exhausted")]
        pool = self.load(FakePool(rows, rows[:1], rows[0]))
        with patch("agent.credential_pool.model_cooldown_until", return_value=None), \
             patch("agent.credential_pool._exhausted_until", return_value=None):
            selection = probe.pool_selection("openai-codex", "m")
        verdicts = {fp: v["verdict"] for fp, v in selection["rows"].items()}
        self.assertEqual(verdicts[probe.sha("tok-a")[:12]], "available")
        self.assertEqual(verdicts[probe.sha("tok-d")[:12]], "dead")
        self.assertEqual(verdicts[probe.sha("tok-e")[:12]], "exhausted")

    def test_active_account_reports_pool_less_and_empty_pools_differently(self):
        accounts = [{"fp": "aaa", "label": "one"}, {"fp": "bbb", "label": "two"}]
        # No pool rows: the singleton login IS the live credential.
        with patch.object(probe, "pool_selection", return_value=None):
            result = probe.active_fp_for("claude-subscription", accounts, "m")
        self.assertEqual(result["fp"], "aaa")
        self.assertEqual(result["pool"]["state"], "no_pool")
        # Pool rows but nothing servable for this model: name nobody.
        with patch.object(probe, "pool_selection",
                          return_value={"state": "empty", "available": 0, "total": 2,
                                        "strategy": "least_used", "model": "m", "rows": {},
                                        "_active_fp": None}):
            result = probe.active_fp_for("openai-codex", accounts, "m")
        self.assertIsNone(result["fp"])
        self.assertEqual(result["pool"]["state"], "empty")
        self.assertIsNone(probe.active_fp_for("openai-codex", [], "m")["fp"])

    def test_scope_model_prefers_session_then_the_provider_pair(self):
        pairs = {"openai-codex": ["gpt-6-luna-900k", "gpt-image-2-high"], "commandcode": ["deepseek/x"]}
        self.assertEqual(probe.scope_model_for("openai-codex", None, "antigravity", "gemini", pairs),
                         "gpt-6-luna-900k")
        # The session model wins for the provider the session actually runs on.
        self.assertEqual(probe.scope_model_for("antigravity", None, "antigravity", "gemini", pairs), "gemini")
        self.assertEqual(probe.scope_model_for("openai-codex", "coding-safe", "antigravity", "gemini", pairs),
                         "coding-safe")
        self.assertIsNone(probe.scope_model_for("nous", None, None, None, pairs))

    def test_config_pairs_are_collected_from_nested_blocks(self):
        cfg = {"moa": {"reference_models": [{"provider": "openai-codex", "model": "gpt-6-luna-900k"}]},
               "image_gen": {"provider": "openai-codex", "model": "gpt-image-2-high"},
               "model": {"provider": "antigravity"}}
        pairs = probe.configured_model_pairs(cfg)
        self.assertEqual(pairs["openai-codex"], ["gpt-6-luna-900k", "gpt-image-2-high"])
        self.assertNotIn("antigravity", pairs)  # no model in that block

    def test_real_selection_leaves_auth_json_untouched(self):
        """Integration: the real pool selector runs against the real auth.json."""
        path = Path(os.path.expanduser("~/.hermes/auth.json"))
        if not path.exists():
            self.skipTest("no auth.json on this machine")
        before = hashlib.sha256(path.read_bytes()).hexdigest()
        probe.pool_selection("openai-codex", "coding-safe")
        probe.pool_selection("openrouter", None)
        self.assertEqual(before, hashlib.sha256(path.read_bytes()).hexdigest())


if __name__ == "__main__":
    unittest.main()
