"""What a PTR line traded, in our terms: an asset class, and how to look it up.

A disclosure names its asset in free text: ``Alphabet Inc. - Class A Common
Stock (GOOGL) [ST]`` on the House's e-filed form, ``Taylor TX ISD BE/R/`` off a
scan, ``Microsoft Corp`` as a Senate "Corporate Bond". This module reads that
text, and nothing else; :mod:`app.ingestion.congress.asset_resolution` looks
what it finds up in ``security``.

The class
---------
:func:`classify` says what kind of asset a line is (:class:`AssetClass`): by
the House's two-letter code where the line has one (a Senate row's words are
mapped to the same codes), else by words in the name, for a scan's lines and
the oldest e-filed form, which have no code. Some codes are decided only by the
lookup: a REIT (``RE``) that resolves to a security is a listed stock, and one
that does not is real estate (:attr:`Classified.otherwise`).

Only ``equity`` is a stock view's. A ``stock_option`` is looked up too, for its
underlying (``security_id`` says which stock), and left out of them.

The ticker
----------
The House's reader keeps a ticker only where the form put it in parentheses at
the end of the name (:func:`~app.ingestion.congress.ptr.split_asset`); a
Senate row has its own column. :func:`extract_ticker` finds the rest: a
parenthesis inside a scan's name (``Nvidia Corporation (NVDA)``), a labelled one
(``(Ticker: CCLFX)``), and a bare upper-case word ending a mixed-case name off
a scan (``Tractor Supply Co TSCO``), which counts only if the name agrees.

The name
--------
:func:`core_name` strips what says which share of an issuer rather than which
issuer — "Class A Common Stock", "Sponsored ADR", "-CL A" — and the legal form,
and spells the usual abbreviations one way, on both sides of a comparison:
the filer's ``Alphabet Inc. - Class A Common Stock`` and OpenFIGI's
``ALPHABET INC-CL A`` are both ``alphabet``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from app.core.tickers import normalise_ticker
from app.ingestion.congress.member_match import fold, similarity


class AssetClass(StrEnum):
    """A line's ``asset_class``. Text with a ``CHECK``, by the rule in
    :mod:`app.db.models.enums`."""

    EQUITY = "equity"
    """A listed stock, ADR or ETF: what a stock view shows."""
    STOCK_OPTION = "stock_option"
    """An option on a listed stock. ``security_id`` is the underlying's."""
    MUNICIPAL_BOND = "municipal_bond"
    TREASURY = "treasury"
    AGENCY_BOND = "agency_bond"
    """Fannie Mae, Freddie Mac, the Home Loan Banks: the House's "agency debt"."""
    CORPORATE_BOND = "corporate_bond"
    """A note or bond of a company, a structured note, a debenture."""
    MUTUAL_FUND = "mutual_fund"
    CRYPTO = "crypto"
    REAL_ESTATE = "real_estate"
    """Property, a farm, a non-traded REIT, a Delaware statutory trust."""
    PRIVATE = "private"
    """Not publicly traded: a private company's stock, an ownership interest,
    a hedge or private equity fund."""
    OTHER = "other"
    """Cash, annuities, accounts, commodities, RSUs, and what the filer called
    "other" without a ticker."""
    UNKNOWN = "unknown"
    """No code, nothing in the name to say, and no security found: a scan's
    line that may well be a stock."""


#: The classes a stock view shows.
STOCK_VIEWS: Final = (AssetClass.EQUITY,)

#: The classes a security is expected for: what coverage counts. ``unknown``
#: counts against it, since it is usually a stock no lookup found.
RESOLVABLE: Final = (AssetClass.EQUITY, AssetClass.STOCK_OPTION, AssetClass.UNKNOWN)

#: The classes that may have a ``security_id``.
WITH_SECURITY: Final = (AssetClass.EQUITY, AssetClass.STOCK_OPTION)


