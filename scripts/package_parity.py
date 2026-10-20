"""Regenerate, or check, the portable session deliverables without external tools."""
import argparse
import hashlib
import io
import json
from pathlib import Path
import subprocess
import sys
import zipfile

ROOT = Path(__file__).resolve().parent.parent
DEST = ROOT / 'docs/session-deliverables'


def artifacts():
    html = (ROOT / 'franco/web/blueprint.html').read_text()
    html = html.replace('<link rel="stylesheet" href="/static/blueprint.css">',
                        '<style>' + (ROOT / 'franco/web/blueprint.css').read_text() + '</style>')
    html = html.replace('<script src="/static/blueprint.js"></script>',
                        '<script>' + (ROOT / 'franco/web/blueprint.js').read_text() + '</script>')
    generated = {
        'franco-blueprint.html': html.encode(),
        'START-HERE-LLM-PROMPT.md': subprocess.check_output(
            [sys.executable, str(ROOT / 'harness/parity/run.py'), 'prompt', 'P00'], cwd=ROOT),
        'franco-comparison.md': (ROOT / 'docs/APOLLO_COMPARISON.md').read_bytes(),
        'franco-parity-roadmap.md': (ROOT / 'docs/PARITY_ROADMAP.md').read_bytes(),
    }
    members = {p.relative_to(ROOT).as_posix(): p.read_bytes()
               for p in (ROOT / 'harness/parity').glob('*') if p.is_file()}
    members['harness/parity/evidence/README.md'] = (ROOT / 'harness/parity/evidence/README.md').read_bytes()
    for path in ['docs/SESSION_GUIDE.md', 'docs/APOLLO_COMPARISON.md', 'docs/PARITY_ROADMAP.md',
                 'franco/web/blueprint.html', 'franco/web/blueprint.css', 'franco/web/blueprint.js',
                 'scripts/package_parity.py']:
        members[path] = (ROOT / path).read_bytes()
    for name in ('franco-workspace.jpg', 'franco-blueprint.jpg'):
        members['docs/session-deliverables/' + name] = (DEST / name).read_bytes()
    for name, data in generated.items():
        members['docs/session-deliverables/' + name] = data
    packed = io.BytesIO()
    with zipfile.ZipFile(packed, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
        for name, data in sorted(members.items()):
            info = zipfile.ZipInfo(name, date_time=(2026, 10, 20, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            archive.writestr(info, data)
    generated['franco-llm-harness.zip'] = packed.getvalue()
    return generated


def inventory(generated):
    paths = {'README.md', 'franco/dashboard.py', 'docs/SESSION_GUIDE.md',
             'docs/APOLLO_COMPARISON.md', 'docs/PARITY_ROADMAP.md',
             'scripts/package_parity.py', 'tests/test_contact_workspace.py',
             'tests/contact_workspace.test.cjs'}
    paths.update(p.relative_to(ROOT).as_posix() for p in (ROOT / 'franco/web').glob('*') if p.is_file())
    paths.update(p.relative_to(ROOT).as_posix() for p in (ROOT / 'harness/parity').rglob('*')
                 if p.is_file() and '__pycache__' not in p.parts and p.suffix != '.pyc')
    paths.update('docs/session-deliverables/' + name for name in generated)
    paths.update('docs/session-deliverables/' + name for name in ('franco-workspace.jpg', 'franco-blueprint.jpg'))
    hashes = {}
    for path in sorted(paths):
        name = Path(path).name
        data = generated[name] if path.startswith('docs/session-deliverables/') and name in generated else (ROOT / path).read_bytes()
        hashes[path] = hashlib.sha256(data).hexdigest()
    return (json.dumps({'schema_version': 1, 'algorithm': 'sha256', 'files': hashes}, indent=2) + '\n').encode()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check', action='store_true')
    args = parser.parse_args()
    generated = artifacts()
    generated['manifest.json'] = inventory(generated)
    if args.check:
        failures = [name for name, data in generated.items()
                    if not (DEST / name).exists() or (DEST / name).read_bytes() != data]
        if failures:
            print('Out of date or missing: ' + ', '.join(failures))
            return 1
        print('Package, starter prompt, document copies, standalone preview and inventory are current.')
        return 0
    DEST.mkdir(parents=True, exist_ok=True)
    for name, data in generated.items():
        (DEST / name).write_bytes(data)
    print('Generated portable deliverables and SHA-256 inventory in docs/session-deliverables/.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
