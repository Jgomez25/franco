# Franco workspace upgrade — October 20, 2026

Compared against Apollo's public product pages and public workspace screenshots. This is an incremental implementation in Franco, not a claim of full Apollo parity or an inspection of its authenticated product.

Sources:
- https://www.apollo.io/
- https://www.apollo.io/product/workspace
- https://www.apollo.io/product/waterfall

## Delivered

| Area | Franco before | Updated Franco |
| --- | --- | --- |
| Navigation | Horizontal list of 13 sections | Persistent desktop sidebar, scrollable mobile navigation, active states for shortcut navigation |
| Home | Review queue and pipeline counts | Existing queue plus three guided actions for discovery, qualification, and outreach |
| Contact discovery UI | Latest 200 contacts, unfiltered | Entire local database searchable by name/company/title/email; status, industry, verification and minimum-score filters |
| Prioritization | Fixed newest-first list | Score, name, and recency sorting with stable server pagination |
| Saved targeting | Signal-based cohorts, no contact filter presets | Named contact views stored in this browser, restore, overwrite by name, delete |
| Contact context | Table columns | Accessible contact profile dialog, research notes, company details, source and validated LinkedIn link |
| List export | Whole-book CSV export | Selected-contact CSV export across pages, including spreadsheet-formula neutralization |
| Visual design | Restrained editorial dashboard | Citrus action accents, rounded surfaces, clear hierarchy, local fonts, dark/light themes, reduced-motion support |

Existing prospecting, enrichment providers, campaigns, signals/cohorts, mail approval, suppression, imports, conversations and usage controls are retained. New exports are data exports only; they do not bypass sending checks or enroll anyone in a sequence.

## Remaining Apollo gaps

Apollo's product includes capabilities that require substantial services, commercial data, or third-party credentials. They were not implemented or represented by fake controls:

- Proprietary contact and company database: requires a licensed data source; Franco still uses its configured collectors and imported records.
- Parallel dialing, call recording and transcription: requires telephony infrastructure and call workflow implementation.
- Native CRM synchronization and a browser extension: require provider OAuth, mappings, conflict handling, and a separate extension package.
- Inbound forms, team routing and round-robin scheduling: require identity, hosted endpoints and calendar integration work.
- Domain/mailbox purchasing, inbox warming and reputation monitoring: require additional providers and operational infrastructure.
- Team permissions, shared views and enterprise administration: Franco remains a local, single-operator app.
- Visual multi-channel sequence editing and customizable reporting: existing sequences and dashboard reporting remain; no claim of full Apollo equivalents.

Saved views are browser-local filter presets, not shared lists. Selection persists across pages within the current search and clears when filters change. Selected export contains the records as loaded, including unverified contacts if selected. The verified filter checks email_verified; final send eligibility remains the responsibility of Franco's existing suppression and verification gates.

## Run locally

Checkout:
`/Users/jorge/Documents/github/franco`

```sh
cd /Users/jorge/Documents/github/franco
.venv/bin/python -m uvicorn franco.dashboard:app --host 127.0.0.1 --port 5555
```

Open http://127.0.0.1:5555. Use Settings and the existing setup checklist to configure your product and providers. No outreach has been sent. The dashboard upgrades and later blueprint/harness work are committed and pushed to the GitHub repository. No deployment was performed.

## Validation

- Python: 35 passed, 2 packaging tests skipped (optional wheel-build checks).
- JavaScript: 2 tests passed, covering CSV formula escaping and corrupted saved-view storage.
- JavaScript syntax checks and git whitespace checks passed.
- Browser: empty install, populated isolated sample database, desktop light/dark themes, mobile 390px, paging, cross-page selection, filtering, saved-view creation/restore, profile dialog and CSV download.
- Downloaded CSV independently parsed: 15 selected verified contacts, correct column names and row count.
- Preview used a separate FRANCO_HOME under work/preview-home, with fictional example.test contacts. Sample contacts were not inserted into the checkout's database.

The screenshots show fictional preview data. Live provider calls, AI generation and mail delivery were not exercised because credentials and product setup are not configured.