class Method(StrEnum):
    """How a line's security was found: its ``resolution_method``. Text with a
    ``CHECK``, by the rule in :mod:`app.db.models.enums`."""

    TICKER = "ticker"
    """The ticker the line gives, or one in its name: one the security traded
    under on the day, its current one, or its CUSIP."""
    FORMER_TICKER = "former_ticker"
    """A ticker the security traded under at another time: a filer writing
    ``FB`` after the rename, or before our history of it starts."""
    NAME = "name"
    """Its issuer's name, by trigram (``pg_trgm``'s similarity)."""
    MANUAL = "manual"
    """A person's override (``congress_asset_override``). Also on a line an
    override made non-equity, which has no security."""


@dataclass(frozen=True, slots=True)
class Classified:
    """What :func:`classify` decided, before any lookup."""

    asset_class: AssetClass
    """The class if a security is found, or if none is looked for."""
    otherwise: AssetClass
    """The class if one is looked for and not found."""
    lookup: bool
    """Whether to look for a security at all."""
    by_name: bool = True
    """Whether the name alone may find it, without a ticker. Not for a line
    that names its issuer but is not its common stock (a preferred, a warrant),
    nor for "other": the name would find the common."""


def _fixed(asset_class: AssetClass) -> Classified:
    return Classified(asset_class, asset_class, lookup=False)


_STOCK: Final = Classified(AssetClass.EQUITY, AssetClass.EQUITY, lookup=True)
_OPTION: Final = Classified(AssetClass.STOCK_OPTION, AssetClass.STOCK_OPTION, lookup=True)

#: The House's codes (the form's list of asset types), and the Senate's words
#: mapped to them, by what they are here. ``GS``, ``CS`` and ``OT`` are
#: decided by the name too; a code not here is ``other``.
CODES: Final[dict[str, Classified]] = {
    "ST": _STOCK,
    "EF": _STOCK,  # exchange-traded funds: listed, and held in 13Fs
    "ET": Classified(AssetClass.EQUITY, AssetClass.OTHER, lookup=True),  # exchange-traded notes
    "RE": Classified(AssetClass.EQUITY, AssetClass.REAL_ESTATE, lookup=True),  # REITs
    "OP": _OPTION,
    "MF": _fixed(AssetClass.MUTUAL_FUND),
    "CT": _fixed(AssetClass.CRYPTO),
    "RP": _fixed(AssetClass.REAL_ESTATE),
    "FA": _fixed(AssetClass.REAL_ESTATE),
    "DS": _fixed(AssetClass.REAL_ESTATE),
    "PS": _fixed(AssetClass.PRIVATE),
    "OI": _fixed(AssetClass.PRIVATE),
    "OL": _fixed(AssetClass.PRIVATE),
    "HE": _fixed(AssetClass.PRIVATE),
    "HN": _fixed(AssetClass.PRIVATE),
    "IC": _fixed(AssetClass.PRIVATE),
}

# --- words in a name -----------------------------------------------------------------

