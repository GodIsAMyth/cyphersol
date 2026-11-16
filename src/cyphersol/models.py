"""Canonical domain models and enumerations matching SRS Section 9."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, is_dataclass
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from typing import Any, Dict, List, Optional


class WorkspaceMode(str, Enum):
    CYPHER_EDGE = "cypher_edge"
    CYPHER_TRACE = "cypher_trace"


class TxDirection(str, Enum):
    CREDIT = "CR"
    DEBIT = "DR"


class AnomalyFlag(str, Enum):
    VALID = "VALID"
    BALANCE_MISMATCH = "BALANCE_MISMATCH"
    DUPLICATE_SUSPECT = "DUPLICATE_SUSPECT"
    DATE_OUT_OF_ORDER = "DATE_OUT_OF_ORDER"
    LOW_OCR_CONFIDENCE = "LOW_OCR_CONFIDENCE"
    MISSING_METADATA = "MISSING_METADATA"
    DATE_UNPARSEABLE = "DATE_UNPARSEABLE"
    DATE_ORDER_AMBIGUOUS = "DATE_ORDER_AMBIGUOUS"
    ZERO_OR_DUAL_AMOUNT = "ZERO_OR_DUAL_AMOUNT"
    DIRECTION_INFERRED = "DIRECTION_INFERRED"
    OCR_AUTOCORRECTED = "OCR_AUTOCORRECTED"
    LLM_REPAIRED = "LLM_REPAIRED"
    OUTSIDE_STATEMENT_PERIOD = "OUTSIDE_STATEMENT_PERIOD"


class VoucherType(str, Enum):
    PAYMENT = "Payment"
    RECEIPT = "Receipt"
    CONTRA = "Contra"
    SALES = "Sales"
    PURCHASE = "Purchase"
    JOURNAL = "Journal"


class MuleRiskTier(str, Enum):
    CRITICAL = "Critical"      # 80 - 100
    SUSPICIOUS = "Suspicious"  # 50 - 79
    LOW_RISK = "Low Risk"      # 0 - 49


def serialize_value(val: Any) -> Any:
    """Convert dataclasses, Enums, Decimals, and dates to JSON-safe primitives."""
    if isinstance(val, Enum):
        return val.value
    if isinstance(val, Decimal):
        return float(round(val, 2))
    if isinstance(val, (datetime, date)):
        return val.isoformat()
    if is_dataclass(val):
        return {k: serialize_value(v) for k, v in asdict(val).items()}
    if isinstance(val, list):
        return [serialize_value(v) for v in val]
    if isinstance(val, dict):
        return {str(k): serialize_value(v) for k, v in val.items()}
    return val


@dataclass
class InvestigationCase:
    case_id: str
    title: str
    mode: WorkspaceMode
    created_at: str
    agency_or_firm_name: Optional[str] = None
    similarity_threshold: float = 0.85
    confidence_threshold: float = 0.65

    def to_dict(self) -> Dict[str, Any]:
        return serialize_value(self)


@dataclass
class BankAccountStatement:
    statement_id: str
    case_id: str
    source_filename: str
    sha256_hash: str
    bank_code: str
    bank_name: str
    account_number: str
    account_holder_name: str
    period_start: str
    period_end: str
    opening_balance: Decimal
    closing_balance: Decimal
    total_transactions: int
    ifsc_code: Optional[str] = None
    account_type: Optional[str] = "Savings"
    balance_mismatch_count: int = 0
    duplicate_suspect_count: int = 0
    is_ocr_derived: bool = False
    ingested_at: str = ""
    # P1 (NLP-09): account holder PAN when the statement header carries one; links holders across banks.
    pan: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return serialize_value(self)


@dataclass
class NormalizedTransaction:
    tx_id: str
    statement_id: str
    case_id: str
    row_index: int
    txn_date: str
    raw_narration: str
    payment_channel: str
    direction: TxDirection
    debit: Decimal
    credit: Decimal
    running_balance: Decimal
    category_id: int
    category_name: str
    categorization_source: str
    confidence_score: float
    txn_timestamp: Optional[str] = None
    value_date: Optional[str] = None
    cheque_or_ref_no: Optional[str] = None
    utr_number: Optional[str] = None
    extracted_counterparty: Optional[str] = None
    counterparty_vpa_or_acc: Optional[str] = None
    counterparty_ifsc: Optional[str] = None
    resolved_entity_id: Optional[str] = None
    top3_suggestions: List[Dict[str, Any]] = field(default_factory=list)
    is_suspense: bool = False
    tally_voucher_type: Optional[VoucherType] = None
    tally_ledger_name: Optional[str] = None
    anomaly_flags: List[AnomalyFlag] = field(default_factory=list)
    is_duplicate_ignored: bool = False
    source_account_number: Optional[str] = None
    source_holder_name: Optional[str] = None
    # P1 (NLP-18/20): POS/ATM card mask and cheque number parsed from the narration.
    card_number_masked: Optional[str] = None
    cheque_number: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return serialize_value(self)


@dataclass
class ResolvedEntity:
    entity_id: str
    case_id: str
    canonical_name: str
    aliases: List[str] = field(default_factory=list)
    linked_account_numbers: List[str] = field(default_factory=list)
    linked_vpas: List[str] = field(default_factory=list)
    has_statement: bool = False
    is_moved_to_misc: bool = False
    total_credited: Decimal = Decimal("0.00")
    total_debited: Decimal = Decimal("0.00")
    tx_count: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return serialize_value(self)


@dataclass
class MuleParameterBreakdown:
    flow_through_ratio: float          # 25%
    velocity_hold_time: float          # 20%
    lifestyle_spending_absence: float  # 15%
    counterparty_concentration: float  # 10%
    fan_in_fan_out_pattern: float      # 10%
    cash_crypto_wallet_leakage: float  # 5%
    structuring_smurfing: float        # 5%
    zero_balance_oscillation: float    # 5%
    burst_dormancy_pattern: float      # 3%
    round_number_anomalies: float      # 2%

    def to_dict(self) -> Dict[str, Any]:
        return serialize_value(self)


@dataclass
class MuleRiskAssessment:
    account_number: str
    account_holder_name: str
    bank_name: str
    composite_score: float
    risk_tier: MuleRiskTier
    parameters: MuleParameterBreakdown
    entity_id: Optional[str] = None
    total_credited: float = 0.0
    total_debited: float = 0.0
    tx_count: int = 0
    median_hold_hours: Optional[float] = None
    cash_crypto_leakage_pct: float = 0.0
    lifestyle_txn_count: int = 0
    weighted_contributions: Dict[str, float] = field(default_factory=dict)
    flagged_evidence_tx_ids: List[str] = field(default_factory=list)
    # TF-15 explainability vector: one dict per parameter (key, label, weight, score, contribution,
    # raw_metric, raw, formula, evidence_tx_ids, evidence_count). TF-23: statements scored together.
    explainability: List[Dict[str, Any]] = field(default_factory=list)
    statement_ids: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return serialize_value(self)


@dataclass
class DrawTemplate:
    template_id: str
    bank_code: str
    template_name: str
    table_bbox: List[float]  # [x0, y0, x1, y1] normalized 0..1 or PDF points
    column_splits: List[float]  # normalized vertical dividers
    column_names: List[str]
    created_at: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return serialize_value(self)


@dataclass
class BidirectionalFlowResult:
    entity_a_id: str
    entity_a_name: str
    entity_b_id: str
    entity_b_name: str
    total_a_to_b: float
    total_b_to_a: float
    net_flow: float
    tx_count_a_to_b: int
    tx_count_b_to_a: int
    min_turnaround_hours: Optional[float] = None
    median_turnaround_hours: Optional[float] = None
    rapid_round_trip_count: int = 0
    transactions: List[Dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return serialize_value(self)


@dataclass
class LayeringNode:
    node_id: str
    entity_id: str
    entity_name: str
    role: str  # "CREDIT_SOURCE", "HAS_STATEMENT", "DETAILS_ONLY", "MISC"
    amount: float
    depth: int
    has_statement: bool
    is_misc: bool = False
    misc_count: int = 0
    tx_ids: List[str] = field(default_factory=list)
    children: List["LayeringNode"] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return serialize_value(self)


@dataclass
class CircularLoop:
    loop_id: str
    hop_count: int
    entities: List[str]
    entity_ids: List[str]
    min_amount: float
    max_amount: float
    retention_ratio: float
    duration_hours: float
    start_date: str
    end_date: str
    hops: List[Dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return serialize_value(self)


class ConflictError(ValueError):
    """Raised when a request conflicts with existing state (e.g. duplicate statement SHA-256). Mapped to HTTP 409."""
