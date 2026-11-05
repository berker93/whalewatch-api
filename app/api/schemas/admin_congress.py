"""The PTR review queue's requests and answers (:mod:`app.api.routers.admin_congress`)."""

from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, Field

from app.api.schemas.types import Money, Percent, Score

_URL = "https://disclosures-clerk.house.gov/public_disc/ptr-pdfs/2026/9116212.pdf"

Owner = Literal["SP", "JT", "DC"]
TransactionKind = Literal["purchase", "sale", "partial_sale", "exchange"]


class AdminPtrFiling(BaseModel):
    """The filing a line is from."""

    id: int = Field(description="The filing's id.", examples=[812])
    chamber: Literal["house", "senate"] = Field(description="Which chamber.", examples=["house"])
    doc_id: str = Field(description="The chamber's id for the document.", examples=["9116212"])
    member: str = Field(
        description="The filer's name as the index lists it.", examples=["Charles J Fleischmann"]
    )
    office: str | None = Field(description="State and district.", examples=["TN03"])
    filing_date: date = Field(description="When the chamber received it.", examples=["2026-07-08"])
    document_url: str | None = Field(description="The original PDF.", examples=[_URL])
    extraction: Literal["text", "ocr", "none"] | None = Field(
        description="How its lines were read: from the text layer, by OCR, or not at all.",
        examples=["ocr"],
    )
    pages: int | None = Field(description="Pages in the PDF.", examples=[1])
    parse_error: str | None = Field(
        description="Why no line could be read from it, on a placeholder's filing.",
        examples=[None],
    )


class AdminPtrRow(BaseModel):
    """One line of a PTR, as read."""

    id: int = Field(description="The line's id: what review takes.", examples=[40213])
    filing: AdminPtrFiling = Field(description="The filing it is from.")
    line: int = Field(description="1-based, in the filing's order.", examples=[1])
    page: int | None = Field(description="The page it starts on.", examples=[1])
    parse_status: Literal["parsed", "needs_review", "placeholder", "rejected"] = Field(
        description="Where it stands.", examples=["needs_review"]
    )
    extraction: Literal["text", "ocr", "none"] = Field(
        description="How it was read.", examples=["ocr"]
    )
    owner: Owner | None = Field(
        description="`SP` spouse, `JT` joint, `DC` dependent child; null for the member.",
        examples=["JT"],
    )
    asset_name: str | None = Field(description="The asset.", examples=["Tractor Supply Co TSCO"])
    ticker: str | None = Field(
        description="As the filer gave it; never on a scan.", examples=[None]
    )
    asset_type: str | None = Field(description="The House's two-letter code.", examples=[None])
    transaction_type: TransactionKind | None = Field(description="What kind.", examples=["sale"])
    transaction_date: date | None = Field(description="The trade's day.", examples=["2026-06-09"])
    notification_date: date | None = Field(
        description="When the member learned of it.", examples=["2026-07-01"]
    )
    amount_low: int | None = Field(description="The band's floor, in dollars.", examples=[1001])
    amount_high: int | None = Field(
        description="The band's ceiling; null on an open band.", examples=[15000]
    )
    amount_exact: Money | None = Field(
        description="A figure the filer wrote instead of a band; never with one.",
        examples=[None],
    )
    cap_gains_over_200: bool | None = Field(
        description="The form's capital-gains box, where it has one.", examples=[None]
    )
    filing_status: str | None = Field(description="`new` or `amended`.", examples=[None])
    subholding_of: str | None = Field(description="The account it sits in.", examples=[None])
    description: str | None = Field(description="The filer's description.", examples=[None])
    comment: str | None = Field(description="The filer's comment.", examples=[None])
    confidence: Score | None = Field(
        description="OCR's confidence, 0 to 1; null for text.", examples=["0.775"]
    )
    problems: list[str] = Field(
        description="Why it is in review, or why its filing has a placeholder.",
        examples=[["OCR confidence 0.78 under 0.80"]],
    )
    raw_text: str = Field(
        description="What was read, for comparing with the original.",
        examples=["JT | Tractor Supply Co TSCO | 06/09/26 | 07/01/26 | type 0.000/0.071/0.000"],
    )
    reviewed_at: datetime | None = Field(description="When a person settled it.", examples=[None])
    review_note: str | None = Field(description="What they said.", examples=[None])


