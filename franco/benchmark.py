"""The P00 benchmark: 100 synthetic contacts and the gates they must pass.

Run it with ``python -m franco.benchmark`` (add ``--json`` for the report).
It always works in a throwaway database under a temporary directory and
never opens data/franco.db, so it cannot mix fixtures into real data.

The contacts are generated, not sampled: a fixed seed, fictional names,
and companies on the reserved ``.example`` TLD (RFC 2606), so every address
is a fixture that the outreach paths must refuse. Ten rows are planted
duplicates (same address, different case) and one company name contains
SQL wildcard characters, so the expected answers are known in advance and
the system's answers are compared against them exactly.

Gates (thresholds agreed by the owner on 2026-10-20; see the matrix):

- duplicates: 90 unique contacts stored from 100 rows; the 10 planted
  duplicates are rejected; re-importing the same file adds nothing.
- filters: every benchmark query returns exactly the expected contact set
  (precision and recall 1.0), and paging covers it with no overlap.
- timing: importing 100 rows, each search request, and the whole
  import → search → export workflow stay under fixed wall-clock budgets.
- cost: zero external network connections, zero recorded spend.
- fixtures: no synthetic address passes the pre-send gate or appears in
  the default sequencer export.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import io
import json
import random
import socket
import statistics
import sys
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path

SEED = 20261020
TOTAL_ROWS = 100
PLANTED_DUPLICATES = 10

# Timing ceilings are user-facing budgets on a developer laptop, agreed by
# the owner on 2026-10-20. Measured on the reference Mac (2026-10-20): import
# 2.4-8.0 s (about 5.6 SQLite connections and one commit per row), search
# p95 under 40 ms, workflow 4.6-15 s. The import cost is pre-existing.
GATES = {
    "import_seconds_max": 15.0,
    "search_ms_p95_max": 250.0,
    "workflow_seconds_max": 30.0,
    "external_connections_max": 0,
    "spend_usd_max": 0.0,
}

FIRST = ["Avery", "Morgan", "Riley", "Jordan", "Casey", "Quinn", "Rowan", "Sage",
         "Emerson", "Hayden", "Parker", "Reese", "Skyler", "Taylor", "Dakota"]
LAST = ["Chen", "Ellis", "Brooks", "Hart", "Lane", "Moss", "Reyes", "Stone",
        "Vale", "Wells", "Yates", "Frost", "Grant", "Hale", "Kerr"]
INDUSTRIES = ["Software", "Retail", "Healthcare", "Logistics"]
TITLES = ["VP Sales", "Head of Growth", "Founder", "Marketing Director", "COO"]
STATUSES = ["verified", "verified", "risky", "guess"]


def synthetic_contacts(seed: int = SEED) -> tuple[list[dict], list[dict]]:
    """(rows to import, ground truth for the unique contacts).

    Deterministic for a given seed. Ground truth is built alongside the
    rows, never read back from the system under test.
    """
    rng = random.Random(seed)
    unique = TOTAL_ROWS - PLANTED_DUPLICATES
    truth = []
    for i in range(unique):
        company = "100% Uptime_Co" if i == 7 else f"Benchmark {i:02d} Labs"
        domain = f"bench-{i:02d}.example"
        first, last = FIRST[i % len(FIRST)], LAST[(i * 7) % len(LAST)]
        truth.append({
            "company": company, "domain": domain,
            "first_name": first, "last_name": last,
            "title": TITLES[i % len(TITLES)],
            "email": f"{first}.{last}.{i:02d}@{domain}".lower(),
            # Independent of the industry cycle, so combined filters mean something.
            "email_status": STATUSES[(i // len(INDUSTRIES)) % len(STATUSES)],
            "segment": INDUSTRIES[i % len(INDUSTRIES)],
            "location": "Denver, CO",
        })
    rows = [dict(t) for t in truth]
    for i in rng.sample(range(unique), PLANTED_DUPLICATES):
        dup = dict(truth[i])
        dup["email"] = dup["email"].upper()     # same person, different case
        rows.insert(rng.randrange(i + 1, len(rows) + 1), dup)
    return rows, truth


def to_csv(rows: list[dict]) -> str:
    from franco.importer import CONTACT_COLUMNS

    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=list(CONTACT_COLUMNS), extrasaction="ignore")
    writer.writeheader()
    writer.writerows(rows)
    return buf.getvalue()


def expected_queries(truth: list[dict]) -> list[tuple[str, dict, set[str]]]:
    """(name, search params, expected emails) worked out from ground truth."""
    every = {t["email"] for t in truth}
    verified = {t["email"] for t in truth if t["email_status"] == "verified"}
    software = {t["email"] for t in truth if t["segment"] == "Software"}
    return [
        ("all", {}, every),
        ("industry", {"industry": "Software"}, software),
        ("verified", {"verified": "true"}, verified),
        ("industry+verified", {"industry": "Software", "verified": "true"}, software & verified),
        ("literal-wildcards", {"q": "100% Uptime_"},
         {t["email"] for t in truth if "100% Uptime_" in t["company"]}),
        ("wildcard-not-pattern", {"q": "Bench_ark"}, set()),
        ("name", {"q": "avery"},
         {t["email"] for t in truth if "avery" in (t["first_name"] + " " + t["last_name"]).lower()}),
        ("status", {"status": "new"}, every),
        ("no-match", {"q": "zz-nothing"}, set()),
    ]


@contextmanager
def count_external_connections():
    """Count (and refuse) every non-loopback socket connection while active."""
    attempts: list[str] = []
    real_connect = socket.socket.connect

    def guarded(self, address):
        host = address[0] if isinstance(address, tuple) else str(address)
        if host not in ("127.0.0.1", "::1", "localhost") and self.family != socket.AF_UNIX:
            attempts.append(str(host))
            raise ConnectionRefusedError("benchmark: external connection refused")
        return real_connect(self, address)

    socket.socket.connect = guarded
    try:
        yield attempts
    finally:
        socket.socket.connect = real_connect


def _gate(gate_id: str, metric: str, threshold, actual, passed: bool) -> dict:
    return {"id": gate_id, "metric": metric, "threshold": threshold,
            "actual": actual, "passed": bool(passed)}


async def run_benchmark(workdir: Path) -> dict:
    from fastapi.testclient import TestClient

    from franco import dashboard
    from franco.export import collect_prospects
    from franco.gate import pre_send_check
    from franco.importer import run_import
    from franco.state import StateManager

    db_path = workdir / "benchmark.db"
    state = StateManager(db_path=str(db_path))
    await state.init_db()
    rows, truth = synthetic_contacts()
    text = to_csv(rows)
    gates: list[dict] = []

    with count_external_connections() as attempts:
        started = time.perf_counter()
        report = await run_import(state, "contacts", text)
        import_seconds = time.perf_counter() - started
        again = await run_import(state, "contacts", text)

        import aiosqlite
        async with aiosqlite.connect(db_path) as db:
            async with db.execute("SELECT email FROM prospects") as cur:
                stored_emails = {r[0] for r in await cur.fetchall()}
            async with db.execute("SELECT COALESCE(SUM(cost_usd), 0) FROM runs") as cur:
                spend = float((await cur.fetchone())[0])

        dup_rejections = [w for _, w in report.rejected if "duplicate row" in w]
        gates.append(_gate("dup-unique", "contacts stored from 100 rows", len(truth),
                           len(stored_emails), stored_emails == {t["email"] for t in truth}))
        gates.append(_gate("dup-rejected", "planted duplicates rejected", PLANTED_DUPLICATES,
                           len(dup_rejections), len(dup_rejections) == PLANTED_DUPLICATES
                           and len(report.rejected) == PLANTED_DUPLICATES))
        async with aiosqlite.connect(db_path) as db:
            async with db.execute("SELECT COUNT(*) FROM prospects") as cur:
                after_again = (await cur.fetchone())[0]
        gates.append(_gate("dup-reimport", "contacts after re-importing the same file",
                           len(truth), after_again, after_again == len(truth)
                           and len(again.imported) + len(again.rejected) == TOTAL_ROWS))

        # Filters, through the same HTTP endpoint the Contacts tab uses.
        saved_db = dashboard.DB_PATH
        dashboard.DB_PATH = db_path
        timings: list[float] = []
        try:
            client = TestClient(dashboard.app, base_url="http://localhost")
            for name, params, expected in expected_queries(truth):
                got, page, overlap = set(), 1, False
                while True:
                    t0 = time.perf_counter()
                    body = client.get("/api/contacts/search",
                                      params={**params, "page_size": 25, "page": page}).json()
                    timings.append((time.perf_counter() - t0) * 1000)
                    emails = {p["email"] for p in body["items"]}
                    overlap |= bool(got & emails)
                    got |= emails
                    if page * body["page_size"] >= body["total"]:
                        break
                    page += 1
                precision = len(got & expected) / len(got) if got else 1.0
                recall = len(got & expected) / len(expected) if expected else 1.0
                gates.append(_gate(f"filter-{name}", "precision/recall/total",
                                   [1.0, 1.0, len(expected)],
                                   [round(precision, 3), round(recall, 3), body["total"]],
                                   got == expected and body["total"] == len(expected)
                                   and not overlap))
        finally:
            dashboard.DB_PATH = saved_db

        p95 = (statistics.quantiles(timings, n=20)[-1] if len(timings) >= 20
               else max(timings))
        exported = await collect_prospects(state)
        workflow_seconds = time.perf_counter() - started

        # Every synthetic address must be refused *because it is a fixture*,
        # not merely for some other reason (unverified, missing footer).
        from franco.fixtures import FIXTURE_REASON
        gate_passes = 0
        for t in truth:
            p = await state.get_prospect_by_email(t["email"])
            footer = "Sender, 1 Main St. Reply 'stop' to opt out."
            result = pre_send_check(t["email"], "Quick question", "Short note. " + footer,
                                    prospect=p, allow_risky=True, required_footer=footer)
            if result.ok or FIXTURE_REASON not in result.reasons:
                gate_passes += 1

    gates.append(_gate("time-import", "seconds to import 100 rows",
                       GATES["import_seconds_max"], round(import_seconds, 3),
                       import_seconds <= GATES["import_seconds_max"]))
    gates.append(_gate("time-search-p95", "search request p95 (ms)",
                       GATES["search_ms_p95_max"], round(p95, 1),
                       p95 <= GATES["search_ms_p95_max"]))
    gates.append(_gate("time-workflow", "import → search → export (s)",
                       GATES["workflow_seconds_max"], round(workflow_seconds, 3),
                       workflow_seconds <= GATES["workflow_seconds_max"]))
    gates.append(_gate("cost-network", "external connections attempted",
                       GATES["external_connections_max"], len(attempts),
                       len(attempts) <= GATES["external_connections_max"]))
    gates.append(_gate("cost-spend", "recorded spend (USD)", GATES["spend_usd_max"],
                       spend, spend <= GATES["spend_usd_max"]))
    gates.append(_gate("fixture-gate", "synthetic addresses not refused as fixtures", 0,
                       gate_passes, gate_passes == 0))
    gates.append(_gate("fixture-export", "synthetic rows in the default sequencer export", 0,
                       len(exported), not exported))
    return {"seed": SEED, "rows": TOTAL_ROWS, "unique": len(truth),
            "passed": all(g["passed"] for g in gates), "gates": gates}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the P00 synthetic benchmark.")
    parser.add_argument("--json", action="store_true", help="print the full JSON report")
    args = parser.parse_args(argv)
    with tempfile.TemporaryDirectory(prefix="franco-benchmark-") as tmp:
        result = asyncio.run(run_benchmark(Path(tmp)))
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        for g in result["gates"]:
            mark = "PASS" if g["passed"] else "FAIL"
            print(f"  {mark}  {g['id']:24} {g['metric']}: {g['actual']} "
                  f"(gate {g['threshold']})")
        print(f"\n  benchmark {'passed' if result['passed'] else 'FAILED'}: "
              f"{sum(g['passed'] for g in result['gates'])}/{len(result['gates'])} gates")
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
