"""Franco CLI — simple commands to install, setup, run, and manage Franco."""

import argparse
import asyncio
import subprocess
import sys
from pathlib import Path


MIN_PYTHON = (3, 11)


def _check_python_version():
    """Refuse to install on a Python that cannot run Franco.

    `python3 -m venv .venv` on macOS builds the venv from /usr/bin/python3,
    which is 3.9 — old enough that Franco's `X | Y` type syntax fails at
    import. The install itself appears to succeed and the failure surfaces
    later as an unrelated-looking ImportError, so check up front and name the
    fix.
    """
    if sys.version_info >= MIN_PYTHON:
        return

    have = f"{sys.version_info.major}.{sys.version_info.minor}"
    want = f"{MIN_PYTHON[0]}.{MIN_PYTHON[1]}"
    print(f"\n  Franco needs Python {want} or newer. This is Python {have}.")
    print(f"  ({sys.executable})\n")
    print("  On macOS, `python3` is usually the system 3.9, so a venv built")
    print("  with it is 3.9 too. Build the venv from a newer Python instead:\n")
    print("    brew install python@3.13")
    print("    rm -rf .venv")
    print("    $(brew --prefix)/bin/python3.13 -m venv .venv")
    print("    source .venv/bin/activate && pip install -e .\n")
    sys.exit(1)


HOME_GITIGNORE = """# Franco home: never commit credentials, contact data or real config
.env
*.env
franco.local.yaml
data/
linkedin_cookies.json
skills/product_knowledge.md
skills/competitive_intel.md
"""


def cmd_init(args):
    """Create a Franco home folder: config, .env, editable prompts and skills."""
    import shutil
    from franco.paths import bundled

    target = Path(args.directory).expanduser().resolve() if args.directory else Path.cwd()
    target.mkdir(parents=True, exist_ok=True)
    made, kept = [], []

    def place(src: Path, dest: Path, mode: int | None = None):
        if dest.exists():
            kept.append(dest.relative_to(target).as_posix())
            return
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dest)
        if mode is not None:
            dest.chmod(mode)
        made.append(dest.relative_to(target).as_posix())

    place(bundled() / "franco.yaml", target / "franco.yaml")
    place(bundled() / ".env.example", target / ".env", mode=0o600)
    for kind in ("prompts", "skills"):
        for src in sorted(bundled(kind).glob("*.md")):
            if src.name in ("product_knowledge.md", "competitive_intel.md"):
                continue   # generated per product by `franco train`, never shipped
            place(src, target / kind / src.name)
    (target / "data").mkdir(exist_ok=True)
    gitignore = target / ".gitignore"
    if not gitignore.exists():
        gitignore.write_text(HOME_GITIGNORE)
        made.append(".gitignore")

    print(f"\n  Franco home: {target}")
    if made:
        print(f"  Created: {', '.join(made)}")
    if kept:
        print(f"  Kept your existing: {', '.join(kept)}")
    print(f"""
  Next, from that folder (Franco finds it by its franco.yaml), or with
  FRANCO_HOME={target} set:

    cd {target}
    franco install              # Chromium, for LinkedIn and JS-heavy sites
    claude login                # the subscription Franco thinks with
    $EDITOR .env                # mail provider + verifier keys
    franco setup                # or: franco train https://your-site.com
    franco dashboard

  Prompts and skills here are yours to edit; delete one to go back to the
  version that ships with Franco. Keep .env and data/ private.
""")


def cmd_install(args):
    """Install all dependencies including Playwright browsers."""
    _check_python_version()
    print("\n  Installing Franco dependencies...\n")

    # Install Python packages (a pip-installed Franco already has them)
    from franco.paths import checkout_root
    if checkout_root() is None:
        print("  [1/2] Python packages: already installed with Franco.\n")
        return _install_browsers()
    print("  [1/2] Installing Python packages...")
    requirements = Path(args._project_root) / "requirements.txt"
    if requirements.exists():
        pip_args = ["-r", "requirements.txt"]
    else:
        # Fall back to an editable install from pyproject.toml
        pip_args = ["-e", "."]
    result = subprocess.run(
        [sys.executable, "-m", "pip", "install", *pip_args],
        cwd=args._project_root,
    )
    if result.returncode != 0:
        print("\n  Failed to install Python packages.")
        print("  Tip: make sure you're inside a virtualenv "
              "(python3 -m venv .venv && source .venv/bin/activate).")
        sys.exit(1)
    print("  ✓ Python packages installed.\n")
    _install_browsers()

    _ensure_importable(args._project_root)
    print("  Franco is installed. Run 'franco setup' next.\n")


def _install_browsers():
    """Chromium for Playwright: used for LinkedIn and JavaScript-heavy sites."""
    print("  [2/2] Installing Playwright browsers...")
    try:
        result = subprocess.run(
            [sys.executable, "-m", "playwright", "install", "chromium"],
            timeout=600,
        )
        playwright_ok = result.returncode == 0
    except (subprocess.TimeoutExpired, OSError):
        playwright_ok = False
    if not playwright_ok:
        print("\n  Playwright browser install failed (optional — needed for LinkedIn).")
    else:
        print("  ✓ Playwright browsers installed.\n")


def _ensure_importable(project_root: str):
    """Make sure `import franco` works outside the repo directory.

    Python 3.13 silently skips .pth files carrying the macOS 'hidden'
    file flag, and some Macs propagate that flag to everything inside
    dot-directories like .venv — which breaks editable installs. When
    that happens, fall back to symlinking the package into site-packages
    (imports don't check the hidden flag; only .pth parsing does).
    """
    check = subprocess.run(
        [sys.executable, "-c", "import franco"],
        cwd="/", capture_output=True,
    )
    if check.returncode == 0:
        return

    try:
        import site
        site_packages = Path(site.getsitepackages()[0])
        link = site_packages / "franco"
        target = Path(project_root) / "franco"
        if not link.exists() and target.is_dir():
            link.symlink_to(target)
            recheck = subprocess.run(
                [sys.executable, "-c", "import franco"],
                cwd="/", capture_output=True,
            )
            if recheck.returncode == 0:
                print("  ✓ Fixed package visibility (editable .pth was being "
                      "ignored; linked the package directly).\n")
                return
    except OSError as e:
        print(f"  Could not apply import fix: {e}")

    print("\n  Warning: 'import franco' fails outside the project directory.")
    print("  Run franco commands from the project root, or reinstall with:")
    print("    pip install -e . --config-settings editable_mode=compat\n")


def cmd_setup(args):
    """Run the interactive setup wizard."""
    from franco.setup import run_setup

    asyncio.run(run_setup())


