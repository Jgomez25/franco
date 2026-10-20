# Franco parity implementation harness

This is a model-neutral task and evidence harness, not an autonomous coding agent or a guarantee of Apollo parity. It works with any coding LLM that can read the repository and edit/test code. No API key is needed to generate prompts. It does not call a model, execute model output, purchase services or deploy the app.

## Feed it to an LLM
From the Franco repository root:

```sh
python3 harness/parity/run.py status
python3 harness/parity/run.py next > /tmp/franco-next-task.md
```

Give the coding LLM the repository, `/tmp/franco-next-task.md`, `harness/parity/ARCHITECTURE.md`, and access to `franco/web/blueprint.*`. For a chat-only LLM, upload the relevant source files named in the task as well. A chat without source-editing/runtime tools can propose patches but cannot verify them.

To request a specific task:

```sh
python3 harness/parity/run.py prompt P02 > /tmp/franco-data-task.md
```

Implement one task at a time. Have a human review the patch, tests and actual logs. Copy the evidence template to `harness/parity/evidence/P00.json`, populate it honestly, and validate:

```sh
mkdir -p harness/parity/evidence
python3 harness/parity/run.py verify P00
python3 harness/parity/run.py next
```

`verify` exits nonzero for missing evidence, incomplete criteria or prerequisites. Evidence is self-reported: the harness validates structure and dependencies, not whether logs or reviewer claims are true. Mocked verification can unblock implementation dependencies but does not qualify the product for live deployment. Keep provider validation and operational readiness as distinct release gates.

## Contents
- `SYSTEM_PROMPT.md`: implementer behavior and review requirements.
- `tasks.json`: 13 ordered work packages, dependencies, source entry points and acceptance IDs.
- `ARCHITECTURE.md`: service boundaries, proposed contracts and invariants.
- `evidence-template.json`: review and verification record.
- `run.py`: dependency selection, prompt assembly and evidence validation (Python standard library only).
- `test_harness.py`: harness behavior tests using the standard library.

## Preview
Run Franco's local server and open `/blueprint`, or open [the standalone preview](../../docs/session-deliverables/franco-blueprint.html) in a browser. The [session guide](../../docs/SESSION_GUIDE.md) links all documents and downloadable deliverables. The demo uses fictional data and page-memory state. Export saves prototype choices, not production configuration. Sample calls, bookings, enrichments and AI output are simulations.

## Owner decisions before provider work
Choose internal-use versus customer-facing SaaS; supported regions; data provider and permitted data uses; first CRM; mailbox provider; telephony provider; calendar provider; infrastructure and identity service; target volumes; retention policy; quality thresholds; and budget limits. Record these in P00. Credentials belong in environment/secret management, not prompts, task files, browser code or evidence logs.

The blueprint does not mean these decisions have been made. Full parity requires agreeing a finite benchmark because Apollo's product changes over time.
