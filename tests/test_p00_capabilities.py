"""P00 acceptance: capability states, the execution gate, and the fixture guard.

P00-A1  Reject unsupported capability execution before any external request.
P00-A2  Prototype fixtures cannot enter production outreach.
"""
import inspect
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from franco import capabilities as caps
from franco import dashboard
from franco.benchmark import count_external_connections, run_benchmark
from franco.collectors import discover as D
from franco.config import ConfigError, load_config
from franco.export import collect_prospects
from franco.fixtures import (
    FIXTURE_REASON, PrototypeDataError, is_fixture_address, is_prototype_payload,
)
from franco.gate import pre_send_check
from franco.importer import run_import
from tests.test_outbox_native import (  # noqa: F401  (state is a fixture)
    FakeProvider, make_sender, seed_campaign, seed_prospect, state,
)

WEB = Path(dashboard.__file__).resolve().parent / "web"
PREVIEW_KEYS = [k for k, c in caps.registry().items() if not c.implemented and not c.excluded]
EXCLUDED_KEYS = [k for k, c in caps.registry().items() if c.excluded]


@pytest.fixture
def no_network(monkeypatch):
    """Record (and refuse) every httpx request and every external socket."""
    calls = []

    async def refuse(self, request, *a, **k):
        calls.append(str(request.url))
        raise AssertionError(f"external request attempted: {request.url}")

    monkeypatch.setattr(httpx.AsyncClient, "send", refuse)
    with count_external_connections() as sockets:
        yield calls, sockets


@pytest.fixture
def blank_env(monkeypatch):
    for name in ("DATAFORSEO_LOGIN", "DATAFORSEO_PASSWORD", "SERPER_API_KEY"):
        monkeypatch.setenv(name, "")
    monkeypatch.setattr(D, "load_dotenv", lambda *a, **k: None, raising=False)
    import franco.config as config_module
    monkeypatch.setattr(config_module, "load_dotenv", lambda *a, **k: None)


# ── States ──


def test_every_capability_has_one_of_the_five_states():
    snapshot = caps.snapshot({}, {})
    assert {c["state"] for c in snapshot} <= set(caps.STATES)
    assert len({c["key"] for c in snapshot}) == len(snapshot)
    # Every harness task P01..P12 that is only a blueprint today shows as preview.
    tasks = {c["task"] for c in snapshot if c["state"] == caps.PREVIEW}
    assert tasks == {f"P{i:02d}" for i in range(1, 13)}


def test_state_transitions_from_config_and_local_evidence():
    now = datetime(2026, 10, 20, tzinfo=timezone.utc)
    env = {"smtp_host": "h", "smtp_username": "u", "smtp_password": "p"}
    assert caps.evaluate("email.smtp", {}).state == caps.UNAVAILABLE
    assert caps.evaluate("email.smtp", env).state == caps.CONFIGURED
    recent = {"last_success": {"email.smtp": (now - timedelta(days=2)).isoformat()}}
    assert caps.evaluate("email.smtp", env, recent, now=now).state == caps.LIVE
    old = {"last_success": {"email.smtp": (now - timedelta(days=90)).isoformat()}}
    stale = caps.evaluate("email.smtp", env, old, now=now)
    assert stale.state == caps.CONFIGURED and "stale" in stale.reason
    paused = caps.evaluate("email.smtp", env, {**recent, "sending_paused": "bounce rate"}, now=now)
    assert paused.state == caps.DEGRADED and paused.executable
    failed = caps.evaluate("discovery.osm", {}, {"last_failure": {"discovery.osm": "timeout"}})
    assert failed.state == caps.DEGRADED
    # Gmail needs its OAuth token as well as the client keys.
    gmail = {"gmail_client_id": "i", "gmail_client_secret": "s"}
    assert caps.evaluate("email.gmail", gmail).state == caps.UNAVAILABLE
    assert caps.evaluate("email.gmail", gmail, {"gmail_token": True}).state == caps.CONFIGURED


# ── P00-A1: refused before any external request ──


@pytest.mark.parametrize("key", PREVIEW_KEYS + EXCLUDED_KEYS + ["calls.anything", ""])
def test_unsupported_capabilities_are_never_executable(key):
    full_env = {k: "x" for c in caps.registry().values() for k in c.env_keys}
    status = caps.evaluate(key, full_env, {"gmail_token": True})
    assert not status.executable
    assert status.state in (caps.PREVIEW, caps.UNAVAILABLE)
    with pytest.raises(caps.CapabilityUnavailable):
        caps.require(key, full_env)