def cmd_run(args):
    """Start Franco's heartbeat loop."""
    from franco.main import main

    main()


def cmd_train(args):
    """Train Franco on a website."""
    from franco.trainer import Trainer

    url = args.url.strip()
    if not url.startswith(("http://", "https://")):
        url = f"https://{url}"
        print(f"  No scheme given — using {url}")

    if args.max_pages < 1:
        print("  max_pages must be at least 1.")
        sys.exit(2)

    from pydantic import ValidationError
    from franco.config import BrainConfig, ConfigError, load_brain_config

    try:
        brain_config = load_brain_config()
        overrides = {
            k: v for k, v in (
                ("provider", args.provider),
                ("model", args.model),
                ("reasoning_effort", args.reasoning_effort),
            ) if v is not None
        }
        if overrides:
            if "provider" in overrides and overrides["provider"] != brain_config.provider:
                # A different provider never inherits the other one's model.
                brain_config = BrainConfig(**overrides)
            else:
                brain_config = BrainConfig(**{**brain_config.model_dump(), **overrides})
    except (ConfigError, ValidationError) as e:
        print(f"  {e}")
        sys.exit(2)

    trainer = Trainer(brain_config=brain_config)
    asyncio.run(trainer.train(url, max_pages=args.max_pages))


def cmd_dashboard(args):
    """Launch the local web dashboard."""
    from franco.dashboard import start_dashboard

    if not 1 <= args.port <= 65535:
        print(f"  Invalid port: {args.port}. Must be 1-65535.")
        sys.exit(2)

    start_dashboard(host=args.host, port=args.port)


def cmd_status(args):
    """Show current pipeline status."""
    from franco.state import StateManager

    async def _status():
        state = StateManager()
        await state.init_db()
        summary = await state.get_state_summary()

        print("\n  Franco Pipeline Status")
        print("  " + "=" * 40)
        print(f"  Prospects:            {summary['prospects']}")
        print(f"  Draft campaigns:      {summary['draft_campaigns']}")
        print(f"  Active campaigns:     {summary['active_campaigns']}")
        print(f"  Open conversations:   {summary['open_conversations']}")
        by_provider = summary.get("usage_today_by_provider", {})
        print(f"  Claude calls today:   {by_provider.get('claude', summary['usage_today'])}")
        print(f"  Codex calls today:    {by_provider.get('codex', 0)}")
        from franco.brain import BRAIN_STATUS_KEY, Brain, active_brain_status
        from franco.config import ConfigError, load_brain_config
        try:
            brain_config = load_brain_config()
        except ConfigError as e:
            brain_config, config_error = None, e
        # Only the configured provider's health: an old Codex pause says
        # nothing once franco.yaml has switched to Claude.
        brain_status = active_brain_status(
            await state.get_setting(BRAIN_STATUS_KEY),
            brain_config.provider if brain_config else "")
        healthy = True
        if getattr(args, "check", False):
            if brain_config is None:
                print(f"  Brain:                config error: {config_error}")
                healthy = False
            else:
                brain = Brain(state, brain_config)
                problem = await brain.preflight()
                label = brain.provider_label + (f" ({brain.model})" if brain.model else "")
                if problem:
                    healthy = False
                    print(f"  Brain:                {label} NOT READY")
                    print(f"                        {problem}")
                elif brain.provider == "codex":
                    print(f"  Brain:                {label} ready (CLI, isolation, login, "
                          "model and account checked; no prompt sent)")
                else:
                    print(f"  Brain:                {label} (not checked: Claude is checked "
                          "by its first call)")
        elif brain_status.get("status") == "paused":
            label = {"claude": "Claude", "codex": "Codex"}.get(brain_status.get("provider"), "Brain")
            print(f"  Brain:                {label} PAUSED since {brain_status.get('since', '?')}")
            print(f"                        {brain_status.get('reason', '')}")
        print()
        return healthy

    if not asyncio.run(_status()):
        # `franco status --check` failing is something scripts can act on.
        raise SystemExit(1)


def cmd_usage(args):
    """Show brain usage: active provider's quota, totals, breakdowns."""
    from franco.state import StateManager
    from franco.config import ConfigError, load_brain_config

    async def _usage():
        state = StateManager()
        await state.init_db()

        try:
            brain_config = load_brain_config()
        except ConfigError as e:
            print(f"  {e}")
            brain_config = None

        # Live quota for the ACTIVE provider only (best-effort). Each
        # subscription's gauge is read from its own source and labelled as
        # such; one is never shown as the other.
        windows = None
        provider = brain_config.provider if brain_config else None
        try:
            if provider == "codex":
                from franco.codex import CodexClient, CodexQuotaClient
                windows = await CodexQuotaClient(
                    CodexClient(model=brain_config.model)).get_utilization()
            elif provider == "claude":
                from franco.integrations.quota import QuotaClient
                windows = await QuotaClient().get_utilization()
        except Exception:
            pass

        label = {"claude": "Claude", "codex": "Codex"}.get(provider, "Brain")
        print(f"\n  {label} Usage")
        print("  " + "=" * 52)
        if windows:
            labels = {"five_hour": "5-hour window", "seven_day": "Weekly",
                      "limit_reached": "Usage limit"}
            for key, w in windows.items():
                resets = f"  (resets {w['resets_at']})" if w.get("resets_at") else ""
                print(f"  {labels.get(key, key):<16} {w['utilization']:5.1f}% used{resets}")
        elif provider == "codex":
            print("  Quota gauge unavailable (check 'codex login', the Codex CLI "
                  "version, and network).")
        elif provider == "claude":
            print("  Quota gauge unavailable (run 'claude login' or check network).")

        totals = await state.usage_totals()
        print()
        print(f"  {'Period':<10} {'Calls':>7} {'Input':>12} {'Output':>10} {'Cache read':>12}")
        for label, key in (("Today", "today"), ("7 days", "week"), ("30 days", "month")):
            t = totals.get(key) or {}
            print(
                f"  {label:<10} {t.get('calls', 0):>7} "
                f"{t.get('input_tokens', 0):>12,} {t.get('output_tokens', 0):>10,} "
                f"{t.get('cache_read_tokens', 0):>12,}"
            )

        by_provider = await state.usage_by_provider(days=args.days)
        if len(by_provider) > 1 or (by_provider and by_provider[0]["provider"] != "claude"):
            print(f"\n  By provider (last {args.days} days):")
            for row in by_provider:
                print(
                    f"    {row['provider']:<14} {row['calls']:>5} calls  "
                    f"{row['output_tokens']:>10,} out tokens"
                )

        by_agent = await state.usage_by_agent(days=args.days)
        if by_agent:
            print(f"\n  By agent (last {args.days} days):")
            for row in by_agent:
                print(
                    f"    {row['agent']:<14} {row['calls']:>5} calls  "
                    f"{row['output_tokens']:>10,} out tokens"
                )

        by_task = await state.usage_by_task(days=args.days)
        if by_task:
            print(f"\n  By task (last {args.days} days):")
            for row in by_task[:10]:
                print(
                    f"    {row['task']:<22} {row['calls']:>5} calls  "
                    f"{row['output_tokens']:>10,} out tokens"
                )
        print(
            "\n  Subscription plans aren't billed per token — these are usage"
            "\n  counts, not costs.\n"
        )

    asyncio.run(_usage())