_CRYPTO: Final = re.compile(
    r"\b(BITCOIN|ETHEREUM|ETHER|LITECOIN|DOGECOIN|SOLANA|CARDANO|RIPPLE|USDC|USDT|TETHER|"
    r"CRYPTO(CURRENCY)?)\b",
    re.IGNORECASE,
)
_OPTION_WORDS: Final = re.compile(
    r"\b(CALLS?|PUTS?)\s+(OPTIONS?|@|AT\b)|\bOPTIONS?\b", re.IGNORECASE
)
_TREASURY: Final = re.compile(
    r"\b(U\.?\s?S\.?|UNITED\s+STATES)\s+(TREAS\w*|TSY)\b|\bT[-\s]?BILLS?\b|"
    r"\bTREASURY\s+(BILLS?|NOTES?|BONDS?|STRIPS?|INFLATION|FRN)\b|\bUST\b",
    re.IGNORECASE,
)
_AGENCY: Final = re.compile(
    r"\b(FNMA|FHLMC|FHLB|GNMA|FFCB|FANNIE\s+MAE|FREDDIE\s+MAC|GINNIE\s+MAE|"
    r"FEDERAL\s+(HOME\s+LOAN|NATIONAL\s+MORTGAGE|FARM\s+CREDIT))",
    re.IGNORECASE,
)
_DEPOSIT: Final = re.compile(
    r"\b(CDS?|CERTIFICATES?\s+OF\s+DEPOSIT|MONEY\s+MARKET|SAVINGS|CHECKING)\b", re.IGNORECASE
)
#: What only a municipal bond's name has. A scan's ``BE/R/`` is "book entry,
#: registered", printed on every one of a broker's muni lines.
_MUNICIPAL: Final = re.compile(
    r"\bMUNI(CIO?PAL)?\b|BE/R/?|\bI\.?S\.?D\b|\bINDE?PT?\b|\bSCH(OOL)?\s+DIST|"
    r"\bG\.?O\.?\s+(BONDS?|UTX|LTD|REF|UNLTD)\b|\bREV(ENUE)?\s+(BONDS?|BDS|REF)\b|\bRV\b|\bBDS\b|"
    r"\bAUTH(ORITY)?\b.*\b(REV|BONDS?)\b|\bCNTY\b|\bWTR\b|\bSWR\b|\bUTIL\b|\bTRANSITIONAL\b|"
    r"\bREVS?\b|\bAGY\b|\bHSG\b|\bSALES\s+(AND\s+|USE\s+)?TAX\b|\bST\s+(GO|PUB)\b|\bGO\s*$",
    re.IGNORECASE,
)
#: A coupon, a maturity, or the words a bond's name has.
_BOND: Final = re.compile(
    r"\d+(\.\d+)?\s*%|\b(DUE|MAT(URES?|URITY)?)\b\s*\d|\bNOTES?\b\s+(CALL|\d)|\bMAKE\s+WHOLE\b|"
    r"\bDEBENTURES?\b|\bBONDS?\b|\bMTN\b|\bCALLABLE\b|\bZERO\s+CPN\b",
    re.IGNORECASE,
)
_PRIVATE: Final = re.compile(
    r"\b(L\.?P\.?|LLC|L\.?L\.?C\.?|FUND\s+[IVX]+|PARTNERS\s+[IVX]+|VENTURES|CAPITAL\s+PARTNERS)\b",
    re.IGNORECASE,
)
_ETF: Final = re.compile(r"\b(ETF|ETN|ISHARES|SPDR|PROSHARES|INVESCO\s+QQQ)\b", re.IGNORECASE)
#: A fund that is not an ETF. ``ADM``, ``INSTL`` and ``INV`` end the names
#: brokers print for a mutual fund's share classes.
_MUTUAL_FUND: Final = re.compile(
    r"\bFUNDS?\b|\bFD\b|\bADM(IRAL)?\b|\bINSTL\b|\bINSTITUTIONAL\b|\bINVESTOR\s+SHARES\b|"
    r"\bPORTF(OLIO)?\b|\bINDEX\b|\bTRUST\s+SHARES\b|\bTAX[-\s]?(EXEMPT|ADVGT|ADVANTAGED|FREE)\b|"
    r"\bFIXED\s+INC(OME)?\b|\bULTRA\s+SHO?RT\b|\bHIGH\s+(INCOME|YIELD)\b",
    re.IGNORECASE,
)
#: A family's or a person's trust: not a security at all.
_TRUST: Final = re.compile(
    r"\bDESC(ENDANTS?)?\b.*\bTRUST\b|\bFAMILY\s+TRUST\b|\bEXEMPT\s+TRUST\b|\bIRREVOCABLE\b|"
    r"\bREVOCABLE\b",
    re.IGNORECASE,
)
#: Not the issuer's common stock, though it names the issuer: the name would
#: find the common, so these are looked up by ticker only.
_NOT_COMMON: Final = re.compile(
    r"\b(PREFERRED|PFD|PREF|WARRANTS?|WTS?|RIGHTS|NOTES?|DEBENTURES?|CONVERTIBLE|CONV|"
    r"BONDS?|SENIOR|SUBORDINATED|SR)\b|\d\s*%|(?<!AMERICAN )\bDEPOSITA?RY\s+SHARES\b",
    re.IGNORECASE,
)
#: A mutual fund's ticker: five letters ending in X.
_FUND_TICKER: Final = re.compile(r"^[A-Z]{4}X$")


