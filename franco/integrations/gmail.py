"""Gmail / Google Workspace mailbox provider — raw REST, no SDK weight.

Setup (one-time):
1. Google Cloud Console → create a project → enable the Gmail API.
2. OAuth consent screen (internal or testing) → create an OAuth client of
   type "Desktop app". Put its id/secret in .env as GMAIL_CLIENT_ID /
   GMAIL_CLIENT_SECRET.
3. Run ``franco gmail auth`` — opens a browser, stores the refresh token
   in data/gmail_token.json. Done: Franco can send and read.

Deliverability notes: use a mailbox on a dedicated SECONDARY domain (never
your main one), configure SPF/DKIM/DMARC, ramp volume slowly, and watch
Google Postmaster Tools. Franco's outbox pacing + kill switches handle the
rest.
"""

import base64
import json
import logging
import time
import webbrowser
from datetime import timezone
from email.message import EmailMessage
from email.utils import parseaddr

import httpx

from franco.paths import PROJECT_ROOT
from franco.integrations.mail_provider import (
    ABSENT,
    FOUND,
    UNKNOWN,
    InboundMessage,
    MailProvider,
    SendResult,
    looks_like_bounce,
)

logger = logging.getLogger("franco.gmail")

TOKEN_FILE = PROJECT_ROOT / "data" / "gmail_token.json"
API_BASE = "https://gmail.googleapis.com/gmail/v1/users/me"
TOKEN_URL = "https://oauth2.googleapis.com/token"
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
SCOPE = "https://www.googleapis.com/auth/gmail.modify"
LOOPBACK_PORT = 8765

TIMEOUT = httpx.Timeout(connect=10.0, read=30.0, write=30.0, pool=10.0)


def _load_token() -> dict | None:
    try:
        if TOKEN_FILE.is_file():
            data = json.loads(TOKEN_FILE.read_text())
            if isinstance(data, dict) and data.get("refresh_token"):
                return data
    except (OSError, json.JSONDecodeError) as e:
        logger.warning(f"Could not read Gmail token file: {e}")
    return None


def _save_token(data: dict):
    TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    TOKEN_FILE.write_text(json.dumps(data, indent=2))
    try:
        TOKEN_FILE.chmod(0o600)
    except OSError:
        pass


