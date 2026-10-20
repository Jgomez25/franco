# What full Apollo parity would take

Prepared October 20, 2026. This is a planning assessment based on Franco's code and Apollo's public product descriptions, not an audit of Apollo's private implementation. Define a fixed scope before treating “parity” as a finish line.

## What “configured collectors and imported records” means

Franco is the software that collects and works with data. It does not ship with a comprehensive database of businesses and their employees.

In this repository:
- `franco/collectors/discover.py` has configurable sources such as DataForSEO Business Listings, DataForSEO SERP, Serper and OpenStreetMap/Overpass. These find businesses or search results; they do not automatically constitute an Apollo-size verified people database.
- Website/profile research and the email-finder integrations can add detail. Coverage depends on source availability, configuration, credits, and the underlying sites.
- CSV import adds records you supply. Searching the Franco UI searches the local records already collected or imported.

Example: asking for “VPs of Sales at US software companies with 50–200 employees” requires searchable person-level data. An empty Franco install cannot answer that across the market. You would first connect an appropriately licensed people-data API, obtain/verify records, or import a list. A new UI cannot manufacture this coverage.

Apollo advertises a large maintained contact/company dataset and enrichment services. Using a provider API could supply some comparable workflows, but access, coverage, export rights, redistribution rights, credits and refresh limits need to be checked against your actual contract. Buying API access is not automatically permission to resell the data.

## Three different goals

| Goal | What it means | Planning range, not a quote |
| --- | --- | --- |
| Your team's practical replacement | One ICP, one CRM, a licensed data source, email sequences, approvals, basic reports and dependable operations | Roughly 2–4 experienced engineers for 3–6 months, with focused product/design and operational support |
| Broad provider-backed product | Team accounts, shared data, multiple channels, inbound, scheduling, calling, sync, extension, analytics, permissions, recovery and support | Roughly 6–10 engineers plus product/design/QA/operations over 12–24 months |
| Comparable data asset and market-scale reliability | Broad fresh proprietary coverage, quality operations, provider relationships, deliverability infrastructure, enterprise support and years of production learning | Multi-year organization-level effort; not credibly bounded by a UI build or a single coding estimate |

These are my order-of-magnitude engineering estimates, not market statistics. They assume mature third-party services, experienced engineers, limited initial providers, and disciplined scope. They exclude building a proprietary global dataset, and they can move substantially after P00 discovery. LLM assistance can accelerate implementation and tests; it does not remove integration debugging, provider contracts, validation, operational ownership or customer support.

For budgeting, calculate labor separately from data, telephony, email, AI, storage and infrastructure. As an illustrative arithmetic model only, 6–10 engineers for 1–2 years at an assumed $150k–$250k fully loaded annual cost gives $0.9m–$5m in engineering labor. This is not a salary survey, vendor quote or all-in project cost. Use your actual rates and usage forecasts to replace these assumptions.

## What you actually need

1. **A bounded product specification.** Users, supported markets, sources, channels, providers, record volumes, workflow concurrency and success metrics. “Every Apollo feature” is a moving target, including features marketed as coming soon.
2. **A data strategy.** License person/company data or build a narrower owned dataset. Track field provenance, verification time, refresh, identity resolution, allowed use, deletion and suppression.
3. **A reliable application platform.** Hosted identity, tenant/role enforcement, transactional state, durable jobs, secrets, backups, queues, retries, observability, audit history and incident response. Franco's current localhost trust model is insufficient for a public team service.
4. **Real execution integrations.** CRM OAuth and bidirectional sync, mailbox authorization and sending controls, telephony and call events, calendar availability and booking, inbound forms and assignment, extension capture. Start with one provider per category.
5. **Quality and operational evidence.** Benchmarked data accuracy, synchronization correctness, no duplicate sends/bookings, tested recovery, throughput and cost limits, support and rollout procedures. A feature that only works once with mock data is not production parity.

Recommended order: foundation → licensed data → safe email sequences → one CRM → measurement → scheduling and inbound → calling/deals → extension and broader AI workflows. The harness records a dependency graph rather than assuming every workstream must execute in that exact linear order.

## What was created in this change

- A separate `/blueprint` route linked from Franco's dashboard.
- Twelve interactive feature concepts plus an overview. Simulations include enrichment, sequence editing/validation, a call lifecycle and disposition, CRM diff preview, routing, booking, mailbox checks, deal-stage progression, period filters, role permissions, capture preview and a fixed AI plan.
- Every screen labels fictional data and simulated execution. No real provider is connected, no LLM is called, and nothing is sent or purchased.
- Prototype state lasts until reload; export downloads choices as JSON. This is not production configuration or an import/migration format.
- A model-neutral harness with 13 work packages (P00–P12), dependencies, source entry points, delivery requirements, acceptance IDs, architecture contracts, a master implementer prompt and an evidence template.
- A standard-library Python CLI selects the next eligible task and checks evidence structure. It does not run the LLM or independently verify claimed evidence. Mocked, sandbox and production verification are distinct.

## How to use the LLM harness

From the repository root:

```sh
python3 harness/parity/run.py status
python3 harness/parity/run.py next > /tmp/franco-next-task.md
```

Give your coding LLM that prompt, repository access, and `harness/parity/ARCHITECTURE.md`. Have it implement one task, run tests, and propose evidence. Review the patch and actual test artifacts, then fill `harness/parity/evidence/P00.json` from the template. Do not fill the reviewer field with an invented approval.

```sh
python3 harness/parity/run.py verify P00
python3 harness/parity/run.py next
```

The CLI does not silently promote mock functionality to live capability. Evidence acceptance is a structural check, not certification. Use the verification label and independent review to decide whether a task is deployment-ready.

## Verification performed

- 4 harness tests: absent evidence, invalid structures, incomplete dependencies, live-claim evidence requirement, and task/prompt consistency.
- 35 Python regression tests passed, including blueprint routes/assets and existing dashboard behavior; 2 optional packaging checks skipped.
- JavaScript syntax checks and existing contact CSV/storage tests.
- Browser exercises: sequence add/validate; simulated enrichment; call start/end/log; inbound rule selection; meeting booking; CRM preview; analytics period selection; permission role change; fixed AI plan; remaining module navigation and dialogs.
- Desktop and mobile visual checks. No live provider tests were performed; all blueprint workflows are synthetic.

## Sources

- [Apollo Workspace](https://www.apollo.io/product/workspace): public descriptions of sequencing, dialing, inbound, scheduling, deliverability, AI and reporting.
- [Apollo Data](https://www.apollo.io/product/b2b-data): public data and verification description.
- [Apollo Developer Docs](https://docs.apollo.io/): API documentation entry point; actual account access and licensing still require confirmation.
- Franco repository: collectors, state, dashboard, agents, integrations and tests inspected in this checkout.