def classify(
    asset_type: str | None,
    asset_name: str | None,
    ticker: str | None = None,
    description: str | None = None,
) -> Classified:
    """What a line is, from its code, its name and its ticker; see the module docstring."""
    name = asset_name or ""
    code = (asset_type or "").upper() or None
    if ticker and _FUND_TICKER.match(ticker) and code in (None, "ST", "OT", "MF"):
        return _fixed(AssetClass.MUTUAL_FUND)
    if code == "GS":
        return _fixed(_government(name) or AssetClass.MUNICIPAL_BOND)
    if code in ("CS", "AB"):
        if _AGENCY.search(name):
            return _fixed(AssetClass.AGENCY_BOND)
        return _fixed(AssetClass.CORPORATE_BOND if code == "CS" else AssetClass.OTHER)
    if code == "OT":
        return _other(name, ticker)
    if code is not None:
        found = CODES.get(code, _fixed(AssetClass.OTHER))
        if found.lookup and _NOT_COMMON.search(name):
            return Classified(found.asset_class, found.otherwise, lookup=True, by_name=False)
        return found
    return _by_words(name, ticker, description)


def _government(name: str) -> AssetClass | None:
    if _TREASURY.search(name):
        return AssetClass.TREASURY
    if _AGENCY.search(name):
        return AssetClass.AGENCY_BOND
    if _DEPOSIT.search(name):
        return AssetClass.OTHER
    return None


def _other(name: str, ticker: str | None) -> Classified:
    """``OT``: "other securities". An ETF or a newly listed stock often enough,
    when the filer gave a ticker; a fund or a partnership interest when not."""
    if ticker is not None:
        return Classified(AssetClass.EQUITY, AssetClass.OTHER, lookup=True, by_name=False)
    found = _by_words(name, None, None)
    return _fixed(AssetClass.OTHER if found.lookup else found.asset_class)


def _by_words(name: str, ticker: str | None, description: str | None) -> Classified:
    """A line with no code: a scan's, or the oldest e-filed form's."""
    if _CRYPTO.search(name):
        return _fixed(AssetClass.CRYPTO)
    if _TRUST.search(name):
        return _fixed(AssetClass.OTHER)
    if _OPTION_WORDS.search(name) or (description and _OPTION_WORDS.search(description)):
        return _OPTION
    if found := _government(name):
        return _fixed(found)
    if _MUNICIPAL.search(name):
        return _fixed(AssetClass.MUNICIPAL_BOND)
    if _BOND.search(name):
        return _fixed(AssetClass.CORPORATE_BOND)
    if _ETF.search(name):
        return Classified(AssetClass.EQUITY, AssetClass.UNKNOWN, lookup=True)
    if _PRIVATE.search(name):
        return _fixed(AssetClass.PRIVATE)
    if _MUTUAL_FUND.search(name):
        return _fixed(AssetClass.MUTUAL_FUND)
    if _NOT_COMMON.search(name):
        return Classified(AssetClass.EQUITY, AssetClass.UNKNOWN, lookup=True, by_name=False)
    return Classified(AssetClass.EQUITY, AssetClass.UNKNOWN, lookup=True)


# --- the ticker ----------------------------------------------------------------------

