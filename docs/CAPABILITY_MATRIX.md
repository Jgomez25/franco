# Franco capability matrix (P00)

**Status: DRAFT, version `2026-10-20.draft`.** This records what the code supports today and proposes a scope. Nobody has agreed it yet. The owner decisions listed at the end must be made before P00 can be marked complete, and before any provider work (P01+) starts.

The live view of this matrix is `GET /api/capabilities` and **Settings → Capabilities** in the dashboard. The registry behind both is `franco/capabilities.py`.

## Capability states

| State | Meaning | Executes? |
|---|---|---|
| `preview` | Exists only as a blueprint simulation (`/blueprint`); no production code. | No |
| `unavailable` | Code exists but configuration is missing, or scope excludes it. | No |
| `configured` | Required configuration is present; no successful provider exchange is recorded locally (or the last one is older than 30 days, shown as *stale*). | Yes |
| `degraded` | Configured, but local health reports a problem: the sending kill switch is on, or the last discovery run failed or went stale. | Yes, and the other gates still apply |
| `live` | Configured, with a successful exchange recorded in this install's database within 30 days. This is observed locally; the provider has not certified it. | Yes |

`require()` refuses `preview`, `unavailable` and unknown keys **before an adapter or HTTP client is built**. For discovery it runs before city geocoding, which is itself an external request. This gate adds to the approval, suppression, verification, budget and kill-switch gates; it never replaces them.

## Supported scope (proposed)

| Dimension | Supported now | Notes |
|---|---|---|
| Deployment | Single operator, one machine, dashboard on `127.0.0.1` only | No login exists. `dashboard.public` is excluded until P01 adds authentication. |
| User roles | One: the local operator (full control) | Rep/manager/admin roles are `preview` (P01). |
| Channel | Email only | SMS/WhatsApp is excluded. Calling is `preview` (P04). |
| Mail providers | Gmail/Workspace (OAuth), SMTP+IMAP, Instantly (legacy) | One is active per `channels.email.provider`. |
| Verification | Reoon, ZeroBounce, Hunter | Without one, addresses stay `guess` and are never sent. |
| Discovery | OpenStreetMap (free), DataForSEO Listings, DataForSEO SERP, Serper | Each runs only when configured; spend is estimated and capped per run. |
| Other inputs | CSV import (accounts, contacts, suppressions), LinkedIn, Cloudflare crawl | |
| Markets | Wherever the configured providers return data; geocoding and discovery are tuned for US cities | **Owner decision:** supported regions and their legal basis. |
| Volume | Default cap of 50 cold emails a day (`max_daily_sends`), a 15-minute heartbeat, and discovery capped by `--max-spend` | **Owner decision:** target volumes. |

## Preview only (blueprint, harness tasks)

| Key | Capability | Task |
|---|---|---|
| `team.roles` | Team roles, permissions, audit | P01 |
| `data.licensed` | Licensed contact database, waterfall enrichment | P02 |
| `sequences.multichannel` | Multi-channel sequence builder | P03 |
| `calls.telephony` | Calling workspace | P04 |
| `crm.sync` | CRM sync | P05 |
| `inbound.forms` | Inbound forms and routing | P06 |
| `meetings.scheduler` | Meeting scheduler | P07 |
| `mail.health` | Mailbox health and DNS monitoring | P08 |
| `deals.intelligence` | Deals and conversation intelligence | P09 |
| `reports.analytics` | Event-derived reporting | P10 |
| `extension.browser` | Browser extension | P11 |
| `ai.workspace` | Grounded AI workspace | P12 |

A capability leaves `preview` only when its task's implementation and verification are actually complete. The blueprint's "DESIGN PREVIEW" label stays until then.

## Explicit exclusions

| Key | Excluded | Why |
|---|---|---|
| `mailbox.purchase` | Buying domains or mailboxes | Purchases happen with the provider, never through Franco. |
| `mailbox.warming` | Inbox warming networks | They fake engagement. |
| `channel.sms` | SMS / WhatsApp outreach | Email is the only channel in scope. |
| `dashboard.public` | Serving the dashboard beyond localhost | There is no authentication until P01. |

## Fixtures never reach outreach (P00-A2)

Fictional data lives only on domains reserved by RFC 2606 and RFC 6761: `.test`, `.example`, `.invalid` and `.localhost`, plus `example.com`, `example.net` and `example.org`. That covers the blueprint, the screenshots and the benchmark. `franco/fixtures.py` recognises these domains, and they are refused at each of these points:

| Point | What happens |
|---|---|
| Pre-send gate (`gate.pre_send_check`) | Blocks every native send, whatever path staged it. |
| Native staging (`Sender._stage_campaign_native`) | Fixture addresses are not staged. |
| Instantly lead upload (`Sender._deploy_campaign`) | Fixture addresses are not uploaded. |
| Default sequencer export (`franco export`, `/api/export/prospects.csv`) | Fixture rows are dropped. `--all` remains the raw dump. |

The blueprint's exported prototype configuration (`kind: franco-prototype-configuration`, `simulated: true`) is refused in two places:

- by `load_config`, because JSON is valid YAML and a renamed export would otherwise load as `franco.yaml`;
- by every CSV import entry point (CLI and dashboard).

A static test confirms that `blueprint.js` contains no network primitive and no `/api/` path.

## Benchmark (100 synthetic contacts)

`python -m franco.benchmark` (add `--json` for the report) runs in a throwaway database and never opens `data/franco.db`.

The data is fixed:

- the seed is `20261020`;
- 90 unique contacts sit on `bench-NN.example` domains;
- 10 planted duplicates differ only in letter case;
- one company name contains `%` and `_` to test literal search.

Expected answers come from the generator's ground truth, not from the system under test.

| Gate | Metric | Threshold (proposed) |
|---|---|---|
| dup-unique | Contacts stored from 100 rows | Exactly the 90 expected addresses |
| dup-rejected | Planted duplicates rejected | 10, and no other rejections |
| dup-reimport | Contacts after re-importing the same file | Still 90 |
| filter-* (9 queries) | Precision / recall / total via `/api/contacts/search`, all pages | 1.0 / 1.0 / exact; no page overlap |
| time-import | Importing 100 rows | ≤ 15 s |
| time-search-p95 | Search request p95 | ≤ 250 ms |
| time-workflow | Import → search → export | ≤ 30 s |
| cost-network | External connections attempted | 0 |
| cost-spend | Recorded spend | $0.00 |
| fixture-gate | Synthetic addresses not refused *as fixtures* by the pre-send gate | 0 |
| fixture-export | Synthetic rows in the default sequencer export | 0 |

Timing ceilings were set **after** measuring on the reference Mac, which was under heavy shared load (load average about 40):

- import took 2.4–8.0 s;
- search p95 stayed under 40 ms;
- the whole workflow took 4.6–15 s.

The import is slow because it opens about 5.6 SQLite connections and commits once per row. That is a pre-existing cost P00 did not change. The 5 s / 10 s budgets in the first draft were unmeasured guesses and failed under load. The unit suite enforces the deterministic gates; the benchmark command enforces the timing ones.

## Owner decisions still open

1. Internal use only, or customer-facing SaaS.
2. Supported regions and the lawful basis for cold email in each.
3. Licensed data provider and permitted data uses (P02).
4. First CRM, telephony provider and calendar provider (P05, P04, P07).
5. Mailbox provider for production volume.
6. Hosting, identity service and infrastructure (P01).
7. Target daily and monthly volumes.
8. Retention policy for contacts, replies and logs.
9. Acceptance of the benchmark thresholds above, and a budget ceiling per run and per month.
