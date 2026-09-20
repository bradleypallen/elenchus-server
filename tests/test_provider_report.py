"""Tests for `provider_report.py`: fetching the provider's usage and cost
figures (off the box), uploading them, and reconciling them with what
the platform measured.

The fetcher is exercised against an `httpx.MockTransport` that answers
in exactly the shapes Anthropic documents for
`GET /v1/organizations/usage_report/messages` and
`GET /v1/organizations/cost_report` — it has **not** been run against
the live Admin API (that needs an Admin key, which never belongs near a
test suite).
"""

from __future__ import annotations

import contextlib
import json
from datetime import date, datetime, timedelta

import httpx
import pytest
from fastapi.testclient import TestClient

from elenchus import auth, costs, pricing, provider_report
from elenchus.db import get_registry
from elenchus.db import platform as pdb
from elenchus.server import app

client = TestClient(app)

WS = "wrkspc_01ELENCHUS"
OTHER_WS = "wrkspc_01SOMEONEELSE"


@pytest.fixture(autouse=True)
def _clean():
    reg = get_registry()
    reg.migrate_platform()
    con = reg.platform_con()
    with reg.platform_lock:
        for table in (
            "provider_usage_daily",
            "provider_report_imports",
            "cost_entries",
            "cost_recurring",
            "usage",
            "auth_sessions",
            "actors",
        ):
            con.execute(f"DELETE FROM {table}")
    for _name, handle in list(reg._handles.items()):
        with contextlib.suppress(Exception):
            handle.state.base.con.close()
    reg._handles.clear()
    client.cookies.clear()
    pricing._reset_cache_for_tests()
    yield
    client.cookies.clear()


def _con():
    return get_registry().platform_con()


def _login(kind: str = "admin") -> int:
    actor_id = pdb.create_actor(
        _con(),
        kind=kind,
        email=f"{kind}@example.com",
        display_name=kind,
        password_hash=auth.hash_password("pw"),
    )
    client.cookies.set(auth.SESSION_COOKIE, auth.create_session(actor_id))
    return actor_id


# ─── The fetcher ─────────────────────────────────────────────────────


def _usage_result(model, workspace, *, uncached=0, read=0, c5m=0, c1h=0, out=0):
    return {
        "account_id": None,
        "api_key_id": None,
        "cache_creation": {"ephemeral_1h_input_tokens": c1h, "ephemeral_5m_input_tokens": c5m},
        "cache_read_input_tokens": read,
        "context_window": None,
        "inference_geo": None,
        "model": model,
        "output_tokens": out,
        "server_tool_use": {"web_search_requests": 0},
        "service_account_id": None,
        "service_tier": None,
        "uncached_input_tokens": uncached,
        "workspace_id": workspace,
    }


def _cost_result(
    model, workspace, cents, *, token_type="uncached_input_tokens", cost_type="tokens"
):
    return {
        "amount": cents,
        "context_window": "0-200k" if model else None,
        "cost_type": cost_type,
        "currency": "USD",
        "description": f"{model or 'Web Search'} Usage",
        "inference_geo": None,
        "model": model,
        "service_tier": "standard" if model else None,
        "token_type": token_type if model else None,
        "workspace_id": workspace,
    }


def _bucket(day: str, results: list[dict]) -> dict:
    nxt = (date.fromisoformat(day) + timedelta(days=1)).isoformat()
    return {"starting_at": f"{day}T00:00:00Z", "ending_at": f"{nxt}T00:00:00Z", "results": results}


