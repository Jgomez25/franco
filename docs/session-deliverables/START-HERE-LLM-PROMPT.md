You are implementing one bounded production capability in the Franco repository.

Read harness/parity/README.md, ARCHITECTURE.md, the assigned task, and applicable repository instructions. Inspect the actual source before deciding what exists. The checkout already contains user changes; preserve them. The blueprint in franco/web/blueprint.* is a synthetic interaction specification, not production functionality.

Work on the assigned task only. Report scope assumptions before edits. If prerequisites are missing, identify the specific dependency and implement only independent work. Never mark a task complete because a mock, screenshot, API shape or happy path works.

Required process:
1. Inspect context files and relevant tests. Map existing implementations before adding a parallel system.
2. Write a short implementation plan with data models, migrations, API contracts, failure states and test strategy.
3. Implement a vertical slice: persistence, service logic, API, UI and operational failure handling. Preserve backward compatibility or supply an explicit migration plan.
4. Keep all provider access behind adapters. Add deterministic fake-provider contract tests. Run sandbox tests only with explicitly available credentials and scoped authorization. Never fabricate credentials or live results.
5. Preserve approval, suppression, verification and budget gates. Do not send messages, make calls, purchase services, or expose the local dashboard publicly as a test.
6. Run appropriate tests, including the task acceptance cases, then record actual commands, results and log references. Report skipped/failed cases explicitly. Screen tests are additional evidence, not a substitute for service correctness.
7. Produce an evidence JSON using evidence-template.json. Include every assigned acceptance ID. Use result=incomplete if any requirement is unmet. Set verification=mocked until a real provider sandbox has been exercised. Provide provider_evidence for sandbox or production claims. Do not invent a reviewer, commit, test result or evidence artifact.
8. End with changed files, behavior, test results, remaining risks, configuration required and the next dependency. Do not advance another task automatically.

Completion requires independent human review of the implementation and evidence. The harness only validates structured reports. It does not prove correctness or authorize deployment. A mocked-complete task may unblock implementation work while still being unready for live use.

Use blueprint interactions and visual hierarchy as design input, while adding genuine loading, empty, error, partial success, unauthorized, disconnected, rate-limited, and stale-data states. Remove the preview label only for a capability whose production implementation and verification are actually complete. Never replace real application data with sample fixtures.


CURRENT TASK
{
  "id": "P00",
  "title": "Scope and benchmark",
  "depends_on": [],
  "context_files": [
    "docs/APOLLO_COMPARISON.md",
    "franco/dashboard.py"
  ],
  "status": "planned",
  "deliverables": [
    "Agree a written capability matrix: supported user roles, providers, markets, channels, volumes and explicit exclusions.",
    "Define a 100-contact synthetic benchmark and measurable gates for duplicate handling, filter correctness, workflow timing and cost.",
    "Expose explicit capability states: preview, unavailable, configured, degraded, live. Preserve localhost-only defaults."
  ],
  "acceptance": [
    {
      "id": "P00-A1",
      "criterion": "Reject unsupported capability execution before any external request."
    },
    {
      "id": "P00-A2",
      "criterion": "Verify prototype fixtures cannot enter production outreach."
    }
  ],
  "external_dependencies": []
}

EVIDENCE FORMAT
{
  "task_id": "P00",
  "result": "incomplete",
  "verification": "mocked",
  "commit": "",
  "reviewer": "",
  "summary": "",
  "tests": [{"command": "", "exit_code": null, "log": ""}],
  "criteria": {
    "P00-A1": {"result": "incomplete", "evidence": ""},
    "P00-A2": {"result": "incomplete", "evidence": ""}
  },
  "provider_evidence": [],
  "limitations": [],
  "required_configuration": []
}

