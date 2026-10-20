# Architecture constraints and contracts

## Existing system
Franco is a Python/FastAPI local application with SQLite state, browser JavaScript/CSS, collectors, agent orchestration, mail adapters, and explicit approval/suppression controls. It is not a multi-tenant SaaS. Keep that mode useful. Current host/origin guards are not shared-service authentication.

## Recommended evolution
For a hosted team product, introduce an authenticated API boundary, workspace-scoped service layer, Postgres or equivalent transactional store, durable job workers and queue, object storage for artifacts, secret management and event ingestion. This is a proposed architecture, not an installed dependency list. Select actual services in P00/P01. Avoid rewriting every working module; adapt existing business logic behind interfaces.

Boundaries:
- UI: capability-aware screens; never authority for authorization or spending.
- API: authentication, tenant and role policy, schema validation and idempotency.
- Services: workflows and domain transitions, reuse existing gates.
- Adapters: provider-specific auth, pagination, rate limits and normalized errors.
- Workers: durable leases, retries, cancellation, reconciliation and dead letters.
- Data: source/field provenance, ownership, event history and deletion propagation.

## Common contracts
Entity: id, workspace_id, version, created_at, updated_at. Every read and write must be tenant-scoped in shared mode.

Job: id, workspace_id, kind, idempotency_key, status, attempt_count, lease_until, next_attempt_at, input_ref, result_ref, error_code. Transitions: queued → leased → succeeded; leased → retry_wait or failed; queued/retry_wait → cancelled. Provider acknowledgement requires reconciliation before retry.

Provider error: category (auth, rate_limit, transient, validation, quota, permanent), retriable, retry_after, provider_request_id, sanitized_message. Never return tokens or raw provider payloads with private fields to logs.

Event: id, workspace_id, provider, provider_event_id, kind, subject_id, occurred_at, received_at, payload_version, payload_ref. Unique provider event keys prevent duplicate effects; event time and receipt time serve different purposes.

Field provenance: entity_id, field_name, source/provider, external_id, observed_at, verified_at, verification_method, confidence, permitted_use_reference. Unknown is not verified.

Proposed API families (design proposals; not existing routes): /v1/workspaces, /v1/jobs, /v1/data/search, /v1/enrichment/jobs, /v1/sequences, /v1/enrollments, /v1/calls, /v1/connections, /v1/sync-runs, /v1/forms, /v1/submissions, /v1/availability, /v1/bookings, /v1/mailboxes/health, /v1/opportunities, /v1/reports, /v1/action-proposals. Long-running requests return 202 + job_id; mutations accept idempotency keys and expected revision where needed. Reads use bounded cursor pagination.

## Production invariants
- Side effects need current eligibility, valid approval where applicable, authorization and budget immediately before dispatch.
- Exactly-once external delivery is not assumed. Use provider idempotency when supported plus local reconciliation.
- A sequence edit changes its revision and invalidates stale approvals.
- Deletion/suppression propagates to search indexes, derived AI context and future jobs.
- OAuth refresh and webhook handlers are tenant-bound, signed/validated and replay-safe.
- Provider disconnection exposes unavailable/degraded state; it never silently substitutes fictional data.
- Database migrations are versioned, backups are restorable and rollout has rollback/forward-repair plans.
- Operational metrics include queue age, job failure rate, retry count, provider latency, cost, and rejected side effects.

## Test layers
Unit tests for pure domain transitions; adapter contract tests for normalized behavior; integration tests against disposable databases and queues; provider sandbox tests for authentic external behavior; browser tests for complete flows; recovery tests for worker crashes and webhook reordering. Deploy only after restore, revoke, rotate and rollback procedures have been exercised.