class _FakeAdminAPI:
    """Two pages per endpoint, so pagination is exercised."""

    def __init__(self):
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.headers.get("x-api-key") != "sk-ant-admin01-test":
            return httpx.Response(
                401, json={"type": "error", "error": {"message": "invalid x-api-key"}}
            )
        page = request.url.params.get("page")
        if request.url.path == provider_report.USAGE_PATH:
            if page is None:
                data = [
                    _bucket(
                        "2026-10-01",
                        [
                            _usage_result("claude-opus-4-8", WS, uncached=1000, out=100),
                            _usage_result("claude-opus-4-8", OTHER_WS, uncached=9999, out=999),
                        ],
                    ),
                    _bucket("2026-10-02", []),
                ]
                return httpx.Response(
                    200, json={"data": data, "has_more": True, "next_page": "page_2"}
                )
            data = [
                _bucket(
                    "2026-10-03",
                    [
                        _usage_result(
                            "claude-opus-4-8", WS, uncached=500, read=200, c5m=50, c1h=25, out=70
                        ),
                        _usage_result("claude-haiku-4-5-20251001", None, uncached=40, out=4),
                    ],
                )
            ]
            return httpx.Response(200, json={"data": data, "has_more": False, "next_page": None})
        if request.url.path == provider_report.COST_PATH:
            if page is None:
                data = [
                    _bucket(
                        "2026-10-01",
                        [
                            _cost_result("claude-opus-4-8", WS, "0.5"),
                            _cost_result(
                                "claude-opus-4-8", WS, "0.25", token_type="output_tokens"
                            ),
                            _cost_result("claude-opus-4-8", OTHER_WS, "777.0"),
                        ],
                    )
                ]
                return httpx.Response(
                    200, json={"data": data, "has_more": True, "next_page": "page_2"}
                )
            data = [
                _bucket(
                    "2026-10-03",
                    [
                        _cost_result("claude-opus-4-8", WS, "123.45"),
                        _cost_result(None, WS, "10.0", cost_type="web_search"),
                    ],
                )
            ]
            return httpx.Response(200, json={"data": data, "has_more": False, "next_page": None})
        return httpx.Response(404, json={"error": {"message": "not found"}})


def _fetch(api: _FakeAdminAPI, **kwargs) -> dict:
    return provider_report.fetch_anthropic(
        admin_key=kwargs.pop("admin_key", "sk-ant-admin01-test"),
        start=date(2026, 10, 1),
        end=date(2026, 10, 3),
        transport=httpx.MockTransport(api),
        **kwargs,
    )


