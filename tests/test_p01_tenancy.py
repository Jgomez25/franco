"""P01-A1: cross-tenant IDs and rep attempts to change credentials are denied.

Shared mode is exercised end to end through the dashboard's HTTP layer
against a temporary database. Local single-user mode is tested separately
at the bottom (and by every other dashboard test, which runs in it).
"""
import sqlite3

import httpx
import pytest
import pytest_asyncio
from fastapi.testclient import TestClient

from franco import dashboard
from franco import tenancy as T
from franco.state import StateManager
from tests.test_outbox_native import seed_prospect


@pytest_asyncio.fixture
async def world(tmp_path, monkeypatch):
    """Two customer workspaces plus the installation workspace."""
    db = tmp_path / "franco.db"
    monkeypatch.setattr(dashboard, "DB_PATH", db)
    monkeypatch.setattr(dashboard, "ENV_FILE", tmp_path / ".env")
    state = StateManager(str(db))
    await state.init_db()
    store = T.TenancyStore(state)
    for ws in ("acme", "globex"):
        await store.create_workspace(ws)
    tokens = {}
    for name, ws, role in (("rep_a", "acme", "rep"), ("mgr_a", "acme", "manager"),
                           ("admin_b", "globex", "admin"), ("owner", "local", "admin")):
        uid = await store.add_user(f"{name}@x.co", ws, role)
        tokens[name] = await store.issue_token(uid, ws)

    items = {}
    for ws in ("acme", "globex"):
        pid = await seed_prospect(state, email=f"lead@{ws}.com")
        items[ws] = await state.add_outbox_item(
            prospect_id=pid, to_email=f"lead@{ws}.com", subject="hi", body="b",
            send_at="2000-01-01", status="pending_review", campaign_id=ws, step=1)
    with sqlite3.connect(db) as conn:
        for ws, item in items.items():
            conn.execute("UPDATE outbox SET workspace_id = ? WHERE id = ?", (ws, item))
            conn.execute("UPDATE prospects SET workspace_id = ? WHERE email = ?",
                         (ws, f"lead@{ws}.com"))
            conn.execute("INSERT INTO companies (id, name, domain, workspace_id) "
                         "VALUES (?, ?, ?, ?)", (f"co-{ws}", ws.title(), f"{ws}.com", ws))
    monkeypatch.setenv("FRANCO_MODE", "shared")
    client = TestClient(dashboard.app, base_url="http://localhost")
    return {"client": client, "tokens": tokens, "items": items, "state": state, "db": db}


def as_(world, who):
    return {"Authorization": f"Bearer {world['tokens'][who]}"}


def audit_rows(db, ws):
    with sqlite3.connect(db) as conn:
        return conn.execute("SELECT actor_user_id, action, outcome, detail_json FROM "
                            "audit_events WHERE workspace_id = ?", (ws,)).fetchall()


# ── Authentication ──


def test_shared_mode_requires_a_live_token(world):
    c = world["client"]
    assert c.get("/api/outbox").status_code == 401
    assert c.get("/api/outbox", headers={"Authorization": "Bearer fr_wrong"}).status_code == 401
    assert c.get("/api/outbox", headers=as_(world, "rep_a")).status_code == 200


@pytest.mark.asyncio
async def test_revoked_tokens_and_disabled_users_are_refused(world):
    store = T.TenancyStore(world["state"])
    members = {m["email"]: m for m in await store.members()}
    await store.revoke_tokens(members["rep_a@x.co"]["user_id"])
    assert world["client"].get("/api/outbox", headers=as_(world, "rep_a")).status_code == 401
    with sqlite3.connect(world["db"]) as conn:
        conn.execute("UPDATE users SET disabled_at = CURRENT_TIMESTAMP WHERE email = ?",
                     ("mgr_a@x.co",))
    assert world["client"].get("/api/outbox", headers=as_(world, "mgr_a")).status_code == 401


