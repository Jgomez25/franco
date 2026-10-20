"""P01 durable jobs and crash-safe sending.

P01-A2: kill a worker between provider acknowledgment and local commit;
replay must not duplicate the side effect.
"""
import json
import os
import signal
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest
import pytest_asyncio

from franco.integrations.mail_provider import ABSENT, FOUND, UNKNOWN, SendResult
from franco.jobs import (
    DEAD, LEASED, QUEUED, RETRY_WAIT, SUCCEEDED, JobStore, backoff,
)
from franco.state import StateManager
from tests.test_outbox_native import FakeProvider, make_sender, seed_prospect

ROOT = Path(__file__).resolve().parents[1]
WORKER = ROOT / "tests" / "durable_send_worker.py"


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


@pytest_asyncio.fixture
async def state(tmp_path):
    sm = StateManager(str(tmp_path / "franco.db"))
    await sm.init_db()
    return sm


async def approved_item(state, email="jane@acme.com"):
    pid = await seed_prospect(state, email=email)
    return await state.add_outbox_item(
        prospect_id=pid, to_email=email, subject="hello",
        body="Short and fine. Question?", send_at="2000-01-01T00:00:00",
        status="approved", campaign_id="c1", step=1)


async def expire_leases(state):
    async with state._connect() as db:
        await db.execute("UPDATE jobs SET lease_until = '2000-01-01T00:00:00' "
                         "WHERE status = 'leased'")
        await db.commit()


# ── The job store ──


@pytest.mark.asyncio
async def test_enqueue_is_idempotent_and_claim_is_exclusive(state):
    jobs = JobStore(state)
    a = await jobs.enqueue("email.send", "k1", input_ref="x")
    b = await jobs.enqueue("email.send", "k1", input_ref="y")
    assert a["id"] == b["id"] and b["input_ref"] == "x"
    first = await jobs.claim(a["id"])
    assert first["status"] == LEASED and first["attempt_count"] == 1
    assert await jobs.claim(a["id"]) is None          # already leased
    assert [x["attempt"] for x in await jobs.attempts(a["id"])] == [1]


@pytest.mark.asyncio
async def test_retry_budget_backoff_and_dead_letter(state):
    jobs = JobStore(state)
    job = await jobs.enqueue("email.send", "k2", max_attempts=2)
    now = _now()
    await jobs.claim(job["id"], now=now)
    assert await jobs.fail(job["id"], 1, "rate", "429", retriable=True, now=now) == RETRY_WAIT
    assert await jobs.claim(job["id"], now=now) is None                  # backing off
    assert await jobs.claim(job["id"], now=now + backoff(1)) is not None
    assert await jobs.fail(job["id"], 2, "rate", "429", retriable=True) == DEAD
    dead = await jobs.list(status=DEAD)
    assert [d["id"] for d in dead] == [job["id"]] and dead[0]["error_code"] == "rate"
    await jobs.requeue(job["id"])
    assert (await jobs.get(job["id"]))["status"] == QUEUED
    assert await jobs.claim(job["id"]) is not None
    assert backoff(1) == timedelta(seconds=60) and backoff(10) == timedelta(hours=1)


@pytest.mark.asyncio
async def test_non_retriable_failure_dead_letters_immediately(state):
    jobs = JobStore(state)
    job = await jobs.enqueue("email.send", "k3")
    await jobs.claim(job["id"])
    assert await jobs.fail(job["id"], 1, "rejected", "550", retriable=False) == DEAD


@pytest.mark.asyncio
async def test_expired_lease_is_not_reclaimable_without_reconciliation(state):
    jobs = JobStore(state)
    job = await jobs.enqueue("email.send", "k4")
    await jobs.claim(job["id"], lease_seconds=0)
    await expire_leases(state)
    assert await jobs.claim(job["id"]) is None
    assert [j["id"] for j in await jobs.expired("email.send")] == [job["id"]]


# ── P01-A2 with a real SIGKILL between provider ack and local commit ──


def run_worker(db, mailbox, mode):
    proc = subprocess.run([sys.executable, str(WORKER), str(db), str(mailbox), mode],
                          cwd=ROOT, capture_output=True, text=True, timeout=120)
    return proc


def mailbox_lines(path):
    return [json.loads(x) for x in path.read_text().splitlines()] if path.exists() else []


