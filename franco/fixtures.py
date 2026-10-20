"""Keep fictional data out of production outreach.

The blueprint, the screenshots and the synthetic benchmark all use fictional
people on domains reserved for documentation and testing (RFC 2606, RFC 6761).
Those domains can never belong to a real prospect, so an address on one is,
by definition, a fixture. Every outreach path refuses them in code: the
pre-send gate, native staging, the Instantly lead upload and the default
sequencer export.

The blueprint's "Export prototype configuration" file is the other fixture
that could leak. It is JSON, and JSON is valid YAML, so a renamed export
would otherwise load as franco.yaml. ``is_prototype_payload`` recognises it.
"""

# RFC 2606 §2 and RFC 6761 §6: names that are never delegated to anyone.
RESERVED_TLDS = frozenset({"test", "example", "invalid", "localhost"})
# RFC 2606 §3: second-level names kept for documentation.
RESERVED_DOMAINS = frozenset({"example.com", "example.net", "example.org"})

PROTOTYPE_KIND = "franco-prototype-configuration"


class PrototypeDataError(ValueError):
    """Simulated prototype data was handed to a production entry point."""


def domain_of(address: str) -> str:
    """The lowercased domain of an email address or a bare domain/URL."""
    value = (address or "").strip().lower()
    if "@" in value:
        value = value.rsplit("@", 1)[1]
    if "://" in value:
        value = value.split("://", 1)[1]
    return value.split("/", 1)[0].split(":", 1)[0].rstrip(".")


def is_reserved_domain(domain: str) -> bool:
    domain = domain_of(domain)
    if not domain:
        return False
    if domain.rsplit(".", 1)[-1] in RESERVED_TLDS:
        return True
    return any(domain == d or domain.endswith("." + d) for d in RESERVED_DOMAINS)


def is_fixture_address(email: str) -> bool:
    """True for an address that can only be fictional (reserved domain)."""
    return "@" in (email or "") and is_reserved_domain(email)


FIXTURE_REASON = "recipient is on a reserved test domain (fixture data, never sent)"


def is_prototype_payload(data) -> bool:
    """True for the blueprint's exported prototype configuration."""
    return isinstance(data, dict) and (
        data.get("kind") == PROTOTYPE_KIND or data.get("simulated") is True)


def looks_like_prototype_text(text: str) -> bool:
    """Cheap check for a prototype export handed to a CSV importer."""
    head = (text or "").lstrip()[:4096]
    return head.startswith("{") and PROTOTYPE_KIND in head
