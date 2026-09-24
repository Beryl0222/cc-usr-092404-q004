"""文化指标口径审议领域包。"""

from .contracts import (
    DOMAIN,
    CURRENT_SCHEMA_VERSION,
    ContractError,
    DomainMismatchError,
    IllegalRevisionError,
    InconsistentRecordError,
    MetricProposal,
    PayloadShapeError,
    PriceBasis,
    SchemaVersionError,
    SourceAuthorization,
    UnknownFieldError,
    load_proposal,
    load_record,
    parse_proposal,
)
from .pipeline import (
    APPROVAL_CHAIN,
    COMPONENTS,
    AmendmentError,
    AmendmentStatus,
    BatchResult,
    CaliberState,
    IntakeOutcome,
    IntakeResult,
    MetricCouncil,
    ReviewError,
    ReviewKind,
)
from .store import JsonStore

__all__ = [
    # 合同
    "DOMAIN",
    "CURRENT_SCHEMA_VERSION",
    "ContractError",
    "DomainMismatchError",
    "IllegalRevisionError",
    "InconsistentRecordError",
    "MetricProposal",
    "PayloadShapeError",
    "PriceBasis",
    "SchemaVersionError",
    "SourceAuthorization",
    "UnknownFieldError",
    "load_proposal",
    "load_record",
    "parse_proposal",
    # 流程
    "APPROVAL_CHAIN",
    "COMPONENTS",
    "AmendmentError",
    "AmendmentStatus",
    "BatchResult",
    "CaliberState",
    "IntakeOutcome",
    "IntakeResult",
    "MetricCouncil",
    "ReviewError",
    "ReviewKind",
    # 持久化
    "JsonStore",
]
