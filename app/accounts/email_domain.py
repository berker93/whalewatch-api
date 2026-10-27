"""Whether the sending domain's DNS is ready: SPF, DKIM and DMARC, before the first send.

What ``check-email-domain`` runs, against the domain ``EMAIL_FROM`` sends
from (``mail.whalewatch.com``). It fails until each of these is published:

``spf``
    A ``v=spf1`` TXT record on the sending domain that includes Postmark's
    (``include:spf.mtasv.net``).
``return_path``
    ``pm-bounces.<domain>`` a CNAME to ``pm.mtasv.net``: Postmark's bounce
    address, on our domain, so SPF passes aligned with the From address.
``dkim``
    ``<selector>._domainkey.<domain>``: the TXT key Postmark generated for the
    domain, ``p=`` not empty.
``dmarc``
    ``_dmarc.<domain>``, or failing that the organisational domain's record,
    whose ``sp=`` (or ``p=``) then governs the subdomain. ``p=none`` passes
    with a warning: it reports, and enforces nothing.

The values are in docs/email-delivery.md. Postmark itself also refuses to send
from a domain whose DKIM it has not verified.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final, Protocol

import dns.asyncresolver
import dns.resolver
from dns.exception import DNSException

POSTMARK_SPF: Final = "include:spf.mtasv.net"
POSTMARK_RETURN_PATH: Final = "pm.mtasv.net"
RETURN_PATH_LABEL: Final = "pm-bounces"

__all__ = ["Check", "DNSException", "DnsResolver", "Resolver", "check_domain"]


class Resolver(Protocol):
    async def txt(self, name: str) -> list[str]:
        """Every TXT record at ``name``, each one's strings joined; none, empty."""
        ...

    async def cname(self, name: str) -> str | None:
        """The CNAME target at ``name``, without the trailing dot; None if none."""
        ...


class DnsResolver:
    """The system's resolver, through dnspython."""

    _NOTHING = (dns.resolver.NXDOMAIN, dns.resolver.NoAnswer, dns.resolver.NoNameservers)

    async def txt(self, name: str) -> list[str]:
        try:
            answer = await dns.asyncresolver.resolve(name, "TXT")
        except self._NOTHING:
            return []
        return [b"".join(record.strings).decode() for record in answer]

    async def cname(self, name: str) -> str | None:
        try:
            answer = await dns.asyncresolver.resolve(name, "CNAME")
        except self._NOTHING:
            return None
        return str(answer[0].target).rstrip(".")


@dataclass(frozen=True, slots=True)
class Check:
    name: str
    ok: bool
    detail: str
    warning: str | None = None


def _tags(record: str) -> dict[str, str]:
    """``v=DMARC1; p=reject; sp=quarantine`` as ``{v: DMARC1, p: reject, sp: quarantine}``."""
    tags = {}
    for part in record.split(";"):
        key, sep, value = part.strip().partition("=")
        if sep:
            tags[key.strip().lower()] = value.strip()
    return tags


async def _spf(resolver: Resolver, domain: str) -> Check:
    records = [r for r in await resolver.txt(domain) if r.lower().startswith("v=spf1")]
    if not records:
        return Check("spf", False, f"no v=spf1 TXT record on {domain}")
    if len(records) > 1:
        return Check("spf", False, f"{len(records)} SPF records on {domain}: SPF allows one")
    if POSTMARK_SPF not in records[0].split():
        return Check("spf", False, f"{domain}'s SPF does not have {POSTMARK_SPF}: {records[0]}")
    return Check("spf", True, records[0])


async def _return_path(resolver: Resolver, domain: str) -> Check:
    name = f"{RETURN_PATH_LABEL}.{domain}"
    target = await resolver.cname(name)
    if target is None:
        return Check("return_path", False, f"no CNAME at {name}")
    if target.lower() != POSTMARK_RETURN_PATH:
        return Check("return_path", False, f"{name} points at {target}, not {POSTMARK_RETURN_PATH}")
    return Check("return_path", True, f"{name} -> {target}")


async def _dkim(resolver: Resolver, domain: str, selector: str) -> Check:
    name = f"{selector}._domainkey.{domain}"
    for record in await resolver.txt(name):
        tags = _tags(record)
        if tags.get("p"):
            return Check("dkim", True, f"{name}: a {tags.get('k', 'rsa')} key")
    return Check("dkim", False, f"no DKIM key (p=) at {name}")


async def _dmarc(resolver: Resolver, domain: str) -> Check:
    labels = domain.split(".")
    # The domain's own record, then the organisational domain's: a naive
    # two-label one, right for whalewatch.com, not for a .co.uk.
    candidates = [(domain, "p")]
    if len(labels) > 2:
        candidates.append((".".join(labels[-2:]), "sp"))
    for owner, policy_tag in candidates:
        for record in await resolver.txt(f"_dmarc.{owner}"):
            tags = _tags(record)
            if tags.get("v", "").upper() != "DMARC1":
                continue
            policy = tags.get(policy_tag) or tags.get("p")
            if policy is None:
                return Check("dmarc", False, f"_dmarc.{owner} has no p=")
            detail = f"_dmarc.{owner}: {record}"
            if policy.lower() == "none":
                return Check(
                    "dmarc", True, detail, warning="the policy is none: reports, enforces nothing"
                )
            return Check("dmarc", True, detail)
    return Check("dmarc", False, f"no v=DMARC1 TXT record at _dmarc.{domain} or its parent")


async def check_domain(resolver: Resolver, domain: str, *, dkim_selector: str) -> Sequence[Check]:
    return [
        await _spf(resolver, domain),
        await _return_path(resolver, domain),
        await _dkim(resolver, domain, dkim_selector),
        await _dmarc(resolver, domain),
    ]