#: ``(NVDA)``, ``(Ticker: CCLFX)``, ``(NYSE: KO)``; a ticker printed upper case.
_PARENTHESISED: Final = re.compile(
    r"\(\s*(?:(?i:ticker|symbol|nyse\s*arca|nysearca|nyse|nasdaq|amex|otc)\s*:?\s*)?"
    r"([A-Z][A-Z0-9]{0,5}(?:[./-][A-Z])?)\s*\)"
)
#: The last word of a scan's mixed-case name, when it is a ticker's shape.
_TRAILING: Final = re.compile(r"\s([A-Z]{2,5})\s*$")
#: Upper-case last words that are not tickers.
_NOT_TICKERS: Final = frozenset(
    {
        *("INC", "CORP", "CO", "LTD", "PLC", "LLC", "LP", "NV", "SA", "SE", "AG", "ADR", "ADS"),
        *("ETF", "ETN", "REIT", "COM", "CL", "CLA", "CLB", "ORD", "SHS", "USD", "II", "III", "IV"),
        *("SP", "SPON", "NEW", "FD", "TR", "GRP", "HLDG", "HLDGS", "INTL", "BE", "GO", "RV"),
    }
)


@dataclass(frozen=True, slots=True)
class Ticker:
    """A ticker to look the line up by."""

    ticker: str
    """As stored: :func:`~app.core.tickers.normalise_ticker`."""
    hinted: bool = False
    """Read off the end of the name rather than given: only good if the name agrees."""


def extract_ticker(ticker: str | None, asset_name: str | None) -> Ticker | None:
    """The line's ticker: the one it was read with, else one in its name."""
    if ticker:
        return Ticker(normalise_ticker(ticker))
    if not asset_name:
        return None
    if match := _PARENTHESISED.search(asset_name):
        return Ticker(normalise_ticker(match[1]))
    match = _TRAILING.search(asset_name)
    rest = asset_name[: match.start()] if match else ""
    if match and match[1] not in _NOT_TICKERS and any(c.islower() for c in rest):
        return Ticker(normalise_ticker(match[1]), hinted=True)
    return None


# --- the name ------------------------------------------------------------------------

#: Removed wherever they are: which share of the issuer, not which issuer.
_SHARE_WORDS: Final = re.compile(
    r"\(.*?\)|\[.*?\]|"
    r"\b(COMMON|ORDINARY|CAPITAL)\s+(STOCK|SHARES?|SHS)\b|"
    r"\bAMERICAN\s+DEPOSITA?RY\s+(SHARES?|RECEIPTS?)\b|\bDEPOSITA?RY\s+(SHARES?|RECEIPTS?)\b|"
    r"\b(SHARES\s+OF\s+)?BENEFICIAL\s+INTEREST\b|\bREPRESENTING\b.*$|"
    r"\b(CLASS|CL|SERIES|SER)\s*[A-Z]\b|\bCL[ABC]\b|"
    r"\b(UN)?SPONS?(ORED)?\b|\bUNSP\b|\bS?P?NS?/ADRS?\b|\bS/ADR\b|\bADRS?\b|\bADS\b|"
    r"\bNY\s+REG(ISTRY)?\s+SHS\b|\bREG(ISTERED)?\s+SHS\b|\bNYS\b|\b(NON[-\s]?)?VOTING\b|"
    r"\bSHS\b|\bSTOCK\b|\bSTK\b|\bSHARES?\b|\bUNITS?\b|\bUSD\s*[\d.]*(\s+\d+)?|\bNEW$|"
    r"\b(N\.?\s?V|S\.?\s?A|P\.?\s?L\.?\s?C|L\.?\s?P|S\.?\s?E|A\.?\s?G)\.?(?=[\s,]|$)",
    re.IGNORECASE,
)
#: What a scan runs together: ``WORKDAY INCCLA``, ``AON PLCSHS``.
_GLUED: Final = re.compile(r"\b(INC|CORP|PLC|COM|LTD)(CLA|CLB|CLC|COM|SHS)\b", re.IGNORECASE)

