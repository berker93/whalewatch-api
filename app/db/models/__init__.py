"""ORM models.

Importing this package must import every model module, because
``Base.metadata`` is only populated as a side effect of the class bodies being
executed. Alembic imports this package and nothing else; a model module missing
from the list below is a table autogenerate will never see.
"""

from app.db.models.base import NAMING_CONVENTION, Base
from app.db.models.enums import AmendmentKind
from app.db.models.figi_response_log import OUTCOME_CHECK, FIGIOutcome, FigiResponseLog
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
from app.db.models.implied_price import (
    CONFIDENCE_CHECK,
    PRICE,
    PRICED_CHECK,
    ImpliedPrice,
    PriceConfidence,
)
from app.db.models.ingestion_run import (
    FINISHED_CHECK,
    NOT_SUCCESS_SAYS_WHY_CHECK,
    RUN_STATUS_CHECK,
    IngestionRun,
    RunStatus,
)
from app.db.models.manual_review import REVIEW_STATUS_CHECK, ManualReview, ReviewStatus
from app.db.models.matview_refresh import MatviewRefresh
from app.db.models.pending_filing import PENDING_STATUS_CHECK, PendingFiling, PendingStatus
from app.db.models.position_change import (
    ACTION_CHECK,
    CHANGE_PCT,
    NEW_CHECK,
    SPLIT_CHECK,
    SPLIT_ROW_CHECK,
    ChangeAction,
    PositionChange,
)
from app.db.models.position_snapshot import WEIGHT_PCT, PositionSnapshot
from app.db.models.sec_13f_security import STATUS_CHECK, ListStatus, Sec13FSecurity
from app.db.models.security import RESOLUTION_SOURCE_CHECK, ResolutionSource, Security
from app.db.models.security_alias import SecurityAlias
from app.db.models.security_split import (
    SPLIT_CONFIDENCE_CHECK,
    SPLIT_RATIO,
    SecuritySplit,
    SplitConfidence,
)

__all__ = [
    "ACTION_CHECK",
    "CATEGORY_CHECK",
    "CHANGE_PCT",
    "CONFIDENCE_CHECK",
    "FINISHED_CHECK",
    "MONEY",
    "NAMING_CONVENTION",
    "NEW_CHECK",
    "NOT_SUCCESS_SAYS_WHY_CHECK",
    "OUTCOME_CHECK",
    "OVERLAP_CHECK",
    "PARSE_STATUS_CHECK",
    "PENDING_STATUS_CHECK",
    "PRICE",
    "PRICED_CHECK",
    "QUANTITY",
    "QUARTER_EXPRESSION",
    "RESOLUTION_SOURCE_CHECK",
    "REVIEW_STATUS_CHECK",
    "RUN_STATUS_CHECK",
    "SPLIT_CHECK",
    "SPLIT_CONFIDENCE_CHECK",
    "SPLIT_RATIO",
    "SPLIT_ROW_CHECK",
    "STATUS_CHECK",
    "SUSPECT_HAS_NOTES_CHECK",
    "WEIGHT_PCT",
    "AmendmentKind",
    "Base",
    "ChangeAction",
    "FIGIOutcome",
    "FigiResponseLog",
    "Filer",
    "FilerCategory",
    "FilerCik",
    "Filing",
    "Holding",
    "ImpliedPrice",
    "IngestionRun",
    "ListStatus",
    "ManualReview",
    "MatviewRefresh",
    "OverlapPolicy",
    "ParseStatus",
    "PendingFiling",
    "PendingStatus",
    "PositionChange",
    "PositionSnapshot",
    "PriceConfidence",
    "ResolutionSource",
    "ReviewStatus",
    "RunStatus",
    "Sec13FSecurity",
    "Security",
    "SecurityAlias",
    "SecuritySplit",
    "SplitConfidence",
]