class TestFetch:
    def test_request_shape_follows_the_documented_api(self):
        api = _FakeAdminAPI()
        _fetch(api)
        usage = next(r for r in api.requests if r.url.path == provider_report.USAGE_PATH)
        assert usage.method == "GET"
        assert usage.url.host == "api.anthropic.com"
        assert usage.headers["anthropic-version"] == "2023-06-01"
        assert usage.headers["x-api-key"] == "sk-ant-admin01-test"
        q = usage.url.params
        assert q["starting_at"] == "2026-10-01T00:00:00Z"
        # `--to` is inclusive; the API's `ending_at` is the bucket boundary after it.
        assert q["ending_at"] == "2026-10-04T00:00:00Z"
        assert q["bucket_width"] == "1d" and q["limit"] == "31"
        assert q.get_list("group_by[]") == ["model", "workspace_id"]
        cost = next(r for r in api.requests if r.url.path == provider_report.COST_PATH)
        assert cost.url.params.get_list("group_by[]") == ["description", "workspace_id"]
        assert "bucket_width" not in cost.url.params  # daily is the only granularity

    def test_follows_pagination(self):
        api = _FakeAdminAPI()
        _fetch(api)
        pages = [(r.url.path, r.url.params.get("page")) for r in api.requests]
        assert pages == [
            (provider_report.USAGE_PATH, None),
            (provider_report.USAGE_PATH, "page_2"),
            (provider_report.COST_PATH, None),
            (provider_report.COST_PATH, "page_2"),
        ]

    def test_report_contents_whole_organization(self):
        report = _fetch(_FakeAdminAPI())
        assert report["format"] == provider_report.FORMAT
        assert report["period"] == {"from": "2026-10-01", "to": "2026-10-03"}
        assert report["scope"]["usage"] == "organization"
        day1 = next(u for u in report["usage"] if u["day"] == "2026-10-01")
        assert day1["uncached_input_tokens"] == 1000 + 9999  # both workspaces
        total = sum(c["amount_usd"] for c in report["costs"])
        # cents → dollars: 0.5 + 0.25 + 777.0 + 123.45 + 10.0 cents
        assert total == pytest.approx(9.112)

    def test_workspace_filter_is_applied_to_both_reports(self):
        report = _fetch(_FakeAdminAPI(), workspace_ids=[WS])
        assert report["scope"] == {
            "workspace_ids": [WS],
            "api_key_ids": [],
            "usage": "workspaces",
            "cost": "workspaces",
        }
        day1 = next(u for u in report["usage"] if u["day"] == "2026-10-01")
        assert (day1["uncached_input_tokens"], day1["output_tokens"]) == (1000, 100)
        day3 = next(
            u for u in report["usage"] if u["day"] == "2026-10-03" and "opus" in u["model"]
        )
        assert day3["cache_read_input_tokens"] == 200
        assert day3["cache_creation_input_tokens"] == 75  # 5-minute + 1-hour
        assert not any("haiku" in u["model"] for u in report["usage"])  # default workspace
        assert sum(c["amount_usd"] for c in report["costs"]) == pytest.approx(1.342)
        web = next(c for c in report["costs"] if c["cost_type"] == "web_search")
        assert web["model"] == "" and web["amount_usd"] == pytest.approx(0.10)

    def test_the_default_workspace_has_a_name(self):
        report = _fetch(_FakeAdminAPI(), workspace_ids=["default"])
        assert [u["model"] for u in report["usage"]] == ["claude-haiku-4-5-20251001"]

    def test_api_key_filter_narrows_usage_and_says_costs_are_wider(self):
        api = _FakeAdminAPI()
        report = _fetch(api, api_key_ids=["apikey_01A", "apikey_01B"])
        usage = next(r for r in api.requests if r.url.path == provider_report.USAGE_PATH)
        assert usage.url.params.get_list("api_key_ids[]") == ["apikey_01A", "apikey_01B"]
        cost = next(r for r in api.requests if r.url.path == provider_report.COST_PATH)
        assert "api_key_ids[]" not in cost.url.params  # the cost report has no such filter
        assert report["scope"]["usage"] == "api_keys" and report["scope"]["cost"] == "organization"

    def test_the_file_never_holds_the_key(self):
        assert "sk-ant" not in json.dumps(_fetch(_FakeAdminAPI()))

    def test_a_refused_key_explains_itself(self):
        with pytest.raises(provider_report.ProviderReportError) as e:
            _fetch(_FakeAdminAPI(), admin_key="sk-ant-api03-an-ordinary-key")
        message = str(e.value)
        assert "Admin API key" in message and "individual accounts" in message
        assert "sk-ant-api03" not in message  # never echo a credential

    def test_needs_a_key_and_a_sane_period(self):
        with pytest.raises(provider_report.ProviderReportError):
            _fetch(_FakeAdminAPI(), admin_key="")
        with pytest.raises(provider_report.ProviderReportError):
            provider_report.fetch_anthropic(
                admin_key="k", start=date(2026, 10, 3), end=date(2026, 10, 1)
            )


