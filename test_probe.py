import base64
import contextlib
import hashlib
import io
import json
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

    def test_antigravity_pools_models_by_family(self):
        models = {"models": {
            "claude-opus-4-6-thinking": {"displayName": "Claude Opus 4.6 (Thinking)",
                                         "quotaInfo": {"remainingFraction": 0.6, "resetTime": "2026-10-03T16:16:20Z"}},
            "claude-sonnet-4-6": {"displayName": "Claude Sonnet 4.6 (Thinking)",
                                  "quotaInfo": {"remainingFraction": 0.9}},
            "gemini-3.1-pro-high": {"displayName": "Gemini 3.1 Pro (High)", "quotaInfo": {"remainingFraction": 1}},
            "gemini-3-flash": {"displayName": "Gemini 3 Flash", "quotaInfo": {"remainingFraction": 0.75}},
            "tab_flash_lite_preview": {"quotaInfo": {"remainingFraction": 0}},
        }}
        load = {"paidTier": {"name": "Google AI Pro"}, "cloudaicompanionProject": "p-1"}

        def post(url, headers=None, json=None, timeout=None):
            return self.Response(200, load if url.endswith(":loadCodeAssist") else models)

        account = {"label": "agy", "token": "agy-token", "base": "", "fp": "cafe00000000",
                   "expires": "2099-01-01T00:00:00+00:00"}
        with patch.object(probe.httpx, "post", post):
            row = probe.probe_antigravity(account)
        self.assertIsNone(row["error"])
        self.assertEqual(row["plan"], "Google AI Pro")
        self.assertEqual([(w["k"], w["pct"]) for w in row["windows"]],
                         [("Claude models", 40.0), ("Gemini Pro", 0.0), ("Gemini Flash", 25.0)])
        self.assertNotIn("agy-token", json.dumps(row))

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


if __name__ == "__main__":
    unittest.main()