@pytest.mark.asyncio
async def test_unconfigured_discovery_makes_no_request_not_even_geocoding(
        state, blank_env, no_network, monkeypatch):
    """Listings providers geocode cities first; that used to happen before
    the configuration check, so an unconfigured run still hit the network."""
    calls, sockets = no_network
    monkeypatch.setattr(D, "geocode_city", D.geocode_city)   # the real one
    config = load_config()
    query = D.DiscoveryQuery(term="roofer", location="Nowhere, ZZ")
    report = await D.run_discovery(state, config, "dataforseo_listings", [query])
    assert report.stopped == "not configured"
    assert "unavailable" in report.errors[0] and "DATAFORSEO_LOGIN" in report.errors[0]
    assert calls == [] and sockets == []


def test_dashboard_refuses_unconfigured_discovery_before_starting(
        tmp_path, blank_env, no_network, monkeypatch):
    calls, sockets = no_network
    monkeypatch.setattr(dashboard, "DB_PATH", tmp_path / "f.db")
    monkeypatch.setattr(dashboard, "ENV_FILE", tmp_path / ".env")
    monkeypatch.setattr(dashboard, "_discovery_task", None)
    client = TestClient(dashboard.app, base_url="http://localhost")
    r = client.post("/api/discover/run", json={"provider": "serper", "cities": ["Denver, CO"]})
    assert r.status_code == 409
    body = r.json()
    assert body["capability"]["state"] == "unavailable" and not body["capability"]["executable"]
    assert dashboard._discovery_task is None          # nothing was started
    assert calls == [] and sockets == []


@pytest.mark.asyncio
async def test_unconfigured_email_providers_make_no_request(state, no_network):
    """Email is gated by each adapter's own configuration check; prove it."""
    from franco.integrations.instantly import InstantlyClient

    calls, sockets = no_network
    assert await InstantlyClient("").create_campaign("x") is None

    class Unconfigured(FakeProvider):
        def is_configured(self):
            return False

    pid = await seed_prospect(state)
    await state.add_outbox_item(
        prospect_id=pid, to_email="jane@acme.com", subject="hello",
        body="Short and fine. Question?", send_at="2000-01-01T00:00:00",
        status="approved", campaign_id="c1", step=1)
    provider = Unconfigured()
    await make_sender(state, provider)._drain_due()
    assert provider.sent == [] and len(await state.get_outbox(status="approved")) == 1
    assert calls == [] and sockets == []


def test_capabilities_api_is_local_only_and_reports_every_state(tmp_path, monkeypatch):
    monkeypatch.setattr(dashboard, "DB_PATH", tmp_path / "f.db")
    client = TestClient(dashboard.app, base_url="http://localhost")
    body = client.get("/api/capabilities").json()
    assert body["states"] == list(caps.STATES) and body["local_only"] is True
    assert body["health_error"] == ""
    by_key = {c["key"]: c for c in body["capabilities"]}
    assert by_key["calls.telephony"]["state"] == "preview"
    assert by_key["dashboard.public"]["state"] == "unavailable"
    assert by_key["discovery.osm"]["executable"] is True
    # Localhost-only defaults are unchanged: a foreign page cannot start a
    # capability, and a foreign Host (DNS rebinding) cannot even read states.
    cross = client.post("/api/discover/run", json={"provider": "osm"},
                        headers={"origin": "https://evil.test"})
    assert cross.status_code == 403
    remote = TestClient(dashboard.app, base_url="http://franco.example.org")
    assert remote.get("/api/capabilities").status_code in (400, 403)
    assert inspect.signature(dashboard.start_dashboard).parameters["host"].default == "127.0.0.1"


# ── P00-A2: fixtures never reach production outreach ──


@pytest.mark.parametrize("address,fixture", [
    ("avery@example.test", True), ("a@bench-01.example", True), ("x@foo.invalid", True),
    ("x@localhost", True), ("REP-A@EXAMPLE.TEST", True), ("x@example.com", True),
    ("x@mail.example.org", True), ("jane@acme.com", False), ("x@examplecompany.com", False),
    ("x@test.com", False), ("x@mytest.io", False), ("", False),
])
def test_reserved_domains_are_fixtures(address, fixture):
    assert is_fixture_address(address) is fixture


def test_pre_send_gate_refuses_fixture_recipients():
    footer = "X, 1 Main St. Reply 'stop' to opt out."
    blocked = pre_send_check("avery@example.test", "Quick question", "Hi. " + footer,
                             required_footer=footer)
    assert not blocked and FIXTURE_REASON in blocked.reasons
    control = pre_send_check("jane@acme.com", "Quick question", "Hi. " + footer,
                             required_footer=footer)
    assert control.ok, control.reasons