@pytest.mark.asyncio
async def test_kill_after_provider_ack_then_replay_sends_once(state, tmp_path):
    item_id = await approved_item(state)
    mailbox = tmp_path / "mailbox.jsonl"

    killed = run_worker(state.db_path, mailbox, "kill-after-ack")
    assert killed.returncode == -signal.SIGKILL, killed.stderr
    assert len(mailbox_lines(mailbox)) == 1                     # the provider has it
    assert (await state.get_outbox_item(item_id))["status"] == "sending"

    replay = run_worker(state.db_path, mailbox, "recover")
    assert replay.returncode == 0, replay.stderr
    assert len(mailbox_lines(mailbox)) == 1                     # not sent twice
    item = await state.get_outbox_item(item_id)
    assert item["status"] == "sent"
    assert item["message_id"] == mailbox_lines(mailbox)[0]["message_id"]
    job = await JobStore(state).get_by_key(f"email.send:{item_id}")
    assert job["status"] == SUCCEEDED
    outcomes = [a["outcome"] for a in await JobStore(state).attempts(job["id"])]
    assert outcomes == ["lease_expired_found"]
    prospect = await state.get_prospect(item["prospect_id"])
    assert prospect.status == "contacted"


@pytest.mark.asyncio
async def test_kill_before_provider_ack_then_replay_sends_exactly_once(state, tmp_path):
    item_id = await approved_item(state)
    mailbox = tmp_path / "mailbox.jsonl"
    assert run_worker(state.db_path, mailbox, "kill-before-ack").returncode == -signal.SIGKILL
    assert mailbox_lines(mailbox) == []

    assert run_worker(state.db_path, mailbox, "recover").returncode == 0
    sent = mailbox_lines(mailbox)
    assert len(sent) == 1
    assert sent[0]["message_id"] == f"<franco.{item_id}@x.co>"   # same id on retry
    assert (await state.get_outbox_item(item_id))["status"] == "sent"
    # A further cycle changes nothing.
    assert run_worker(state.db_path, mailbox, "recover").returncode == 0
    assert len(mailbox_lines(mailbox)) == 1


@pytest.mark.asyncio
async def test_kill_with_unconfirmable_provider_holds_for_a_human(state, tmp_path):
    item_id = await approved_item(state)
    mailbox = tmp_path / "mailbox.jsonl"
    assert run_worker(state.db_path, mailbox, "kill-after-ack").returncode == -signal.SIGKILL
    for _ in range(2):
        assert run_worker(state.db_path, mailbox, "recover-unknown").returncode == 0
    assert len(mailbox_lines(mailbox)) == 1
    item = await state.get_outbox_item(item_id)
    assert item["status"] == "needs_check" and "cannot confirm" in item["error"]
    job = await JobStore(state).get_by_key(f"email.send:{item_id}")
    assert job["status"] == DEAD and job["error_code"] == "unconfirmed"


# ── In-process recovery paths ──


class Recorder(FakeProvider):
    def __init__(self, finding=ABSENT, result=None):
        super().__init__()
        self.finding, self.result, self.asked = finding, result, []

    async def send_email(self, to_email, subject, body, thread_ref="", in_reply_to="",
                         message_id=""):
        if self.result is not None:
            result, self.result = self.result, None
            return result
        return await super().send_email(to_email, subject, body, thread_ref,
                                        in_reply_to, message_id)

    async def find_sent(self, message_id, to_email="", subject="", since=None):
        self.asked.append(message_id)
        return self.finding


@pytest.mark.asyncio
async def test_ambiguous_result_is_reconciled_not_retried(state):
    item_id = await approved_item(state)
    provider = Recorder(finding=FOUND, result=SendResult(ok=False, error="timeout",
                                                         ambiguous=True))
    sender = make_sender(state, provider, require_approval=False)
    await sender._drain_due()
    assert (await state.get_outbox_item(item_id))["status"] == "sending"
    assert await state.count_outbox_sent_today() == 1       # spends the cap meanwhile
    await sender._drain_due()                                # lease still live
    assert provider.asked == [] and provider.sent == []
    await expire_leases(state)
    await sender._drain_due()
    assert provider.asked == [f"<franco.{item_id}@x.co>"]
    assert provider.sent == []                               # never resent
    assert (await state.get_outbox_item(item_id))["status"] == "sent"


