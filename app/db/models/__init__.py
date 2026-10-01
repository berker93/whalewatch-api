"""ORM models.

Importing this package must import every model module, because
``Base.metadata`` is only populated as a side effect of the class bodies being
executed. Alembic imports this package and nothing else; a model module missing
from the list below is a table autogenerate will never see.
"""

from app.db.models.base import NAMING_CONVENTION, Base
from app.db.models.enums import AmendmentKind
from app.db.models.filer import (
    CATEGORY_CHECK,
    OVERLAP_CHECK,
    Filer,
    FilerCategory,
    FilerCik,
    OverlapPolicy,
)
from app.db.models.filing import (
    PARSE_STATUS_CHECK,
    QUARTER_EXPRESSION,
    SUSPECT_HAS_NOTES_CHECK,
    Filing,
    ParseStatus,
)
from app.db.models.holding import MONEY, QUANTITY, Holding
from app.db.models.ingestion_run import (
    FINISHED_CHECK,
    NOT_SUCCESS_SAYS_WHY_CHECK,
    RUN_STATUS_CHECK,
    IngestionRun,
    RunStatus,
)
from app.db.models.pending_filing import PENDING_STATUS_CHECK, PendingFiling, PendingStatus
from app.db.models.position_snapshot import WEIGHT_PCT, PositionSnapshot
from app.db.models.security import Security

__all__ = [
    "CATEGORY_CHECK",
    "FINISHED_CHECK",
    "MONEY",
    "NAMING_CONVENTION",
    "NOT_SUCCESS_SAYS_WHY_CHECK",
    "OVERLAP_CHECK",
    "PARSE_STATUS_CHECK",
    "PENDING_STATUS_CHECK",
    "QUANTITY",
    "QUARTER_EXPRESSION",
    "RUN_STATUS_CHECK",
    "SUSPECT_HAS_NOTES_CHECK",
    "WEIGHT_PCT",
    "AmendmentKind",
    "Base",
    "Filer",
    "FilerCategory",
    "FilerCik",
    "Filing",
    "Holding",
    "IngestionRun",
    "OverlapPolicy",
    "ParseStatus",
    "PendingFiling",
    "PendingStatus",
    "PositionSnapshot",
    "RunStatus",
    "Security",
]
