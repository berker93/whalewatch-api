"""Filed names to members of Congress: :mod:`app.ingestion.congress.member_match`
and the dataset reader, :mod:`app.ingestion.congress.legislators`.

Against a cut of the real ``unitedstates/congress-legislators`` dataset
(tests/fixtures/congress_legislators/, terms trimmed to the fields read), with
names as the House Clerk's index really spells them: every filed name here is
one from the 2026 index, unless it says otherwise.
"""

from datetime import date
from pathlib import Path
from typing import Final

import httpx
import pytest

from app.ingestion.congress.legislators import (
    FILES,
    Copy,
    LegislatorsError,
    fetch_dataset,
    read_dataset,
)
from app.ingestion.congress.member_match import (
    AUTO_ACCEPT,
    FiledName,
    Member,
    Method,
    Registry,
    Term,
    match,
    name_key,
    similarity,
)
from app.storage.raw import LocalRawStore

FIXTURES: Final = Path(__file__).parent / "fixtures" / "congress_legislators"
FILED: Final = date(2026, 6, 1)


def _copies() -> list[Copy]:
    return [
        Copy(name=name, body=(FIXTURES / f"{name}.json").read_bytes(), key=f"k/{name}", url="u")
        for name in FILES
    ]


@pytest.fixture(scope="module")
def registry() -> Registry:
    dataset = read_dataset(_copies())
    return Registry(
        tuple(
            Member(
                id=position,
                bioguide_id=person.bioguide_id,
                first_name=person.first_name,
                last_name=person.last_name,
                middle_name=person.middle_name,
                nickname=person.nickname,
                suffix=person.suffix,
                full_name=person.official_full,
                other_last_names=person.other_last_names,
                terms=person.terms,
            )
            for position, person in enumerate(dataset.legislators, start=1)
        )
    )


def _filed(
    first: str | None,
    last: str,
    state: str | None = None,
    *,
    chamber: str = "house",
    on: date = FILED,
    until: date | None = None,
) -> FiledName:
    return FiledName(
        chamber=chamber,
        first_name=first,
        last_name=last,
        state=state,
        first_filed=on,
        last_filed=until or on,
    )


def _bioguide(registry: Registry, member_id: int) -> str:
    return registry.by_id[member_id].bioguide_id


def _matched(registry: Registry, filed: FiledName) -> tuple[str, Method]:
    found = match(filed, registry)
    assert found.accepted is not None, found
    return _bioguide(registry, found.accepted.member_id), found.accepted.method


# --- the dataset -------------------------------------------------------------------


def test_only_people_with_a_term_since_2008_are_kept() -> None:
    dataset = read_dataset(_copies())

    kept = {person.bioguide_id for person in dataset.legislators}
    assert dataset.listed == 14
    assert "B000226" not in kept  # Richard Bassett, last term 1793
    assert {"P000197", "B001228", "S000030"} <= kept
    assert len(kept) == 13


def test_a_person_reads_with_their_latest_term_and_every_name() -> None:
    people = {person.bioguide_id: person for person in read_dataset(_copies()).legislators}

    kean = people["K000398"]
    assert (kean.first_name, kean.middle_name, kean.last_name, kean.suffix) == (
        "Thomas",
        "Howard",
        "Kean",
        "Jr.",
    )
    assert kean.full_name == "Thomas H. Kean, Jr."
    assert (kean.latest.chamber, kean.latest.state, kean.latest.district) == ("house", "NJ", 7)
    assert kean.current

    bono = people["B001228"]
    assert bono.other_last_names == ("Bono",)
    assert not bono.current

    scott = people["S001217"]
    assert (scott.latest.chamber, scott.latest.state, scott.latest.district) == (
        "senate",
        "FL",
        None,
    )


def test_a_person_missing_what_everyone_has_fails_the_read() -> None:
    body = b'[{"id": {"bioguide": "X000001"}, "name": {"first": "A"}, "terms": []}]'

    with pytest.raises(LegislatorsError, match="no first or last name"):
        read_dataset([Copy(name=FILES[0], body=body, key="k", url="u")])


def test_a_file_that_is_not_json_fails_the_read() -> None:
    with pytest.raises(LegislatorsError, match="not JSON"):
        read_dataset([Copy(name=FILES[0], body=b"<html>", key="k", url="u")])


