"""What Franco can actually do right now, and the gate that enforces it.

Every capability has exactly one of five states:

- ``preview``      exists only as a blueprint simulation (franco/web/blueprint.*);
                   there is no production code behind it.
- ``unavailable``  production code exists but is missing configuration, or the
                   capability is excluded by the agreed scope.
- ``configured``   the configuration it needs is present; no successful
                   exchange with the provider is recorded yet (or the last one
                   is stale).
- ``degraded``     configured, but local health says something is wrong (the
                   sending kill switch is on, the last run failed).
- ``live``         configured, and this install recorded a successful exchange
                   with the provider within ``LIVE_WINDOW_DAYS``. This is
                   observed locally, not certified by the provider.

Only configured, degraded and live are executable. ``require()`` raises
before an adapter is built, so an unsupported capability never reaches the
network. The other gates (approval, suppression, verification, budget, kill
switch) still apply on top of this one; this gate never loosens them.

The scope behind this registry is written up in docs/CAPABILITY_MATRIX.md.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Callable
from datetime import datetime, timedelta, timezone

PREVIEW, UNAVAILABLE, CONFIGURED, DEGRADED, LIVE = (
    "preview", "unavailable", "configured", "degraded", "live")
STATES = (PREVIEW, UNAVAILABLE, CONFIGURED, DEGRADED, LIVE)
EXECUTABLE = frozenset({CONFIGURED, DEGRADED, LIVE})

LIVE_WINDOW_DAYS = 30
MATRIX_VERSION = "2026-10-20.owner-decisions"


@dataclass(frozen=True)
class Capability:
    key: str
    label: str
    area: str
    implemented: bool
    env_keys: tuple[str, ...] = ()
    needs_gmail_token: bool = False
    excluded: str = ""          # why the agreed scope rules it out
    task: str = ""              # harness/parity task that would implement it
    configured: Callable[[dict], bool] | None = None   # adapter's own check


@dataclass
class CapabilityStatus:
    key: str
    label: str
    area: str
    state: str
    reason: str
    task: str = ""
    last_success: str = ""
    executable: bool = field(init=False)

    def __post_init__(self):
        self.executable = self.state in EXECUTABLE

    def as_dict(self) -> dict:
        return asdict(self)


class CapabilityUnavailable(RuntimeError):
    """Raised before any external request for a non-executable capability."""

    def __init__(self, status: CapabilityStatus):
        self.status = status
        super().__init__(f"{status.label} is {status.state}: {status.reason}")


def _discovery_capabilities() -> list[Capability]:
    from franco.collectors.discover import PROVIDERS

    # The adapter's own configured() decides, so the gate and the adapter
    # can never disagree about whether a provider is ready.
    return [Capability(f"discovery.{p.key}", p.label, "discovery", True,
                       env_keys=tuple(p.env_keys), configured=p.configured)
            for p in PROVIDERS.values()]


_STATIC: tuple[Capability, ...] = (
    # Email channel (one is active, per channels.email.provider)
    Capability("email.gmail", "Gmail / Google Workspace mailbox", "email", True,
               env_keys=("gmail_client_id", "gmail_client_secret"),
               needs_gmail_token=True),
    Capability("email.smtp", "SMTP + IMAP mailbox", "email", True,
               env_keys=("smtp_host", "smtp_username", "smtp_password")),
    Capability("email.instantly", "Instantly campaigns (legacy)", "email", True,
               env_keys=("instantly_api_key",)),
    # Email verification
    Capability("verify.reoon", "Reoon email verification", "verification", True,
               env_keys=("reoon_api_key",)),
    Capability("verify.zerobounce", "ZeroBounce email verification", "verification", True,
               env_keys=("zerobounce_api_key",)),
    Capability("verify.hunter", "Hunter pattern lookup + verification", "verification", True,
               env_keys=("hunter_api_key",)),
    # Other prospecting inputs
    Capability("prospecting.linkedin", "LinkedIn prospecting", "prospecting", True,
               env_keys=("linkedin_email", "linkedin_password")),
    Capability("training.cloudflare", "Cloudflare deep crawling for training", "training", True,
               env_keys=("cloudflare_account_id", "cloudflare_api_token")),
    # Built in P01; off unless the operator opts into shared mode.
    Capability("team.roles", "Workspaces, roles and API tokens (shared mode)", "administration",
               True, env_keys=("franco_mode",),
               configured=lambda env: str(env.get("franco_mode") or "").lower() == "shared"),
    # Blueprint-only: simulated in franco/web/blueprint.*, no production code.
    Capability("data.licensed", "Licensed contact database and waterfall enrichment", "data", False, task="P02"),
    Capability("sequences.multichannel", "Multi-channel sequence builder", "sequences", False, task="P03"),
    Capability("calls.telephony", "Calling workspace (dialer, recording)", "calls", False, task="P04"),
    Capability("crm.sync", "CRM sync (Salesforce, HubSpot)", "integrations", False, task="P05"),
    Capability("inbound.forms", "Inbound forms and lead routing", "inbound", False, task="P06"),
    Capability("meetings.scheduler", "Meeting scheduler and round-robin", "meetings", False, task="P07"),
    Capability("mail.health", "Mailbox health and DNS monitoring", "deliverability", False, task="P08"),
    Capability("deals.intelligence", "Deals and conversation intelligence", "pipeline", False, task="P09"),
    Capability("reports.analytics", "Event-derived analytics and reports", "reporting", False, task="P10"),
    Capability("extension.browser", "Browser extension capture", "extension", False, task="P11"),
    Capability("ai.workspace", "Grounded AI workspace", "assistant", False, task="P12"),
    # Excluded by scope: implemented nowhere, and not planned.
    Capability("mailbox.purchase", "Domain and mailbox purchasing", "deliverability", False,
               excluded="Franco never buys domains or mailboxes; do that with your provider."),
    Capability("mailbox.warming", "Inbox warming", "deliverability", False,
               excluded="Warming networks fake engagement; excluded from scope."),
    Capability("channel.sms", "SMS / WhatsApp outreach", "channels", False,
               excluded="Only the email channel is in scope."),
    Capability("dashboard.public", "Dashboard reachable beyond localhost", "administration", False,
               excluded="Franco is a single-operator local app (owner decision, 2026-10-20); "
                        "the dashboard has no login and stays on localhost."),
)


def registry() -> dict[str, Capability]:
    caps = list(_STATIC[:3]) + _discovery_capabilities() + list(_STATIC[3:])
    return {c.key: c for c in caps}


def _parse_ts(value: str) -> datetime | None:
    if not value:
        return None
    try:
        ts = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


def evaluate(key: str, env: dict | None = None, facts: dict | None = None,
             now: datetime | None = None) -> CapabilityStatus:
    """The state of one capability. Pure: everything it needs is passed in.

    ``env`` holds lowercased setting names (load_env().model_dump() plus
    anything read from the environment). ``facts`` is what ``gather_facts``
    read from the local database; without it only preview / unavailable /
    configured can be told apart, which is all the execution gate needs.
    """
    env = env or {}
    facts = facts or {}
    cap = registry().get(key)
    if cap is None:
        return CapabilityStatus(key, key, "unknown", UNAVAILABLE,
                                "not a capability Franco supports")

    def status(state, reason, last=""):
        return CapabilityStatus(cap.key, cap.label, cap.area, state, reason,
                                task=cap.task, last_success=last)

    if cap.excluded:
        return status(UNAVAILABLE, "excluded by scope: " + cap.excluded)
    if not cap.implemented:
        return status(PREVIEW, f"blueprint simulation only; production work is "
                               f"harness task {cap.task}, not complete")
    missing = [k.upper() for k in cap.env_keys if not str(env.get(k) or "").strip()]
    ready = cap.configured(env) if cap.configured else not missing
    if not ready:
        problems = env.get("credential_problems") or {}
        why = [f"{k} is {problems[k]} in the vault" for k in missing if k in problems]
        return status(UNAVAILABLE, "; ".join(why) if why else
                      "needs " + (", ".join(missing) or "configuration"))
    if cap.needs_gmail_token and not facts.get("gmail_token", env.get("_gmail_token")):
        return status(UNAVAILABLE, "run 'franco gmail auth' to create the Gmail token")

    if cap.area == "email" and facts.get("sending_paused"):
        return status(DEGRADED, f"sending is paused ({facts['sending_paused']})")
    failure = (facts.get("last_failure") or {}).get(key)
    if failure:
        return status(DEGRADED, "the last run failed: " + str(failure)[:160])

    last = (facts.get("last_success") or {}).get(key, "")
    ts = _parse_ts(last)
    now = now or datetime.now(timezone.utc)
    if ts and now - ts <= timedelta(days=LIVE_WINDOW_DAYS):
        return status(LIVE, "a successful exchange is recorded locally", last)
    if ts:
        return status(CONFIGURED, f"the last recorded success is older than "
                                  f"{LIVE_WINDOW_DAYS} days (stale)", last)
    return status(CONFIGURED, "configured; no successful exchange recorded yet")


def require(key: str, env: dict | None = None, facts: dict | None = None) -> CapabilityStatus:
    """Return the status if executable, else raise CapabilityUnavailable.

    Call it before constructing an adapter or opening a client.
    """
    result = evaluate(key, env, facts)
    if not result.executable:
        raise CapabilityUnavailable(result)
    return result


def current_env() -> dict:
    """Settings as the gate sees them: EnvConfig plus env-only keys."""
    import os

    from franco.config import load_env

    env = load_env().model_dump()
    for name in ("cloudflare_account_id", "cloudflare_api_token", "franco_mode"):
        env.setdefault(name, os.getenv(name.upper(), ""))
    return env


async def gather_facts(state, gmail_token_path=None) -> dict:
    """Read health and success evidence from the local database. Read-only."""
    import aiosqlite

    facts: dict = {"last_success": {}, "last_failure": {}}
    if gmail_token_path is not None:
        facts["gmail_token"] = gmail_token_path.is_file()
    facts["sending_paused"] = await state.get_setting("sending_paused") or ""
    async with aiosqlite.connect(state.db_path) as db:
        async with db.execute(
            "SELECT provider, MAX(sent_at) FROM outbox "
            "WHERE status = 'sent' AND provider != '' GROUP BY provider") as cur:
            for provider, sent_at in await cur.fetchall():
                facts["last_success"][f"email.{provider}"] = sent_at or ""
        async with db.execute(
            "SELECT MAX(created_at) FROM actions WHERE action_type = 'send_campaign'") as cur:
            row = await cur.fetchone()
            if row and row[0]:
                facts["last_success"]["email.instantly"] = row[0]
        seen: set[str] = set()
        async with db.execute(
            "SELECT provider, status, error, ended_at FROM runs "
            "WHERE stage = 'discover' AND provider != '' "
            "ORDER BY started_at DESC") as cur:
            for provider, run_status, error, ended_at in await cur.fetchall():
                key = f"discovery.{provider}"
                if key not in seen:      # the latest run decides health
                    seen.add(key)
                    if run_status in ("failed", "stale"):
                        facts["last_failure"][key] = error or run_status
                if run_status == "completed" and key not in facts["last_success"]:
                    facts["last_success"][key] = ended_at or ""
    return facts


def snapshot(env: dict, facts: dict) -> list[dict]:
    return [evaluate(key, env, facts).as_dict() for key in registry()]