@pytest.mark.asyncio
async def test_retriable_failure_backs_off_then_dead_letters(state):
    item_id = await approved_item(state)
    provider = Recorder(result=SendResult(ok=False, error="rate limited", retriable=True))
    sender = make_sender(state, provider, require_approval=False)
    await sender._drain_due()
    item = await state.get_outbox_item(item_id)
    assert item["status"] == "approved" and item["error"].startswith("retrying")
    await sender._drain_due()                                # still backing off
    assert provider.sent == []
    job = await JobStore(state).get_by_key(f"email.send:{item_id}")
    assert job["status"] == RETRY_WAIT and job["next_attempt_at"]


@pytest.mark.asyncio
async def test_outbox_row_changed_after_claim_is_not_sent(state):
    item_id = await approved_item(state)
    provider = Recorder()
    sender = make_sender(state, provider, require_approval=False)
    item = await state.get_outbox_item(item_id)
    await state.update_outbox_item(item_id, status="cancelled")
    assert await sender._claim_send(item, "<m@x.co>") is None
    assert provider.sent == []


# ── Adapter contracts ──


class _Persona:
    name, email = "Franco", "franco@x.co"


class _Cfg:
    persona = _Persona


def _gmail(monkeypatch, handler):
    from franco.integrations import gmail as G

    provider = G.GmailProvider.__new__(G.GmailProvider)
    provider.config = _Cfg

    async def token():
        return "t"

    provider._token = token
    real = httpx.AsyncClient

    def client(*a, **k):
        k["transport"] = httpx.MockTransport(handler)
        return real(*a, **k)

    monkeypatch.setattr(G.httpx, "AsyncClient", client)
    return provider


@pytest.mark.asyncio
@pytest.mark.parametrize("response,expect", [
    (httpx.ConnectError("down"), {"retriable": True, "ambiguous": False}),
    (httpx.ReadTimeout("slow"), {"retriable": False, "ambiguous": True}),
    (httpx.Response(500), {"retriable": False, "ambiguous": True}),
    (httpx.Response(429), {"retriable": True, "ambiguous": False}),
    (httpx.Response(400, json={}), {"retriable": False, "ambiguous": False}),
])
async def test_gmail_send_failures_are_classified(monkeypatch, response, expect):
    def handler(request):
        if isinstance(response, Exception):
            raise response
        return response

    result = await _gmail(monkeypatch, handler).send_email(
        "jane@acme.com", "hi", "body", message_id="<m@x.co>")
    assert not result.ok
    assert {"retriable": result.retriable, "ambiguous": result.ambiguous} == expect


@pytest.mark.asyncio
async def test_gmail_send_carries_the_committed_message_id(monkeypatch):
    seen = {}

    def handler(request):
        if request.url.path.endswith("/messages/send"):
            import base64
            raw = json.loads(request.content)["raw"]
            seen["raw"] = base64.urlsafe_b64decode(raw).decode()
            return httpx.Response(200, json={"id": "g1", "threadId": "t1"})
        return httpx.Response(200, json={"payload": {"headers": []}})

    result = await _gmail(monkeypatch, handler).send_email(
        "jane@acme.com", "hi", "body", message_id="<franco.abc@x.co>")
    assert result.ok and result.message_id == "<franco.abc@x.co>"
    assert "Message-ID: <franco.abc@x.co>" in seen["raw"]


@pytest.mark.asyncio
@pytest.mark.parametrize("answers,expect", [
    ([{"messages": [{"id": "1"}]}], FOUND),                 # by Message-ID
    ([{}, {"messages": [{"id": "2"}]}], FOUND),             # by recipient+subject+time
    ([{}, {}], ABSENT),                                      # Gmail is authoritative
    ([None], UNKNOWN),                                       # could not ask
])
async def test_gmail_find_sent(monkeypatch, answers, expect):
    queries = []

    def handler(request):
        queries.append(request.url.params.get("q"))
        answer = answers[len(queries) - 1]
        return httpx.Response(500) if answer is None else httpx.Response(200, json=answer)

    finding = await _gmail(monkeypatch, handler).find_sent(
        "<franco.abc@x.co>", "jane@acme.com", 'say "hi"', datetime(2026, 10, 20, 12, 0))
    assert finding == expect
    assert queries[0] == "rfc822msgid:franco.abc@x.co"
    if len(queries) > 1:
        # naive timestamps are UTC: 2026-10-20 12:00 UTC = 1792497600, minus 60 s
        assert queries[1] == 'in:sent to:jane@acme.com subject:"say  hi" after:1792497540'