async def test_the_files_are_fetched_and_archived_under_their_checksums(
    tmp_path: Path,
) -> None:
    asked: list[str] = []

    def answer(request: httpx.Request) -> httpx.Response:
        asked.append(str(request.url))
        name = request.url.path.rsplit("/", 1)[-1]
        return httpx.Response(200, content=(FIXTURES / name).read_bytes())

    store = LocalRawStore(tmp_path)
    copies = await fetch_dataset(
        store,
        base_url="https://example.test/cl",
        user_agent="t",
        transport=httpx.MockTransport(answer),
    )

    assert asked == [
        "https://example.test/cl/legislators-current.json",
        "https://example.test/cl/legislators-historical.json",
    ]
    for copy in copies:
        assert copy.key.startswith(f"raw/congress-legislators/{copy.name}/")
        assert await store.get(copy.key) == copy.body


async def test_a_file_that_does_not_answer_200_stops_the_fetch(tmp_path: Path) -> None:
    transport = httpx.MockTransport(lambda _: httpx.Response(404))

    with pytest.raises(LegislatorsError, match="answered 404"):
        await fetch_dataset(
            LocalRawStore(tmp_path), base_url="https://x.test", user_agent="t", transport=transport
        )


# --- folding -----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("first", "last", "key"),
    [
        ("Thomas H.", "Kean", "thomas h kean"),
        ("Neal Patrick MD, Facs", "Dunn", "neal patrick dunn"),
        ("Richard Dean Dr", "McCormick", "richard dean mccormick"),
        ('Charles J. "Chuck"', "Fleischmann", "charles j chuck fleischmann"),
        ("Linda T.", "Sánchez", "linda t sanchez"),
        ("Beto", "O'Rourke", "beto orourke"),  # not from the 2026 index
        ("Mitch", "McConnell, Jr.", "mitch mcconnell"),  # eFD's style, not seen live
        (None, "Boozman", "boozman"),
    ],
)
def test_a_name_folds_to_its_key(first: str | None, last: str, key: str) -> None:
    assert name_key(first, last) == key


def test_trigram_similarity_is_pg_trgms() -> None:
    """Values from Postgres 16: SELECT similarity('rohit khanna', 'ro khanna')."""
    assert similarity("rohit khanna", "ro khanna") == pytest.approx(0.642857, abs=1e-6)
    assert similarity("richard w allen", "rick w allen") == pytest.approx(0.611111, abs=1e-6)
    assert similarity("word", "word") == 1.0
    assert similarity("", "word") == 0.0


# --- the tiers ---------------------------------------------------------------------


def test_an_exact_name_in_office_is_accepted(registry: Registry) -> None:
    assert _matched(registry, _filed("Nancy", "Pelosi", "CA")) == ("P000197", Method.EXACT)
    # The official full name, its suffix aside: the index has its own field.
    assert _matched(registry, _filed("Thomas H.", "Kean", "NJ")) == ("K000398", Method.EXACT)
    # Nickname and surname.
    assert _matched(registry, _filed("Chuck", "Fleischmann", "TN")) == ("F000459", Method.EXACT)
    # The official full name, nickname in quotes and all.
    assert _matched(registry, _filed('Charles J. "Chuck"', "Fleischmann", "TN")) == (
        "F000459",
        Method.EXACT,
    )
    assert _matched(registry, _filed("Debbie", "Wasserman Schultz", "FL")) == (
        "W000797",
        Method.EXACT,
    )


def test_an_exact_match_has_full_confidence(registry: Registry) -> None:
    found = match(_filed("Nancy", "Pelosi", "CA"), registry)

    assert found.status == "matched"
    assert found.confidence == 1.0


@pytest.mark.parametrize(
    ("first", "last", "state", "bioguide"),
    [
        ("Thomas H", "Kean", "NJ", "K000398"),  # no full stop
        ("Thomas H", "Kean, Jr.", "NJ", "K000398"),  # the suffix in the surname, as eFD has it
        ("Linda T.", "Sanchez", "CA", "S001156"),  # no accent
        ("Charles J", "Fleischmann", "TN", "F000459"),
        ("Chuck Dr", "Fleischmann", "TN", "F000459"),  # an honorific, not from the index
    ],
)
def test_a_normalised_name_in_office_is_accepted(
    registry: Registry, first: str, last: str, state: str, bioguide: str
) -> None:
    found = match(_filed(first, last, state), registry)

    assert found.accepted is not None
    assert (_bioguide(registry, found.accepted.member_id), found.accepted.method) == (
        bioguide,
        Method.NORMALISED,
    )
    assert found.accepted.confidence >= AUTO_ACCEPT


def test_a_surname_shared_in_the_state_is_told_apart_by_the_first_name(
    registry: Registry,
) -> None:
    """Linda and Loretta Sanchez, sisters, sat for California together from 2003
    to 2017. Not a 2026 filing: dated to when both were in office."""
    in_2012 = date(2012, 5, 1)

    assert _matched(registry, _filed("Linda T.", "Sanchez", "CA", on=in_2012))[0] == "S001156"
    assert _matched(registry, _filed("Loretta", "Sanchez", "CA", on=in_2012))[0] == "S000030"


