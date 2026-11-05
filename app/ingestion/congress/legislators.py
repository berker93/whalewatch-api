"""The ``unitedstates/congress-legislators`` dataset: who has sat in Congress.

An open dataset, kept by volunteers from the Bioguide and the chambers' own
rolls, published as JSON at
``https://unitedstates.github.io/congress-legislators/``: everyone who has
served, each under their Bioguide id (``P000197``), with every name the
dataset knows them by and every term. Two files, read together:
``legislators-current.json`` (about 540 people) and
``legislators-historical.json`` (about 12,000, back to 1789). Not behind a
US-only gate like the chambers' sites, so fetched directly, not through the US
fetcher.

What is kept
------------
Only people with a term ending on or after :data:`SINCE`, the first year of
the House Clerk's disclosure indexes: about 1,330. Nobody earlier filed a PTR
the indexes list, and fewer names make fewer false trigram candidates.

From each, the Bioguide id; ``name``'s first, middle, last, suffix, nickname
and ``official_full``; the last names in ``other_names`` (a name they served
under before marrying, say); and each term's chamber (``rep`` is the House,
``sen`` the Senate), state, district (House only; 0 at large), party, start
and end. A term's party is its last: ``party_affiliations``, for someone who
switched mid-term, is not kept. Everything else (contact details, ids in
other systems, family) is left in the archived copy.

The bytes are archived as served, under their checksum, before they are read
(``raw/congress-legislators/{file}/{sha256}.json``): a dataset changed under
us, or a reader bug, is fixed against the copy that was seeded from.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from typing import Any, Final

import httpx

from app.core.logging import get_logger
from app.ingestion.congress.member_match import Term
from app.storage.raw import RawStore, congress_legislators_key

logger = get_logger(__name__)

#: Where the dataset is published, without a trailing slash.
DEFAULT_BASE_URL: Final = "https://unitedstates.github.io/congress-legislators"

#: The two files, current first: a person in both is read from it.
FILES: Final = ("legislators-current", "legislators-historical")

#: Members whose last term ended before this are not kept: the first House
#: index is 2008's.
SINCE: Final = date(2008, 1, 1)

TIMEOUT_SECONDS: Final = 60.0

_BIOGUIDE: Final = re.compile(r"[A-Z][0-9]{6}")
_STATE: Final = re.compile(r"[A-Z]{2}")
_CHAMBER_OF: Final = {"rep": "house", "sen": "senate"}


class LegislatorsError(Exception):
    """The dataset could not be fetched, or is not the shape this reads."""


@dataclass(frozen=True, slots=True)
class Legislator:
    """One person, as the dataset has them."""

    bioguide_id: str
    first_name: str
    last_name: str
    middle_name: str | None
    suffix: str | None
    nickname: str | None
    official_full: str | None
    other_last_names: tuple[str, ...]
    terms: tuple[Term, ...]
    current: bool
    """Listed in ``legislators-current``: in office now, as the dataset has it."""

    @property
    def latest(self) -> Term:
        return max(self.terms, key=lambda term: (term.start, term.end))

    @property
    def full_name(self) -> str:
        """``official_full``, or the name put together when there is none."""
        if self.official_full:
            return self.official_full
        name = " ".join(p for p in (self.first_name, self.middle_name, self.last_name) if p)
        return f"{name}, {self.suffix}" if self.suffix else name


@dataclass(frozen=True, slots=True)
class Copy:
    """One file, as fetched and archived."""

    name: str
    body: bytes
    key: str
    url: str


@dataclass(frozen=True, slots=True)
class Dataset:
    """Both files, read."""

    copies: tuple[Copy, ...]
    legislators: tuple[Legislator, ...]
    listed: int
    """People in the files, before :data:`SINCE` left most of them out."""


# --- fetching ---------------------------------------------------------------------


def file_url(base_url: str, name: str) -> str:
    return f"{base_url.rstrip('/')}/{name}.json"


async def fetch_dataset(
    store: RawStore,
    *,
    base_url: str = DEFAULT_BASE_URL,
    user_agent: str,
    transport: httpx.AsyncBaseTransport | None = None,
) -> tuple[Copy, ...]:
    """Download both files and archive each under its checksum.

    An unchanged file is already archived under its checksum, so a weekly
    re-seed writes nothing new to the store.

    :raises LegislatorsError: Either file could not be fetched.
    """
    copies: list[Copy] = []
    async with httpx.AsyncClient(
        headers={"user-agent": user_agent, "accept": "application/json"},
        timeout=TIMEOUT_SECONDS,
        follow_redirects=True,
        transport=transport,
    ) as http:
        for name in FILES:
            url = file_url(base_url, name)
            try:
                response = await http.get(url)
            except httpx.HTTPError as failure:
                raise LegislatorsError(f"{url} could not be fetched: {failure!r}") from failure
            if response.status_code != 200:
                raise LegislatorsError(f"{url} answered {response.status_code}")
            copies.append(await archive(store, name, response.content, url=url))
    return tuple(copies)


async def archive(store: RawStore, name: str, body: bytes, *, url: str) -> Copy:
    key = congress_legislators_key(name, hashlib.sha256(body).hexdigest())
    await store.put(key, body, "application/json")
    return Copy(name=name, body=body, key=key, url=url)


# --- reading ----------------------------------------------------------------------


def read_dataset(copies: Sequence[Copy], *, since: date = SINCE) -> Dataset:
    """Both files' people with a term since ``since``, current first.

    :raises LegislatorsError: A file is not JSON, or a person in it lacks what
        every person has — a Bioguide id, a first and last name, a term with
        dates, a chamber and a state. One bad person fails the read: the
        dataset is curated, and a gap in it means its shape changed.
    """
    seen: dict[str, Legislator] = {}
    listed = 0
    for copy in copies:
        try:
            people = json.loads(copy.body)
        except ValueError as failure:
            raise LegislatorsError(f"{copy.name} is not JSON: {failure}") from failure
        if not isinstance(people, list):
            raise LegislatorsError(f"{copy.name} is not a list of people")
        for position, person in enumerate(people):
            listed += 1
            try:
                legislator = read_person(person, current=copy.name == FILES[0])
            except (LegislatorsError, KeyError, TypeError, ValueError) as failure:
                raise LegislatorsError(f"{copy.name} person {position}: {failure!r}") from failure
            if legislator.bioguide_id in seen:
                continue  # in both files: the current one's, read first
            if max(term.end for term in legislator.terms) >= since:
                seen[legislator.bioguide_id] = legislator
    return Dataset(copies=tuple(copies), legislators=tuple(seen.values()), listed=listed)


def read_person(person: Mapping[str, Any], *, current: bool) -> Legislator:
    bioguide = person["id"]["bioguide"]
    if not isinstance(bioguide, str) or not _BIOGUIDE.fullmatch(bioguide):
        raise LegislatorsError(f"{bioguide!r} is not a Bioguide id")
    name = person["name"]
    first, last = _text(name.get("first")), _text(name.get("last"))
    if first is None or last is None:
        raise LegislatorsError(f"{bioguide} has no first or last name")
    terms = tuple(_term(term) for term in person["terms"])
    if not terms:
        raise LegislatorsError(f"{bioguide} has no terms")
    others = tuple(
        dict.fromkeys(
            other_last
            for other in person.get("other_names") or ()
            if (other_last := _text(other.get("last"))) is not None and other_last != last
        )
    )
    return Legislator(
        bioguide_id=bioguide,
        first_name=first,
        last_name=last,
        middle_name=_text(name.get("middle")),
        suffix=_text(name.get("suffix")),
        nickname=_text(name.get("nickname")),
        official_full=_text(name.get("official_full")),
        other_last_names=others,
        terms=terms,
        current=current,
    )


def _term(term: Mapping[str, Any]) -> Term:
    chamber = _CHAMBER_OF.get(term["type"])
    if chamber is None:
        raise LegislatorsError(f"term type {term['type']!r} is neither rep nor sen")
    state = term["state"]
    if not isinstance(state, str) or not _STATE.fullmatch(state):
        raise LegislatorsError(f"{state!r} is not a state")
    start, end = date.fromisoformat(term["start"]), date.fromisoformat(term["end"])
    if end < start:
        raise LegislatorsError(f"a term ends ({end}) before it starts ({start})")
    district = term.get("district")
    if chamber == "house":
        if not isinstance(district, int):
            raise LegislatorsError(f"a House term has district {district!r}")
        # -1 is "unknown" in the 1800s; nobody kept has one, but it is no district.
        district = None if district < 0 else district
    else:
        district = None
    return Term(
        chamber=chamber,
        state=state,
        start=start,
        end=end,
        district=district,
        party=_text(term.get("party")),
    )


def _text(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise LegislatorsError(f"{value!r} is not text")
    return " ".join(value.split()) or None