def cmd_export(args):
    """Export the prospect list as a sequencer-ready CSV."""
    from franco.state import StateManager
    from franco.export import export_prospects_csv

    async def _export():
        state = StateManager()
        await state.init_db()

        email_statuses = None
        if args.email_status:
            email_statuses = [s.strip() for s in args.email_status.split(",") if s.strip()]
        statuses = None
        if args.status:
            statuses = [s.strip() for s in args.status.split(",") if s.strip()]

        count, _ = await export_prospects_csv(
            state,
            out_path=args.out,
            email_statuses=email_statuses,
            min_score=args.min_score,
            statuses=statuses,
            include_all=args.all,
        )
        scope = "all prospects" if args.all else (
            f"email status {email_statuses or ['verified', 'risky']}"
            + (f", score >= {args.min_score}" if args.min_score else "")
        )
        print(f"\n  Exported {count} prospect(s) to {args.out}  ({scope})")
        if count == 0 and not args.all:
            print("  Tip: no deliverable emails yet? Add a REOON_API_KEY to .env so")
            print("  Franco can verify addresses, or use --all for the raw list.\n")
        else:
            print("  The CSV imports directly into Instantly, Smartlead, or any sequencer.\n")

    asyncio.run(_export())


def cmd_gmail(args):
    """Gmail provider utilities (auth / test)."""
    from franco.config import load_env

    env = load_env()
    if args.gmail_action == "auth":
        from franco.integrations.gmail import run_auth_flow
        ok = run_auth_flow(env.gmail_client_id, env.gmail_client_secret)
        sys.exit(0 if ok else 1)

    if args.gmail_action == "test":
        from franco.config import load_config
        from franco.integrations.gmail import GmailProvider

        async def _test():
            provider = GmailProvider(load_config(), env)
            ok, detail = await provider.test_connection()
            print(f"\n  {'✓' if ok else '✗'} {detail}\n")
            sys.exit(0 if ok else 1)
        asyncio.run(_test())


def cmd_outbox(args):
    """Review and approve queued outgoing emails."""
    from franco.state import StateManager

    async def _outbox():
        state = StateManager()
        await state.init_db()

        if args.approve_all:
            n = await state.approve_outbox()
            print(f"\n  Approved {n} email(s). They'll send on schedule.\n")
            return
        if args.approve:
            n = await state.approve_outbox(args.approve)
            print(f"\n  {'Approved.' if n else 'No pending item with that id.'}\n")
            return
        if args.reject:
            await state.update_outbox_item(args.reject, status="rejected")
            print("\n  Rejected.\n")
            return
        if args.edit:
            await _edit_outbox_item(state, args.edit)
            return

        paused = await state.get_setting("sending_paused")
        if paused:
            print(f"\n  ⚠ SENDING PAUSED: {paused}")
            print("  Resume with: franco sending resume")
        if not await state.approval_required(_config_requires_approval()):
            print("\n  ⚡ AUTOPILOT ON: new emails send without review "
                  "(franco autopilot off to stop)")

        pending = await state.get_outbox(status="pending_review", limit=50)
        approved = await state.get_outbox(status="approved", limit=10)
        print(f"\n  Outbox — {len(pending)} awaiting approval, "
              f"{len(approved)}+ approved/scheduled")
        print("  " + "=" * 60)
        for item in pending:
            print(f"\n  [{item['id']}] step {item['step']} ({item['kind']}) "
                  f"→ {item['to_email']}  (send {item['send_at'][:16]})")
            ctx = await state.outbox_context(item["prospect_id"])
            if ctx.get("campaign"):
                print(f"  Why: {ctx.get('company')} · {ctx.get('title')} · campaign "
                      f"{ctx['campaign']} · owner {ctx.get('owner') or '—'}"
                      + (f" · score {ctx['score']}" if ctx.get("score") is not None else ""))
            if ctx.get("observation"):
                print(f"  Observed: {ctx['observation']}")
                print(f"  Source:   {ctx.get('source_url')} (checked {ctx.get('checked')})")
            print(f"  Subject: {item['subject']}")
            body_preview = (item["body"][:200] + "...") if len(item["body"]) > 200 else item["body"]
            for line in body_preview.splitlines():
                print(f"    {line}")
        if pending:
            print("\n  Approve: franco outbox --approve <id>   |   all: franco outbox --approve-all")
            print("  Reject:  franco outbox --reject <id>   |   edit: franco outbox --edit <id>\n")
        else:
            print("  Nothing awaiting review.\n")

    asyncio.run(_outbox())


async def _edit_outbox_item(state, item_id: str):
    """Open a pending email in $EDITOR: first line "Subject: ...", then the
    body. Saving changes it; the gate still checks it before it sends."""
    import os
    import subprocess
    import tempfile

    item = await state.get_outbox_item(item_id)
    if not item or item["status"] != "pending_review":
        print("\n  No pending item with that id (only unapproved emails can be edited).\n")
        return
    with tempfile.NamedTemporaryFile("w+", suffix=".txt", delete=False) as f:
        f.write(f"Subject: {item['subject']}\n\n{item['body']}\n")
        path = f.name
    try:
        editor = os.environ.get("VISUAL") or os.environ.get("EDITOR") or "vi"
        subprocess.call([*editor.split(), path])
        with open(path) as f:
            text = f.read()
    finally:
        os.unlink(path)
    first, _, body = text.partition("\n")
    if not first.lower().startswith("subject:") or not body.strip():
        print("\n  Unchanged: the first line must be 'Subject: ...' followed by the body.\n")
        return
    subject = first.split(":", 1)[1].strip()
    body = body.strip()
    if subject == item["subject"] and body == item["body"].strip():
        print("\n  No changes.\n")
        return
    await state.update_outbox_item(item_id, subject=subject, body=body)
    print("\n  Saved. Approve it with: franco outbox --approve " + item_id + "\n")


