"""Bring your own lists, as CSV files.

Three kinds, each with a template (`franco import --template --kind K`):

- ``accounts``: hand-researched accounts for approved copy. One row per
  account and buyer: the company, the one person to contact, the approved
  campaign they get, and the fact that justifies contacting them, with the
  page it came from and the date it was checked. That fact becomes the
  account's TRIGGER_OBSERVATION and fills {{verified_observation}}; a row
  without one is rejected, because an account without a sourced reason to
  write is never emailed.
- ``contacts``: a list of companies, optionally with a person at each, for
  generated outreach. An alternative to (or a top-up for) paid discovery:
  Franco profiles each company and finds and verifies people as usual.
- ``suppressions``: email addresses and whole domains never to contact.

Every row is validated before anything is written, and a dry run reports
exactly what would happen. The CLI (`franco import`) and the dashboard's
Import panel both go through this module.
"""

import csv
import io
import re
from dataclasses import dataclass, field
from datetime import date, datetime

from franco.models.company import Company
from franco.models.prospect import Prospect
from franco.outreach import TRIGGER_SIGNAL, clean_observation, csv_safe
from franco.signals import seed_signal_catalog

REQUIRED = (
    "company", "domain", "campaign", "first_name", "last_name", "title",
    "email", "observation", "source_url", "source_date",
)
OPTIONAL = ("owner", "segment", "hypothesis", "score", "email_status", "linkedin_url")
COLUMNS = REQUIRED + OPTIONAL

# Spreadsheet headers people actually write, mapped to ours.
ALIASES = {
    "account": "company", "company_name": "company", "website": "domain",
    "public_url": "domain", "campaign_key": "campaign", "first": "first_name",
    "last": "last_name", "buyer_role": "title", "role": "title",
    "verified_observation": "observation", "public_observation": "observation",
    "observed_trigger": "observation", "trigger": "observation",
    "evidence_url": "source_url", "trigger_source": "source_url",
    "evidence_link": "source_url", "source_date_checked": "source_date",
    "date_checked": "source_date", "checked": "source_date",
    "qualified_score": "score", "score_0_10": "score",
    "funnel": "campaign",
}

# Contact lists (generated outreach): only the company is required.
CONTACT_REQUIRED = ("company", "domain")
CONTACT_OPTIONAL = (
    "first_name", "last_name", "title", "email", "email_status",
    "linkedin_url", "segment", "location", "phone", "owner", "notes",
    # raad mode: pin a funnel, and/or give the sourced fact to personalize on
    "campaign", "observation", "source_url", "source_date",
)
CONTACT_COLUMNS = CONTACT_REQUIRED + CONTACT_OPTIONAL

# Suppression lists: an address or a domain per row.
SUPPRESSION_COLUMNS = ("value", "reason")
SUPPRESSION_ALIASES = {"email": "value", "domain": "value", "address": "value",
                       "email_or_domain": "value", "note": "reason"}

KINDS = ("accounts", "contacts", "suppressions")

EMAIL_STATUSES = {"verified", "risky", "guess", "invalid"}
EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$")

TEMPLATE = (
    ",".join(COLUMNS) + "\n"
    "Example Market,example-market.com,M1,Dana,Whitfield,CEO,dana@example-market.com,"
    "\"your seller help center describes a paid Featured Listing upgrade\","
    "https://example-market.com/help/featured,2026-10-14,Hemin,Vertical marketplace,"
    "\"Flat featured fee; no relevance or measurement story\",8,,\n"
)


CONTACTS_TEMPLATE = (
    ",".join(CONTACT_COLUMNS) + "\n"
    "Example Market,example-market.com,Dana,Whitfield,CEO,dana@example-market.com,"
    ",,Vertical marketplace,\"Austin, TX\",,,,,"
    "\"your seller help center describes a paid Featured Listing upgrade\","
    "https://example-market.com/help/featured,2026-10-14\n"
    "Another Co,another.example,,,,,,,,,,,company only: Franco finds the people,,,,\n"
)