class TestCommandLine:
    def test_writes_the_file(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setenv(provider_report.ADMIN_KEY_ENV, "sk-ant-admin01-test")
        monkeypatch.setattr(provider_report, "_utc_today", lambda: date(2026, 11, 2))
        api = _FakeAdminAPI()
        real = provider_report.fetch_anthropic
        monkeypatch.setattr(
            provider_report,
            "fetch_anthropic",
            lambda **kw: real(**kw, transport=httpx.MockTransport(api)),
        )
        out = tmp_path / "anthropic-2026-10.json"
        code = provider_report.main(
            ["--from", "2026-10-01", "--to", "2026-10-03", "--workspace-id", WS, "--out", str(out)]
        )
        assert code == 0
        report = json.loads(out.read_text())
        assert (
            report["format"] == provider_report.FORMAT and report["scope"]["usage"] == "workspaces"
        )
        assert "Costs tab" in capsys.readouterr().out

    def test_a_month_in_progress_stops_at_today(self, tmp_path, monkeypatch):
        monkeypatch.setenv(provider_report.ADMIN_KEY_ENV, "sk-ant-admin01-test")
        monkeypatch.setattr(provider_report, "_utc_today", lambda: date(2026, 10, 2))
        seen = {}
        monkeypatch.setattr(
            provider_report,
            "fetch_anthropic",
            lambda **kw: (
                seen.update(kw)
                or {
                    "period": {"from": "", "to": ""},
                    "costs": [],
                    "usage": [],
                    "scope": {"usage": "", "cost": ""},
                }
            ),
        )
        assert provider_report.main(["--month", "2026-10", "--out", str(tmp_path / "o.json")]) == 0
        assert (seen["start"], seen["end"]) == (date(2026, 10, 1), date(2026, 10, 2))

    def test_a_period_that_has_not_started_is_refused(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setenv(provider_report.ADMIN_KEY_ENV, "sk-ant-admin01-test")
        monkeypatch.setattr(provider_report, "_utc_today", lambda: date(2026, 9, 19))
        assert provider_report.main(["--month", "2026-10", "--out", str(tmp_path / "o.json")]) == 2
        assert "hasn't started" in capsys.readouterr().err

    def test_no_key_is_a_clean_error(self, tmp_path, monkeypatch, capsys):
        monkeypatch.delenv(provider_report.ADMIN_KEY_ENV, raising=False)
        code = provider_report.main(["--month", "2026-08", "--out", str(tmp_path / "x.json")])
        assert code == 2
        assert provider_report.ADMIN_KEY_ENV in capsys.readouterr().err
        assert not (tmp_path / "x.json").exists()

    def test_the_fetcher_opens_no_database(self):
        """It runs on an admin's laptop: importing it must not pull in the
        server (which creates a data directory and opens the platform DB)."""
        import subprocess
        import sys

        code = (
            "import sys; import elenchus.provider_report; "
            "assert 'elenchus.server' not in sys.modules; "
            "assert 'elenchus.db.registry' not in sys.modules; print('ok')"
        )
        result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
        assert result.returncode == 0, result.stderr


# ─── Upload + reconciliation ─────────────────────────────────────────


def _report(**overrides) -> dict:
    base = {
        "format": provider_report.FORMAT,
        "provider": "anthropic",
        "fetched_at": "2026-11-02T09:00:00Z",
        "period": {"from": "2026-10-01", "to": "2026-10-31"},
        "scope": {
            "workspace_ids": [WS],
            "api_key_ids": [],
            "usage": "workspaces",
            "cost": "workspaces",
        },
        "usage": [
            {
                "day": "2026-10-05",
                "model": "claude-opus-4-8",
                "uncached_input_tokens": 2_000_000,
                "cache_read_input_tokens": 0,
                "cache_creation_input_tokens": 0,
                "output_tokens": 100_000,
            }
        ],
        "costs": [
            {
                "day": "2026-10-05",
                "model": "claude-opus-4-8",
                "cost_type": "tokens",
                "amount_usd": 12.5,
            }
        ],
    }
    return {**base, **overrides}


def _platform_usage(day: str, *, prompt: int, completion: int, model="claude-opus-4-8") -> None:
    con = _con()
    rid = pdb.record_usage(
        con,
        actor_id=None,
        base_id=None,
        model=model,
        category="success",
        prompt_tokens=prompt,
        completion_tokens=completion,
        cost_usd=0.0,
        attempts=1,
        latency_ms=1,
        purpose="dialectic_turn",
    )
    con.execute(
        "UPDATE usage SET occurred_at = ? WHERE id = ?",
        [datetime.fromisoformat(day) + timedelta(hours=12), rid],
    )


def _reconciliation() -> list[dict]:
    today = date(2026, 11, 2)
    return costs.build_report(_con(), today=today)["infrastructure"]["reconciliation"]


class TestUpload:
    def test_admin_only(self):
        assert client.post("/api/admin/costs/provider-report", json=_report()).status_code == 401
        _login("researcher")
        assert client.post("/api/admin/costs/provider-report", json=_report()).status_code == 403

    def test_upload_and_list(self):
        admin = _login()
        r = client.post("/api/admin/costs/provider-report", json=_report())
        assert r.status_code == 200, r.text
        assert r.json()["rows"] == 1 and r.json()["total_cost_usd"] == 12.5
        (imp,) = client.get("/api/admin/costs/ledger").json()["provider_imports"]
        assert imp["imported_by"] == admin and imp["period_to"] == "2026-10-31"
        assert imp["scope"]["workspace_ids"] == [WS]

    def test_a_fresher_report_replaces_the_days_it_covers(self):
        _login()
        client.post("/api/admin/costs/provider-report", json=_report())
        fresher = _report(costs=[{**_report()["costs"][0], "amount_usd": 13.0}])
        r = client.post("/api/admin/costs/provider-report", json=fresher)
        assert r.json()["rows_replaced"] == 1
        total = (
            _con().execute("SELECT SUM(cost_usd), COUNT(*) FROM provider_usage_daily").fetchone()
        )
        assert total == (13.0, 1)
        assert len(client.get("/api/admin/costs/ledger").json()["provider_imports"]) == 2

    @pytest.mark.parametrize(
        "bad",
        [
            {"format": "something-else"},
            {"provider": ""},
            {"period": {"from": "2026-10-31", "to": "2026-10-01"}},
            {"usage": [{"day": "2026-12-25", "model": "m"}]},  # outside the period
            {"usage": [{"day": "2026-10-05", "model": "m", "output_tokens": -5}]},
            {"costs": [{"day": "2026-10-05", "amount_usd": "lots"}]},
            {"usage": "not a list"},
        ],
    )
    def test_rejects_a_bad_file_with_a_message(self, bad):
        _login()
        r = client.post("/api/admin/costs/provider-report", json=_report(**bad))
        assert r.status_code == 422, r.text
        assert r.json()["detail"]["user_message"]
        assert _con().execute("SELECT COUNT(*) FROM provider_usage_daily").fetchone()[0] == 0

    def test_upload_is_logged(self, caplog):
        _login()
        with caplog.at_level("INFO", logger="elenchus.provider_report"):
            client.post("/api/admin/costs/provider-report", json=_report())
        assert any("Provider report imported" in r.message for r in caplog.records)


class TestReconciliation:
    def test_books_that_agree(self):
        _login()
        _platform_usage("2026-10-05", prompt=2_000_000, completion=100_000)  # $10 + $2.50
        client.post("/api/admin/costs/provider-report", json=_report())
        (month,) = _reconciliation()
        assert month["month"] == "2026-10" and month["source"] == "imported"
        assert (month["provider_usd"], month["computed_usd"]) == (12.5, 12.5)
        assert month["days_covered"] == 31 and month["days_in_month"] == 31
        (model,) = month["models"]
        assert model["model"] == "claude-opus-4-8" and model["verdict"] == "agrees"

    def test_a_stale_rate_shows_as_tokens_agree_dollars_dont(self, monkeypatch):
        monkeypatch.setenv(
            "ELENCHUS_PRICING_JSON",
            '{"claude-opus-4-8": {"input_per_1m": 15, "output_per_1m": 75}}',
        )
        pricing._reset_cache_for_tests()
        _login()
        _platform_usage("2026-10-05", prompt=2_000_000, completion=100_000)
        client.post("/api/admin/costs/provider-report", json=_report())
        (month,) = _reconciliation()
        assert month["computed_usd"] == 37.5 and month["provider_usd"] == 12.5
        assert month["models"][0]["verdict"] == "rates_differ"
        assert "price table" in month["models"][0]["verdict_text"]

    def test_usage_from_outside_the_platform(self):
        _login()
        _platform_usage("2026-10-05", prompt=1_000_000, completion=50_000)  # half of it
        client.post("/api/admin/costs/provider-report", json=_report())
        assert _reconciliation()[0]["models"][0]["verdict"] == "provider_saw_more"

    def test_a_report_that_misses_something(self):
        _login()
        _platform_usage("2026-10-05", prompt=4_000_000, completion=200_000)
        client.post("/api/admin/costs/provider-report", json=_report())
        assert _reconciliation()[0]["models"][0]["verdict"] == "platform_recorded_more"

    def test_dated_model_ids_match_their_alias(self):
        _login()
        _platform_usage(
            "2026-10-05", prompt=1000, completion=100, model="claude-haiku-4-5-20251001"
        )
        report = _report(
            usage=[
                {
                    **_report()["usage"][0],
                    "model": "claude-haiku-4-5",
                    "uncached_input_tokens": 1000,
                    "output_tokens": 100,
                }
            ],
            costs=[
                {
                    "day": "2026-10-05",
                    "model": "claude-haiku-4-5",
                    "cost_type": "tokens",
                    "amount_usd": 0.0015,
                }
            ],
        )
        client.post("/api/admin/costs/provider-report", json=report)
        (model,) = _reconciliation()[0]["models"]
        assert model["model"] == "claude-haiku-4-5" and model["verdict"] == "agrees"

    def test_a_version_is_never_mistaken_for_a_date(self):
        assert provider_report.canonical_model("claude-opus-4-8") == "claude-opus-4-8"
        assert (
            provider_report.canonical_model("anthropic/claude-sonnet-4.6") == "claude-sonnet-4-6"
        )

    def test_only_the_covered_days_are_compared(self):
        """A report for Oct 1–10 is set beside the platform's Oct 1–10 —
        not the whole month, which would always look like a shortfall."""
        _login()
        _platform_usage("2026-10-05", prompt=2_000_000, completion=100_000)
        _platform_usage("2026-10-20", prompt=9_000_000, completion=900_000)  # not covered
        partial = _report(period={"from": "2026-10-01", "to": "2026-10-10"})
        client.post("/api/admin/costs/provider-report", json=partial)
        (month,) = _reconciliation()
        assert month["days_covered"] == 10
        assert month["computed_usd"] == 12.5 and month["models"][0]["verdict"] == "agrees"

    def test_non_token_costs_and_cache_tokens_are_surfaced(self):
        _login()
        _platform_usage("2026-10-05", prompt=2_000_000, completion=100_000)
        report = _report(
            usage=[
                {
                    **_report()["usage"][0],
                    "uncached_input_tokens": 1_500_000,
                    "cache_read_input_tokens": 500_000,
                }
            ],
            costs=[
                *_report()["costs"],
                {"day": "2026-10-06", "model": "", "cost_type": "web_search", "amount_usd": 0.4},
            ],
        )
        client.post("/api/admin/costs/provider-report", json=report)
        (month,) = _reconciliation()
        assert month["non_token_usd"] == 0.4 and month["provider_usd"] == 12.9
        assert month["cache_tokens"] == 500_000
        # Total input still matches what the platform sent, so the tokens agree.
        assert month["models"][0]["provider_input_tokens"] == 2_000_000

    def test_an_imported_month_wins_over_a_typed_figure(self):
        _login()
        _platform_usage("2026-10-05", prompt=2_000_000, completion=100_000)
        typed = {
            "incurred_on": "2026-11-01",
            "category": "llm_provider",
            "vendor": "Anthropic",
            "amount": 11.0,
            "covers_month": "2026-10",
        }
        assert client.post("/api/admin/costs/ledger", json=typed).status_code == 200
        assert (
            client.post(
                "/api/admin/costs/ledger", json={**typed, "amount": 3.0, "covers_month": "2026-09"}
            ).status_code
            == 200
        )
        client.post("/api/admin/costs/provider-report", json=_report())
        october, september = _reconciliation()
        assert (october["source"], october["provider_usd"], october["entered_usd"]) == (
            "imported",
            12.5,
            11.0,
        )
        assert (september["source"], september["provider_usd"]) == ("entered", 3.0)
        assert september["models"] == []