def cmd_import(args):
    """Import a list from a CSV: researched accounts, contacts, or suppressions."""
    from franco.config import load_config, load_env
    from franco.importer import KINDS, TEMPLATES, default_kind, make_verifier, run_import
    from franco.state import StateManager

    config = load_config()
    kind = args.kind or default_kind(config)
    if args.template:
        sys.stdout.write(TEMPLATES[kind])
        return
    if not args.file:
        print("\n  Usage: franco import FILE.csv [--kind accounts|contacts|suppressions]"
              " [--dry-run] [--verify]")
        print(f"  Start from a template: franco import --template --kind {kind} > {kind}.csv\n")
        sys.exit(2)
    if kind not in KINDS:
        print(f"\n  --kind must be one of {', '.join(KINDS)}.\n")
        sys.exit(2)

    campaigns = set(config.outreach.sequence_keys())
    if kind == "accounts" and not campaigns:
        print("\n  No approved campaigns or funnels configured (outreach.campaigns / outreach.funnels)."
              "\n  For a plain company/contact list use --kind contacts.\n")
        sys.exit(1)
    with open(args.file, encoding="utf-8-sig") as f:
        text = f.read()
    verify = make_verifier(load_env()) if args.verify and kind != "suppressions" else None

    async def _import():
        state = StateManager()
        await state.init_db()
        report = await run_import(
            state, kind, text, campaigns, default_owner=args.owner,
            verify=verify, dry_run=args.dry_run, source=f"import:{args.file}",
        )
        verb = "Valid" if args.dry_run else ("Added" if kind == "suppressions" else "Imported")
        total = len(report.imported) + len(report.rejected) + (
            len(report.warnings) if kind == "suppressions" else 0)
        print(f"\n  {kind}: {verb} {len(report.imported)}   Rejected: {len(report.rejected)}"
              f"   (of {total} rows)")
        for n, why in report.rejected:
            print(f"    ✗ row {n}: {why}")
        for n, why in report.warnings:
            print(f"    ! row {n}: {why}")
        if report.imported and not args.dry_run:
            if kind == "accounts":
                print("\n  Approved copy is staged on the next `franco run` cycle."
                      " Review the preview with `franco preview`.")
            elif kind == "contacts":
                print("\n  Franco profiles these companies and writes for deliverable"
                      " contacts on the next `franco run` cycles.")
            if kind != "suppressions" and not args.verify:
                print("  Only verified addresses are emailed: set email_status in the"
                      " file or re-run with --verify.")
        print()

    from franco.fixtures import PrototypeDataError
    try:
        asyncio.run(_import())
    except PrototypeDataError as e:
        print(f"\n  {e}\n")
        sys.exit(1)


def cmd_suppress(args):
    """Manage the shared do-not-contact list."""
    from franco.state import StateManager

    async def _run():
        state = StateManager()
        await state.init_db()
        action = args.suppress_action
        if action == "add":
            for value in args.values:
                new = await state.add_suppression(value, reason=args.reason, source="manual")
                print(f"  {'Added' if new else 'Already listed'}: {value}")
        elif action == "remove":
            for value in args.values:
                gone = await state.remove_suppression(value)
                print(f"  {'Removed' if gone else 'Not listed'}: {value}")
        elif action == "import":
            from franco.importer import import_suppressions, read_suppressions
            added = 0
            for path in args.values:
                with open(path, encoding="utf-8-sig") as f:
                    report = await import_suppressions(
                        state, read_suppressions(f.read()), default_reason=args.reason,
                        source=f"import:{path}")
                added += len(report.imported)
                for n, why in report.rejected:
                    print(f"    ✗ {path} row {n}: {why}")
            print(f"\n  Added {added} new suppression(s).\n")
        elif action == "export":
            from franco.importer import suppressions_csv
            sys.stdout.write(suppressions_csv(await state.list_suppressions()))
        else:
            rows = await state.list_suppressions()
            print(f"\n  {len(rows)} suppressed address(es)/domain(s)")
            for row in rows[:200]:
                print(f"    {row['value']:<40} {row['kind']:<7} {row['reason'] or ''}")
            print()

    asyncio.run(_run())


def cmd_profile(args):
    """Read what companies' own websites say about them. Free, no Claude calls."""
    from franco.state import StateManager
    from franco.pipeline import run_profile_stage

    async def _profile():
        state = StateManager()
        await state.init_db()

        pending = await state.count_companies_needing_profile(args.stale_days)
        if not pending:
            print("\n  Nothing to profile. Every company with a website has "
                  "been read recently.\n")
            return

        print(f"\n  {pending} companies need profiling. Reading up to "
              f"{args.limit}...\n")
        companies, observations, _ = await run_profile_stage(
            state, limit=args.limit, stale_days=args.stale_days)
        print(f"  Read {companies} sites → {observations} observations (free)")
        print("\n  See what turned up: franco dashboard → Signals\n")

    asyncio.run(_profile())


def cmd_discover(args):
    """Find businesses. The only stage that spends money, so it estimates first."""
    from franco.state import StateManager
    from franco.config import load_config
    from franco.collectors.discover import (
        PROVIDERS, build_queries, provider_menu, run_discovery,
    )

    async def _discover():
        if args.providers:
            print("\n  Discovery providers")
            print("  " + "=" * 68)
            for m in provider_menu():
                mark = "ready" if m["configured"] else "needs setup"
                print(f"\n  {m['label']}  [{m['key']}]  ({mark})")
                print(f"    {m['blurb']}")
                print(f"    Cost: {m['cost_note']}")
                print(f"    Free: {m['free_tier']}")
                if m["env_keys"]:
                    print(f"    Needs: {', '.join(m['env_keys'])} in .env  —  {m['signup_url']}")
                if m["caveat"]:
                    print(f"    Note: {m['caveat']}")
            print("\n  Run one with: franco discover --provider <key> --estimate\n")
            return

        if args.provider not in PROVIDERS:
            print(f"\n  Unknown provider {args.provider!r}. "
                  f"See: franco discover --providers\n")
            return

        config = load_config()
        state = StateManager()
        await state.init_db()

        # Semicolons, not commas: "Denver, CO" is one city, not two.
        cities = [c.strip() for c in args.city.split(";") if c.strip()] or None
        queries = build_queries(config, cities=cities,
                                depth=args.depth, limit=args.limit)

        report = await run_discovery(state, config, args.provider, queries,
                                     max_spend=args.max_spend, dry_run=True)
        print(f"\n  {len(queries)} queries via {PROVIDERS[args.provider].label}")
        for q in queries[:8]:
            print(f"    - {q.keyword()}" + ("" if q.coordinate else
                  "   (no coordinates — add icp.geo_coordinates for radius search)"))
        if len(queries) > 8:
            print(f"    ... and {len(queries) - 8} more")
        print(f"\n  Estimated cost: ${report.estimated_cost:.4f}"
              f"   (cap: ${args.max_spend:.2f})")

        if args.estimate:
            print("\n  Estimate only. Re-run without --estimate to collect.\n")
            return

        print("\n  Running...\n")
        from franco.pipeline import run_prospecting

        result = await run_prospecting(
            state, config, args.provider, queries,
            max_spend=args.max_spend, profile=not args.no_profile,
        )
        r = result.discover or {}
        print(f"  DISCOVER — {r.get('found', 0)} results across "
              f"{r.get('queries', 0)} queries")
        print(f"    {r.get('new_companies', 0)} new companies, "
              f"{r.get('known_companies', 0)} already known")
        print(f"    {r.get('junk', 0)} filtered out as directories/aggregators")
        print(f"    {r.get('observations', 0)} observations recorded")
        print(f"    actual cost: ${r.get('actual_cost', 0):.4f}")
        if r.get("stopped"):
            print(f"    stopped early: {r['stopped']}")

        if args.no_profile:
            print("\n  PROFILE — skipped (--no-profile)")
        else:
            print(f"\n  PROFILE — {result.profiled_companies} sites read, "
                  f"{result.profile_observations} observations  (free)")

        for err in result.errors[:5]:
            print(f"    ! {err}")
        print("\n  Next: franco dashboard → Signals → cohort builder\n")

    asyncio.run(_discover())


