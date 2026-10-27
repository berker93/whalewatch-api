"""ORM models.

Importing this package must import every model module, because
``Base.metadata`` is only populated as a side effect of the class bodies being
executed. Alembic imports this package and nothing else; a model module missing
from the list below is a table autogenerate will never see.
"""

from app.db.models.account import (
    PURPOSE_CHECK,
    AccountToken,
    AppUser,
    SavedFilter,
    SignupRequest,
    TokenPurpose,
    UserActivityDay,
    WatchlistItem,
)
from app.db.models.admin import (
    ActionSource,
    AdminAction,
    AdminActionKind,
    AdminUser,
    MetricDay,
)
from app.db.models.alert import (
    FILER_KINDS,
    KIND_CHECK,
    PAYLOAD_KEYS,
    SECURITY_KINDS,
    AlertKind,
    AlertRule,
    Channel,
    Notification,
    NotificationPayload,
)
from app.db.models.base import NAMING_CONVENTION, Base
from app.db.models.crawl_watermark import CrawlWatermark
from app.db.models.email import (
    STATUS_RANKS,
    STREAMS,
    EmailKind,
    EmailLog,
    EmailStatus,
    EmailSuppression,
    Stream,
    SuppressionReason,
    SuppressionScope,
)
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
from app.db.models.insider import Insider, ReportingOwner
from app.db.models.insider_cluster import InsiderCluster
from app.db.models.insider_transaction import OPEN_MARKET_CODES, InsiderTransaction
from app.db.models.manual_review import REVIEW_STATUS_CHECK, ManualReview, ReviewStatus
from app.db.models.matview_refresh import MatviewRefresh
from app.db.models.pending_filing import PENDING_STATUS_CHECK, PendingFiling, PendingStatus
from app.db.models.plan import (
    ENTITLEMENT_KEYS,
    FLAG_KEYS,
    LIMIT_KEYS,
    LIVE_STATUSES,
    DunningStep,
    PaddleEvent,
    PaddleEventOutcome,
    Plan,
    PlanEntitlement,
    PlanInterval,
    PlanOverride,
    Subscription,
    SubscriptionEntitlement,
    SubscriptionStatus,
)
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
from app.db.models.privacy import (
    BASES,
    PURPOSE_BASIS_CHECK,
    REQUESTED_BY_CHECK,
    SOURCE_CHECK,
    AccountDeletion,
    Basis,
    ConsentRecord,
    ConsentSource,
    DeletionRequester,
    Purpose,
)
from app.db.models.sec_13f_security import STATUS_CHECK, ListStatus, Sec13FSecurity
from app.db.models.sec_company_ticker import SecCompanyTicker
from app.db.models.security import RESOLUTION_SOURCE_CHECK, ResolutionSource, Security
from app.db.models.security_alias import SecurityAlias
from app.db.models.security_quote import QUOTE_SOURCE_CHECK, QuoteSource, SecurityQuote
from app.db.models.security_split import (
    SPLIT_CONFIDENCE_CHECK,
    SPLIT_RATIO,
    SecuritySplit,
    SplitConfidence,
)
from app.db.models.usage import (
    MAX_KEY_NAME,
    METERED_KEY_CHECK,
    METERED_KEYS,
    ApiKey,
    UsageCounter,
)

__all__ = [
    "ACTION_CHECK",
    "BASES",
    "CATEGORY_CHECK",
    "CHANGE_PCT",
    "CONFIDENCE_CHECK",
    "ENTITLEMENT_KEYS",
    "FILER_KINDS",
    "FINISHED_CHECK",
    "FLAG_KEYS",
    "KIND_CHECK",
    "LIMIT_KEYS",
    "LIVE_STATUSES",
    "MAX_KEY_NAME",
    "METERED_KEYS",
    "METERED_KEY_CHECK",
    "MONEY",
    "NAMING_CONVENTION",
    "NEW_CHECK",
    "NOT_SUCCESS_SAYS_WHY_CHECK",
    "OPEN_MARKET_CODES",
    "OUTCOME_CHECK",
    "OVERLAP_CHECK",
    "PARSE_STATUS_CHECK",
    "PAYLOAD_KEYS",
    "PENDING_STATUS_CHECK",
    "PRICE",
    "PRICED_CHECK",
    "PURPOSE_BASIS_CHECK",
    "PURPOSE_CHECK",
    "QUANTITY",
    "QUARTER_EXPRESSION",
    "QUOTE_SOURCE_CHECK",
    "REQUESTED_BY_CHECK",
    "RESOLUTION_SOURCE_CHECK",
    "REVIEW_STATUS_CHECK",
    "RUN_STATUS_CHECK",
    "SECURITY_KINDS",
    "SOURCE_CHECK",
    "SPLIT_CHECK",
    "SPLIT_CONFIDENCE_CHECK",
    "SPLIT_RATIO",
    "SPLIT_ROW_CHECK",
    "STATUS_CHECK",
    "STATUS_RANKS",
    "STREAMS",
    "SUSPECT_HAS_NOTES_CHECK",
    "WEIGHT_PCT",
    "AccountDeletion",
    "AccountToken",
    "ActionSource",
    "AdminAction",
    "AdminActionKind",
    "AdminUser",
    "AlertKind",
    "AlertRule",
    "AmendmentKind",
    "ApiKey",
    "AppUser",
    "Base",
    "Basis",
    "ChangeAction",
    "Channel",
    "ConsentRecord",
    "ConsentSource",
    "CrawlWatermark",
    "DeletionRequester",
    "DunningStep",
    "EmailKind",
    "EmailLog",
    "EmailStatus",
    "EmailSuppression",
    "FIGIOutcome",
    "FigiResponseLog",
    "Filer",
    "FilerCategory",
    "FilerCik",
    "Filing",
    "Holding",
    "ImpliedPrice",
    "IngestionRun",
    "Insider",
    "InsiderCluster",
    "InsiderTransaction",
    "ListStatus",
    "ManualReview",
    "MatviewRefresh",
    "MetricDay",
    "Notification",
    "NotificationPayload",
    "OverlapPolicy",
    "PaddleEvent",
    "PaddleEventOutcome",
    "ParseStatus",
    "PendingFiling",
    "PendingStatus",
    "Plan",
    "PlanEntitlement",
    "PlanInterval",
    "PlanOverride",
    "PositionChange",
    "PositionSnapshot",
    "PriceConfidence",
    "Purpose",
    "QuoteSource",
    "ReportingOwner",
    "ResolutionSource",
    "ReviewStatus",
    "RunStatus",
    "SavedFilter",
    "Sec13FSecurity",
    "SecCompanyTicker",
    "Security",
    "SecurityAlias",
    "SecurityQuote",
    "SecuritySplit",
    "SignupRequest",
    "SplitConfidence",
    "Stream",
    "Subscription",
    "SubscriptionEntitlement",
    "SubscriptionStatus",
    "SuppressionReason",
    "SuppressionScope",
    "TokenPurpose",
    "UsageCounter",
    "UserActivityDay",
    "WatchlistItem",
]
