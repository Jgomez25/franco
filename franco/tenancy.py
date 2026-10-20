"""Workspaces, users, roles and the server-side policy that enforces them.

Two modes:

- ``local`` (the default): today's single-operator app. Every request is the
  local operator, an admin of the 'local' workspace. Nothing else changes;
  the localhost-only guard in the dashboard still applies.
- ``shared`` (``FRANCO_MODE=shared``): every API request needs a bearer token
  (``franco users add`` issues one). The caller is a member of exactly one
  workspace with one role, and the policy below decides what they may do.

The policy is an allowlist. In shared mode an API route that is not listed
here is refused, so a route nobody made tenant-safe can never leak another
workspace's data. Routes that act on one record name the table that owns
it; the record must belong to the caller's workspace, and a record in
another workspace answers 404 (its existence is not confirmed).

Provider credentials are installation-wide (one .env / vault), so managing
them needs the admin role in the installation workspace, 'local'. An admin
of any other workspace is denied.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import uuid
from dataclasses import dataclass

import aiosqlite

ROLES = ("rep", "manager", "admin")
RANK = {r: i for i, r in enumerate(ROLES)}
INSTALLATION_WORKSPACE = "local"
LOCAL_USER = "local-operator"

# permission → least role that holds it
PERMISSIONS = {
    "read": "rep",
    "outbox.review": "rep",
    "outbox.bulk": "manager",
    "audit.read": "manager",
    "credentials.manage": "admin",       # and the installation workspace
}

# Tables whose rows carry a workspace_id and may be named in a route.
OWNED_TABLES = frozenset({"outbox", "companies", "prospects", "campaigns", "conversations"})


@dataclass(frozen=True)
class Route:
    method: str
    pattern: re.Pattern
    permission: str
    owner_table: str = ""      # the path parameter "id" is a row of this table


def _route(method: str, path: str, permission: str, owner: str = "") -> Route:
    return Route(method, re.compile("^" + path + "$"), permission, owner)


# Every API route available in shared mode. Anything else is refused there.
SHARED_ROUTES = (
    _route("GET", r"/api/me", "read"),
    _route("GET", r"/api/capabilities", "read"),
    _route("GET", r"/api/outbox", "read"),
    _route("POST", r"/api/outbox/approve-all", "outbox.bulk"),
    _route("POST", r"/api/outbox/(?P<id>[^/]+)/(approve|reject|edit|resolve)",
           "outbox.review", "outbox"),
    _route("GET", r"/api/contacts/search", "read"),
    _route("GET", r"/api/companies", "read"),
    _route("GET", r"/api/companies/(?P<id>[^/]+)/contacts", "read", "companies"),
    _route("GET", r"/api/audit", "audit.read"),
    _route("GET", r"/api/settings", "credentials.manage"),
    _route("POST", r"/api/settings/env", "credentials.manage"),
    _route("POST", r"/api/settings/test-instantly", "credentials.manage"),
    _route("GET", r"/api/secrets", "credentials.manage"),
    _route("POST", r"/api/secrets/[^/]+/revoke", "credentials.manage"),
)


def mode() -> str:
    return "shared" if os.environ.get("FRANCO_MODE", "").strip().lower() == "shared" \
        else "local"


@dataclass(frozen=True)
class Principal:
    user_id: str
    workspace_id: str
    role: str
    email: str = ""
    token_id: str = ""

    def holds(self, permission: str) -> bool:
        need = PERMISSIONS.get(permission)
        if need is None or RANK.get(self.role, -1) < RANK[need]:
            return False
        if permission == "credentials.manage":
            return self.workspace_id == INSTALLATION_WORKSPACE
        return True


LOCAL_PRINCIPAL = Principal(LOCAL_USER, INSTALLATION_WORKSPACE, "admin",
                            "operator@localhost")


@dataclass(frozen=True)
class Decision:
    allowed: bool
    status: int = 200
    reason: str = ""
    route: Route | None = None


def match(method: str, path: str) -> tuple[Route | None, dict]:
    for route in SHARED_ROUTES:
        if route.method == method:
            m = route.pattern.match(path)
            if m:
                return route, m.groupdict()
    return None, {}


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class TenancyStore:
    def __init__(self, state):
        self.state = state

    def _connect(self):
        return self.state._connect()

    async def create_workspace(self, workspace_id: str, name: str = "") -> None:
        async with self._connect() as db:
            await db.execute("INSERT OR IGNORE INTO workspaces (id, name) VALUES (?, ?)",
                             (workspace_id, name or workspace_id))
            await db.commit()

    async def add_user(self, email: str, workspace_id: str, role: str,
                       display_name: str = "") -> str:
        if role not in ROLES:
            raise ValueError(f"role must be one of {', '.join(ROLES)}")
        email = email.strip().lower()
        async with self._connect() as db:
            async with db.execute("SELECT id FROM workspaces WHERE id = ?",
                                  (workspace_id,)) as cur:
                if await cur.fetchone() is None:
                    raise ValueError(f"no workspace {workspace_id!r}")
            await db.execute("INSERT OR IGNORE INTO users (id, email, display_name) "
                             "VALUES (?, ?, ?)", (uuid.uuid4().hex[:12], email, display_name))
            async with db.execute("SELECT id FROM users WHERE email = ?", (email,)) as cur:
                (user_id,) = await cur.fetchone()
            await db.execute("INSERT INTO memberships (workspace_id, user_id, role) "
                             "VALUES (?, ?, ?) ON CONFLICT (workspace_id, user_id) "
                             "DO UPDATE SET role = excluded.role",
                             (workspace_id, user_id, role))
            await db.commit()
            return user_id

    async def issue_token(self, user_id: str, workspace_id: str) -> str:
        """A new bearer token. Returned once; only its hash is kept."""
        token = "fr_" + secrets.token_urlsafe(32)
        async with self._connect() as db:
            await db.execute("INSERT INTO api_tokens (id, user_id, workspace_id, token_hash) "
                             "VALUES (?, ?, ?, ?)",
                             (uuid.uuid4().hex[:12], user_id, workspace_id, hash_token(token)))
            await db.commit()
        from franco.vault import register_secret
        register_secret(token)
        return token

    async def revoke_tokens(self, user_id: str) -> int:
        async with self._connect() as db:
            cur = await db.execute("UPDATE api_tokens SET revoked_at = CURRENT_TIMESTAMP "
                                   "WHERE user_id = ? AND revoked_at IS NULL", (user_id,))
            await db.commit()
            return cur.rowcount

    async def resolve(self, token: str) -> Principal | None:
        """The principal for a live token whose user is still a member."""
        if not token:
            return None
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT t.id AS token_id, t.user_id, t.workspace_id, m.role, u.email "
                "FROM api_tokens t JOIN users u ON u.id = t.user_id "
                "JOIN memberships m ON m.user_id = t.user_id "
                "AND m.workspace_id = t.workspace_id "
                "WHERE t.token_hash = ? AND t.revoked_at IS NULL "
                "AND u.disabled_at IS NULL", (hash_token(token),)) as cur:
                row = await cur.fetchone()
            if row is None:
                return None
            await db.execute("UPDATE api_tokens SET last_used_at = CURRENT_TIMESTAMP "
                             "WHERE id = ?", (row["token_id"],))
            await db.commit()
        return Principal(row["user_id"], row["workspace_id"], row["role"],
                         row["email"], row["token_id"])

    async def owner(self, table: str, row_id: str) -> str | None:
        if table not in OWNED_TABLES:
            raise ValueError(f"{table} is not workspace-owned")
        async with self._connect() as db:
            async with db.execute(f"SELECT workspace_id FROM {table} WHERE id = ?",
                                  (row_id,)) as cur:
                row = await cur.fetchone()
                return row[0] if row else None

    async def audit(self, principal: Principal | None, action: str, outcome: str,
                    target_type: str = "", target_id: str = "", request_id: str = "",
                    detail: dict | None = None) -> None:
        async with self._connect() as db:
            await db.execute(
                "INSERT INTO audit_events (id, workspace_id, actor_user_id, action, "
                "target_type, target_id, outcome, request_id, detail_json) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (uuid.uuid4().hex[:12],
                 principal.workspace_id if principal else INSTALLATION_WORKSPACE,
                 principal.user_id if principal else "", action, target_type,
                 target_id, outcome, request_id, json.dumps(detail or {})))
            await db.commit()

    async def audit_events(self, workspace_id: str, limit: int = 100) -> list[dict]:
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM audit_events WHERE workspace_id = ? "
                "ORDER BY created_at DESC, rowid DESC LIMIT ?",
                (workspace_id, int(limit))) as cur:
                return [dict(r) for r in await cur.fetchall()]

    async def members(self) -> list[dict]:
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT m.workspace_id, m.role, u.id AS user_id, u.email, u.disabled_at "
                "FROM memberships m JOIN users u ON u.id = m.user_id "
                "ORDER BY m.workspace_id, u.email") as cur:
                return [dict(r) for r in await cur.fetchall()]


async def authorize(store: TenancyStore, method: str, path: str,
                    principal: Principal | None) -> Decision:
    """Shared-mode decision for one API request. Pure policy plus one lookup."""
    if principal is None:
        return Decision(False, 401, "a valid bearer token is required")
    route, params = match(method, path)
    if route is None:
        return Decision(False, 403, "this endpoint is not available in shared mode")
    if not principal.holds(route.permission):
        return Decision(False, 403, f"role '{principal.role}' may not "
                                    f"{route.permission.replace('.', ' ')}", route)
    if route.owner_table:
        owner = await store.owner(route.owner_table, params.get("id", ""))
        if owner != principal.workspace_id:
            # Another workspace's record looks exactly like a missing one.
            return Decision(False, 404, "not found", route)
    return Decision(True, 200, "", route)