def cmd_signals(args):
    """Review the signal vocabulary — Franco proposes, you confirm.

    Nothing is collected until a signal is confirmed, so a fresh install
    prospects against nothing until someone makes these decisions.
    """
    from franco.state import StateManager
    from franco.signals import seed_signal_catalog

    async def _signals():
        state = StateManager()
        await state.init_db()
        await seed_signal_catalog(state)

        codes = [a.strip().upper() for a in (args.confirm or args.reject or "").split(",")
                 if a.strip()]
        if codes:
            status = "confirmed" if args.confirm else "rejected"
            if codes == ["ALL"]:
                codes = [c["code"] for c in await state.get_signal_codes()]
            elif codes == ["FREE"]:
                # Only signals whose cost note STARTS with free/included. A
                # substring match would sweep in "1 credit (free tiers
                # available)", which is not free.
                codes = [c["code"] for c in await state.get_signal_codes()
                         if (c["cost_note"] or "").lower().startswith(("free", "included"))]
            changed = sum(
                [1 for c in codes if await state.set_signal_status(c, status)]
            )
            print(f"\n  {changed} signal(s) {status}.\n")
            return

        rows = await state.get_signal_codes()
        counts = {c["signal_code"]: c for c in await state.signal_counts()}
        by_status = {"confirmed": 0, "proposed": 0, "rejected": 0}
        for r in rows:
            by_status[r["status"]] = by_status.get(r["status"], 0) + 1

        print(f"\n  Signals — {by_status['confirmed']} confirmed, "
              f"{by_status['proposed']} awaiting you, {by_status['rejected']} off")
        print("  " + "=" * 66)
        mark = {"confirmed": "[on] ", "proposed": "[ ? ]", "rejected": "[off]"}
        current = None
        for r in rows:
            if r["category"] != current:
                current = r["category"]
                print(f"\n  {current.upper()}")
            seen = counts.get(r["code"], {}).get("companies", 0)
            seen_txt = f"  ({seen} companies)" if seen else ""
            print(f"    {mark.get(r['status'], '     ')} {r['code']:<22} "
                  f"{r['label']}{seen_txt}")
            print(f"          {r['cost_note']}")

        if by_status["proposed"]:
            print("\n  Confirm with: franco signals --confirm CODE[,CODE...]")
            print("  Everything free: franco signals --confirm free")
            print("  Details and descriptions: franco dashboard → Signals\n")
        else:
            print("")

    asyncio.run(_signals())


def _config_requires_approval() -> bool:
    try:
        from franco.config import load_config
        return bool(getattr(load_config().channels.email, "require_approval", True))
    except Exception:
        return True


def cmd_preview(args):
    """Review an approved-copy campaign's preview batch, then sign it off."""
    import csv
    from franco.outreach import PREVIEW_CSV_COLUMNS, preview_record
    from franco.state import StateManager

    config = None
    try:
        from franco.config import load_config
        config = load_config()
    except Exception:
        pass
    keys = config.outreach.sequence_keys() if config else []
    size = config.outreach.preview_accounts if config else 10

    async def _run():
        state = StateManager()
        await state.init_db()
        action, key = args.preview_action, args.campaign
        if action not in ("list", "signoff", "reopen"):
            action, key = "list", action     # `franco preview M1`

        if action in ("signoff", "reopen"):
            if not key:
                print(f"\n  Name the campaign: franco preview {action} KEY"
                      f"  ({', '.join(keys) or 'none configured'})\n")
                sys.exit(2)
            if keys and key not in keys:
                print(f"\n  '{key}' is not a configured campaign or funnel ({', '.join(keys)}).\n")
                sys.exit(1)
            if action == "reopen":
                await state.clear_signoff(key)
                print(f"\n  {key} is back in preview: nothing more sends until it is"
                      " signed off again.\n")
                return
            staged = await state.staged_accounts_by_owner(key)
            by = args.by or (config.persona.name if config else "") or "unknown"
            await state.set_signoff(key, by)
            print(f"\n  {key} signed off by {by}. Its emails can now send"
                  f" ({sum(staged.values())} account(s) staged so far), and the rest"
                  " of its accounts stage on the next cycle.")
            if await state.approval_required(_config_requires_approval()):
                print("  Each email still needs approval: franco outbox"
                      " (or franco autopilot on).")
            print()
            return

        rows = [preview_record(i) for i in await state.preview_rows(key)]
        if args.csv:
            with open(args.csv, "w", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=PREVIEW_CSV_COLUMNS)
                writer.writeheader()
                writer.writerows(rows)
            print(f"\n  Wrote {len(rows)} preview row(s) to {args.csv}.\n")
            return

        for k in ([key] if key else keys):
            signoff = await state.get_signoff(k)
            staged = await state.staged_accounts_by_owner(k)
            owners = ", ".join(f"{o or 'no owner'}: {n}" for o, n in staged.items()) or "none"
            state_line = (f"signed off by {signoff.get('by')} {signoff.get('at', '')[:10]}"
                          if signoff else
                          f"IN PREVIEW: up to {size} account(s) per owner, nothing sends")
            print(f"\n  {k}: {state_line}\n     staged accounts by owner: {owners}")
        for r in rows:
            print(f"\n  ── {r['campaign']} · {r['company']} ({r['domain']}) · owner {r['owner'] or '-'}")
            print(f"     Buyer:    {r['buyer']}, {r['title']} <{r['email']}>")
            print(f"     Observed: {r['observation']}")
            print(f"     Source:   {r['source_url']} (checked {r['checked']})")
            print(f"     Status:   {r['status']}")
            print(f"     Subject:  {r['subject']}")
            for line in r["body"].splitlines():
                print(f"     | {line}")
        if not rows:
            print("\n  Nothing staged yet. Import accounts (franco import FILE.csv)"
                  " and let one `franco run` cycle stage them.")
        else:
            print("\n  Share it: franco preview --csv preview.csv"
                  "   Sign off: franco preview signoff KEY --by NAME")
        print()

    asyncio.run(_run())