def test_tokens_are_stored_only_as_hashes(world):
    with sqlite3.connect(world["db"]) as conn:
        dump = "\n".join(conn.iterdump())
    for token in world["tokens"].values():
        assert token not in dump and T.hash_token(token) in dump


# ── P01-A1: cross-tenant IDs are denied ──


def test_cross_tenant_ids_are_denied_and_audited(world):
    c, items = world["client"], world["items"]
    r = c.post(f"/api/outbox/{items['globex']}/approve", headers=as_(world, "rep_a"))
    assert r.status_code == 404                      # indistinguishable from missing
    for action in ("reject", "edit", "resolve"):
        assert c.post(f"/api/outbox/{items['globex']}/{action}", json={},
                      headers=as_(world, "rep_a")).status_code == 404
    assert c.get("/api/companies/co-globex/contacts",
                 headers=as_(world, "rep_a")).status_code == 404
    with sqlite3.connect(world["db"]) as conn:
        status = conn.execute("SELECT status FROM outbox WHERE id = ?",
                              (items["globex"],)).fetchone()[0]
    assert status == "pending_review"                # untouched
    denied = [r for r in audit_rows(world["db"], "acme") if r[2] == "denied"]
    assert len(denied) == 5
    assert all(items["globex"] in r[1] or "co-globex" in r[1] for r in denied)
    assert all(r[0] for r in denied)                 # the actor is recorded
    assert audit_rows(world["db"], "globex") == []   # nothing written into globex


def test_own_workspace_ids_work_and_lists_are_scoped(world):
    c, items = world["client"], world["items"]
    assert c.post(f"/api/outbox/{items['acme']}/approve",
                  headers=as_(world, "rep_a")).json()["success"]
    outbox = c.get("/api/outbox", headers=as_(world, "rep_a")).json()
    every = [r for key in ("pending", "approved", "sent", "failed", "needs_check", "sending")
             for r in outbox[key]]
    assert {r["to_email"] for r in every} == {"lead@acme.com"}
    contacts = c.get("/api/contacts/search", headers=as_(world, "rep_a")).json()
    assert {p["email"] for p in contacts["items"]} == {"lead@acme.com"}
    companies = c.get("/api/companies", headers=as_(world, "admin_b")).json()
    assert [co["id"] for co in companies] == ["co-globex"]
    allowed = [r for r in audit_rows(world["db"], "acme") if r[2] == "allowed"]
    assert len(allowed) == 1 and allowed[0][1].endswith("/approve")


def test_bulk_approval_needs_a_manager_and_stays_in_the_workspace(world):
    c, items = world["client"], world["items"]
    assert c.post("/api/outbox/approve-all", headers=as_(world, "rep_a")).status_code == 403
    r = c.post("/api/outbox/approve-all", headers=as_(world, "mgr_a")).json()
    assert r["approved"] == 1
    with sqlite3.connect(world["db"]) as conn:
        statuses = dict(conn.execute("SELECT workspace_id, status FROM outbox"))
    assert statuses == {"acme": "approved", "globex": "pending_review"}


# ── P01-A1: reps (and other workspaces' admins) cannot change credentials ──


@pytest.mark.parametrize("who", ["rep_a", "mgr_a", "admin_b"])
def test_credential_changes_are_denied_without_installation_admin(world, who, monkeypatch):
    calls = []

    async def refuse(self, request, *a, **k):
        calls.append(str(request.url))
        raise AssertionError("no external request expected")

    monkeypatch.setattr(httpx.AsyncClient, "send", refuse)
    c, h = world["client"], as_(world, who)
    assert c.post("/api/settings/env", json={"SMTP_PASSWORD": "x"}, headers=h).status_code == 403
    assert c.post("/api/settings/test-instantly", json={"api_key": "k"},
                  headers=h).status_code == 403
    assert c.get("/api/settings", headers=h).status_code == 403
    assert c.get("/api/secrets", headers=h).status_code == 403
    assert c.post("/api/secrets/SMTP_PASSWORD/revoke", headers=h).status_code == 403
    assert calls == []
    assert not (dashboard.ENV_FILE).exists()         # nothing written
    ws = {"rep_a": "acme", "mgr_a": "acme", "admin_b": "globex"}[who]
    assert sum(1 for r in audit_rows(world["db"], ws) if r[2] == "denied") == 5