@pytest.mark.asyncio
@pytest.mark.parametrize("folders,expect", [
    ({"Sent": [b"1"]}, FOUND),
    ({"Sent": []}, UNKNOWN),                 # SMTP never claims ABSENT
    ({}, UNKNOWN),                           # no sent folder at all
])
async def test_smtp_find_sent_never_claims_absent(monkeypatch, folders, expect):
    from franco.integrations import smtp_mail as S

    class FakeImap:
        def __init__(self, *a):
            self.folder = None

        def login(self, *a):
            return "OK", []

        def select(self, name, readonly=False):
            self.folder = name.strip('"')
            return ("OK", []) if self.folder in folders else ("NO", [])

        def search(self, charset, *criteria):
            assert criteria == ("HEADER", "Message-ID", "<m@x.co>")
            return "OK", [b" ".join(folders[self.folder])]

        def logout(self):
            pass

    monkeypatch.setattr(S.imaplib, "IMAP4_SSL", FakeImap)
    provider = S.SmtpImapProvider.__new__(S.SmtpImapProvider)
    provider.imap_host, provider.imap_port = "imap.x.co", 993
    provider.imap_user = provider.imap_pass = "u"
    assert await provider.find_sent("<m@x.co>") == expect


@pytest.mark.asyncio
async def test_base_provider_answers_unknown():
    assert await FakeProvider().find_sent("<m@x.co>") == UNKNOWN


@pytest.mark.asyncio
async def test_migration_adds_job_tables_to_an_existing_database(tmp_path):
    import sqlite3

    from franco.state import MIGRATIONS
    path = tmp_path / "old.db"
    sm = StateManager(str(path))
    await sm.init_db()
    with sqlite3.connect(path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == len(MIGRATIONS)
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"jobs", "job_attempts"} <= tables
    os.remove(path)


# ── Operator resolution in the dashboard ──


@pytest.mark.asyncio
async def test_operator_resolves_unconfirmed_sends(state, tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from franco import dashboard

    monkeypatch.setattr(dashboard, "DB_PATH", Path(state.db_path))
    found, missing = await approved_item(state), await approved_item(state, "bo@acme.com")
    class Flaky(Recorder):
        ambiguous = True

        async def send_email(self, *a, **k):
            if self.ambiguous:
                return SendResult(ok=False, error="timeout", ambiguous=True)
            return await super().send_email(*a, **k)

    provider = Flaky(finding=UNKNOWN)
    sender = make_sender(state, provider, require_approval=False)
    await sender._drain_due()
    await expire_leases(state)
    await sender._drain_due()
    assert {(await state.get_outbox_item(i))["status"] for i in (found, missing)} == {"needs_check"}

    client = TestClient(dashboard.app, base_url="http://localhost")
    outbox = client.get("/api/outbox").json()
    assert {r["id"] for r in outbox["needs_check"]} == {found, missing}
    assert len(outbox["dead_jobs"]) == 2

    assert client.post(f"/api/outbox/{found}/resolve", json={"action": "nope"}).status_code == 400
    assert client.post(f"/api/outbox/{found}/resolve", json={"action": "sent"},
                       headers={"origin": "https://evil.test"}).status_code == 403
    assert client.post(f"/api/outbox/{found}/resolve", json={"action": "sent"}).json()["success"]
    assert client.post(f"/api/outbox/{found}/resolve",
                       json={"action": "sent"}).status_code == 409    # only once
    assert client.post(f"/api/outbox/{missing}/resolve", json={"action": "resend"}).json()["success"]

    assert (await state.get_outbox_item(found))["status"] == "sent"
    jobs = JobStore(state)
    assert (await jobs.get_by_key(f"email.send:{found}"))["status"] == SUCCEEDED
    assert (await jobs.get_by_key(f"email.send:{missing}"))["status"] == QUEUED

    provider.finding, provider.ambiguous = ABSENT, False
    await sender._drain_due()                    # the released one sends, once
    assert [m["to"] for m in provider.sent] == ["bo@acme.com"]
    assert provider.sent[0]["message_id"] == f"<franco.{missing}@x.co>"
