"""Shared fixtures for the approved-copy (Phase 1) tests: a real
FrancoConfig with outreach/compliance settings, a provider that records
headers, a brain stub, and account seeding through the real importer."""

import copy
from datetime import date, datetime, timezone

from franco.config import EnvConfig, FrancoConfig
from franco.integrations.mail_provider import MailProvider, SendResult

ADDRESS = "1 Example Street, Suite 100, Springfield, CA, 90000"
TWO_LINE_FOOTER = ("{sender_name} | {company} | {postal_address}\n"
                   "Business outreach about {company}. Reply \"no thanks\" to stop.")
OBSERVATION = "your seller help center describes a paid Featured Listing upgrade"

M1_STEPS = [
    {"day": 1, "subject": "Sponsored listings at {{company}}",
     "body": "Hi {{first_name}},\n\nI noticed {{verified_observation}}.\n\nWorth a look?\n\nHemin"},
    {"day": 5, "subject": "The decision behind a sponsored listing",
     "body": "Hi {{first_name}},\n\nA second, shorter note.\n\nHemin"},
    {"day": 9, "subject": "A bounded way to evaluate this",
     "body": "Hi {{first_name}},\n\nA third note.\n\nHemin"},
]


def _merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def make_config(**over) -> FrancoConfig:
    base = {
        "persona": {"name": "Hemin Joshi", "company": "Gortex", "role": "Founder",
                    "email": "sender@example.com", "linkedin": "", "tone": "plain"},
        "product": {"name": "Gortex", "description": "d", "pricing": "",
                    "key_benefits": [], "objection_responses": {}},
        "icp": {"industries": [], "company_size": "", "titles": [], "geography": []},
        "channels": {"email": {"provider": "smtp", "require_approval": True,
                               "max_daily_sends": 50},
                     "linkedin": {"enabled": False}},
        "outreach": {"mode": "templates", "timezone": "America/Los_Angeles",
                     "send_hour": 9, "preview_accounts": 10,
                     "campaigns": {"M1": {"name": "Marketplaces", "steps": M1_STEPS}}},
        "compliance": {"require_footer": True, "postal_address": ADDRESS,
                       "footer": TWO_LINE_FOOTER, "blocked_claims": ["real-time auction"]},
    }
    return FrancoConfig(**_merge(base, over))


class RecordingProvider(MailProvider):
    """Accepts the ``headers`` kwarg and records everything it sends."""
    name = "fake"

    def __init__(self):
        self.sent = []
        self.inbound = []

    def is_configured(self):
        return True

    async def send_email(self, to_email, subject, body, thread_ref="",
                         in_reply_to="", headers=None, message_id=""):
        self.sent.append({"to": to_email, "subject": subject, "body": body,
                          "headers": headers, "in_reply_to": in_reply_to})
        return SendResult(ok=True, message_id=f"<m{len(self.sent)}@x>", thread_ref="t1")

    async def get_replies(self, limit=50):
        return self.inbound

    async def test_connection(self):
        return True, "fake"


class Brain:
    """No subprocess: a fixed intent, a fixed reply, a fixed recontact date."""
    paused_reason = ""

    def __init__(self, intent="interested", reply="Happy to share more.", recontact=""):
        self.intent, self.reply, self.recontact = intent, reply, recontact

    def load_skills_for_agent(self, *_):
        return ""

    def load_prompt(self, *_a, **_k):
        return ""

    async def think(self, prompt, session_id=None, agent="", task=""):
        return self.intent if task == "classify_intent" else self.reply

    async def think_json(self, prompt, session_id=None, agent="", task=""):
        return {"recontact_after": self.recontact}


def make_sender(state, config, provider=None):
    from franco.agents.sender import Sender
    sender = Sender(brain=None, state=state, config=config, env=EnvConfig())
    sender.provider = provider or RecordingProvider()
    sender.send_pacing = False
    return sender


def make_writer(state, config):
    from franco.agents.writer import Writer
    return Writer(brain=None, state=state, config=config, env=EnvConfig())


def make_handler(state, config, brain=None, provider=None):
    from franco.agents.handler import Handler
    handler = Handler(brain=brain or Brain(), state=state, config=config, env=EnvConfig())
    handler.provider = provider or RecordingProvider()
    return handler


def account_row(i, **kw) -> dict:
    row = {
        "company": f"Co{i}", "domain": f"co{i}.com", "campaign": "M1",
        "first_name": "Dana", "last_name": f"W{i}", "title": "CEO",
        "email": f"dana{i}@co{i}.com", "observation": OBSERVATION,
        "source_url": f"https://co{i}.com/help", "source_date": date.today().isoformat(),
        "owner": "Hemin", "email_status": "verified",
    }
    row.update(kw)
    return row


async def import_accounts(state, rows, campaigns=("M1",)):
    from franco.importer import import_accounts as _imp
    report = await _imp(state, rows, set(campaigns))
    assert not report.rejected, report.rejected
    return report


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).replace(tzinfo=None).isoformat()


PAST = "2000-01-01T00:00:00"