class AdminPtrPage(BaseModel):
    """One page of the queue."""

    items: list[AdminPtrRow] = Field(description="The lines on this page.")
    total: int = Field(description="How many lines match, on every page.", examples=[312])
    limit: int = Field(description="The page size asked for.", examples=[50])
    offset: int = Field(description="How many matches came before this page.", examples=[0])


class AdminPtrCorrections(BaseModel):
    """Fields to set before accepting. Leave a field out to keep it; send null to clear it."""

    owner: Owner | None = Field(
        default=None, description="`SP`, `JT`, `DC`; null for the member.", examples=["JT"]
    )
    asset_name: str | None = Field(
        default=None,
        min_length=1,
        max_length=500,
        description="The asset, as the original has it.",
        examples=["Tractor Supply Co"],
    )
    ticker: str | None = Field(
        default=None,
        max_length=12,
        pattern=r"^[A-Z0-9.\-]+$",
        description="Upper case.",
        examples=["TSCO"],
    )
    asset_type: str | None = Field(
        default=None,
        pattern=r"^[A-Z]{2}$",
        description="The House's two-letter code.",
        examples=["ST"],
    )
    transaction_type: TransactionKind | None = Field(
        default=None, description="What kind.", examples=["sale"]
    )
    transaction_date: date | None = Field(
        default=None, description="The trade's day.", examples=["2026-06-09"]
    )
    notification_date: date | None = Field(
        default=None, description="When it was notified.", examples=["2026-07-01"]
    )
    amount_low: int | None = Field(
        default=None, ge=1, description="The band's floor.", examples=[1001]
    )
    amount_high: int | None = Field(
        default=None,
        ge=1,
        description="The band's ceiling; null on an open band.",
        examples=[15000],
    )
    amount_exact: str | None = Field(
        default=None,
        pattern=r"^[0-9]{1,12}(\.[0-9]{1,2})?$",
        description="A figure instead of a band, as the filer wrote it; clear the band with it.",
        examples=["710.52"],
    )


class AdminPtrReview(BaseModel):
    """A person's call on one line in review."""

    decision: Literal["accept", "reject"] = Field(
        description="`accept`: a trade, as corrected. `reject`: not a trade at all.",
        examples=["accept"],
    )
    corrections: AdminPtrCorrections | None = Field(
        default=None, description="With `accept` only: what the reader got wrong."
    )
    note: str | None = Field(
        default=None, max_length=500, description="Why, for the next reviewer.", examples=[None]
    )


class AdminPtrCoverage(BaseModel):
    """How much of a chamber's PTRs has been read."""

    chamber: Literal["house", "senate"] = Field(description="Which chamber.", examples=["house"])
    filings_seen: int = Field(
        description="Filings a run has tried: read, given a placeholder, or missing a PDF.",
        examples=[5530],
    )
    filings_read: int = Field(
        description="With a line parsed: what a feed of trades shows.", examples=[4812]
    )
    review_only: int = Field(
        description="With lines read and none parsed yet: all in the review queue.",
        examples=[38],
    )
    by_text: int = Field(
        description="Lines read from the text layer, parsed or in review.", examples=[4790]
    )
    by_ocr: int = Field(description="Lines read by OCR, parsed or in review.", examples=[60])
    by_html: int = Field(
        description="Lines read from a Senate report's table, parsed or in review.",
        examples=[0],
    )
    placeholders: int = Field(description="With a placeholder: no line read.", examples=[702])
    missing: int = Field(description="Whose PDF the chamber does not have up yet.", examples=[16])
    rows_parsed: int = Field(description="Lines read in full, or accepted.", examples=[61204])
    rows_needs_review: int = Field(description="Lines in the review queue.", examples=[312])
    coverage_pct: Percent | None = Field(
        description="Filings read over filings seen, in percent, to a tenth.", examples=["87.0"]
    )
    members_matched: int = Field(
        description="Filings whose filer is matched to a member of Congress.", examples=[5390]
    )
    members_in_review: int = Field(
        description="Filings whose filer has candidates waiting in the member review queue.",
        examples=[96],
    )
    members_unmatched: int = Field(
        description="Filings whose filer is no known member: shown as filed, flagged.",
        examples=[4],
    )
    members_unlinked: int = Field(
        description="Filings not linked to a filer yet: before the registry was seeded.",
        examples=[0],
    )


# --- the member review queue ------------------------------------------------------

