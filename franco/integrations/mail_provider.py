"""Mail provider abstraction — Franco owns its sending, Instantly optional.

A MailProvider gives Franco full inbox control: send, read replies,
thread. Two native providers ship:

- ``gmail``  — a Google Workspace / Gmail mailbox via the Gmail REST API
  (recommended: a real mailbox on a dedicated secondary domain is the
  most deliverable way to send <50 cold emails/day).
- ``smtp``   — any mailbox that speaks SMTP + IMAP (AgentMail, Fastmail,
  a Workspace app-mailbox, self-hosted). Configuration via .env.

``instantly`` remains supported through the legacy Sender/Handler path —
it isn't a MailProvider because Instantly owns sequencing/sending itself.
"""

import logging
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field

logger = logging.getLogger("franco.mail")

NATIVE_PROVIDERS = ("gmail", "smtp")


@dataclass
class SendResult:
    ok: bool
    message_id: str = ""     # RFC Message-ID (or provider id) of the sent mail
    thread_ref: str = ""     # provider thread handle (Gmail threadId, etc.)
    error: str = ""
    # The request reached the provider and then failed in a way that leaves
    # delivery unknown (timeout, dropped connection). Never retry blind:
    # the sender reconciles with find_sent first.
    ambiguous: bool = False
    # A clean failure worth retrying later (rate limit, 5xx before acceptance).
    retriable: bool = False


# What find_sent can say about a message. "unknown" is the honest default:
# a provider that cannot look its sent mail up must never claim "absent".
FOUND, ABSENT, UNKNOWN = "found", "absent", "unknown"


@dataclass
class InboundMessage:
    provider_id: str         # provider-unique id, used for reply dedup
    from_email: str
    subject: str = ""
    body: str = ""
    message_id: str = ""     # RFC Message-ID header
    in_reply_to: str = ""    # RFC In-Reply-To header (bounce/thread matching)
    thread_ref: str = ""
    date: str = ""
    is_bounce: bool = False
    headers: dict = field(default_factory=dict)


BOUNCE_SENDERS = ("mailer-daemon", "postmaster", "mail delivery subsystem")
BOUNCE_SUBJECTS = (
    "delivery status notification", "undeliverable", "returned mail",
    "delivery failure", "failure notice", "could not be delivered",
    "delivery incomplete", "address not found",
)


def looks_like_bounce(from_email: str, subject: str) -> bool:
    """A *candidate* bounce. Anyone can send a message that looks like one,
    so the handler only acts on it after matching it to an email Franco
    actually sent (see parse_bounce and referenced_ids)."""
    f = (from_email or "").lower()
    s = (subject or "").lower()
    return any(m in f for m in BOUNCE_SENDERS) or any(m in s for m in BOUNCE_SUBJECTS)


_DSN_RECIPIENT = re.compile(
    r"^(?:final|original)-recipient:\s*rfc822;\s*<?([^>\s]+@[^>\s]+)>?", re.I | re.M)
_DSN_STATUS = re.compile(r"^status:\s*([245])\.\d{1,3}\.\d{1,3}", re.I | re.M)
_ADDRESS = re.compile(r"[\w.+'-]+@[\w-]+(?:\.[\w-]+)+")
_TRANSIENT = ("delay", "will retry", "'ll retry", "keep trying", "still trying",
              "temporary problem", "temporarily", "try again later")


@dataclass
class BounceInfo:
    recipients: set[str]      # addresses the report says failed ("" set if none found)
    permanent: bool           # False for "delayed, still trying" (4.x.x) notices


def parse_bounce(msg: "InboundMessage") -> BounceInfo:
    """Who a delivery report says failed, and whether it gave up.

    Uses the RFC 3464 fields when the text carries them (Final-Recipient,
    Status), else the addresses in the text and its wording.
    """
    text = f"{msg.subject}\n{msg.body}"
    recipients = {a.lower() for a in _DSN_RECIPIENT.findall(text)}
    if not recipients:
        recipients = {a.lower().strip(".") for a in _ADDRESS.findall(msg.body or "")}
    statuses = _DSN_STATUS.findall(text)
    if statuses:
        permanent = "5" in statuses
    else:
        lowered = text.lower()
        permanent = not any(hint in lowered for hint in _TRANSIENT)
    return BounceInfo(recipients=recipients, permanent=permanent)


def referenced_ids(msg: "InboundMessage") -> list[str]:
    """Message-IDs the report points back to (In-Reply-To, then References)."""
    ids = [msg.in_reply_to] if msg.in_reply_to else []
    refs = (msg.headers or {}).get("references", "")
    ids += [r for r in re.findall(r"<[^>]+>", refs) if r not in ids]
    return [i.strip() for i in ids if i.strip()]


class MailProvider(ABC):
    """A mailbox Franco can send from and read replies out of."""

    name: str = "base"

    @abstractmethod
    def is_configured(self) -> bool:
        """Whether required credentials are present (no network)."""

    @abstractmethod
    async def send_email(
        self,
        to_email: str,
        subject: str,
        body: str,
        thread_ref: str = "",
        in_reply_to: str = "",
        headers: dict[str, str] | None = None,
        message_id: str = "",
    ) -> SendResult:
        """Send one plain-text email. Reply threading when refs are given;
        ``headers`` adds extra ones (e.g. List-Unsubscribe). ``message_id``
        is the RFC Message-ID the caller committed to before sending, so a
        crash can be reconciled with find_sent."""

    async def find_sent(self, message_id: str, to_email: str = "",
                        subject: str = "", since=None) -> str:
        """Was this message sent? FOUND, ABSENT or UNKNOWN (the default).

        Only answer ABSENT when the provider's sent record is authoritative:
        ABSENT releases the message to be sent again.
        """
        return UNKNOWN

    @abstractmethod
    async def get_replies(self, limit: int = 50) -> list[InboundMessage]:
        """Fetch recent inbound messages (dedup is the caller's job)."""

    @abstractmethod
    async def test_connection(self) -> tuple[bool, str]:
        """Cheap end-to-end credential check. Returns (ok, detail)."""


def get_mail_provider(config, env) -> MailProvider | None:
    """Build the configured native provider, or None for instantly/unknown."""
    provider = (config.channels.email.provider or "").strip().lower()
    if provider == "gmail":
        from franco.integrations.gmail import GmailProvider
        return GmailProvider(config, env)
    if provider == "smtp":
        from franco.integrations.smtp_mail import SmtpImapProvider
        return SmtpImapProvider(config, env)
    return None