def test_a_name_with_only_a_surname_two_members_share_is_not_accepted(
    registry: Registry,
) -> None:
    """Two senators Scott. Not a real filing: eFD always gives a first name."""
    found = match(_filed(None, "Scott", chamber="senate"), registry)

    assert found.accepted is None


def test_a_former_name_matches(registry: Registry) -> None:
    """Mary Bono Mack served as Mary Bono until 2007. Not from the index."""
    assert _matched(registry, _filed("Mary", "Bono", "CA", on=date(2009, 3, 1)))[0] == "B001228"


def test_a_trigram_match_is_never_accepted_however_close(registry: Registry) -> None:
    for first, last, state, bioguide in (
        ("Rohit", "Khanna", "CA", "K000389"),
        ("Richard W.", "Allen", "GA", "A000372"),
        ("Daniel", "Crenshaw", "TX", "C001120"),
    ):
        found = match(_filed(first, last, state), registry)

        assert found.accepted is None
        assert found.status == "review"
        best = found.candidates[0]
        assert (_bioguide(registry, best.member_id), best.method) == (bioguide, Method.TRIGRAM)


def test_a_review_carries_the_best_candidates_score(registry: Registry) -> None:
    found = match(_filed("Rohit", "Khanna", "CA"), registry)

    assert found.confidence == 0.643


def test_a_name_that_fits_someone_not_in_that_seat_goes_to_review(registry: Registry) -> None:
    """Nancy Pelosi has never sat for New York: a typo in StateDst, say."""
    found = match(_filed("Nancy", "Pelosi", "NY"), registry)

    assert found.accepted is None
    assert [(c.method, c.confidence) for c in found.candidates] == [(Method.EXACT, 0.6)]


def test_a_former_member_is_in_office_for_a_year_after_their_term(registry: Registry) -> None:
    """Mary Bono Mack's last term ended 3 January 2013."""
    assert _matched(registry, _filed("Mary", "Bono Mack", "CA", on=date(2013, 9, 1)))
    late = match(_filed("Mary", "Bono Mack", "CA", on=date(2014, 6, 1)), registry)
    assert late.accepted is None
    assert late.candidates[0].confidence == 0.6


def test_a_senate_filing_is_matched_among_senators(registry: Registry) -> None:
    assert _matched(registry, _filed("John", "Boozman", chamber="senate")) == (
        "B001236",
        Method.EXACT,
    )
    assert _matched(registry, _filed("Tim", "Scott", chamber="senate"))[0] == "S001184"


def test_an_unknown_name_has_no_candidates(registry: Registry) -> None:
    found = match(_filed("Zebulon", "Quartermaine", "WY"), registry)

    assert found.status == "unmatched"
    assert found.candidates == ()
    assert found.confidence is None


def test_a_rejected_member_is_never_suggested_again(registry: Registry) -> None:
    khanna = next(m.id for m in registry.members if m.bioguide_id == "K000389")

    found = match(_filed("Rohit", "Khanna", "CA"), registry, rejected=[khanna])

    assert khanna not in {candidate.member_id for candidate in found.candidates}


def test_two_members_who_fit_split_the_confidence() -> None:
    """Not real people: the dataset has no two members in one seat with one name."""
    term = Term(chamber="house", state="TX", start=date(2025, 1, 3), end=date(2027, 1, 3))
    twins = Registry(
        (
            Member(
                id=1, bioguide_id="A000001", first_name="John", last_name="Smith", terms=(term,)
            ),
            Member(
                id=2,
                bioguide_id="A000002",
                first_name="John",
                middle_name="Q.",
                last_name="Smith",
                terms=(term,),
            ),
        )
    )

    found = match(_filed("John", "Smith", "TX"), twins)

    assert found.accepted is None
    # "John Smith" is how either could write themselves.
    assert sorted((c.member_id, c.method, c.confidence) for c in found.candidates) == [
        (1, Method.EXACT, 0.5),
        (2, Method.EXACT, 0.5),
    ]
    # With the middle initial, still both: John Smith may have one he does not use.
    with_initial = match(_filed("John Q", "Smith", "TX"), twins)
    assert with_initial.accepted is None
    assert sorted((c.member_id, c.method, c.confidence) for c in with_initial.candidates) == [
        (1, Method.NORMALISED, 0.475),
        (2, Method.NORMALISED, 0.475),
    ]
