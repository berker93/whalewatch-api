"""A name as a chamber's index filed it, to the member of Congress it is.

Pure: a :class:`Registry` of members in, a :class:`Match` out, no database.
:mod:`app.ingestion.congress.members` loads the registry, groups filings by
:func:`name_key`, and writes what this decides.

Three tiers, tried in order
---------------------------
**Exact.** The filed ``first last``, ignoring case and spacing, is one of the
member's names as the registry spells them: ``first last``, ``first middle
last``, ``nickname last``, or the official full name without its suffix.
``Suzan K. DelBene`` is exact; ``Suzan K DelBene`` is not.

**Normalised.** The same after both sides are folded: accents and punctuation
dropped, honorifics and degrees left out (``Hon.``, ``Dr``, ``MD, Facs``),
suffixes left out (``Jr``, ``III``), a quoted nickname taken as a first name
(``Charles J. "Chuck"``). Or the surnames agree and the filed first name is
the member's first name, middle name or nickname: ``Harold Dallas Rogers`` is
``Harold Rogers``, ``Earl Leroy Carter`` is ``Earl L. "Buddy" Carter``.

**Trigram.** Neither, so the closest names by ``pg_trgm``'s ``similarity()``
(:func:`similarity` is a port of it): ``Rohit Khanna`` to ``Ro Khanna`` 0.64,
``Richard W. Allen`` to ``Rick W. Allen`` 0.61. Usually right — the House
indexes people by their legal names, the registry by the names they go by —
and **never** accepted here, whatever its score: a person accepts it once,
for every filing under that name.

In office
---------
Exact and normalised matches are looked for first among the members who sat in
the filing's chamber when it was filed — for the House, for the state its
``StateDst`` names — or within :data:`GRACE_DAYS` after, since a member who
left still files for trades made in office. One such member is the match. A
name that only fits someone not in office then (a former representative now in
the Senate whose new term the registry does not have yet, a typo in
``StateDst``) is a candidate at :data:`OUT_OF_OFFICE` of the score. Two that fit
split it. Trigram looks only at members in office.

The confidence, and the threshold
---------------------------------
Each candidate has a confidence from 0 to 1: :data:`EXACT_SCORE` or
:data:`NORMALISED_SCORE`, times :data:`OUT_OF_OFFICE` if out of office,
divided by the number of members the name fits as well; or trigram's
similarity. A match is accepted automatically only if it is exact or
normalised and its confidence is at least :data:`AUTO_ACCEPT` — that is, one
member in office fits. Anything else waits for a person, with its candidates.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta
from enum import StrEnum
from functools import cached_property
from typing import Final

#: The lowest confidence accepted without a person. Mirrored by the
#: ``auto_match_is_confident`` CHECK on ``congress_filed_name``.
AUTO_ACCEPT: Final = 0.9

EXACT_SCORE: Final = 1.0
NORMALISED_SCORE: Final = 0.95

#: The factor on a name that fits a member who was not in the chamber (and
#: state) at the time. Brings an exact match to 0.6: below :data:`AUTO_ACCEPT`.
OUT_OF_OFFICE: Final = 0.6

#: Trigram candidates below this are not shown: ``pg_trgm``'s own default
#: ``similarity_threshold``.
TRIGRAM_FLOOR: Final = 0.3

#: The most candidates kept for a person to choose from.
MAX_CANDIDATES: Final = 3

#: How long after a term ends its member is still "in office" for a filing:
#: the last PTRs come in after the term, for trades made during it.
GRACE_DAYS: Final = 366

#: Words in a filed name that are not part of it: honorifics, ranks, degrees.
#: Matched after folding, so ``Hon.`` is ``hon`` and ``M.D.`` is ``m d`` — the
#: single letters are left alone, as they are also initials.
NOT_NAMES: Final = frozenset(
    {
        "hon",
        "honorable",
        "the",
        "mr",
        "mrs",
        "ms",
        "miss",
        "dr",
        "md",
        "facs",
        "phd",
        "jd",
        "esq",
        "cpa",
        "dds",
        "dvm",
        "rn",
        "ret",
        "col",
        "gen",
        "sen",
        "senator",
        "rep",
        "representative",
    }
)

#: Generational suffixes, folded. Not ``v``, which is as often an initial.
SUFFIXES: Final = frozenset({"jr", "sr", "ii", "iii", "iv"})

_QUOTED: Final = re.compile('["\u201c\u201d(]([^"\u201c\u201d()]+)["\u201c\u201d)]')
_SPACE: Final = re.compile(r"\s+")
_NOT_ALNUM: Final = re.compile(r"[^a-z0-9]+")
_TRAILING_SUFFIX: Final = re.compile(r",?\s+(jr|sr|ii|iii|iv)\.?$", re.IGNORECASE)


class Method(StrEnum):
    """How a name was matched. Text with a ``CHECK``, by the rule in
    :mod:`app.db.models.enums`."""

    EXACT = "exact"
    NORMALISED = "normalised"
    TRIGRAM = "trigram"
    """Only ever a candidate's: a filed name is never matched by it."""
    MANUAL = "manual"
    """A person's: accepted from the review queue, or assigned. Never changed
    by a run."""