class GmailProvider(MailProvider):
    name = "gmail"

    def __init__(self, config, env):
        self.config = config
        self.client_id = getattr(env, "gmail_client_id", "")
        self.client_secret = getattr(env, "gmail_client_secret", "")
        self._access_token = ""
        self._access_expires = 0.0

    def is_configured(self) -> bool:
        return bool(self.client_id and self.client_secret and _load_token())

    async def _token(self) -> str | None:
        """Valid access token, refreshing via the stored refresh token."""
        if self._access_token and time.time() < self._access_expires - 60:
            return self._access_token
        stored = _load_token()
        if not stored:
            logger.error("Gmail: not authorized. Run 'franco gmail auth'.")
            return None
        try:
            async with httpx.AsyncClient(timeout=TIMEOUT) as client:
                resp = await client.post(TOKEN_URL, data={
                    "client_id": self.client_id,
                    "client_secret": self.client_secret,
                    "refresh_token": stored["refresh_token"],
                    "grant_type": "refresh_token",
                })
        except httpx.HTTPError as e:
            logger.error(f"Gmail token refresh failed: {e}")
            return None
        if resp.status_code != 200:
            logger.error(
                f"Gmail token refresh rejected ({resp.status_code}). "
                "Re-run 'franco gmail auth'."
            )
            return None
        data = resp.json()
        self._access_token = data.get("access_token", "")
        self._access_expires = time.time() + float(data.get("expires_in", 3600))
        return self._access_token or None

    async def _request(self, method: str, path: str, **kwargs):
        token = await self._token()
        if not token:
            return None
        headers = {"Authorization": f"Bearer {token}"}
        try:
            async with httpx.AsyncClient(timeout=TIMEOUT) as client:
                resp = await client.request(
                    method, f"{API_BASE}{path}", headers=headers, **kwargs
                )
        except httpx.HTTPError as e:
            logger.error(f"Gmail request failed ({path}): {e}")
            return None
        if resp.status_code >= 400:
            logger.error(f"Gmail API {resp.status_code} on {path}: {resp.text[:200]}")
            return None
        try:
            return resp.json()
        except ValueError:
            return {}

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
        msg = EmailMessage()
        persona = self.config.persona
        msg["To"] = to_email
        msg["From"] = f"{persona.name} <{persona.email}>" if persona.email else persona.name
        msg["Subject"] = subject
        if message_id:
            # Gmail is not documented to keep a client Message-ID; find_sent
            # falls back to recipient + subject + time when it does not.
            msg["Message-ID"] = message_id
        if in_reply_to:
            msg["In-Reply-To"] = in_reply_to
            msg["References"] = in_reply_to
        for name, value in (headers or {}).items():
            msg[name] = value
        msg.set_content(body)

        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
        payload: dict = {"raw": raw}
        if thread_ref:
            payload["threadId"] = thread_ref

        token = await self._token()
        if not token:
            return SendResult(ok=False, error="gmail is not authorized")
        try:
            async with httpx.AsyncClient(timeout=TIMEOUT) as client:
                resp = await client.post(f"{API_BASE}/messages/send", json=payload,
                                         headers={"Authorization": f"Bearer {token}"})
        except httpx.ConnectError as e:
            # Never reached Google: nothing was sent.
            return SendResult(ok=False, error=f"gmail unreachable: {e}"[:300], retriable=True)
        except httpx.HTTPError as e:
            # Sent and then lost the answer: Google may have the message.
            return SendResult(ok=False, error=f"gmail send interrupted: {e}"[:300],
                              ambiguous=True)
        if resp.status_code == 429:
            return SendResult(ok=False, error="gmail rate limited", retriable=True)
        if resp.status_code >= 500:
            return SendResult(ok=False, error=f"gmail {resp.status_code}", ambiguous=True)
        if resp.status_code >= 400:
            logger.error(f"Gmail API {resp.status_code} on send: {resp.text[:200]}")
            return SendResult(ok=False, error=f"gmail rejected the message ({resp.status_code})")
        try:
            data = resp.json()
        except ValueError:
            data = {}

        # Fetch the assigned RFC Message-ID for bounce/thread matching later.
        assigned = ""
        gmail_id = data.get("id", "")
        if gmail_id:
            meta = await self._request(
                "GET", f"/messages/{gmail_id}",
                params={"format": "metadata", "metadataHeaders": "Message-ID"},
            )
            for h in ((meta or {}).get("payload") or {}).get("headers", []):
                if h.get("name", "").lower() == "message-id":
                    assigned = h.get("value", "")
        return SendResult(
            ok=True,
            message_id=assigned or message_id or gmail_id,
            thread_ref=data.get("threadId", ""),
        )

    async def find_sent(self, message_id: str, to_email: str = "",
                        subject: str = "", since=None) -> str:
        """Search the mailbox. Sent mail is in Gmail the moment send returns,
        so an empty answer to both searches means it was not sent."""
        queries = []
        if message_id:
            queries.append(f"rfc822msgid:{message_id.strip('<>')}")
        if to_email and subject and since is not None:
            if since.tzinfo is None:            # stored timestamps are naive UTC
                since = since.replace(tzinfo=timezone.utc)
            clean = subject.replace('"', " ").strip()
            queries.append(f'in:sent to:{to_email} subject:"{clean}" '
                           f"after:{int(since.timestamp()) - 60}")
        if not queries:
            return UNKNOWN
        for q in queries:
            listing = await self._request("GET", "/messages",
                                          params={"q": q, "maxResults": 1})
            if listing is None:
                return UNKNOWN              # could not ask: never guess "absent"
            if listing.get("messages"):
                return FOUND
        return ABSENT

    async def get_replies(self, limit: int = 50) -> list[InboundMessage]:
        listing = await self._request(
            "GET", "/messages",
            params={"q": "in:inbox -from:me newer_than:7d", "maxResults": limit},
        )
        if not listing:
            return []
        out = []
        for stub in listing.get("messages", [])[:limit]:
            data = await self._request(
                "GET", f"/messages/{stub['id']}", params={"format": "full"}
            )
            if not data:
                continue
            msg = self._parse_message(data)
            if msg:
                out.append(msg)
        return out

    @staticmethod
    def _parse_message(data: dict) -> InboundMessage | None:
        payload = data.get("payload") or {}
        headers = {
            h.get("name", "").lower(): h.get("value", "")
            for h in payload.get("headers", [])
        }
        from_email = parseaddr(headers.get("from", ""))[1].lower()
        if not from_email:
            return None
        subject = headers.get("subject", "")
        body = GmailProvider._extract_text(payload) or data.get("snippet", "")
        return InboundMessage(
            provider_id=data.get("id", ""),
            from_email=from_email,
            subject=subject,
            body=body[:5000],
            message_id=headers.get("message-id", ""),
            in_reply_to=headers.get("in-reply-to", ""),
            thread_ref=data.get("threadId", ""),
            date=headers.get("date", ""),
            is_bounce=looks_like_bounce(from_email, subject),
            headers=headers,
        )

    @staticmethod
    def _extract_text(payload: dict) -> str:
        """Depth-first hunt for the text/plain part of a MIME tree."""
        def decode(part) -> str:
            data = (part.get("body") or {}).get("data", "")
            if not data:
                return ""
            try:
                return base64.urlsafe_b64decode(data + "===").decode(errors="replace")
            except Exception:
                return ""

        if payload.get("mimeType", "").startswith("text/plain"):
            return decode(payload)
        for part in payload.get("parts", []) or []:
            text = GmailProvider._extract_text(part)
            if text:
                return text
        if payload.get("mimeType", "").startswith("text/html"):
            import re
            html = decode(payload)
            return re.sub(r"<[^>]+>", " ", html)
        return ""

    async def test_connection(self) -> tuple[bool, str]:
        if not self.client_id or not self.client_secret:
            return False, "GMAIL_CLIENT_ID / GMAIL_CLIENT_SECRET missing from .env"
        if not _load_token():
            return False, "Not authorized yet — run 'franco gmail auth'"
        data = await self._request("GET", "/profile")
        if data and data.get("emailAddress"):
            return True, f"Connected as {data['emailAddress']}"
        return False, "Token refresh or profile fetch failed"


