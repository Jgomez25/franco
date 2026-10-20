# Franco capability matrix (P00)

**Status: owner decisions recorded, version `2026-10-20.owner-decisions`.** The owner settled deployment mode, regions, volume and the benchmark gates on 2026-10-20. Provider choices for later tasks are still open (see the end). P00 still needs independent human review before it counts as complete.

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

## Supported scope

| Dimension | Supported now | Notes |
|---|---|---|
| Deployment | Single operator, one machine, dashboard on `127.0.0.1` only | **Decided 2026-10-20:** internal use by one operator. No login, no tenancy; `dashboard.public` is excluded. |
| User roles | One: the local operator (full control) | **Decided:** team roles are excluded (`team.roles`). |
| Channel | Email only | SMS/WhatsApp is excluded. Calling is `preview` (P04). |
| Mail providers | Gmail/Workspace (OAuth), SMTP+IMAP, Instantly (legacy) | One is active per `channels.email.provider`. |
| Verification | Reoon, ZeroBounce, Hunter | Without one, addresses stay `guess` and are never sent. |
| Discovery | OpenStreetMap (free), DataForSEO Listings, DataForSEO SERP, Serper | Each runs only when configured; spend is estimated and capped per run. |
| Other inputs | CSV import (accounts, contacts, suppressions), LinkedIn, Cloudflare crawl | |
| Markets | United States only | **Decided 2026-10-20.** Cold email follows CAN-SPAM: the opt-out and postal-address footer are already enforced. Franco does not detect a recipient's country, so prospecting stays pointed at US targets in the ICP. |
| Volume | Up to 50 cold emails a day (`max_daily_sends`), a 15-minute heartbeat, and discovery capped by `--max-spend` | **Decided 2026-10-20:** plan for up to 50 a day on one dedicated mailbox. |

## Preview only (blueprint, harness tasks)

| Key | Capability | Task |
|---|---|---|
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
| `dashboard.public` | Serving the dashboard beyond localhost | Single-operator local app; there is no login. |
| `team.roles` | Team accounts, roles, shared access | Single operator by owner decision. |

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

| Gate | Metric | Threshold (agreed 2026-10-20) |
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

## Owner decisions

Decided on 2026-10-20:

1. **Deployment:** internal use, one operator, localhost only.
2. **Regions:** United States only.
3. **Volume:** up to 50 cold emails a day.
4. **Benchmark:** the gates above, as proposed.

Still open. None of these blocks P00; each must be decided before the task that needs it:

1. Licensed data provider and permitted data uses (P02).
2. Telephony, CRM and calendar providers (P04, P05, P07). Those capabilities stay `preview` until chosen.
3. Production mailbox provider. Gmail/Workspace on a dedicated secondary domain is the documented recommendation.
4. Retention policy for contacts, replies and logs.
5. A monthly spend ceiling. Today only the per-run discovery cap exists (`--max-spend`, $1 by default in the dashboard).