SUPPRESSIONS_TEMPLATE = (
    "value,reason\n"
    "someone@example.com,asked to stop\n"
    "competitor.example,never contact this domain\n"
)

TEMPLATES = {"accounts": TEMPLATE, "contacts": CONTACTS_TEMPLATE,
             "suppressions": SUPPRESSIONS_TEMPLATE}


@dataclass
class ImportReport:
    imported: list[str] = field(default_factory=list)      # emails
    rejected: list[tuple[int, str]] = field(default_factory=list)  # (row, why)
    warnings: list[tuple[int, str]] = field(default_factory=list)


def _norm_header(name: str) -> str:
    key = re.sub(r"[^a-z0-9]+", "_", (name or "").strip().lower()).strip("_")
    return ALIASES.get(key, key)


def read_rows(text: str, aliases: dict[str, str] | None = None) -> list[dict[str, str]]:
    """CSV text → rows keyed by our column names (aliases resolved)."""
    reader = csv.DictReader(io.StringIO(text.lstrip("\ufeff")))
    rows = []
    for raw in reader:
        row: dict[str, str] = {}
        for k, v in raw.items():
            if k is None:
                continue
            key = _norm_header(k)
            if aliases:
                key = aliases.get(key, key)
            row[key] = (v or "").strip()
        if any(row.values()):
            rows.append(row)
    return rows


def read_suppressions(text: str) -> list[dict[str, str]]:
    """A suppression CSV. A file with no header (one address per line) works too."""
    first = text.lstrip("\ufeff").split("\n", 1)[0]
    if "@" in first or ("." in first and "," not in first and " " not in first.strip()):
        text = "value\n" + text   # headerless: the first line is data
    return read_rows(text, SUPPRESSION_ALIASES)


def _clean_domain(value: str) -> str:
    d = re.sub(r"^https?://", "", (value or "").strip().lower())
    return d.split("/", 1)[0].removeprefix("www.")


def _parse_date(value: str) -> date | None:
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%d %b %Y", "%b %d, %Y"):
        try:
            return datetime.strptime(value.strip(), fmt).date()
        except ValueError:
            continue
    return None


def validate_row(row: dict[str, str], campaigns: set[str]) -> tuple[list[str], list[str]]:
    """(problems that reject the row, warnings that don't)."""
    problems, warnings = [], []
    missing = [c for c in REQUIRED if not row.get(c)]
    if missing:
        problems.append("missing " + ", ".join(missing))
    campaign = row.get("campaign", "")
    if campaign and campaign not in campaigns:
        known = ", ".join(sorted(campaigns)) or "none configured"
        problems.append(f"campaign '{campaign}' is not a configured campaign or funnel ({known})")
    email = row.get("email", "").lower()
    if email and not EMAIL_RE.match(email):
        problems.append(f"invalid email '{email}'")
    domain = _clean_domain(row.get("domain", ""))
    if email and domain and not (email.endswith("@" + domain) or email.endswith("." + domain)):
        warnings.append(f"email domain differs from company domain {domain}")
    url = row.get("source_url", "")
    if url and not url.startswith(("http://", "https://")):
        problems.append("source_url must be a full http(s) link to the page you checked")
    if row.get("source_date"):
        checked = _parse_date(row["source_date"])
        if checked is None:
            problems.append(f"source_date '{row['source_date']}' is not a date (use YYYY-MM-DD)")
        elif checked > date.today():
            problems.append("source_date is in the future")
    if row.get("observation") and len(clean_observation(row["observation"])) < 12:
        problems.append("observation is too short to be a specific, checkable fact")
    score = row.get("score", "")
    if score:
        try:
            if not 0 <= int(score) <= 10:
                raise ValueError
        except ValueError:
            problems.append(f"score '{score}' must be a whole number 0-10")
    status = row.get("email_status", "").lower()
    if status and status not in EMAIL_STATUSES:
        problems.append(f"email_status must be one of {sorted(EMAIL_STATUSES)}")
    return problems, warnings