def run_auth_flow(client_id: str, client_secret: str) -> bool:
    """One-time OAuth (installed-app loopback). Blocks until the browser
    round-trip completes; stores the refresh token for GmailProvider."""
    import http.server
    import secrets
    import threading
    import urllib.parse

    if not client_id or not client_secret:
        print("  Set GMAIL_CLIENT_ID and GMAIL_CLIENT_SECRET in .env first.")
        print("  (Google Cloud Console → APIs → Credentials → OAuth client, Desktop app)")
        return False

    state = secrets.token_urlsafe(16)
    redirect_uri = f"http://localhost:{LOOPBACK_PORT}"
    auth_url = AUTH_URL + "?" + urllib.parse.urlencode({
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": SCOPE,
        "access_type": "offline",
        "prompt": "consent",
        "state": state,
    })

    result: dict = {}
    done = threading.Event()

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            if query.get("state", [""])[0] != state:
                self.send_response(400); self.end_headers()
                return
            result["code"] = query.get("code", [""])[0]
            result["error"] = query.get("error", [""])[0]
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(
                b"<h2>Franco is connected to Gmail.</h2>You can close this tab.")
            done.set()

        def log_message(self, *args):
            pass

    server = http.server.HTTPServer(("localhost", LOOPBACK_PORT), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    print(f"\n  Opening browser for Google sign-in...\n  {auth_url}\n")
    webbrowser.open(auth_url)
    if not done.wait(timeout=300):
        server.shutdown()
        print("  Timed out waiting for the browser round-trip.")
        return False
    server.shutdown()

    if result.get("error") or not result.get("code"):
        print(f"  Authorization failed: {result.get('error') or 'no code returned'}")
        return False

    resp = httpx.post(TOKEN_URL, data={
        "client_id": client_id,
        "client_secret": client_secret,
        "code": result["code"],
        "grant_type": "authorization_code",
        "redirect_uri": redirect_uri,
    }, timeout=30)
    if resp.status_code != 200:
        print(f"  Token exchange failed ({resp.status_code}): {resp.text[:200]}")
        return False
    data = resp.json()
    if not data.get("refresh_token"):
        print("  Google returned no refresh token. Remove Franco from your"
              " account's third-party access list and retry.")
        return False
    _save_token({
        "refresh_token": data["refresh_token"],
        "scope": data.get("scope", SCOPE),
        "obtained_at": int(time.time()),
    })
    print(f"  ✓ Gmail authorized. Token stored at {TOKEN_FILE}")
    return True