#: The methods a run may match a filed name by, without a person.
AUTOMATIC: Final = (Method.EXACT, Method.NORMALISED)


# --- folding ------------------------------------------------------------------


def fold(text: str) -> list[str]:
    """``text``'s words, lower case, without accents or punctuation.

    An apostrophe joins (``O'Rourke`` is ``orourke``); a full stop, a hyphen
    or anything else not a letter or digit separates (``Ocasio-Cortez`` is
    ``ocasio cortez``, ``J.D.`` is ``j d``).
    """
    decomposed = unicodedata.normalize("NFKD", text)
    plain = "".join(c for c in decomposed if not unicodedata.combining(c)).casefold()
    plain = plain.replace("'", "").replace("\u2019", "")
    return [word for word in _NOT_ALNUM.split(plain) if word]


def _spaced(text: str) -> str:
    """Case and spacing ignored, nothing else: what "exact" compares."""
    return _SPACE.sub(" ", text).strip().casefold()


def _without_suffix(text: str) -> str:
    return _TRAILING_SUFFIX.sub("", text.strip())


@dataclass(frozen=True, slots=True)
class FiledParts:
    """A filed name, folded."""

    given: tuple[str, ...]
    """The first-name field's words, honorifics and suffixes out, quotes too."""
    nicknames: tuple[tuple[str, ...], ...]
    """What it had in quotes or brackets: ``Chuck``."""
    surname: tuple[str, ...]


def filed_parts(first_name: str | None, last_name: str) -> FiledParts:
    first = first_name or ""
    nicknames = tuple(
        tuple(words) for words in (fold(quoted) for quoted in _QUOTED.findall(first)) if words
    )
    given = tuple(
        word
        for word in fold(_QUOTED.sub(" ", first))
        if word not in NOT_NAMES and word not in SUFFIXES
    )
    surname = fold(last_name)
    while surname and surname[-1] in SUFFIXES:
        surname.pop()
    return FiledParts(given=given, nicknames=nicknames, surname=tuple(surname))


def name_key(first_name: str | None, last_name: str) -> str:
    """What filings are grouped by: the folded name, honorifics and suffixes out.

    ``Hon. Thomas H. Kean Jr`` and ``Thomas H Kean`` have the same key, so the
    member is matched — or reviewed — once for both. A quoted nickname stays
    in: it is part of how that filer writes their name.
    """
    parts = filed_parts(first_name, last_name)
    nicknames = [word for words in parts.nicknames for word in words]
    return " ".join([*parts.given, *nicknames, *parts.surname]) or " ".join(fold(last_name))


# --- trigrams -----------------------------------------------------------------


def trigrams(text: str) -> frozenset[str]:
    """``pg_trgm``'s ``show_trgm()``: each word lower-cased, padded with two
    spaces before and one after, cut into threes."""
    found: set[str] = set()
    for word in re.findall(r"[a-z0-9]+", text.casefold()):
        padded = f"  {word} "
        found.update(padded[i : i + 3] for i in range(len(padded) - 2))
    return frozenset(found)


def _similar(a: frozenset[str], b: frozenset[str]) -> float:
    if not a or not b:
        return 0.0
    shared = len(a & b)
    return shared / (len(a) + len(b) - shared)


def similarity(a: str, b: str) -> float:
    """``pg_trgm``'s ``similarity(a, b)``: shared trigrams over all of them."""
    return _similar(trigrams(a), trigrams(b))


# --- the registry ---------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Term:
    chamber: str
    state: str
    start: date
    end: date
    district: int | None = None
    party: str | None = None