def test_installation_admin_can_change_credentials(world):
    r = world["client"].post("/api/settings/env", json={"SMTP_HOST": "smtp.x.co"},
                             headers=as_(world, "owner"))
    assert r.status_code == 200 and r.json()["success"]
    assert "SMTP_HOST=smtp.x.co" in dashboard.ENV_FILE.read_text()


# ── The allowlist is complete and current ──


def _api_routes():
    for route in dashboard.app.routes:
        path = getattr(route, "path", "")
        if path.startswith("/api/"):
            for method in route.methods - {"HEAD"}:
                yield method, path


def test_every_api_route_not_made_tenant_safe_is_refused_in_shared_mode(world):
    c = world["client"]
    unlisted = []
    for method, path in _api_routes():
        concrete = (path.replace("{item_id}", "x").replace("{company_id}", "x")
                    .replace("{name}", "x").replace("{key}", "x").replace("{action}", "x")
                    .replace("{kind}", "x").replace("{entity_type}", "x")
                    .replace("{entity_id}", "x"))
        if T.match(method, concrete)[0] is None:
            unlisted.append((method, path))
            r = c.request(method, concrete, json={}, headers=as_(world, "owner"))
            assert r.status_code == 403, (method, path, r.status_code)
    # Spot-check the riskiest ones are in that list.
    assert ("POST", "/api/franco/start") in unlisted
    assert ("POST", "/api/import") in unlisted
    assert ("POST", "/api/discover/run") in unlisted


def test_every_allowlisted_route_exists():
    actual = {(m, p) for m, p in _api_routes()}
    for route in T.SHARED_ROUTES:
        assert any(m == route.method and route.pattern.match(
            p.replace("{item_id}", "abc").replace("{company_id}", "abc")
             .replace("{name}", "abc").replace("{action}", "approve"))
            for m, p in actual), route.pattern.pattern


def test_audit_is_readable_by_managers_for_their_workspace_only(world):
    c, items = world["client"], world["items"]
    c.post(f"/api/outbox/{items['globex']}/approve", headers=as_(world, "rep_a"))
    assert c.get("/api/audit", headers=as_(world, "rep_a")).status_code == 403
    events = c.get("/api/audit", headers=as_(world, "mgr_a")).json()["events"]
    assert events and {e["workspace_id"] for e in events} == {"acme"}
    for token in world["tokens"].values():
        assert token not in str(events)


def test_roles_and_permissions_are_server_side():
    rep = T.Principal("u", "acme", "rep")
    assert rep.holds("read") and rep.holds("outbox.review")
    assert not rep.holds("outbox.bulk") and not rep.holds("credentials.manage")
    assert not T.Principal("u", "globex", "admin").holds("credentials.manage")
    assert T.Principal("u", "local", "admin").holds("credentials.manage")
    assert not T.Principal("u", "acme", "intruder").holds("read")


# ── Local single-user mode, separately ──


def test_local_mode_needs_no_token_and_is_the_local_admin(world, monkeypatch):
    monkeypatch.delenv("FRANCO_MODE")
    c = world["client"]
    me = c.get("/api/me").json()
    assert me == {"mode": "local", "user_id": "local-operator",
                  "email": "operator@localhost", "workspace_id": "local", "role": "admin"}
    # Local mode keeps every route and sees everything, as before.
    outbox = c.get("/api/outbox").json()
    assert {r["to_email"] for r in outbox["pending"]} == {"lead@acme.com", "lead@globex.com"}
    assert c.post("/api/settings/env", json={"SMTP_HOST": "h"}).json()["success"]
    # The localhost guard still applies in local mode.
    assert c.post("/api/settings/env", json={"SMTP_HOST": "h"},
                  headers={"origin": "https://evil.test"}).status_code == 403
