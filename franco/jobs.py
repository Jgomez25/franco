"""Durable jobs: leases, idempotency keys, retry budgets, dead letters.

A job is one unit of side-effecting work (``email.send`` today). Its life:

    queued ──claim──▶ leased ──succeed──▶ succeeded
                        │ └──fail(retriable, budget left)──▶ retry_wait ──claim──▶ …
                        │ └──fail(otherwise) / unconfirmed──▶ dead
                        └──lease expires (worker died)──▶ recovered by the owner
    queued / retry_wait ──cancel──▶ cancelled

A job whose lease expired is never simply claimed again. The worker may have
died after the provider accepted the work and before anything was recorded
locally, so the owner of the job kind must reconcile with the provider first
(``recover``): found means succeeded, absent means retry, unknown means dead
for a human to inspect. That is what stops a crash from becoming a
duplicate send.

Every claim is a compare-and-set, so two workers can never hold one job.
Timestamps are naive UTC ISO strings, like the rest of the database.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import aiosqlite

QUEUED, LEASED, RETRY_WAIT, SUCCEEDED, DEAD, CANCELLED = (
    "queued", "leased", "retry_wait", "succeeded", "dead", "cancelled")
CLAIMABLE = (QUEUED, RETRY_WAIT)
FINAL = (SUCCEEDED, DEAD, CANCELLED)

DEFAULT_LEASE_SECONDS = 300
DEFAULT_MAX_ATTEMPTS = 5
BACKOFF_BASE_SECONDS = 60
BACKOFF_CAP_SECONDS = 3600


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def backoff(attempt: int) -> timedelta:
    """60 s, 120 s, 240 s … capped at an hour. Deterministic (no jitter):
    one local worker has no thundering herd to spread out."""
    return timedelta(seconds=min(BACKOFF_CAP_SECONDS,
                                 BACKOFF_BASE_SECONDS * 2 ** max(0, attempt - 1)))


class JobStore:
    def __init__(self, state):
        self.state = state

    def _connect(self):
        return self.state._connect()

    async def _one(self, db, sql: str, params: tuple) -> dict | None:
        db.row_factory = aiosqlite.Row
        async with db.execute(sql, params) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None

    async def enqueue(self, kind: str, idempotency_key: str, input_ref: str = "",
                      max_attempts: int = DEFAULT_MAX_ATTEMPTS,
                      workspace_id: str = "local") -> dict:
        """Create the job, or return the existing one for this key."""
        async with self._connect() as db:
            await db.execute(
                "INSERT OR IGNORE INTO jobs (id, workspace_id, kind, idempotency_key, "
                "input_ref, max_attempts) VALUES (?, ?, ?, ?, ?, ?)",
                (uuid.uuid4().hex[:12], workspace_id, kind, idempotency_key,
                 input_ref, int(max_attempts)))
            await db.commit()
            return await self._one(db, "SELECT * FROM jobs WHERE idempotency_key = ?",
                                   (idempotency_key,))

    async def get(self, job_id: str) -> dict | None:
        async with self._connect() as db:
            return await self._one(db, "SELECT * FROM jobs WHERE id = ?", (job_id,))

    async def get_by_key(self, idempotency_key: str) -> dict | None:
        async with self._connect() as db:
            return await self._one(db, "SELECT * FROM jobs WHERE idempotency_key = ?",
                                   (idempotency_key,))

    async def claim(self, job_id: str, lease_seconds: int = DEFAULT_LEASE_SECONDS,
                    now: datetime | None = None) -> dict | None:
        """Lease a claimable, due job and open an attempt. None if not claimed."""
        now = now or utcnow()
        lease = (now + timedelta(seconds=lease_seconds)).isoformat()
        async with self._connect() as db:
            cur = await db.execute(
                "UPDATE jobs SET status = ?, attempt_count = attempt_count + 1, "
                "lease_until = ?, updated_at = ? WHERE id = ? AND status IN (?, ?) "
                "AND attempt_count < max_attempts "
                "AND (next_attempt_at IS NULL OR next_attempt_at <= ?)",
                (LEASED, lease, now.isoformat(), job_id, *CLAIMABLE, now.isoformat()))
            if cur.rowcount != 1:
                await db.rollback()
                return None
            job = await self._one(db, "SELECT * FROM jobs WHERE id = ?", (job_id,))
            await db.execute(
                "INSERT INTO job_attempts (id, job_id, attempt, started_at) VALUES (?, ?, ?, ?)",
                (uuid.uuid4().hex[:12], job_id, job["attempt_count"], now.isoformat()))
            await db.commit()
            return job

    async def finish(self, job_id: str, attempt: int, status: str, outcome: str,
                      error_code: str = "", error: str = "", result_ref: str = "",
                      next_attempt_at: datetime | None = None,
                      now: datetime | None = None) -> None:
        now = (now or utcnow()).isoformat()
        async with self._connect() as db:
            await db.execute(
                "UPDATE jobs SET status = ?, lease_until = NULL, next_attempt_at = ?, "
                "error_code = ?, error = ?, result_ref = CASE WHEN ? != '' THEN ? "
                "ELSE result_ref END, updated_at = ? WHERE id = ?",
                (status, next_attempt_at.isoformat() if next_attempt_at else None,
                 error_code, error[:500], result_ref, result_ref, now, job_id))
            await db.execute(
                "UPDATE job_attempts SET outcome = ?, error = ?, ended_at = ? "
                "WHERE job_id = ? AND attempt = ?",
                (outcome, error[:500], now, job_id, int(attempt)))
            await db.commit()

    async def succeed(self, job_id: str, attempt: int, result_ref: str = "",
                      outcome: str = "succeeded") -> None:
        await self.finish(job_id, attempt, SUCCEEDED, outcome, result_ref=result_ref)

    async def fail(self, job_id: str, attempt: int, error_code: str, error: str,
                   retriable: bool, now: datetime | None = None) -> str:
        """Record a failed attempt. Returns the job's new status."""
        now = now or utcnow()
        job = await self.get(job_id)
        if retriable and job and job["attempt_count"] < job["max_attempts"]:
            await self.finish(job_id, attempt, RETRY_WAIT, "failed", error_code, error,
                               next_attempt_at=now + backoff(attempt), now=now)
            return RETRY_WAIT
        await self.finish(job_id, attempt, DEAD, "failed", error_code, error, now=now)
        return DEAD

    async def recover(self, job_id: str, attempt: int, finding: str,
                      now: datetime | None = None) -> str:
        """Close an attempt whose lease expired, by what reconciliation found.

        ``found``: the side effect happened; succeeded. ``absent``: it did
        not; retry now (counts against the budget). ``unknown``: nobody can
        tell; dead, for a human to decide.
        """
        now = now or utcnow()
        if finding == "found":
            await self.finish(job_id, attempt, SUCCEEDED, "lease_expired_found", now=now)
            return SUCCEEDED
        if finding == "absent":
            job = await self.get(job_id)
            if job and job["attempt_count"] < job["max_attempts"]:
                await self.finish(job_id, attempt, RETRY_WAIT, "lease_expired_absent",
                                   "lease_expired", "worker stopped before the provider "
                                   "accepted the work", next_attempt_at=now, now=now)
                return RETRY_WAIT
            await self.finish(job_id, attempt, DEAD, "lease_expired_absent",
                               "retries_exhausted", "retry budget used up", now=now)
            return DEAD
        await self.finish(job_id, attempt, DEAD, "lease_expired_unknown", "unconfirmed",
                           "worker stopped mid-send and the provider cannot confirm "
                           "whether it was delivered", now=now)
        return DEAD

    async def cancel(self, job_id: str) -> bool:
        async with self._connect() as db:
            cur = await db.execute(
                "UPDATE jobs SET status = ?, updated_at = ? WHERE id = ? AND status IN (?, ?)",
                (CANCELLED, utcnow().isoformat(), job_id, *CLAIMABLE))
            await db.commit()
            return cur.rowcount == 1

    async def requeue(self, job_id: str, extra_attempts: int = 1) -> None:
        """A human decided a dead job may run again: grant attempts, queue it."""
        async with self._connect() as db:
            await db.execute(
                "UPDATE jobs SET status = ?, max_attempts = MAX(max_attempts, "
                "attempt_count + ?), next_attempt_at = NULL, error_code = '', error = '', "
                "updated_at = ? WHERE id = ? AND status = ?",
                (QUEUED, int(extra_attempts), utcnow().isoformat(), job_id, DEAD))
            await db.commit()

    async def expired(self, kind: str, now: datetime | None = None) -> list[dict]:
        """Leased jobs of this kind whose worker is presumed dead."""
        now = now or utcnow()
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM jobs WHERE kind = ? AND status = ? AND lease_until < ?",
                (kind, LEASED, now.isoformat())) as cur:
                return [dict(r) for r in await cur.fetchall()]

    async def list(self, status: str | None = None, kind: str | None = None,
                   limit: int = 100, workspace_id: str | None = None) -> list[dict]:
        where, params = [], []
        if workspace_id is not None:
            where.append("workspace_id = ?")
            params.append(workspace_id)
        if status:
            where.append("status = ?")
            params.append(status)
        if kind:
            where.append("kind = ?")
            params.append(kind)
        sql = "SELECT * FROM jobs" + (" WHERE " + " AND ".join(where) if where else "")
        sql += " ORDER BY updated_at DESC LIMIT ?"
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(sql, (*params, int(limit))) as cur:
                return [dict(r) for r in await cur.fetchall()]

    async def attempts(self, job_id: str) -> list[dict]:
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM job_attempts WHERE job_id = ? ORDER BY attempt",
                (job_id,)) as cur:
                return [dict(r) for r in await cur.fetchall()]