def cmd_funnels(args):
    """Raad mode: how your companies split across funnels, or why one landed where it did."""
    from franco.config import load_config
    from franco.funnels import company_facts, funnel_report, place, validate_funnels
    from franco.state import StateManager

    outreach = load_config().outreach
    if not outreach.funnels:
        print("\n  No funnels configured (outreach.funnels). See README → Raad mode.\n")
        return
    problems = validate_funnels(outreach)
    if problems:
        print("\n  Funnel conditions need fixing:")
        for p in problems:
            print(f"    ✗ {p}")
        print()
        sys.exit(1)

    async def _run():
        state = StateManager()
        await state.init_db()
        if args.company:
            company = await state.get_company_by_domain(args.company.lower().removeprefix("www."))
            if company is None:
                print(f"\n  No company with domain {args.company}.\n")
                return
            facts = await company_facts(state, company.id)
            placement = place(outreach, company, facts)
            print(f"\n  {company.name} ({company.domain})")
            if placement.funnel:
                print(f"  → {placement.funnel}: {outreach.funnels[placement.funnel].name}")
                print(f"    because: {'; '.join(placement.reasons)}")
                for f in placement.facts:
                    print(f"    fact: {f['signal_code']} = {f.get('value_text') or f.get('value_num')}"
                          f"  ({f.get('evidence_url') or 'no source'})")
            else:
                print("  → no funnel matches. What Franco knows:")
                for code, f in sorted(facts.items()):
                    print(f"    {code} = {f.get('value_text') or f.get('value_num')}")
            print()
            return

        benefits = {b.key: b.text for b in outreach.benefits}
        print("\n  Customer benefits (every message lives in one):")
        for key, text in benefits.items():
            print(f"    {key}: {text}")
        report = await funnel_report(state, outreach)
        for key, row in report["funnels"].items():
            signoff = await state.get_signoff(key)
            status = f"signed off by {signoff['by']}" if signoff else "in preview"
            print(f"\n  {key} · {row['name']}  [{status}]")
            print(f"    benefit: {row['benefit']}   landing: {row['landing_url'] or '-'}")
            print(f"    match:   {' AND '.join(row['require']) or 'anything'}"
                  + (f"   but not {', '.join(row['exclude'])}" if row["exclude"] else ""))
            print(f"    {row['companies']} compan{'y' if row['companies'] == 1 else 'ies'},"
                  f" {row['reachable']} with a verified new contact"
                  + (f"  e.g. {', '.join(row['examples'])}" if row["examples"] else ""))
        print(f"\n  Unplaced: {report['unplaced']}"
              + (f"  e.g. {', '.join(report['unplaced_examples'])}" if report["unplaced_examples"] else ""))
        print("  Why one company landed where it did: franco funnels --company DOMAIN\n")

    asyncio.run(_run())


def cmd_autopilot(args):
    """Toggle full email autopilot without editing config or restarting."""
    from franco.state import StateManager

    async def _run():
        state = StateManager()
        await state.init_db()
        config_default = _config_requires_approval()
        action = args.autopilot_action

        if action == "on":
            await state.set_autopilot("on")
            print("\n  Autopilot ON. New emails and replies are approved as they're")
            print("  drafted and send on schedule with no review.")
            if not args.keep_pending:
                n = await state.approve_outbox()
                print(f"  Approved {n} email(s) already waiting in the outbox.")
            print("  Still enforced: send gate, footer + blocked claims, suppression,")
            print("  account stop, daily cap, quiet hours, bounce kill switch.")
            print("  Emergency stop: franco sending pause\n")
        elif action == "off":
            await state.set_autopilot("off")
            print("\n  Autopilot OFF. Every new email waits in the outbox for you.")
            print("  Emails already approved still send; reject them with")
            print("  franco outbox --reject <id> or pause: franco sending pause\n")
        elif action == "default":
            await state.set_autopilot("")
            mode = "approval required" if config_default else "autopilot"
            print(f"\n  Override cleared — following franco.yaml ({mode}).\n")
        else:
            override = await state.get_autopilot()
            on = not await state.approval_required(config_default)
            source = f"set by 'franco autopilot {override}'" if override \
                else "from channels.email.require_approval in franco.yaml"
            print(f"\n  Autopilot: {'ON' if on else 'OFF'} ({source})")
            paused = await state.get_setting("sending_paused")
            if paused:
                print(f"  ⚠ Sending is paused: {paused}")
            print("")

    asyncio.run(_run())


def cmd_sending(args):
    """Pause/resume the sending kill switch."""
    from franco.state import StateManager

    async def _run():
        state = StateManager()
        await state.init_db()
        if args.sending_action == "pause":
            await state.set_setting("sending_paused", "paused manually")
            print("\n  Sending paused. Nothing will leave the outbox.\n")
        elif args.sending_action == "resume":
            await state.set_setting("sending_paused", "")
            await state.set_setting("bounce_count", "0")
            print("\n  Sending resumed (bounce counter reset).\n")
        else:
            paused = await state.get_setting("sending_paused")
            print(f"\n  Sending: {'PAUSED — ' + paused if paused else 'active'}\n")

    asyncio.run(_run())


