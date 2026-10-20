"""A real Franco send worker for the crash tests (P01-A2). Run as a script.

    python tests/durable_send_worker.py DB MAILBOX MODE

The fake provider's "mailbox" is a file: a line written there is the
provider's acknowledgement, durable before the worker can record anything.

MODE:
  kill-after-ack   append to the mailbox, then SIGKILL this process before
                   Franco records the send
  kill-before-ack  SIGKILL inside send_email, before the provider has it
  recover          an ordinary cycle; find_sent reads the mailbox and is
                   authoritative (found / absent)
  recover-unknown  an ordinary cycle; find_sent cannot tell (unknown)
"""
import asyncio
import json
import os
import signal
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from franco.integrations.mail_provider import (  # noqa: E402
    ABSENT, FOUND, UNKNOWN, SendResult,
)
from franco.state import StateManager  # noqa: E402
from tests.test_outbox_native import FakeProvider, make_sender  # noqa: E402


class FileMailbox(FakeProvider):
    def __init__(self, path: Path, mode: str):
        super().__init__()
        self.path, self.mode = path, mode

    def _lines(self):
        if not self.path.exists():
            return []
        return [json.loads(x) for x in self.path.read_text().splitlines() if x.strip()]

    async def send_email(self, to_email, subject, body, thread_ref="", in_reply_to="",
                         message_id="", headers=None):
        if self.mode == "kill-before-ack":
            os.kill(os.getpid(), signal.SIGKILL)
        with open(self.path, "a") as f:
            f.write(json.dumps({"to": to_email, "message_id": message_id}) + "\n")
            f.flush()
            os.fsync(f.fileno())
        if self.mode == "kill-after-ack":
            os.kill(os.getpid(), signal.SIGKILL)
        return SendResult(ok=True, message_id=message_id, thread_ref="t")

    async def find_sent(self, message_id, to_email="", subject="", since=None):
        if self.mode == "recover-unknown":
            return UNKNOWN
        return FOUND if any(x["message_id"] == message_id for x in self._lines()) else ABSENT


async def main(db: str, mailbox: str, mode: str):
    state = StateManager(db)
    sender = make_sender(state, FileMailbox(Path(mailbox), mode), require_approval=False)
    # The dying worker takes no lease time, so the next process sees it expired.
    sender.lease_seconds = 0 if mode.startswith("kill") else 300
    await sender._drain_due()


if __name__ == "__main__":
    asyncio.run(main(*sys.argv[1:4]))
