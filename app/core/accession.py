"""The one place that knows how an accession number is spelled.

Two spellings are in circulation and both are pasted by people who have no
reason to care which they have: ``0001067983-24-000011`` is what EDGAR's own
indexes print and what ``filing.accession_no`` stores, and
``000106798324000011`` is what appears in archive URLs. They are not
interchangeable downstream — the column is ``CHAR(20)`` holding the dashed form,
so a lookup with the undashed one matches nothing and reports the filing as
absent rather than as misspelled.

So every entry point normalises at the door, and this module is what they all
call: the CLI argument, the API path parameter, and whatever ingests a queue of
them next. A second copy of the rule is a second place for the two spellings to
diverge.
"""

from typing import Final

#: Ten digits of transmitter, two of year, six of sequence.
ACCESSION_PARTS: Final = (10, 2, 6)

#: The dashed form's length, which is what ``CHAR(20)`` in the schema means.
ACCESSION_LENGTH: Final = sum(ACCESSION_PARTS) + len(ACCESSION_PARTS) - 1

#: ``##########-##-######``, for error messages and OpenAPI examples.
ACCESSION_SHAPE: Final = "-".join("#" * part for part in ACCESSION_PARTS)


def normalise_accession(value: str) -> str:
    """``000106798324000011`` or ``0001067983-24-000011`` -> the dashed form.

    :raises ValueError: if ``value`` is not eighteen digits with or without the
        two dashes. Callers turn this into whatever their layer's failure looks
        like — a non-zero exit in the CLI, a 422 in the API — because the one
        thing this function must not do is guess.
    """
    digits = value.strip().replace("-", "")
    if not digits.isdigit() or len(digits) != sum(ACCESSION_PARTS):
        raise ValueError(f"{value!r} is not an accession number: expected {ACCESSION_SHAPE}")
    first, second = ACCESSION_PARTS[0], ACCESSION_PARTS[0] + ACCESSION_PARTS[1]
    return f"{digits[:first]}-{digits[first:second]}-{digits[second:]}"