@dataclass(frozen=True)
class Member:
    """A member as the registry has them: ``congress_member`` and its terms."""

    id: int
    bioguide_id: str
    first_name: str
    last_name: str
    middle_name: str | None = None
    nickname: str | None = None
    suffix: str | None = None
    full_name: str | None = None
    """The registry's ``official_full``, when it has one."""
    other_last_names: tuple[str, ...] = ()
    """Names they served under before: ``Bono`` for Mary Bono Mack."""
    terms: tuple[Term, ...] = ()

    @cached_property
    def spellings(self) -> frozenset[str]:
        """Every way of writing them "exact" accepts, case and spacing aside."""
        firsts = [self.first_name] + ([self.nickname] if self.nickname else [])
        written: set[str] = set()
        for last in (self.last_name, *self.other_last_names):
            for first in firsts:
                written.add(_spaced(f"{first} {last}"))
                if self.middle_name:
                    written.add(_spaced(f"{first} {self.middle_name} {last}"))
        if self.full_name:
            written.add(_spaced(_without_suffix(self.full_name)))
        return frozenset(written)

    @cached_property
    def folded(self) -> frozenset[str]:
        """:attr:`spellings`, folded, with a nickname in quotes taken out."""
        out: set[str] = set()
        for spelling in self.spellings:
            words = [w for w in fold(_QUOTED.sub(" ", spelling)) if w not in SUFFIXES]
            out.add(" ".join(words))
        return frozenset(out)

    @cached_property
    def folded_surnames(self) -> frozenset[str]:
        return frozenset(" ".join(fold(last)) for last in (self.last_name, *self.other_last_names))

    @cached_property
    def given_names(self) -> frozenset[str]:
        """First words of the names they may file under first: first, middle,
        nickname, and the official full name's first."""
        words: set[str] = set()
        for name in (self.first_name, self.middle_name, self.nickname):
            if name and (folded := fold(name)):
                words.add(folded[0])
        if self.full_name and (folded := fold(self.full_name)):
            words.add(folded[0])
        # An initial is not a given name to match on: "J." fits every James.
        return frozenset(word for word in words if len(word) > 1)

    @cached_property
    def trigram_sets(self) -> tuple[frozenset[str], ...]:
        return tuple(trigrams(name) for name in self.folded)

    @cached_property
    def chambers(self) -> frozenset[str]:
        return frozenset(term.chamber for term in self.terms)

    def in_office(self, chamber: str, state: str | None, first: date, last: date) -> bool:
        """Whether a term in ``chamber`` (and ``state``, if given) overlaps the
        days from ``first`` to ``last``, allowing :data:`GRACE_DAYS` after it."""
        grace = timedelta(days=GRACE_DAYS)
        return any(
            term.chamber == chamber
            and (state is None or term.state == state)
            and term.start <= last
            and term.end + grace >= first
            for term in self.terms
        )


@dataclass(frozen=True)
class Registry:
    """Every member a filing can be matched to."""

    members: tuple[Member, ...]

    @cached_property
    def by_id(self) -> dict[int, Member]:
        return {member.id: member for member in self.members}

    @cached_property
    def _by_surname_word(self) -> dict[str, tuple[Member, ...]]:
        found: dict[str, list[Member]] = {}
        for member in self.members:
            words = {word for surname in member.folded_surnames for word in surname.split()}
            for word in words:
                found.setdefault(word, []).append(member)
        return {word: tuple(members) for word, members in found.items()}

    @cached_property
    def _by_seat(self) -> dict[tuple[str, str | None], tuple[Member, ...]]:
        found: dict[tuple[str, str | None], dict[int, Member]] = {}
        for member in self.members:
            for term in member.terms:
                for seat in ((term.chamber, term.state), (term.chamber, None)):
                    found.setdefault(seat, {})[member.id] = member
        return {seat: tuple(members.values()) for seat, members in found.items()}

    def sharing_surname(self, surname: Sequence[str]) -> tuple[Member, ...]:
        """Members with a word of ``surname`` in a surname of theirs: the only
        ones an exact or normalised match can be."""
        found = {m.id: m for word in surname for m in self._by_surname_word.get(word, ())}
        return tuple(found.values())

    def seated(self, chamber: str, state: str | None) -> tuple[Member, ...]:
        """Members with a term in ``chamber`` (for ``state``, if given), ever."""
        return self._by_seat.get((chamber, state), ())

    def __len__(self) -> int:
        return len(self.members)


# --- a match ----------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class FiledName:
    """What a chamber's index says of one filer, across their filings."""

    chamber: str
    first_name: str | None
    last_name: str
    state: str | None
    """For the House, the state of its ``StateDst``; for the Senate, none."""
    first_filed: date
    last_filed: date