#: One spelling of each, for the comparison; ``None`` drops the word.
_WORDS: Final[dict[str, str | None]] = {
    **dict.fromkeys(("inc", "incorporated", "corp", "corporation", "co", "company"), None),
    **dict.fromkeys(("cos", "companies", "com", "sp", "spon", "cap"), None),
    **dict.fromkeys(("ltd", "limited", "plc", "llc", "lp", "nv", "sa", "se", "ag", "spa"), None),
    **dict.fromkeys(("the", "and", "of", "a", "de", "del"), None),
    **dict.fromkeys(("holding", "holdings", "hldg", "hldgs"), "hldg"),
    **dict.fromkeys(("international", "intl", "internatl"), "intl"),
    **dict.fromkeys(("group", "grp"), "grp"),
    **dict.fromkeys(("technologies", "technology", "tech", "techs"), "tech"),
    **dict.fromkeys(("financial", "finl", "fin"), "finl"),
    **dict.fromkeys(("pharmaceuticals", "pharmaceutical", "pharma", "pharm"), "pharm"),
    **dict.fromkeys(("industries", "inds", "industry"), "inds"),
    **dict.fromkeys(("bancorporation", "bancorp"), "bancorp"),
    **dict.fromkeys(("communications", "communication", "comms", "communic"), "comm"),
    **dict.fromkeys(("systems", "sys", "system"), "sys"),
    **dict.fromkeys(("services", "svcs", "svc", "service"), "svcs"),
    **dict.fromkeys(("america", "amer"), "amer"),
    **dict.fromkeys(("national", "natl"), "natl"),
    **dict.fromkeys(("trust", "tr"), "tr"),
    **dict.fromkeys(("manufacturing", "mfg", "manufacturers", "mfrs"), "mfg"),
    **dict.fromkeys(("research", "resh", "rsch"), "rsch"),
    **dict.fromkeys(("business", "bus"), "bus"),
    **dict.fromkeys(("machines", "mach", "machine"), "mach"),
    **dict.fromkeys(("insurance", "ins"), "ins"),
    **dict.fromkeys(("mountains", "mtns", "mtn", "mountain"), "mtn"),
    **dict.fromkeys(("software", "softwre", "sftwr"), "sftwr"),
    **dict.fromkeys(("resources", "res"), "res"),
    **dict.fromkeys(("energy", "enrgy"), "energy"),
}


def core_name(name: str | None) -> str:
    """The issuer's name, compared one way on both sides: lower case, the
    share words, legal forms and stop words out, abbreviations spelt one way.

    ``Alphabet Inc. - Class A Common Stock (GOOGL)``, ``ALPHABET INC-CL A`` and
    ``ALPHABET INC`` are all ``alphabet``; ``Tractor Supply Co`` and ``TRACTOR
    SUPPLY COMPANY`` are ``tractor supply``.
    """
    if not name:
        return ""
    stripped = _SHARE_WORDS.sub(" ", _GLUED.sub(r"\1 \2", name))
    words = []
    for word in fold(stripped):
        spelt = _WORDS.get(word, word)
        if spelt is not None:
            words.append(spelt)
    return " ".join(words)


def name_similarity(filed: str | None, *names: str | None) -> float:
    """The best ``pg_trgm`` similarity of ``filed``'s core name to any of
    ``names``': 1.0 when the cores are the same."""
    filed_core = core_name(filed)
    if not filed_core:
        return 0.0
    best = 0.0
    for name in names:
        core = core_name(name)
        if not core:
            continue
        if core == filed_core:
            return 1.0
        best = max(best, similarity(filed_core, core))
    return best


# --- the override key ------------------------------------------------------------------


def asset_key(asset_type: str | None, ticker: str | None, asset_name: str | None) -> str:
    """What lines are grouped by for a person's override, and the unresolved
    queue: the code, the ticker and the folded name, as filed.

    ``ST|GOOGL|alphabet inc class a common stock``. A code is part of it, since
    ``Microsoft Corp`` as a stock and as a bond are different assets; case,
    spacing and punctuation are not.
    """
    folded = " ".join(fold(asset_name or ""))
    return "|".join(
        ((asset_type or "").upper(), normalise_ticker(ticker) if ticker else "", folded)
    )