def cmd_secrets(args):
    """Encrypted credential vault: status, migrate, set, revoke, rotate."""
    import getpass

    from franco import vault as V
    from franco.paths import PROJECT_ROOT

    store = V.default_vault()
    env_file = PROJECT_ROOT / ".env"
    token_file = PROJECT_ROOT / "data" / "gmail_token.json"
    action = args.secrets_action
    try:
        if action == "status":
            if not store.exists():
                print("\n  No vault yet: secrets are read from .env (plaintext)."
                      "\n  Move them with: franco secrets migrate\n")
                return
            print(f"\n  Vault: {store.path} (key in the OS keychain)\n")
            for ref in store.refs():
                extra = f", expires {ref['expires_at']}" if ref.get("expires_at") else ""
                print(f"    {ref['name']:24} {ref['status']:8} {ref['fingerprint']}"
                      f"  rotated {ref.get('rotated_at') or 'never'}{extra}")
            print()
        elif action == "migrate":
            moved = V.migrate_env(store, env_file, token_file)
            print(f"\n  Moved {len(moved)} secret(s) into the vault: {', '.join(moved) or 'none'}."
                  "\n  .env keeps their names, blank. Undo with: franco secrets unmigrate\n")
        elif action == "unmigrate":
            if not store.exists():
                print("\n  No vault to undo.\n")
                return
            restored = V.unmigrate_env(store, env_file, token_file)
            print(f"\n  Restored {len(restored)} secret(s) to plaintext; vault and key removed.\n")
        elif action == "set":
            if not args.name:
                sys.exit("  Usage: franco secrets set NAME")
            value = getpass.getpass(f"  Value for {args.name} (not echoed): ").strip()
            ref = store.put(args.name, value)
            print(f"\n  Stored {args.name} ({ref['fingerprint']}).\n")
        elif action == "revoke":
            if not args.name:
                sys.exit("  Usage: franco secrets revoke NAME")
            store.revoke(args.name)
            print(f"\n  Revoked {args.name}: its value is destroyed. "
                  f"Set it again to recover.\n")
        elif action == "rotate-key":
            count = store.rotate_key()
            print(f"\n  Re-encrypted {count} secret(s) under a new key.\n")
    except V.VaultError as e:
        print(f"\n  Vault: {e}\n")
        sys.exit(1)


def cmd_users(args):
    """Workspaces, members and API tokens for shared mode."""
    from franco import tenancy as T
    from franco.state import StateManager

    async def _go():
        state = StateManager()
        await state.init_db()
        store = T.TenancyStore(state)
        if args.users_action == "list":
            print()
            for m in await store.members():
                print(f"  {m['workspace_id']:16} {m['role']:8} {m['email']}"
                      + ("  (disabled)" if m["disabled_at"] else ""))
            print(f"\n  Mode: {T.mode()} (set FRANCO_MODE=shared to require tokens)\n")
        elif args.users_action == "add":
            if not args.email:
                sys.exit("  Usage: franco users add EMAIL --workspace W --role rep|manager|admin")
            await store.create_workspace(args.workspace)
            user_id = await store.add_user(args.email, args.workspace, args.role)
            token = await store.issue_token(user_id, args.workspace)
            print(f"\n  {args.email} is a {args.role} in workspace '{args.workspace}'."
                  f"\n  API token (shown once; store it in a password manager):\n\n    {token}\n")
        elif args.users_action == "revoke":
            if not args.email:
                sys.exit("  Usage: franco users revoke EMAIL")
            members = [m for m in await store.members() if m["email"] == args.email.lower()]
            if not members:
                sys.exit(f"  No user {args.email}")
            n = await store.revoke_tokens(members[0]["user_id"])
            print(f"\n  Revoked {n} token(s) for {args.email}.\n")

    try:
        asyncio.run(_go())
    except ValueError as e:
        sys.exit(f"  {e}")