async def import_accounts(
    state,
    rows: list[dict[str, str]],
    campaigns: set[str],
    default_owner: str = "",
    verify=None,
    dry_run: bool = False,
) -> ImportReport:
    """Validate every row, then record the valid ones.

    ``verify``: optional ``async (email) -> status | None`` used for rows
    not already marked verified. ``dry_run`` validates without writing.
    """
    report = ImportReport()
    if not dry_run:
        await seed_signal_catalog(state)
    seen_emails: set[str] = set()

    for n, row in enumerate(rows, start=2):  # row 1 is the header
        problems, warnings = validate_row(row, campaigns)
        email = row.get("email", "").lower()
        domain = _clean_domain(row.get("domain", ""))
        owner = row.get("owner") or default_owner

        if not problems:
            if email in seen_emails:
                problems.append("duplicate email in this file")
            blocked = await state.suppression_for(email)
            if blocked:
                problems.append(f"suppressed ({blocked['kind']}: {blocked['reason'] or 'listed'})")
            existing_company = await state.get_company_by_domain(domain)
            if existing_company and existing_company.owner and owner \
                    and existing_company.owner != owner:
                problems.append(f"account already owned by {existing_company.owner}")
            existing = await state.get_prospect_by_email(email)
            if existing and existing.status != "new":
                problems.append(f"already in outreach (status '{existing.status}')")

        for w in warnings:
            report.warnings.append((n, w))
        if problems:
            report.rejected.append((n, "; ".join(problems)))
            continue
        seen_emails.add(email)
        if dry_run:
            report.imported.append(email)
            continue

        score = int(row["score"]) if row.get("score") else None
        company_id = await state.add_company(Company(
            name=row["company"], domain=domain, website=f"https://{domain}",
            industry=row.get("segment", ""), source="import",
            source_url=row["source_url"],
        ))
        await state.update_company_account(
            company_id, campaign_key=row["campaign"], owner=owner,
            segment=row.get("segment", ""), hypothesis=row.get("hypothesis", ""),
            qualified_score=score,
        )

        observation = clean_observation(row["observation"])
        checked = _parse_date(row["source_date"]).isoformat() + " 00:00:00"
        latest = await state.latest_observation(company_id, TRIGGER_SIGNAL)
        if not (latest and latest["value_text"] == observation
                and latest["evidence_url"] == row["source_url"]):
            await state.add_observation(
                TRIGGER_SIGNAL, company_id=company_id, collector="import",
                value_text=observation, evidence_url=row["source_url"],
                observed_at=checked, confidence=1.0,
            )

        status = (row.get("email_status") or "guess").lower()
        if verify is not None and status != "verified":
            status = await verify(email) or status
        prospect_id = await state.add_prospect(Prospect(
            company_id=company_id, first_name=row["first_name"],
            last_name=row["last_name"], title=row["title"], email=email,
            email_status=status, email_verified=(status == "verified"),
            linkedin_url=row.get("linkedin_url", ""), source="import",
            source_url=row["source_url"], company=row["company"],
            industry=row.get("segment", ""),
            score=(score * 10 if score is not None else 0),
        ))
        # A re-import refreshes what research can change about the contact.
        await state.update_prospect_email(prospect_id, email, status)
        report.imported.append(email)
    return report


# ── Contact lists (generated outreach) ──