@dataclass(frozen=True, slots=True)
class Candidate:
    member_id: int
    method: Method
    confidence: float


@dataclass(frozen=True, slots=True)
class Match:
    """What :func:`match` found: the member, or the candidates for a person."""

    candidates: tuple[Candidate, ...] = ()
    accepted: Candidate | None = None

    @property
    def status(self) -> str:
        if self.accepted is not None:
            return "matched"
        return "review" if self.candidates else "unmatched"

    @property
    def confidence(self) -> float | None:
        if self.accepted is not None:
            return self.accepted.confidence
        return self.candidates[0].confidence if self.candidates else None


@dataclass(frozen=True, slots=True)
class _Fit:
    member: Member
    method: Method


def _fit(member: Member, parts: FiledParts, spelled: str, folded: frozenset[str]) -> Method | None:
    if spelled in member.spellings:
        return Method.EXACT
    if folded & member.folded:
        return Method.NORMALISED
    if " ".join(parts.surname) in member.folded_surnames:
        firsts = [*parts.given[:1], *(words[0] for words in parts.nicknames)]
        if any(first in member.given_names for first in firsts):
            return Method.NORMALISED
    return None


def _filed_forms(parts: FiledParts) -> frozenset[str]:
    forms = {" ".join([*parts.given, *parts.surname])}
    forms.update(" ".join([*nickname, *parts.surname]) for nickname in parts.nicknames)
    return frozenset(form for form in forms if form)


def _score(method: Method) -> float:
    return EXACT_SCORE if method is Method.EXACT else NORMALISED_SCORE


def _shared(fits: Sequence[_Fit], factor: float) -> list[Candidate]:
    """Each fit's score times ``factor``, split between them: two members that
    fit as well are each half as likely."""
    return [
        Candidate(fit.member.id, fit.method, round(_score(fit.method) * factor / len(fits), 3))
        for fit in fits
    ]


def match(filed: FiledName, registry: Registry, *, rejected: Iterable[int] = ()) -> Match:
    """The member ``filed`` is, or up to :data:`MAX_CANDIDATES` for a person.

    :param rejected: Members a person has said it is not. Never suggested again,
        and never matched.
    """
    refused = frozenset(rejected)
    parts = filed_parts(filed.first_name, filed.last_name)
    spelled = _spaced(f"{filed.first_name or ''} {filed.last_name}")
    folded = _filed_forms(parts)

    def sitting(member: Member) -> bool:
        return member.in_office(filed.chamber, filed.state, filed.first_filed, filed.last_filed)

    fits: list[_Fit] = []
    elsewhere: list[_Fit] = []
    for member in registry.sharing_surname(parts.surname):
        if member.id in refused:
            continue
        method = _fit(member, parts, spelled, folded)
        if method is not None:
            (fits if sitting(member) else elsewhere).append(_Fit(member, method))

    if fits:
        # Exact beats normalised only when it alone is exact: a second member
        # who fits normalised is still a second member.
        candidates = _shared(fits, 1.0)
        if len(candidates) == 1 and candidates[0].confidence >= AUTO_ACCEPT:
            return Match(candidates=tuple(candidates), accepted=candidates[0])
        return Match(candidates=_best(candidates))

    candidates = _shared(elsewhere, OUT_OF_OFFICE) if elsewhere else []
    filed_sets = [trigrams(form) for form in folded]
    for member in registry.seated(filed.chamber, filed.state):
        if member.id in refused or not sitting(member):
            continue
        best = max(
            (_similar(mine, theirs) for mine in filed_sets for theirs in member.trigram_sets),
            default=0.0,
        )
        if best >= TRIGRAM_FLOOR:
            candidates.append(Candidate(member.id, Method.TRIGRAM, round(best, 3)))
    return Match(candidates=_best(candidates))


def _best(candidates: Sequence[Candidate]) -> tuple[Candidate, ...]:
    ranked = sorted(candidates, key=lambda c: (-c.confidence, c.member_id))
    return tuple(ranked[:MAX_CANDIDATES])


@dataclass(slots=True)
class Tally:
    """How a pass over many names came out."""

    matched: dict[Method, int] = field(default_factory=dict)
    review: int = 0
    unmatched: int = 0

    def add(self, found: Match) -> None:
        if found.accepted is not None:
            self.matched[found.accepted.method] = self.matched.get(found.accepted.method, 0) + 1
        elif found.candidates:
            self.review += 1
        else:
            self.unmatched += 1
