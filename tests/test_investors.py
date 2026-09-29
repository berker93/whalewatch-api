"""The investor list's schema, and the committed list against it.

No database. The committed file is validated here, on every run of the unit
suite, because a broken list is a missing fund on the site — found by a user,
not by a seed that happens to be run before a deploy.

The rejection cases are written as small YAML documents rather than as dicts,
because YAML is where the mistakes are actually made: a slug pasted twice, a
CIK copied onto the wrong entry, a category spelt the way it reads in a
sentence.
"""

from pathlib import Path

import pytest

from app.db.models.filer import FilerCategory, OverlapPolicy
from app.ingestion.investors import (
    DEFAULT_INVESTORS_PATH,
    InvestorListError,
    load_investors,
    parse_investors,
)


def _entry(
    slug: str = "berkshire-hathaway",
    *,
    ciks: str = "[1067983]",
    category: str = "value",
    country: str = "US",
    extra: str = "",
) -> str:
    return f"""
- slug: {slug}
  display_name: Berkshire Hathaway
  manager_name: Warren Buffett
  category: {category}
  country: {country}
  ciks: {ciks}{extra}
"""


# --- the committed list ------------------------------------------------------


def test_the_committed_list_is_valid() -> None:
    entries = load_investors()

    assert 90 <= len(entries) <= 120, "the universe is meant to be about a hundred filers"


def test_the_committed_list_covers_every_category() -> None:
    """An empty category is an empty filter on the site, which reads as a bug."""
    categories = {entry.category for entry in load_investors()}

    assert categories == set(FilerCategory)


def test_the_committed_list_is_at_the_path_the_cli_reads() -> None:
    """The seed defaults to this path, and the image ships it via ``COPY . .``."""
    assert Path(__file__).resolve().parents[1] / "data" / "investors.yaml" == DEFAULT_INVESTORS_PATH


# --- what a valid entry looks like -------------------------------------------


def test_a_minimal_entry_parses_with_notes_optional() -> None:
    (entry,) = parse_investors(_entry())

    assert entry.slug == "berkshire-hathaway"
    assert entry.category is FilerCategory.VALUE
    assert entry.notes is None


def test_ciks_are_padded_to_the_spelling_the_database_stores() -> None:
    (entry,) = parse_investors(_entry(ciks="[1067983, 21]"))

    assert entry.padded_ciks == ("0001067983", "0000000021")


# --- the three the acceptance criteria name ----------------------------------


def test_a_duplicate_slug_is_rejected() -> None:
    text = _entry(ciks="[1]") + _entry(ciks="[2]")

    with pytest.raises(InvestorListError, match="duplicate slug 'berkshire-hathaway'"):
        parse_investors(text)


def test_a_cik_on_two_entries_is_rejected_naming_both() -> None:
    text = _entry("berkshire-hathaway", ciks="[1067983]") + _entry("buffett", ciks="[1067983]")

    with pytest.raises(
        InvestorListError, match="duplicate CIK 1067983 on berkshire-hathaway, buffett"
    ):
        parse_investors(text)


def test_a_cik_repeated_within_one_entry_is_rejected() -> None:
    with pytest.raises(InvestorListError, match="more than once"):
        parse_investors(_entry(ciks="[1067983, 1067983]"))


def test_an_unknown_category_is_rejected_and_located_by_slug() -> None:
    """Located by slug, because "entry 57" in a hundred-entry file is a count."""
    with pytest.raises(InvestorListError, match=r"berkshire-hathaway\.category"):
        parse_investors(_entry(category="event_driven"))


# --- the rest of the schema --------------------------------------------------


@pytest.mark.parametrize(
    "slug",
    [
        "Berkshire-Hathaway",  # uppercase: a second URL for the same page
        "berkshire hathaway",  # a space: percent-encoded in every link
        "berkshire--hathaway",
        "-berkshire",
        "berkshire-",
        "berkshire_hathaway",
        "berkshire.hathaway",
    ],
)
def test_a_slug_that_is_not_url_safe_is_rejected(slug: str) -> None:
    with pytest.raises(InvestorListError, match="slug"):
        parse_investors(_entry(slug=f'"{slug}"'))


@pytest.mark.parametrize("country", ["us", "USA", "U"])
def test_a_country_that_is_not_iso_alpha_2_is_rejected(country: str) -> None:
    with pytest.raises(InvestorListError, match="country"):
        parse_investors(_entry(country=country))


def test_a_bare_no_for_norway_is_rejected_rather_than_read_as_false() -> None:
    """YAML 1.1 reads ``NO`` as a boolean. The field refuses it; it must be quoted."""
    with pytest.raises(InvestorListError, match="country"):
        parse_investors(_entry(country="NO"))

    (entry,) = parse_investors(_entry(country='"NO"'))
    assert entry.country == "NO"


def test_a_misspelt_field_is_rejected_rather_than_ignored() -> None:
    with pytest.raises(InvestorListError, match="manger_name"):
        parse_investors(_entry(extra="\n  manger_name: Warren Buffett"))


@pytest.mark.parametrize("ciks", ["[]", "[0]", "[12345678901]"])
def test_ciks_must_be_present_and_in_edgars_range(ciks: str) -> None:
    with pytest.raises(InvestorListError, match="ciks"):
        parse_investors(_entry(ciks=ciks))


def test_a_blank_display_name_is_rejected() -> None:
    text = _entry().replace("display_name: Berkshire Hathaway", 'display_name: "  "')

    with pytest.raises(InvestorListError, match="display_name"):
        parse_investors(text)


def test_malformed_yaml_is_reported_as_a_list_error() -> None:
    with pytest.raises(InvestorListError, match="not valid YAML"):
        parse_investors("- slug: [unclosed")


def test_a_missing_file_is_reported_as_a_list_error(tmp_path: Path) -> None:
    with pytest.raises(InvestorListError, match="cannot read"):
        load_investors(tmp_path / "absent.yaml")


# --- overlap -----------------------------------------------------------------


def test_overlap_defaults_to_successor() -> None:
    (entry,) = parse_investors(_entry(ciks="[1, 2]"))

    assert entry.overlap is OverlapPolicy.SUCCESSOR


def test_overlap_sum_parses_on_a_multi_cik_entry() -> None:
    (entry,) = parse_investors(_entry(ciks="[1, 2]", extra="\n  overlap: sum"))

    assert entry.overlap is OverlapPolicy.SUM


def test_overlap_sum_on_a_single_cik_is_rejected() -> None:
    """Nothing to sum: almost certainly the flag landed on the wrong entry."""
    with pytest.raises(InvestorListError, match="at least two CIKs"):
        parse_investors(_entry(extra="\n  overlap: sum"))


def test_an_unknown_overlap_policy_is_rejected() -> None:
    with pytest.raises(InvestorListError, match="overlap"):
        parse_investors(_entry(ciks="[1, 2]", extra="\n  overlap: dedupe"))


def test_the_committed_list_lists_the_primary_rokos_filer_last() -> None:
    """The LLP is Rokos's primary filer and older than the US LP, so the list
    breaks "oldest first" on purpose. A tidy-up that sorted it would hand the
    2025Q3 overlap to the US LP's one-off filing."""
    rokos = next(entry for entry in load_investors() if entry.slug == "rokos")

    assert rokos.ciks[-1] == 1666335