def validate_contact_row(
    row: dict[str, str], campaigns: set[str] | None = None,
) -> tuple[list[str], list[str]]:
    problems, warnings = [], []
    campaign = row.get("campaign", "")
    if campaign and campaigns is not None and campaign not in campaigns:
        known = ", ".join(sorted(campaigns)) or "none configured"
        problems.append(f"campaign '{campaign}' is not a configured campaign or funnel ({known})")
    if row.get("observation"):
        if not row.get("source_url", "").startswith(("http://", "https://")):
            problems.append("an observation needs the full http(s) source_url it came from")
        checked = _parse_date(row.get("source_date", ""))
        if checked is None:
            problems.append("an observation needs the source_date it was checked (YYYY-MM-DD)")
        elif checked > date.today():
            problems.append("source_date is in the future")
        if len(clean_observation(row["observation"])) < 12:
            problems.append("observation is too short to be a specific, checkable fact")
    if not row.get("company"):
        problems.append("missing company")
    domain = _clean_domain(row.get("domain", ""))
    if not domain or "." not in domain:
        problems.append("missing or invalid domain")
    email = row.get("email", "").lower()
    if email and not EMAIL_RE.match(email):
        problems.append(f"invalid email '{email}'")
    if email and domain and not (email.endswith("@" + domain) or email.endswith("." + domain)):
        warnings.append(f"email domain differs from company domain {domain}")
    status = row.get("email_status", "").lower()
    if status and status not in EMAIL_STATUSES:
        problems.append(f"email_status must be one of {sorted(EMAIL_STATUSES)}")
    if (row.get("first_name") or row.get("last_name")) and not (
            row.get("first_name") and row.get("last_name")) and not email:
        problems.append("a person needs a first and last name, or an email")
    url = row.get("linkedin_url", "")
    if url and not url.startswith("https://"):
        problems.append("linkedin_url must be a full https:// link")
    return problems, warnings


async def import_contacts(
    state,
    rows: list[dict[str, str]],
    default_owner: str = "",
    verify=None,
    dry_run: bool = False,
    campaigns: set[str] | None = None,
) -> ImportReport:
    """Companies (and optionally one person at each) for generated outreach.

    A company-only row just adds the company; Franco profiles it and finds
    people the same way it does for discovered businesses. ``imported``
    lists the company domains (or emails, for rows with a person).
    """
    report = ImportReport()
    seen: set[str] = set()
    if not dry_run and any(r.get("observation") for r in rows):
        await seed_signal_catalog(state)
    for n, row in enumerate(rows, start=2):
        problems, warnings = validate_contact_row(row, campaigns)
        email = row.get("email", "").lower()
        domain = _clean_domain(row.get("domain", ""))
        has_person = bool(email or (row.get("first_name") and row.get("last_name")))
        key = email or f"{domain}|{row.get('first_name', '').lower()}|{row.get('last_name', '').lower()}"
        if not problems:
            if key in seen:
                problems.append("duplicate row in this file")
            blocked = await state.suppression_for(email or f"x@{domain}")
            if blocked:
                problems.append(f"suppressed ({blocked['kind']}: {blocked['reason'] or 'listed'})")
            if email:
                existing = await state.get_prospect_by_email(email)
                if existing and existing.status != "new":
                    problems.append(f"already in outreach (status '{existing.status}')")
        for w in warnings:
            report.warnings.append((n, w))
        if problems:
            report.rejected.append((n, "; ".join(problems)))
            continue
        seen.add(key)
        label = email or domain
        if dry_run:
            report.imported.append(label)
            continue

        company_id = await state.add_company(Company(
            name=row["company"], domain=domain, website=f"https://{domain}",
            industry=row.get("segment", ""), location=row.get("location", ""),
            phone=row.get("phone", ""), notes=row.get("notes", ""), source="import",
        ))
        owner = row.get("owner") or default_owner
        account = {k: v for k, v in (("owner", owner), ("segment", row.get("segment", "")),
                                     ("campaign_key", row.get("campaign", ""))) if v}
        if account:
            await state.update_company_account(company_id, **account)
        if row.get("observation"):
            observation = clean_observation(row["observation"])
            latest = await state.latest_observation(company_id, TRIGGER_SIGNAL)
            if not (latest and latest["value_text"] == observation
                    and latest["evidence_url"] == row["source_url"]):
                await state.add_observation(
                    TRIGGER_SIGNAL, company_id=company_id, collector="import",
                    value_text=observation, evidence_url=row["source_url"],
                    observed_at=_parse_date(row["source_date"]).isoformat() + " 00:00:00",
                    confidence=1.0,
                )
        if has_person:
            status = (row.get("email_status") or "guess").lower() if email else "guess"
            if verify is not None and email and status != "verified":
                status = await verify(email) or status
            prospect_id = await state.add_prospect(Prospect(
                company_id=company_id, first_name=row.get("first_name", ""),
                last_name=row.get("last_name", ""), title=row.get("title", ""),
                email=email, email_status=status, email_verified=(status == "verified"),
                linkedin_url=row.get("linkedin_url", ""), source="import",
                company=row["company"], industry=row.get("segment", ""),
            ))
            if email:
                await state.update_prospect_email(prospect_id, email, status)
        report.imported.append(label)
    return report