NameMethod = Literal["exact", "normalised", "manual"]
CandidateMethod = Literal["exact", "normalised", "trigram"]


class AdminMember(BaseModel):
    """A member of Congress, from the registry."""

    bioguide_id: str = Field(description="The Bioguide's id.", examples=["K000389"])
    full_name: str = Field(description="As the registry has it.", examples=["Ro Khanna"])
    chamber: Literal["house", "senate"] = Field(
        description="Their latest term's.", examples=["house"]
    )
    state: str = Field(description="Their latest term's.", examples=["CA"])
    district: int | None = Field(
        description="Their latest term's, in the House; 0 at large.", examples=[17]
    )
    party: str | None = Field(description="Their latest term's.", examples=["Democrat"])
    current: bool = Field(description="In office, as the registry last said.", examples=[True])


class AdminMemberCandidate(BaseModel):
    """One member a filed name might be."""

    id: int = Field(description="What `accept` takes.", examples=[311])
    member: AdminMember = Field(description="Who it might be.")
    method: CandidateMethod = Field(
        description=(
            "`trigram`: a similar name, never accepted without a person. `exact` or "
            "`normalised`: the name fits, but so does another member's, or the member "
            "was not in that seat when it was filed."
        ),
        examples=["trigram"],
    )
    confidence: Score = Field(description="0 to 1.", examples=["0.643"])


class AdminFiledName(BaseModel):
    """A filer, as a chamber's index names them, and who that is."""

    id: int = Field(description="The filed name's id: what review takes.", examples=[57])
    chamber: Literal["house", "senate"] = Field(description="Which chamber.", examples=["house"])
    state: str | None = Field(
        description="From a House filing's state and district; null for the Senate.",
        examples=["CA"],
    )
    first_name: str | None = Field(description="As filed.", examples=["Rohit"])
    last_name: str = Field(description="As filed.", examples=["Khanna"])
    name_key: str = Field(
        description="The name folded: what filings are grouped by.", examples=["rohit khanna"]
    )
    status: Literal["matched", "review", "unmatched"] = Field(
        description=(
            "`matched`: a member. `review`: candidates, none accepted. `unmatched`: no "
            "candidate, or all rejected; its filings show the name as filed, flagged."
        ),
        examples=["review"],
    )
    method: NameMethod | None = Field(
        description="How it was matched; `manual` is a person's, which no run changes.",
        examples=[None],
    )
    confidence: Score | None = Field(
        description="The match's; in review, the best candidate's.", examples=["0.643"]
    )
    member: AdminMember | None = Field(description="Who it is, when matched.")
    filings: int = Field(description="Filings under the name.", examples=[14])
    first_filed: date | None = Field(description="The first of them.", examples=["2026-01-12"])
    last_filed: date | None = Field(description="The latest.", examples=["2026-10-30"])
    document_url: str | None = Field(description="The latest one's original.", examples=[_URL])
    candidates: list[AdminMemberCandidate] = Field(
        description="Pending, best first. Empty unless in review."
    )
    reviewed_at: datetime | None = Field(description="When a person last decided.", examples=[None])
    review_note: str | None = Field(description="What they said.", examples=[None])


class AdminFiledNamePage(BaseModel):
    """One page of the member review queue."""

    items: list[AdminFiledName] = Field(description="The names on this page.")
    total: int = Field(description="How many names match, on every page.", examples=[10])
    limit: int = Field(description="The page size asked for.", examples=[50])
    offset: int = Field(description="How many matches came before this page.", examples=[0])


class AdminFiledNameDecision(BaseModel):
    """A person's call on one filed name."""

    decision: Literal["accept", "assign", "reject"] = Field(
        description=(
            "`accept`: the candidate `candidate_id` is who it is. `assign`: the member "
            "`bioguide_id` is, candidate or not; also corrects any match. `reject`: none "
            "of the candidates, which are never suggested for it again."
        ),
        examples=["accept"],
    )
    candidate_id: int | None = Field(
        default=None, ge=1, description="With `accept` only.", examples=[311]
    )
    bioguide_id: str | None = Field(
        default=None,
        pattern=r"^[A-Z][0-9]{6}$",
        description="With `assign` only.",
        examples=[None],
    )
    note: str | None = Field(
        default=None, max_length=500, description="Why, for the next reviewer.", examples=[None]
    )
