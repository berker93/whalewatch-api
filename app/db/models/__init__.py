"""ORM models.

Importing this package must import every model module, because
``Base.metadata`` is only populated as a side effect of the class bodies being
executed. Alembic imports this package and nothing else; a model module missing
from the list below is a table autogenerate will never see.
"""

from app.db.models.base import NAMING_CONVENTION, Base
from app.db.models.enums import AmendmentKind
from app.db.models.filer import Filer, FilerCik
from app.db.models.filing import (
    PARSE_STATUS_CHECK,
    QUARTER_EXPRESSION,
    SUSPECT_HAS_NOTES_CHECK,
    Filing,
    ParseStatus,
)
from app.db.models.holding import MONEY, QUANTITY, Holding
from app.db.models.security import Security

__all__ = [
    "MONEY",
    "NAMING_CONVENTION",
    "PARSE_STATUS_CHECK",
    "QUANTITY",
    "QUARTER_EXPRESSION",
    "SUSPECT_HAS_NOTES_CHECK",
    "AmendmentKind",
    "Base",
    "Filer",
    "FilerCik",
    "Filing",
    "Holding",
    "ParseStatus",
    "Security",
]