def main():
    from franco.paths import PROJECT_ROOT
    project_root = str(PROJECT_ROOT)

    parser = argparse.ArgumentParser(
        prog="franco",
        description="Franco — Autonomous AI Sales Agent. Always Be Closing.",
    )
    subparsers = parser.add_subparsers(dest="command")

    # franco init
    sub = subparsers.add_parser(
        "init", help="Create a Franco home folder (config, .env, prompts, skills, data)")
    sub.add_argument("directory", nargs="?", default="",
                     help="Folder to set up (default: the current folder)")
    sub.set_defaults(func=cmd_init)

    # franco install
    sub = subparsers.add_parser("install", help="Install dependencies")
    sub.set_defaults(func=cmd_install)

    # franco setup
    sub = subparsers.add_parser("setup", help="Run the interactive setup wizard")
    sub.set_defaults(func=cmd_setup)

    # franco run
    sub = subparsers.add_parser("run", help="Start Franco's heartbeat loop")
    sub.set_defaults(func=cmd_run)

    # franco train <url>
    sub = subparsers.add_parser("train", help="Train Franco on a website")
    sub.add_argument("url", help="Website URL to crawl and learn from")
    sub.add_argument(
        "max_pages",
        nargs="?",
        type=int,
        default=100,
        help="Max pages to crawl (default: 100)",
    )
    sub.add_argument(
        "--provider", choices=["claude", "codex"], default=None,
        help="Subscription to think with (default: the one in your config, else claude)",
    )
    sub.add_argument("--model", default=None, help="Model for this provider (default: provider default)")
    sub.add_argument(
        "--reasoning-effort", default=None,
        help="Codex reasoning effort (minimal/low/medium/high/xhigh)",
    )
    sub.set_defaults(func=cmd_train)

    # franco dashboard
    sub = subparsers.add_parser("dashboard", help="Open the web dashboard")
    sub.add_argument("--host", default="127.0.0.1", help="Host (default: 127.0.0.1)")
    sub.add_argument("--port", type=int, default=5555, help="Port (default: 5555)")
    sub.set_defaults(func=cmd_dashboard)

    # franco status
    sub = subparsers.add_parser("status", help="Show pipeline status")
    sub.add_argument(
        "--check", action="store_true",
        help="Also check the configured brain now (Codex: CLI version, isolation "
             "and login; no model call)",
    )
    sub.set_defaults(func=cmd_status)

    # franco export
    sub = subparsers.add_parser(
        "export", help="Export prospects as a sequencer-ready CSV"
    )
    sub.add_argument("--out", default="prospects.csv", help="Output file (default: prospects.csv)")
    sub.add_argument(
        "--email-status", default="",
        help="Comma-separated statuses to include (default: verified,risky)",
    )
    sub.add_argument("--min-score", type=int, default=0, help="Minimum ICP score")
    sub.add_argument("--status", default="", help="Comma-separated pipeline statuses (e.g. new,queued)")
    sub.add_argument("--all", action="store_true", help="Export everything, no filters")
    sub.set_defaults(func=cmd_export)

    # franco gmail auth|test
    sub = subparsers.add_parser("gmail", help="Gmail provider setup")
    sub.add_argument("gmail_action", choices=["auth", "test"],
                     help="auth: one-time OAuth; test: verify connection")
    sub.set_defaults(func=cmd_gmail)

    # franco outbox
    sub = subparsers.add_parser("outbox", help="Review/approve queued emails")
    sub.add_argument("--approve", metavar="ID", default="", help="Approve one item")
    sub.add_argument("--approve-all", action="store_true", help="Approve all pending")
    sub.add_argument("--reject", metavar="ID", default="", help="Reject one item")
    sub.add_argument("--edit", metavar="ID", default="",
                     help="Edit one pending email in $EDITOR")
    sub.set_defaults(func=cmd_outbox)

    # franco import accounts.csv
    sub = subparsers.add_parser(
        "import", help="Import a CSV list: researched accounts, contacts, or suppressions")
    sub.add_argument("file", nargs="?", default="", help="CSV file")
    sub.add_argument("--kind", choices=["accounts", "contacts", "suppressions"], default="",
                     help="What the file holds (default: accounts in approved-copy "
                          "mode, contacts otherwise)")
    sub.add_argument("--template", action="store_true",
                     help="Print the CSV column template for --kind and exit")
    sub.add_argument("--dry-run", action="store_true",
                     help="Validate every row without importing anything")
    sub.add_argument("--verify", action="store_true",
                     help="Verify addresses not already marked verified")
    sub.add_argument("--owner", default="",
                     help="Owner for rows without an owner column")
    sub.set_defaults(func=cmd_import)

    # franco suppress list|add|remove|import|export
    sub = subparsers.add_parser("suppress", help="Shared do-not-contact list")
    sub.add_argument("suppress_action", nargs="?", default="list",
                     choices=["list", "add", "remove", "import", "export"])
    sub.add_argument("values", nargs="*", help="Emails/domains, or files for import")
    sub.add_argument("--reason", default="", help="Why (recorded with each entry)")
    sub.set_defaults(func=cmd_suppress)

    # franco sending pause|resume|status
    sub = subparsers.add_parser("discover", help="Find businesses matching your ICP")
    sub.add_argument("--provider", default="osm",
                     help="Discovery provider (default: osm — free, no account)")
    sub.add_argument("--providers", action="store_true",
                     help="List every provider with cost and setup, then exit")
    sub.add_argument("--estimate", action="store_true",
                     help="Print projected spend and exit without calling anything")
    sub.add_argument("--city", default="",
                     help='Semicolon-separated cities, e.g. "Denver, CO;Dallas, TX" '
                          '(default: icp.geography)')
    sub.add_argument("--depth", type=int, default=30,
                     help="SERP depth (default: 30 — depth 100 costs 10x since Sept 2025)")
    sub.add_argument("--limit", type=int, default=100,
                     help="Max records per query (default: 100)")
    sub.add_argument("--max-spend", type=float, default=1.0,
                     help="Hard cap in dollars (default: 1.00)")
    sub.add_argument("--no-profile", action="store_true",
                     help="Don't profile what was found (profiling is free)")
    sub.set_defaults(func=cmd_discover)

    sub = subparsers.add_parser(
        "profile", help="Read discovered companies' websites (free, no Claude)")
    sub.add_argument("--limit", type=int, default=200,
                     help="Max sites to read (default: 200)")
    sub.add_argument("--stale-days", type=int, default=90,
                     help="Re-read a site after this many days (default: 90)")
    sub.set_defaults(func=cmd_profile)

    sub = subparsers.add_parser(
        "signals", help="Review/confirm which signals Franco prospects against")
    sub.add_argument("--confirm", metavar="CODES", default="",
                     help="Comma-separated codes to confirm ('all' or 'free' accepted)")
    sub.add_argument("--reject", metavar="CODES", default="",
                     help="Comma-separated codes to turn off")
    sub.set_defaults(func=cmd_signals)

    sub = subparsers.add_parser(
        "funnels", help="Raad mode: how companies split across funnels")
    sub.add_argument("--company", default="", metavar="DOMAIN",
                     help="Explain which funnel one company is in, and why")
    sub.set_defaults(func=cmd_funnels)

    sub = subparsers.add_parser(
        "preview", help="Review an approved-copy campaign's preview, then sign it off")
    sub.add_argument("preview_action", nargs="?", default="list",
                     help="list (default), signoff or reopen; or just a campaign key")
    sub.add_argument("campaign", nargs="?", default="", help="Campaign key, e.g. M1")
    sub.add_argument("--by", default="", help="With signoff: who is signing off")
    sub.add_argument("--csv", default="", metavar="FILE",
                     help="Write the preview (one row per account) to a CSV file")
    sub.set_defaults(func=cmd_preview)

    sub = subparsers.add_parser(
        "autopilot", help="Send without review: on / off / default / status")
    sub.add_argument("autopilot_action", nargs="?", default="status",
                     choices=["on", "off", "default", "status"])
    sub.add_argument("--keep-pending", action="store_true",
                     help="With 'on': leave drafts already waiting for review")
    sub.set_defaults(func=cmd_autopilot)

    sub = subparsers.add_parser("sending", help="Kill switch: pause/resume sending")
    sub.add_argument("sending_action", nargs="?", default="status",
                     choices=["pause", "resume", "status"])
    sub.set_defaults(func=cmd_sending)

    # franco users
    sub = subparsers.add_parser("users", help="Workspaces, members and API tokens")
    sub.add_argument("users_action", nargs="?", default="list", choices=["list", "add", "revoke"])
    sub.add_argument("email", nargs="?", default="")
    sub.add_argument("--workspace", default="local")
    sub.add_argument("--role", default="rep", choices=["rep", "manager", "admin"])
    sub.set_defaults(func=cmd_users)

    # franco secrets
    sub = subparsers.add_parser("secrets", help="Encrypted credential vault")
    sub.add_argument("secrets_action", nargs="?", default="status",
                     choices=["status", "migrate", "unmigrate", "set", "revoke", "rotate-key"])
    sub.add_argument("name", nargs="?", default="")
    sub.set_defaults(func=cmd_secrets)

    # franco usage
    sub = subparsers.add_parser("usage", help="Show brain usage and quota (Claude or Codex)")
    sub.add_argument("--days", type=int, default=30, help="Breakdown window (default: 30)")
    sub.set_defaults(func=cmd_usage)

    args = parser.parse_args()
    args._project_root = project_root

    if args.command is None:
        parser.print_help()
        print("\n  Quick start:")
        print("    franco install   — Install dependencies")
        print("    franco setup     — Configure Franco (first time)")
        print("    franco run       — Start closing deals")
        print("    franco dashboard — Open the web dashboard")
        print()
        sys.exit(0)

    try:
        args.func(args)
    except KeyboardInterrupt:
        print("\n  Interrupted. Goodbye.")
        sys.exit(130)
    except Exception as e:
        # ConfigError and friends carry actionable messages — show them
        # cleanly instead of a raw traceback.
        from franco.config import ConfigError

        if isinstance(e, ConfigError):
            print(f"\n  Configuration problem:\n  {e}\n")
        else:
            print(f"\n  Error running 'franco {args.command}': {e}\n")
        sys.exit(1)


if __name__ == "__main__":
    main()