# ── Suppression lists ──


async def import_suppressions(
    state, rows: list[dict[str, str]], default_reason: str = "",
    source: str = "import", dry_run: bool = False,
) -> ImportReport:
    """``imported`` lists newly added values; already-listed ones are warnings."""
    report = ImportReport()
    for n, row in enumerate(rows, start=2):
        value = (row.get("value") or "").strip().strip('"').lower()
        if not value:
            report.rejected.append((n, "no email or domain"))
            continue
        if "@" in value:
            if not EMAIL_RE.match(value):
                report.rejected.append((n, f"invalid email '{value}'"))
                continue
        else:
            value = _clean_domain(value)
            if "." not in value:
                report.rejected.append((n, f"'{value}' is not an email or a domain"))
                continue
        reason = row.get("reason") or default_reason
        if dry_run:
            if await state.suppression_for(value if "@" in value else f"x@{value}"):
                report.warnings.append((n, f"{value} already suppressed"))
            else:
                report.imported.append(value)
            continue
        if await state.add_suppression(value, reason=reason, source=source):
            report.imported.append(value)
        else:
            report.warnings.append((n, f"{value} already suppressed"))
    return report


def suppressions_csv(rows: list[dict]) -> str:
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(("value", "kind", "reason", "source", "created_at"))
    for row in rows:
        writer.writerow([csv_safe(row.get(k) or "") for k in
                         ("value", "kind", "reason", "source", "created_at")])
    return out.getvalue()


async def run_import(
    state, kind: str, text: str, campaigns: set[str] | None = None,
    default_owner: str = "", verify=None, dry_run: bool = False,
    source: str = "import",
) -> ImportReport:
    """One entry point for every list kind (CLI and dashboard)."""
    from franco.fixtures import PrototypeDataError, looks_like_prototype_text
    if looks_like_prototype_text(text):
        raise PrototypeDataError("this is a blueprint prototype export (simulated data), "
                         "not a CSV list; it cannot be imported")
    if kind == "accounts":
        return await import_accounts(state, read_rows(text), campaigns or set(),
                                     default_owner=default_owner, verify=verify,
                                     dry_run=dry_run)
    if kind == "contacts":
        return await import_contacts(state, read_rows(text), default_owner=default_owner,
                                     verify=verify, dry_run=dry_run,
                                     campaigns=campaigns)
    if kind == "suppressions":
        return await import_suppressions(state, read_suppressions(text),
                                         source=source, dry_run=dry_run)
    raise ValueError(f"kind must be one of {', '.join(KINDS)}")


def make_verifier(env):
    """``async (email) -> status`` using the configured verification
    providers (spends one verification credit per unverified address)."""
    from franco.integrations.email_finder import (
        _verify_candidate, classify_mx, get_mx_host,
    )

    async def verify(email: str) -> str:
        mx = await get_mx_host(email.split("@", 1)[1])
        if not mx:
            return "invalid"
        status, _ = await _verify_candidate(email, classify_mx(mx), env)
        return status

    return verify


def default_kind(config) -> str:
    outreach = getattr(config, "outreach", None)
    return "accounts" if outreach is not None and outreach.mode == "templates" else "contacts"
