#!/usr/bin/env python3
"""Model-neutral planning/evidence harness. Does not invoke an LLM or execute its output."""
import argparse
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent
TASKS = json.loads((ROOT / 'tasks.json').read_text())['tasks']

def read_evidence(folder, task):
    path = folder / (task['id'] + '.json')
    if not path.exists():
        return None, ['evidence missing']
    try:
        evidence = json.loads(path.read_text())
    except (OSError, ValueError):
        return None, ['evidence is not valid JSON']
    errors = []
    if not isinstance(evidence, dict):
        return None, ['evidence must be an object']
    if evidence.get('task_id') != task['id']:
        errors.append('task_id mismatch')
    if evidence.get('result') != 'passed':
        errors.append('result is not passed')
    if evidence.get('verification') not in ('mocked', 'sandbox', 'production'):
        errors.append('verification must be mocked, sandbox or production')
    for field in ('commit', 'reviewer', 'summary'):
        if not isinstance(evidence.get(field), str) or not evidence[field].strip():
            errors.append(field + ' missing')
    tests = evidence.get('tests', [])
    if not isinstance(tests, list) or not tests or not all(isinstance(t, dict) and t.get('command') and t.get('exit_code') == 0 and t.get('log') for t in tests):
        errors.append('successful test commands and log references required')
    criteria = evidence.get('criteria', {})
    if not isinstance(criteria, dict):
        criteria = {}
    for item in task['acceptance']:
        value = criteria.get(item['id'], {})
        if not isinstance(value, dict) or value.get('result') != 'passed' or not value.get('evidence'):
            errors.append(item['id'] + ' needs passing evidence')
    if evidence.get('verification') != 'mocked' and not evidence.get('provider_evidence'):
        errors.append('sandbox/production verification needs provider_evidence')
    return evidence, errors

def completed(folder):
    done = set()
    # Tasks are topologically ordered, but repeat to tolerate a reordered file.
    for _ in TASKS:
        for t in TASKS:
            _, errors = read_evidence(folder, t)
            if not errors and set(t['depends_on']) <= done:
                done.add(t['id'])
    return done

def prompt(task):
    return (ROOT / 'SYSTEM_PROMPT.md').read_text() + '\n\nCURRENT TASK\n' + json.dumps(task, indent=2) + '\n\nEVIDENCE FORMAT\n' + (ROOT / 'evidence-template.json').read_text()

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['status','next','prompt','verify'])
    parser.add_argument('task', nargs='?')
    parser.add_argument('--evidence-dir', type=Path, default=ROOT/'evidence')
    args = parser.parse_args()
    done = completed(args.evidence_dir)
    if args.command == 'status':
        for t in TASKS:
            evidence, _ = read_evidence(args.evidence_dir, t)
            state = ('evidence accepted (' + evidence['verification'] + ')') if t['id'] in done else 'ready' if set(t['depends_on']) <= done else 'blocked by prerequisites'
            print(f"{t['id']}  {state:32}  {t['title']}")
        print('Evidence is reported by implementers; acceptance here is not independent certification or full parity.')
        return 0
    if args.command == 'next':
        task = next((t for t in TASKS if t['id'] not in done and set(t['depends_on']) <= done), None)
        if not task:
            print('All task evidence accepted. Review live verification, scope and operational readiness separately.')
            return 0
        print(prompt(task))
        return 0
    task = next((t for t in TASKS if t['id'] == args.task), None)
    if not task:
        parser.error('provide a valid task ID, such as P00')
    if args.command == 'prompt':
        print(prompt(task))
        return 0
    evidence, errors = read_evidence(args.evidence_dir, task)
    missing = set(task['depends_on']) - done
    if missing:
        errors.append('prerequisite evidence not accepted: ' + ', '.join(sorted(missing)))
    print('\n'.join(errors) if errors else f"{task['id']}: evidence structure accepted ({evidence['verification']}). Human review still required.")
    return 1 if errors else 0

if __name__ == '__main__':
    raise SystemExit(main())