@pytest.mark.asyncio
async def test_native_staging_skips_and_dispatch_blocks_fixtures(state):
    real = await seed_prospect(state)
    fake = await seed_prospect(state, email="avery@example.test")
    await seed_campaign(state, [real, fake])
    provider = FakeProvider()
    sender = make_sender(state, provider, require_approval=False)
    await sender._run_native()
    staged = await state.get_outbox()
    assert {i["to_email"] for i in staged} == {"jane@acme.com"}
    assert [m["to"] for m in provider.sent] == ["jane@acme.com"]

    # A fixture row that reached the outbox some other way still never sends.
    await state.add_outbox_item(
        prospect_id=fake, to_email="avery@example.test", subject="hello",
        body="Short and fine. Question?", send_at="2000-01-01T00:00:00",
        status="approved", campaign_id="c2", step=1)
    await sender._drain_due()
    failed = [i for i in await state.get_outbox(status="failed")
              if i["to_email"] == "avery@example.test"]
    assert failed and FIXTURE_REASON in failed[0]["error"]
    assert "avery@example.test" not in [m["to"] for m in provider.sent]


@pytest.mark.asyncio
async def test_instantly_lead_upload_excludes_fixtures(state):
    from franco.agents.sender import Sender

    class FakeInstantly:
        def __init__(self):
            self.leads = []

        async def create_campaign(self, name):
            return {"id": "ic1"}

        async def set_campaign_emails(self, cid, seqs):
            return {}

        async def add_leads(self, cid, leads):
            self.leads.extend(leads)
            return {}

        async def activate_campaign(self, cid):
            return {}

    class Env:
        instantly_api_key = "k"

    class Cfg:
        class channels:
            class email:
                provider = "instantly"; max_daily_sends = 50; send_to_risky = False

    real = await seed_prospect(state)
    fake = await seed_prospect(state, email="morgan@example.test")
    campaign = await seed_campaign(state, [real, fake])
    sender = Sender(brain=None, state=state, config=Cfg(), env=Env())
    sender.instantly = FakeInstantly()
    await sender._deploy_campaign(campaign)
    assert [lead["email"] for lead in sender.instantly.leads] == ["jane@acme.com"]


@pytest.mark.asyncio
async def test_default_export_excludes_fixtures_but_raw_dump_keeps_them(state):
    await seed_prospect(state)
    await seed_prospect(state, email="riley@example.test")
    assert [r["email"] for r in await collect_prospects(state)] == ["jane@acme.com"]
    assert len(await collect_prospects(state, include_all=True)) == 2


def _prototype_export() -> dict:
    """The same shape blueprint.js writes from "Export prototype configuration"."""
    source = (WEB / "blueprint.js").read_text()
    assert "kind:'franco-prototype-configuration'" in source and "simulated:true" in source
    return {"kind": "franco-prototype-configuration", "version": 1, "simulated": True,
            "steps": [], "crm": False, "role": "Sales rep"}


def test_prototype_export_cannot_become_the_configuration(tmp_path):
    path = tmp_path / "franco.yaml"
    path.write_text(json.dumps(_prototype_export()))
    assert is_prototype_payload(_prototype_export())
    with pytest.raises(ConfigError, match="prototype"):
        load_config(str(path))


@pytest.mark.asyncio
async def test_prototype_export_cannot_be_imported(state):
    with pytest.raises(PrototypeDataError):
        await run_import(state, "contacts", json.dumps(_prototype_export(), indent=2))


def test_blueprint_has_no_route_to_production_apis():
    """The blueprint page is simulation only: no network primitive, no API path."""
    js = (WEB / "blueprint.js").read_text()
    html = (WEB / "blueprint.html").read_text()
    for primitive in ("fetch(", "XMLHttpRequest", "sendBeacon", "WebSocket",
                      "EventSource", "import(", "/api/"):
        assert primitive not in js, primitive
    assert "<form" not in html.replace('<form method="dialog">', "")
    assert re.findall(r'src="([^"]+)"', html) == ["/static/blueprint.js"]


# ── The 100-contact benchmark ──


@pytest.mark.asyncio
async def test_benchmark_gates_pass(tmp_path):
    """Correctness, cost and fixture gates are deterministic and must pass.

    Wall-clock gates depend on machine load, so here they are only required
    to be measured; `python -m franco.benchmark` enforces their thresholds.
    """
    result = await run_benchmark(tmp_path)
    assert result["rows"] == 100 and result["unique"] == 90
    timing = [g for g in result["gates"] if g["id"].startswith("time-")]
    failed = [g for g in result["gates"] if not g["passed"] and g not in timing]
    assert not failed, failed
    assert len(timing) == 3 and all(isinstance(g["actual"], float) for g in timing)
