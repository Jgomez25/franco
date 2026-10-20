# Session deliverables and implementation entry point

All application changes, implementation harnesses, documents and shareable deliverables from this session are in this repository. The intended local checkout is `/Users/jorge/Documents/github/franco` (macOS may display the parent as `GitHub`).

## Start here

1. Read [the Apollo comparison](APOLLO_COMPARISON.md) for the implemented dashboard features.
2. Read [the parity roadmap](PARITY_ROADMAP.md) for the remaining production work, data requirements and effort estimates.
3. Open [the standalone interactive blueprint](session-deliverables/franco-blueprint.html) in your browser, or run Franco and visit `/blueprint`.
4. Give your coding LLM [the starter prompt](session-deliverables/START-HERE-LLM-PROMPT.md), repository access and [the architecture contract](../harness/parity/ARCHITECTURE.md).
5. Follow [the harness guide](../harness/parity/README.md) to select tasks and record reviewed evidence.

GitHub displays HTML source; download the standalone HTML to interact with it. The preview uses fictional data and simulated actions. No implementation task has been certified complete merely by creating its prototype.

## Application changes

| Files | Purpose |
| --- | --- |
| `franco/dashboard.py` | Database-wide filtered/paginated contact search API and blueprint route |
| `franco/web/app.js`, `app.css`, `index.html` | Workspace navigation, home actions, dashboard styling and blueprint link |
| `franco/web/contacts.js` | Contact search, filters, saved views, profiles, selection and CSV export |
| `franco/web/blueprint.html`, `blueprint.css`, `blueprint.js` | Twelve interactive feature concepts and overview |
| `tests/test_contact_workspace.py` | Search, pagination, filters, validation and route regressions |
| `tests/contact_workspace.test.cjs` | CSV escaping and saved-view corruption handling |

## LLM implementation harness

Everything needed to drive the work is under `harness/parity/`:

- `README.md`: operating instructions and prerequisites.
- `SYSTEM_PROMPT.md`: bounded-task implementation and review instructions.
- `ARCHITECTURE.md`: data/service boundaries, proposed API contracts and invariants.
- `tasks.json`: 13 work packages, dependencies, delivery requirements and acceptance IDs.
- `run.py`: task selection, prompt generation and evidence validation.
- `evidence-template.json`: template for honest, reviewed results.
- `test_harness.py`: dependency and evidence validation tests.
- `evidence/README.md`: where future review records belong; currently no task evidence is claimed.

```sh
python3 harness/parity/run.py status
python3 harness/parity/run.py next > /tmp/franco-next-task.md
python3 -m unittest discover -s harness/parity -p 'test_*.py'
```

The saved starter prompt always targets P00. Use `next` for the current eligible task after evidence has been added.

## Standalone deliverables

`docs/session-deliverables/` contains every user-facing artifact originally produced outside the checkout:

- `franco-blueprint.html`: self-contained interactive preview.
- `franco-workspace.jpg`, `franco-blueprint.jpg`: screenshots with sample data.
- `START-HERE-LLM-PROMPT.md`: generated P00 prompt.
- `franco-comparison.md`, `franco-parity-roadmap.md`: copies of the canonical documents above.
- `franco-llm-harness.zip`: portable harness, prototype source, starter prompt and documentation.
- `manifest.json`: SHA-256 inventory of session source and deliverables, excluding the manifest itself.

Regenerate the package and inventory after changing their inputs:

```sh
python3 scripts/package_parity.py
python3 scripts/package_parity.py --check
```

The check validates generated content and inventory hashes. It does not assert production capability or validate a model's implementation claims.

## Validation and local setup

The feature work passed 35 Python tests (2 optional packaging checks skipped), 2 JavaScript tests, 4 harness tests, and desktop/mobile browser checks. No real provider accounts were exercised.

```sh
.venv/bin/python -m uvicorn franco.dashboard:app --host 127.0.0.1 --port 5555
```

For a fresh clone, create a virtual environment and install the project as described in the main README. Local credentials, virtual environments, databases, caches, downloaded test CSVs and scratch preview databases are runtime artifacts and are not part of the GitHub source package.
