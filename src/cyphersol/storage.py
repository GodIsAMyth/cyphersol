"""Local SQLite persistence layer (the "vault") for CypherEdge & CypherTrace.

Security properties (see docs/REVIEW_2026-11.md BE-03/04/05/21):

* The vault is a plain SQLite file unless the optional ``sqlcipher3`` package is importable *and* the
  ``CYPHERSOL_DB_KEY`` environment variable is set, in which case it is opened with ``PRAGMA key``
  (SQLCipher encryption at rest). ``CypherVault.encrypted`` reports which mode is active.
* A database file (and its directory) created by this class is restricted to the owner (0600 / 0700).
  SQLite creates its -wal/-shm side files with the main file's permissions.
* ``audit_logs`` and ``statement_provenance`` are append-only: ``BEFORE UPDATE``/``BEFORE DELETE`` triggers
  abort any change. Audit rows form a SHA-256 hash chain (``prev_hash`` -> ``row_hash``) that is enforced on
  insert and can be re-verified with :meth:`CypherVault.verify_audit_chain`. Triggers protect against
  accidental or application-level changes; they cannot stop someone with raw write access to the file
  from rebuilding it, which the hash chain makes detectable only if the latest hash is kept elsewhere.
* Cases, statements and transactions are written with UPSERT (``ON CONFLICT DO UPDATE``) so that saving a
  case never cascade-deletes its evidence, and a statement's hash, filename and ingest time are immutable.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import logging
import os
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Dict, Iterable, Iterator, List, Optional, Sequence, Set, Tuple

from cyphersol.models import (
    AnomalyFlag,
    BankAccountStatement,
    ConflictError,
    DrawTemplate,
    InvestigationCase,
    NormalizedTransaction,
    ResolvedEntity,
    TxDirection,
    VoucherType,
    WorkspaceMode,
)

_LOG = logging.getLogger("cyphersol.storage")

DB_KEY_ENV = "CYPHERSOL_DB_KEY"
GENESIS_HASH = "0" * 64
DEFAULT_ACTOR = "local_operator"

try:  # Optional SQLCipher driver (encryption at rest); stdlib sqlite3 is the fallback.
    from sqlcipher3 import dbapi2 as _sqlcipher  # type: ignore
except ImportError:  # pragma: no cover - depends on the environment
    _sqlcipher = None


def _utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _audit_row_hash(
    prev_hash: str, log_id: str, case_id: Optional[str], actor: str, action: str, details_json: str, created_at: str
) -> str:
    payload = json.dumps(
        [prev_hash, log_id, case_id, actor, action, details_json, created_at], ensure_ascii=False, separators=(",", ":")
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _rule_id(pattern: str, direction: str, case_id: Optional[str]) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_OID, f"{pattern}:{direction}:{case_id or ''}"))


@dataclass
class ProvenanceRecord:
    """One append-only chain-of-custody row for an ingested statement file (BE-04)."""

    statement_id: str
    case_id: str
    source_filename: str
    sha256: str
    file_size_bytes: Optional[int]
    parser_version: str
    analyst: str
    ingested_at: str = ""
    details: Dict[str, Any] = field(default_factory=dict)


@dataclass
class IngestFileResult:
    """Persisted per-file outcome of an ingest batch (BE-11 / ING-14)."""

    filename: str
    status: str  # "ok" | "failed" | "duplicate"
    sha256: Optional[str] = None
    file_size_bytes: Optional[int] = None
    statement_id: Optional[str] = None
    transactions: int = 0
    error: Optional[str] = None
    error_type: Optional[str] = None


@dataclass
class IngestBatch:
    """Everything one ingest request writes, committed in a single transaction."""

    case_id: str
    statements: List[Tuple[BankAccountStatement, List[NormalizedTransaction]]] = field(default_factory=list)
    provenance: List[ProvenanceRecord] = field(default_factory=list)
    updated_transactions: List[NormalizedTransaction] = field(default_factory=list)
    entities: Optional[List[ResolvedEntity]] = None
    file_results: List[IngestFileResult] = field(default_factory=list)
    audit: List[Tuple[str, Dict[str, Any]]] = field(default_factory=list)
    settings: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    superseded_statement_ids: List[str] = field(default_factory=list)
    actor: str = DEFAULT_ACTOR


_TX_COLUMNS = (
    "tx_id", "statement_id", "case_id", "row_index", "txn_date", "txn_timestamp", "value_date",
    "raw_narration", "cheque_or_ref_no", "utr_number", "payment_channel", "direction",
    "debit", "credit", "running_balance", "extracted_counterparty", "counterparty_vpa_or_acc",
    "counterparty_ifsc", "resolved_entity_id", "category_id", "category_name",
    "categorization_source", "confidence_score", "top3_suggestions_json", "is_suspense",
    "tally_voucher_type", "tally_ledger_name", "anomaly_flags_json", "is_duplicate_ignored",
    "source_account_number", "source_holder_name", "card_number_masked", "cheque_number",
)
# change_rev: the case revision a row was last written at (revision read inside the write transaction + 1).
# The analysis cache reloads only rows with change_rev > its cached revision (BE-07).
_TX_UPSERT_SQL = (
    f"INSERT INTO transactions ({', '.join(_TX_COLUMNS)}, change_rev) "
    f"VALUES ({', '.join('?' * (len(_TX_COLUMNS) + 1))}) "
    "ON CONFLICT(tx_id) DO UPDATE SET "
    + ", ".join(f"{c} = excluded.{c}" for c in _TX_COLUMNS + ("change_rev",) if c not in ("tx_id", "case_id"))
)

_STMT_COLUMNS = (
    "statement_id", "case_id", "source_filename", "sha256_hash", "bank_code", "bank_name",
    "account_number", "ifsc_code", "account_holder_name", "account_type", "period_start",
    "period_end", "opening_balance", "closing_balance", "total_transactions",
    "balance_mismatch_count", "duplicate_suspect_count", "is_ocr_derived", "ingested_at", "pan",
)
# Evidence identity (hash, file name, ingest time, owning case) is never overwritten by an upsert.
_STMT_IMMUTABLE = ("statement_id", "case_id", "source_filename", "sha256_hash", "ingested_at")
_STMT_UPSERT_SQL = (
    f"INSERT INTO statements ({', '.join(_STMT_COLUMNS)}) VALUES ({', '.join('?' * len(_STMT_COLUMNS))}) "
    "ON CONFLICT(statement_id) DO UPDATE SET "
    + ", ".join(f"{c} = excluded.{c}" for c in _STMT_COLUMNS if c not in _STMT_IMMUTABLE)
)

_FLAGGED_SQL = "anomaly_flags_json NOT IN ('[]', '[\"VALID\"]')"

# Sortable transaction columns (P3 data grid). Keys are API names, values are SQL expressions; only these
# whitelisted expressions ever reach ORDER BY.
TX_SORT_COLUMNS: Dict[str, str] = {
    "txn_date": "txn_date",
    "date": "txn_date",
    "debit": "debit",
    "credit": "credit",
    "amount": "MAX(debit, credit)",
    "running_balance": "running_balance",
    "balance": "running_balance",
    "payment_channel": "payment_channel",
    "channel": "payment_channel",
    "category_id": "category_id",
    "category_name": "category_name",
    "confidence_score": "confidence_score",
    "extracted_counterparty": "IFNULL(extracted_counterparty, '')",
    "counterparty": "IFNULL(extracted_counterparty, '')",
    "source_holder_name": "IFNULL(source_holder_name, '')",
    "source_account_number": "IFNULL(source_account_number, '')",
    "raw_narration": "raw_narration",
    "categorization_source": "categorization_source",
    # Statement order; meaningful together with statement_id (anomaly context windows).
    "row_index": "row_index",
}
MAX_TX_IDS = 500


def _srs_channel_sql(channel: Optional[str]) -> str:
    """SQLite scalar function: internal payment channel -> SRS FR-CAT-01 channel name (NLP-24)."""
    try:
        from cyphersol.nlp.narration_parser import srs_payment_channel
    except Exception:  # pragma: no cover - defensive; the mapping is pure
        return (channel or "").strip().upper() or "OTHER"
    return srs_payment_channel(channel)


class CypherVault:
    """Thread-safe local SQLite repository for cases, statements, transactions, entities, rules, and audit logs."""

    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        abs_path = os.path.abspath(db_path)
        db_dir = os.path.dirname(abs_path)
        if not os.path.isdir(db_dir):
            os.makedirs(db_dir, exist_ok=True)
            self._chmod(db_dir, 0o700)
        created_file = not os.path.exists(abs_path)
        self._lock = threading.RLock()
        self._key = os.environ.get(DB_KEY_ENV) or None
        self.encrypted = bool(self._key and _sqlcipher is not None)
        if self._key and _sqlcipher is None:
            _LOG.warning(
                "%s is set but the optional 'sqlcipher3' package is not installed; the vault at %s is NOT encrypted.",
                DB_KEY_ENV,
                abs_path,
            )
        if created_file:
            # Create the file ourselves with owner-only permissions before SQLite opens it.
            fd = os.open(abs_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.close(fd)
            self._chmod(abs_path, 0o600)
        self._init_schema()

    @staticmethod
    def _chmod(path: str, mode: int) -> None:
        try:
            os.chmod(path, mode)
        except OSError as exc:  # pragma: no cover - e.g. filesystems without POSIX modes
            _LOG.warning("Could not set mode %o on %s: %s", mode, path, exc)

    # ------------------------------------------------------------------------------------------ connections
    def _open(self) -> sqlite3.Connection:
        if self.encrypted:
            conn = _sqlcipher.connect(self.db_path, check_same_thread=False)
            conn.execute("PRAGMA key = '{}'".format(self._key.replace("'", "''")))
            conn.row_factory = _sqlcipher.Row
        else:
            conn = sqlite3.connect(self.db_path, check_same_thread=False)
            conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        conn.execute("PRAGMA busy_timeout=5000;")
        conn.execute("PRAGMA temp_store=MEMORY;")
        conn.execute("PRAGMA foreign_keys=ON;")
        try:
            conn.create_function("srs_channel", 1, _srs_channel_sql, deterministic=True)
        except (TypeError, sqlite3.NotSupportedError):  # pragma: no cover - very old SQLite / sqlcipher builds
            conn.create_function("srs_channel", 1, _srs_channel_sql)
        return conn

    @contextmanager
    def _connect(self, write: bool = False) -> Iterator[sqlite3.Connection]:
        """One connection per operation. ``write=True`` takes the SQLite write lock up front (BEGIN IMMEDIATE),
        so read-modify-write sequences (audit chain, duplicate checks) are serialised across processes too."""
        conn = self._open()
        try:
            with conn:
                if write:
                    conn.execute("BEGIN IMMEDIATE")
                yield conn
        finally:
            conn.close()

    # ------------------------------------------------------------------------------------------ schema
    def _init_schema(self) -> None:
        with self._lock, self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS cases (
                    case_id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    mode TEXT NOT NULL,
                    agency_or_firm_name TEXT,
                    created_at TEXT NOT NULL,
                    similarity_threshold REAL NOT NULL DEFAULT 0.85,
                    confidence_threshold REAL NOT NULL DEFAULT 0.65,
                    revision INTEGER NOT NULL DEFAULT 0
                );

                CREATE TABLE IF NOT EXISTS statements (
                    statement_id TEXT PRIMARY KEY,
                    case_id TEXT NOT NULL,
                    source_filename TEXT NOT NULL,
                    sha256_hash TEXT NOT NULL,
                    bank_code TEXT NOT NULL,
                    bank_name TEXT NOT NULL,
                    account_number TEXT NOT NULL,
                    ifsc_code TEXT,
                    account_holder_name TEXT NOT NULL,
                    account_type TEXT,
                    period_start TEXT NOT NULL,
                    period_end TEXT NOT NULL,
                    opening_balance REAL NOT NULL,
                    closing_balance REAL NOT NULL,
                    total_transactions INTEGER NOT NULL,
                    balance_mismatch_count INTEGER NOT NULL DEFAULT 0,
                    duplicate_suspect_count INTEGER NOT NULL DEFAULT 0,
                    is_ocr_derived INTEGER NOT NULL DEFAULT 0,
                    ingested_at TEXT NOT NULL,
                    FOREIGN KEY (case_id) REFERENCES cases(case_id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS transactions (
                    tx_id TEXT PRIMARY KEY,
                    statement_id TEXT NOT NULL,
                    case_id TEXT NOT NULL,
                    row_index INTEGER NOT NULL,
                    txn_date TEXT NOT NULL,
                    txn_timestamp TEXT,
                    value_date TEXT,
                    raw_narration TEXT NOT NULL,
                    cheque_or_ref_no TEXT,
                    utr_number TEXT,
                    payment_channel TEXT NOT NULL,
                    direction TEXT NOT NULL,
                    debit REAL NOT NULL,
                    credit REAL NOT NULL,
                    running_balance REAL NOT NULL,
                    extracted_counterparty TEXT,
                    counterparty_vpa_or_acc TEXT,
                    counterparty_ifsc TEXT,
                    resolved_entity_id TEXT,
                    category_id INTEGER NOT NULL,
                    category_name TEXT NOT NULL,
                    categorization_source TEXT NOT NULL,
                    confidence_score REAL NOT NULL,
                    top3_suggestions_json TEXT NOT NULL,
                    is_suspense INTEGER NOT NULL DEFAULT 0,
                    tally_voucher_type TEXT,
                    tally_ledger_name TEXT,
                    anomaly_flags_json TEXT NOT NULL,
                    is_duplicate_ignored INTEGER NOT NULL DEFAULT 0,
                    source_account_number TEXT,
                    source_holder_name TEXT,
                    FOREIGN KEY (case_id) REFERENCES cases(case_id) ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS idx_tx_case ON transactions(case_id);
                CREATE INDEX IF NOT EXISTS idx_tx_stmt ON transactions(statement_id);

                CREATE TABLE IF NOT EXISTS entities (
                    entity_id TEXT PRIMARY KEY,
                    case_id TEXT NOT NULL,
                    canonical_name TEXT NOT NULL,
                    aliases_json TEXT NOT NULL,
                    linked_accounts_json TEXT NOT NULL,
                    linked_vpas_json TEXT NOT NULL,
                    has_statement INTEGER NOT NULL DEFAULT 0,
                    is_moved_to_misc INTEGER NOT NULL DEFAULT 0,
                    total_credited REAL NOT NULL DEFAULT 0,
                    total_debited REAL NOT NULL DEFAULT 0,
                    tx_count INTEGER NOT NULL DEFAULT 0,
                    is_system INTEGER NOT NULL DEFAULT 0,
                    FOREIGN KEY (case_id) REFERENCES cases(case_id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS user_rules (
                    rule_id TEXT PRIMARY KEY,
                    pattern TEXT NOT NULL,
                    direction TEXT NOT NULL,
                    category_id INTEGER NOT NULL,
                    custom_ledger_name TEXT,
                    created_at TEXT NOT NULL,
                    case_id TEXT
                );

                CREATE TABLE IF NOT EXISTS user_rule_exclusions (
                    rule_id TEXT NOT NULL,
                    tx_id TEXT NOT NULL,
                    PRIMARY KEY (rule_id, tx_id)
                );

                CREATE TABLE IF NOT EXISTS manual_merges (
                    case_id TEXT NOT NULL,
                    alias_name TEXT NOT NULL,
                    canonical_target TEXT NOT NULL,
                    PRIMARY KEY(case_id, alias_name)
                );

                CREATE TABLE IF NOT EXISTS layering_misc (
                    case_id TEXT NOT NULL,
                    entity_id TEXT NOT NULL,
                    PRIMARY KEY(case_id, entity_id)
                );

                CREATE TABLE IF NOT EXISTS draw_templates (
                    template_id TEXT PRIMARY KEY,
                    bank_code TEXT NOT NULL,
                    template_name TEXT NOT NULL,
                    table_bbox_json TEXT NOT NULL,
                    column_splits_json TEXT NOT NULL,
                    column_names_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS audit_logs (
                    log_id TEXT PRIMARY KEY,
                    case_id TEXT,
                    actor TEXT NOT NULL,
                    action TEXT NOT NULL,
                    details_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    prev_hash TEXT,
                    row_hash TEXT
                );

                CREATE TABLE IF NOT EXISTS llm_settings (
                    setting_key TEXT PRIMARY KEY,
                    setting_value_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS statement_provenance (
                    provenance_id TEXT PRIMARY KEY,
                    statement_id TEXT NOT NULL,
                    case_id TEXT NOT NULL,
                    source_filename TEXT NOT NULL,
                    sha256 TEXT NOT NULL,
                    file_size_bytes INTEGER,
                    parser_version TEXT NOT NULL,
                    analyst TEXT NOT NULL,
                    ingested_at TEXT NOT NULL,
                    details_json TEXT NOT NULL DEFAULT '{}'
                );

                CREATE TABLE IF NOT EXISTS ingest_file_results (
                    result_id TEXT PRIMARY KEY,
                    batch_id TEXT NOT NULL,
                    case_id TEXT NOT NULL,
                    filename TEXT NOT NULL,
                    sha256 TEXT,
                    file_size_bytes INTEGER,
                    status TEXT NOT NULL,
                    statement_id TEXT,
                    transactions INTEGER NOT NULL DEFAULT 0,
                    error TEXT,
                    error_type TEXT,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS entity_decisions (
                    case_id TEXT NOT NULL,
                    seq INTEGER NOT NULL,
                    decision_id TEXT PRIMARY KEY,
                    decision_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS entity_decision_history (
                    history_id TEXT PRIMARY KEY,
                    case_id TEXT NOT NULL,
                    action TEXT NOT NULL,
                    decision_id TEXT,
                    decision_json TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS layering_misc_paths (
                    case_id TEXT NOT NULL,
                    source_key TEXT NOT NULL,
                    path_key TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(case_id, source_key, path_key)
                );

                CREATE TABLE IF NOT EXISTS case_settings (
                    case_id TEXT NOT NULL,
                    setting_key TEXT NOT NULL,
                    setting_value_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(case_id, setting_key)
                );

                CREATE INDEX IF NOT EXISTS idx_stmt_case ON statements(case_id);
                CREATE INDEX IF NOT EXISTS idx_tx_case_stmt ON transactions(case_id, statement_id, row_index);
                CREATE INDEX IF NOT EXISTS idx_entdec_case ON entity_decisions(case_id, seq);
                CREATE INDEX IF NOT EXISTS idx_entdec_hist_case ON entity_decision_history(case_id, created_at);
                CREATE INDEX IF NOT EXISTS idx_stmt_case_sha ON statements(case_id, sha256_hash);
                CREATE INDEX IF NOT EXISTS idx_tx_case_date ON transactions(case_id, txn_date, row_index);
                CREATE INDEX IF NOT EXISTS idx_tx_entity ON transactions(case_id, resolved_entity_id);
                CREATE INDEX IF NOT EXISTS idx_tx_category ON transactions(case_id, category_id);
                CREATE INDEX IF NOT EXISTS idx_tx_suspense ON transactions(case_id, is_suspense);
                CREATE INDEX IF NOT EXISTS idx_ent_case ON entities(case_id);
                CREATE INDEX IF NOT EXISTS idx_audit_case ON audit_logs(case_id, created_at);
                CREATE INDEX IF NOT EXISTS idx_prov_stmt ON statement_provenance(statement_id);
                CREATE INDEX IF NOT EXISTS idx_prov_case ON statement_provenance(case_id, ingested_at);
                CREATE INDEX IF NOT EXISTS idx_ingest_case ON ingest_file_results(case_id, created_at);
                """
            )
            self._migrate(conn)
            conn.executescript(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS uq_user_rules_scope
                    ON user_rules(pattern, direction, IFNULL(case_id, ''));

                CREATE TRIGGER IF NOT EXISTS trg_audit_no_update BEFORE UPDATE ON audit_logs
                BEGIN SELECT RAISE(ABORT, 'audit_logs is append-only'); END;
                CREATE TRIGGER IF NOT EXISTS trg_audit_no_delete BEFORE DELETE ON audit_logs
                BEGIN SELECT RAISE(ABORT, 'audit_logs is append-only'); END;
                CREATE TRIGGER IF NOT EXISTS trg_audit_chain BEFORE INSERT ON audit_logs
                WHEN NEW.row_hash IS NULL OR length(NEW.row_hash) != 64
                  OR NEW.prev_hash IS NOT COALESCE(
                       (SELECT row_hash FROM audit_logs ORDER BY rowid DESC LIMIT 1),
                       '0000000000000000000000000000000000000000000000000000000000000000')
                BEGIN SELECT RAISE(ABORT, 'audit_logs insert breaks the hash chain'); END;

                CREATE TRIGGER IF NOT EXISTS trg_entdec_hist_no_update BEFORE UPDATE ON entity_decision_history
                BEGIN SELECT RAISE(ABORT, 'entity_decision_history is append-only'); END;
                CREATE TRIGGER IF NOT EXISTS trg_entdec_hist_no_delete BEFORE DELETE ON entity_decision_history
                BEGIN SELECT RAISE(ABORT, 'entity_decision_history is append-only'); END;

                CREATE TRIGGER IF NOT EXISTS trg_prov_no_update BEFORE UPDATE ON statement_provenance
                BEGIN SELECT RAISE(ABORT, 'statement_provenance is append-only'); END;
                CREATE TRIGGER IF NOT EXISTS trg_prov_no_delete BEFORE DELETE ON statement_provenance
                BEGIN SELECT RAISE(ABORT, 'statement_provenance is append-only'); END;

                CREATE TRIGGER IF NOT EXISTS trg_stmt_identity_immutable
                BEFORE UPDATE OF sha256_hash, source_filename, ingested_at, case_id ON statements
                WHEN NEW.sha256_hash IS NOT OLD.sha256_hash OR NEW.source_filename IS NOT OLD.source_filename
                  OR NEW.ingested_at IS NOT OLD.ingested_at OR NEW.case_id IS NOT OLD.case_id
                BEGIN SELECT RAISE(ABORT, 'statement evidence identity is immutable'); END;
                """
            )

    @staticmethod
    def _columns(conn: sqlite3.Connection, table: str) -> Set[str]:
        return {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}

    @staticmethod
    def _has_trigger(conn: sqlite3.Connection, name: str) -> bool:
        return conn.execute("SELECT 1 FROM sqlite_master WHERE type='trigger' AND name=?", (name,)).fetchone() is not None

    def _migrate(self, conn: sqlite3.Connection) -> None:
        """In-place, data-preserving migration of vaults created by earlier versions.

        Runs in one explicit transaction: in the sqlite3 module's legacy isolation mode DDL does not open a
        transaction, so without it a DROP TABLE would autocommit and a later failure would lose the rows it
        was rebuilding.
        """
        if conn.in_transaction:
            conn.commit()
        conn.execute("BEGIN IMMEDIATE")
        try:
            self._migrate_body(conn)
        except BaseException:
            conn.rollback()
            raise
        conn.commit()

    def _migrate_body(self, conn: sqlite3.Connection) -> None:
        if "revision" not in self._columns(conn, "cases"):
            conn.execute("ALTER TABLE cases ADD COLUMN revision INTEGER NOT NULL DEFAULT 0")
        if "is_system" not in self._columns(conn, "entities"):
            conn.execute("ALTER TABLE entities ADD COLUMN is_system INTEGER NOT NULL DEFAULT 0")
        # P1: statement PAN (NLP-09) and parsed card mask / cheque number (NLP-18/20).
        if "pan" not in self._columns(conn, "statements"):
            conn.execute("ALTER TABLE statements ADD COLUMN pan TEXT")
        tx_cols = self._columns(conn, "transactions")
        for col in ("card_number_masked", "cheque_number"):
            if col not in tx_cols:
                conn.execute(f"ALTER TABLE transactions ADD COLUMN {col} TEXT")
        # P2: per-row change revision for incremental analysis-cache refresh (BE-07).
        if "change_rev" not in tx_cols:
            conn.execute("ALTER TABLE transactions ADD COLUMN change_rev INTEGER NOT NULL DEFAULT 0")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_tx_case_change ON transactions(case_id, change_rev)")

        # user_rules: legacy table had UNIQUE(pattern, direction) and no case scope -> rebuild once.
        rule_sql = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='user_rules'").fetchone()[0]
        if "case_id" not in self._columns(conn, "user_rules") or "UNIQUE" in rule_sql.upper():
            cols = self._columns(conn, "user_rules")
            legacy = conn.execute(
                "SELECT pattern, direction, category_id, custom_ledger_name, created_at"
                + (", case_id" if "case_id" in cols else ", NULL AS case_id")
                + " FROM user_rules ORDER BY created_at ASC"
            ).fetchall()
            conn.execute("DROP TABLE user_rules")
            conn.execute(
                """
                CREATE TABLE user_rules (
                    rule_id TEXT PRIMARY KEY,
                    pattern TEXT NOT NULL,
                    direction TEXT NOT NULL,
                    category_id INTEGER NOT NULL,
                    custom_ledger_name TEXT,
                    created_at TEXT NOT NULL,
                    case_id TEXT
                )
                """
            )
            for r in legacy:
                pat, d = str(r["pattern"] or "").strip().lower(), str(r["direction"] or "").upper()
                try:
                    cat_id = int(r["category_id"])
                except (TypeError, ValueError):
                    cat_id = None
                if not pat or d not in ("CR", "DR", "BOTH") or cat_id is None:
                    _LOG.warning(
                        "Skipping malformed legacy user rule (pattern=%r, direction=%r, category_id=%r)",
                        r["pattern"], r["direction"], r["category_id"],
                    )
                    continue
                conn.execute(
                    "INSERT OR REPLACE INTO user_rules (rule_id, pattern, direction, category_id, custom_ledger_name, "
                    "created_at, case_id) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (_rule_id(pat, d, r["case_id"]), pat, d, cat_id, r["custom_ledger_name"],
                     r["created_at"], r["case_id"]),
                )

        # audit_logs: add and backfill the hash chain before the append-only triggers exist.
        cols = self._columns(conn, "audit_logs")
        if "prev_hash" not in cols:
            conn.execute("ALTER TABLE audit_logs ADD COLUMN prev_hash TEXT")
        if "row_hash" not in cols:
            conn.execute("ALTER TABLE audit_logs ADD COLUMN row_hash TEXT")
        if not self._has_trigger(conn, "trg_audit_no_update"):
            prev = GENESIS_HASH
            for r in conn.execute("SELECT rowid AS rid, * FROM audit_logs ORDER BY rowid ASC").fetchall():
                if r["row_hash"] and r["prev_hash"] == prev:
                    prev = r["row_hash"]
                    continue
                h = _audit_row_hash(prev, r["log_id"], r["case_id"], r["actor"], r["action"], r["details_json"], r["created_at"])
                conn.execute("UPDATE audit_logs SET prev_hash = ?, row_hash = ? WHERE rowid = ?", (prev, h, r["rid"]))
                prev = h

        # Statements ingested before provenance existed get a backfilled (clearly marked) custody row.
        missing = conn.execute(
            "SELECT s.* FROM statements s WHERE NOT EXISTS "
            "(SELECT 1 FROM statement_provenance p WHERE p.statement_id = s.statement_id)"
        ).fetchall()
        for s in missing:
            conn.execute(
                "INSERT INTO statement_provenance (provenance_id, statement_id, case_id, source_filename, sha256, "
                "file_size_bytes, parser_version, analyst, ingested_at, details_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (str(uuid.uuid4()), s["statement_id"], s["case_id"], s["source_filename"], s["sha256_hash"], None,
                 "unknown (ingested before provenance tracking)", "unknown", s["ingested_at"] or _utc_now(),
                 json.dumps({"backfilled": True, "backfilled_at": _utc_now()})),
            )

    # ------------------------------------------------------------------------------------------ revisions
    @staticmethod
    def _bump_revision(conn: sqlite3.Connection, case_ids: Iterable[Optional[str]]) -> None:
        ids = sorted({c for c in case_ids if c})
        if ids:
            conn.executemany("UPDATE cases SET revision = revision + 1 WHERE case_id = ?", [(c,) for c in ids])

    def get_case_revision(self, case_id: str) -> Optional[int]:
        """Monotonic counter bumped by every mutation of the case's data (analysis-cache key, BE-07)."""
        with self._lock, self._connect() as conn:
            r = conn.execute("SELECT revision FROM cases WHERE case_id = ?", (case_id,)).fetchone()
            return int(r["revision"]) if r else None

    def bump_case_revision(self, case_id: str) -> None:
        with self._lock, self._connect(write=True) as conn:
            self._bump_revision(conn, [case_id])

    # ------------------------------------------------------------------------------------------ audit log
    def _log_audit_conn(
        self, conn: sqlite3.Connection, case_id: Optional[str], action: str, details: Dict[str, Any], actor: str
    ) -> str:
        last = conn.execute("SELECT row_hash FROM audit_logs ORDER BY rowid DESC LIMIT 1").fetchone()
        prev = (last["row_hash"] if last else None) or GENESIS_HASH
        log_id, created = str(uuid.uuid4()), _utc_now()
        details_json = json.dumps(details, ensure_ascii=False, default=str, sort_keys=True)
        row_hash = _audit_row_hash(prev, log_id, case_id, actor, action, details_json, created)
        conn.execute(
            "INSERT INTO audit_logs (log_id, case_id, actor, action, details_json, created_at, prev_hash, row_hash) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (log_id, case_id, actor, action, details_json, created, prev, row_hash),
        )
        return row_hash

    def log_audit(self, case_id: Optional[str], action: str, details: Dict[str, Any], actor: str = DEFAULT_ACTOR) -> None:
        with self._lock, self._connect(write=True) as conn:
            self._log_audit_conn(conn, case_id, action, details, actor)

    def get_audit_logs(self, case_id: str, limit: int = 100) -> List[Dict[str, Any]]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM audit_logs WHERE case_id = ? ORDER BY rowid DESC LIMIT ?",
                (case_id, limit),
            ).fetchall()
            return [
                {
                    "log_id": r["log_id"],
                    "actor": r["actor"],
                    "action": r["action"],
                    "details": json.loads(r["details_json"]),
                    "created_at": r["created_at"],
                    "row_hash": r["row_hash"],
                }
                for r in rows
            ]

    def get_audit_details_by_action(self, case_id: str, action: str, limit: int = 1000) -> List[Dict[str, Any]]:
        """Details of a case's audit entries with one action, oldest first (e.g. OCR_AUTOCORRECT for ING-10)."""
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT details_json, created_at FROM audit_logs WHERE case_id = ? AND action = ? "
                "ORDER BY rowid ASC LIMIT ?",
                (case_id, action, int(limit)),
            ).fetchall()
        out: List[Dict[str, Any]] = []
        for r in rows:
            try:
                details = json.loads(r["details_json"])
            except (TypeError, ValueError):
                continue
            if isinstance(details, dict):
                out.append({**details, "_created_at": r["created_at"]})
        return out

    def verify_audit_chain(self) -> Dict[str, Any]:
        """Recompute the whole audit hash chain. Returns {ok, count, head, first_bad_log_id, reason}."""
        with self._lock, self._connect() as conn:
            prev, count = GENESIS_HASH, 0
            for r in conn.execute("SELECT * FROM audit_logs ORDER BY rowid ASC"):
                count += 1
                if r["prev_hash"] != prev:
                    return {"ok": False, "count": count, "head": prev, "first_bad_log_id": r["log_id"], "reason": "prev_hash"}
                expect = _audit_row_hash(prev, r["log_id"], r["case_id"], r["actor"], r["action"], r["details_json"], r["created_at"])
                if r["row_hash"] != expect:
                    return {"ok": False, "count": count, "head": prev, "first_bad_log_id": r["log_id"], "reason": "row_hash"}
                prev = expect
            return {"ok": True, "count": count, "head": prev, "first_bad_log_id": None, "reason": None}

    # ------------------------------------------------------------------------------------------ cases
    @staticmethod
    def _row_to_case(r: sqlite3.Row) -> InvestigationCase:
        return InvestigationCase(
            case_id=r["case_id"],
            title=r["title"],
            mode=WorkspaceMode(r["mode"]),
            agency_or_firm_name=r["agency_or_firm_name"],
            created_at=r["created_at"],
            similarity_threshold=float(r["similarity_threshold"]),
            confidence_threshold=float(r["confidence_threshold"]),
        )

    def list_cases(self) -> List[InvestigationCase]:
        with self._lock, self._connect() as conn:
            rows = conn.execute("SELECT * FROM cases ORDER BY created_at DESC").fetchall()
            return [self._row_to_case(r) for r in rows]

    def get_case(self, case_id: str) -> Optional[InvestigationCase]:
        with self._lock, self._connect() as conn:
            r = conn.execute("SELECT * FROM cases WHERE case_id = ?", (case_id,)).fetchone()
            return self._row_to_case(r) if r else None

    def save_case(self, case: InvestigationCase) -> None:
        """UPSERT (BE-21): updating an existing case never deletes and re-inserts it, so no cascade fires."""
        with self._lock, self._connect(write=True) as conn:
            conn.execute(
                """
                INSERT INTO cases
                (case_id, title, mode, agency_or_firm_name, created_at, similarity_threshold, confidence_threshold)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(case_id) DO UPDATE SET
                    title = excluded.title,
                    mode = excluded.mode,
                    agency_or_firm_name = excluded.agency_or_firm_name,
                    similarity_threshold = excluded.similarity_threshold,
                    confidence_threshold = excluded.confidence_threshold,
                    revision = cases.revision + 1
                """,
                (
                    case.case_id,
                    case.title,
                    case.mode.value,
                    case.agency_or_firm_name,
                    case.created_at,
                    case.similarity_threshold,
                    case.confidence_threshold,
                ),
            )

    def update_case_thresholds(
        self, case_id: str, similarity_threshold: Optional[float] = None, confidence_threshold: Optional[float] = None
    ) -> None:
        with self._lock, self._connect(write=True) as conn:
            if similarity_threshold is not None:
                conn.execute("UPDATE cases SET similarity_threshold = ? WHERE case_id = ?", (similarity_threshold, case_id))
            if confidence_threshold is not None:
                conn.execute("UPDATE cases SET confidence_threshold = ? WHERE case_id = ?", (confidence_threshold, case_id))
            self._bump_revision(conn, [case_id])

    # ------------------------------------------------------------------------------------------ statements
    @staticmethod
    def _stmt_params(s: BankAccountStatement) -> tuple:
        return (
            s.statement_id, s.case_id, s.source_filename, s.sha256_hash, s.bank_code, s.bank_name,
            s.account_number, s.ifsc_code, s.account_holder_name, s.account_type, s.period_start,
            s.period_end, float(s.opening_balance), float(s.closing_balance), s.total_transactions,
            s.balance_mismatch_count, s.duplicate_suspect_count, 1 if s.is_ocr_derived else 0,
            s.ingested_at or _utc_now(), getattr(s, "pan", None),
        )

    @staticmethod
    def _tx_params(t: NormalizedTransaction) -> tuple:
        return (
            t.tx_id, t.statement_id, t.case_id, t.row_index, t.txn_date, t.txn_timestamp, t.value_date,
            t.raw_narration, t.cheque_or_ref_no, t.utr_number, t.payment_channel, t.direction.value,
            float(t.debit), float(t.credit), float(t.running_balance), t.extracted_counterparty,
            t.counterparty_vpa_or_acc, t.counterparty_ifsc, t.resolved_entity_id, t.category_id,
            t.category_name, t.categorization_source, float(t.confidence_score),
            json.dumps(t.top3_suggestions), 1 if t.is_suspense else 0,
            t.tally_voucher_type.value if t.tally_voucher_type else None, t.tally_ledger_name,
            json.dumps([f.value for f in t.anomaly_flags]), 1 if t.is_duplicate_ignored else 0,
            t.source_account_number, t.source_holder_name,
            getattr(t, "card_number_masked", None), getattr(t, "cheque_number", None),
        )

    def _save_statement_conn(
        self, conn: sqlite3.Connection, statement: BankAccountStatement, transactions: List[NormalizedTransaction]
    ) -> None:
        conn.execute(_STMT_UPSERT_SQL, self._stmt_params(statement))
        self._upsert_transactions_conn(conn, transactions)

    def save_statement_and_transactions(
        self, statement: BankAccountStatement, transactions: List[NormalizedTransaction]
    ) -> None:
        with self._lock, self._connect(write=True) as conn:
            self._save_statement_conn(conn, statement, transactions)
            self._bump_revision(conn, [statement.case_id])

    def upsert_transactions(self, transactions: List[NormalizedTransaction]) -> None:
        if not transactions:
            return
        with self._lock, self._connect(write=True) as conn:
            self._upsert_transactions_conn(conn, transactions)
            self._bump_revision(conn, {t.case_id for t in transactions})

    def _upsert_transactions_conn(self, conn: sqlite3.Connection, transactions: Sequence[NormalizedTransaction]) -> None:
        revs: Dict[Optional[str], int] = {}
        for cid in {t.case_id for t in transactions}:
            r = conn.execute("SELECT revision FROM cases WHERE case_id = ?", (cid,)).fetchone()
            revs[cid] = (int(r[0]) if r else 0) + 1
        conn.executemany(_TX_UPSERT_SQL, [self._tx_params(t) + (revs[t.case_id],) for t in transactions])

    @staticmethod
    def _row_to_stmt(r: sqlite3.Row) -> BankAccountStatement:
        return BankAccountStatement(
            statement_id=r["statement_id"],
            case_id=r["case_id"],
            source_filename=r["source_filename"],
            sha256_hash=r["sha256_hash"],
            bank_code=r["bank_code"],
            bank_name=r["bank_name"],
            account_number=r["account_number"],
            ifsc_code=r["ifsc_code"],
            account_holder_name=r["account_holder_name"],
            account_type=r["account_type"],
            period_start=r["period_start"],
            period_end=r["period_end"],
            opening_balance=Decimal(f"{r['opening_balance']:.2f}"),
            closing_balance=Decimal(f"{r['closing_balance']:.2f}"),
            total_transactions=int(r["total_transactions"]),
            balance_mismatch_count=int(r["balance_mismatch_count"]),
            duplicate_suspect_count=int(r["duplicate_suspect_count"]),
            is_ocr_derived=bool(r["is_ocr_derived"]),
            ingested_at=r["ingested_at"],
            pan=r["pan"],
        )

    def get_statements(self, case_id: str) -> List[BankAccountStatement]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM statements WHERE case_id = ? ORDER BY ingested_at ASC, rowid ASC", (case_id,)
            ).fetchall()
            return [self._row_to_stmt(r) for r in rows]

    def find_statements_by_sha(self, case_id: str, sha256_hashes: Iterable[str]) -> Dict[str, BankAccountStatement]:
        """Existing statements of the case keyed by SHA-256 (duplicate-upload detection, BE-12)."""
        wanted = sorted({h.lower() for h in sha256_hashes if h})
        if not wanted:
            return {}
        with self._lock, self._connect() as conn:
            return self._find_by_sha_conn(conn, case_id, wanted)

    def _find_by_sha_conn(self, conn: sqlite3.Connection, case_id: str, wanted: Sequence[str]) -> Dict[str, BankAccountStatement]:
        out: Dict[str, BankAccountStatement] = {}
        for i in range(0, len(wanted), 500):
            chunk = list(wanted[i:i + 500])
            rows = conn.execute(
                f"SELECT * FROM statements WHERE case_id = ? AND lower(sha256_hash) IN ({','.join('?' * len(chunk))})",
                [case_id, *chunk],
            ).fetchall()
            for r in rows:
                out.setdefault(r["sha256_hash"].lower(), self._row_to_stmt(r))
        return out

    # ------------------------------------------------------------------------------------------ transactions
    # Decoding caches (BE-07): amounts, flag lists and suggestion lists repeat heavily across rows, so each
    # distinct stored value is converted once. Cached values are immutable (Decimal, tuples of enums, the JSON
    # text); every transaction still receives its own list objects.
    _DEC_CACHE: Dict[float, Decimal] = {}
    _FLAGS_CACHE: Dict[str, Tuple[AnomalyFlag, ...]] = {}
    _TOP3_CACHE: Dict[str, Tuple[Dict[str, Any], ...]] = {}
    _DIRECTION_BY_VALUE = {d.value: d for d in TxDirection}
    _VOUCHER_BY_VALUE = {v.value: v for v in VoucherType}
    _DECODE_CACHE_MAX = 200_000

    @classmethod
    def _dec(cls, v: Any) -> Decimal:
        d = cls._DEC_CACHE.get(v)
        if d is None:
            d = Decimal(f"{v:.2f}")
            if len(cls._DEC_CACHE) >= cls._DECODE_CACHE_MAX:
                cls._DEC_CACHE.clear()
            cls._DEC_CACHE[v] = d
        return d

    @classmethod
    def _flags(cls, raw: str) -> List[AnomalyFlag]:
        f = cls._FLAGS_CACHE.get(raw)
        if f is None:
            f = tuple(AnomalyFlag(x) for x in json.loads(raw))
            if len(cls._FLAGS_CACHE) >= 4096:
                cls._FLAGS_CACHE.clear()
            cls._FLAGS_CACHE[raw] = f
        return list(f)

    @classmethod
    def _top3(cls, raw: str) -> List[Dict[str, Any]]:
        t = cls._TOP3_CACHE.get(raw)
        if t is None:
            t = tuple(json.loads(raw))
            if len(cls._TOP3_CACHE) >= cls._DECODE_CACHE_MAX:
                cls._TOP3_CACHE.clear()
            cls._TOP3_CACHE[raw] = t
        return list(t)  # suggestion dicts are shared read-only; callers replace the list, never mutate a dict

    @classmethod
    def _row_to_tx(cls, r: sqlite3.Row) -> NormalizedTransaction:
        vt = r["tally_voucher_type"]
        return NormalizedTransaction(
            tx_id=r["tx_id"],
            statement_id=r["statement_id"],
            case_id=r["case_id"],
            row_index=int(r["row_index"]),
            txn_date=r["txn_date"],
            txn_timestamp=r["txn_timestamp"],
            value_date=r["value_date"],
            raw_narration=r["raw_narration"],
            cheque_or_ref_no=r["cheque_or_ref_no"],
            utr_number=r["utr_number"],
            payment_channel=r["payment_channel"],
            direction=cls._DIRECTION_BY_VALUE.get(r["direction"]) or TxDirection(r["direction"]),
            debit=cls._dec(r["debit"]),
            credit=cls._dec(r["credit"]),
            running_balance=cls._dec(r["running_balance"]),
            extracted_counterparty=r["extracted_counterparty"],
            counterparty_vpa_or_acc=r["counterparty_vpa_or_acc"],
            counterparty_ifsc=r["counterparty_ifsc"],
            resolved_entity_id=r["resolved_entity_id"],
            category_id=int(r["category_id"]),
            category_name=r["category_name"],
            categorization_source=r["categorization_source"],
            confidence_score=float(r["confidence_score"]),
            top3_suggestions=cls._top3(r["top3_suggestions_json"]),
            is_suspense=bool(r["is_suspense"]),
            tally_voucher_type=(cls._VOUCHER_BY_VALUE.get(vt) or VoucherType(vt)) if vt else None,
            tally_ledger_name=r["tally_ledger_name"],
            anomaly_flags=cls._flags(r["anomaly_flags_json"]),
            is_duplicate_ignored=bool(r["is_duplicate_ignored"]),
            source_account_number=r["source_account_number"],
            source_holder_name=r["source_holder_name"],
            card_number_masked=r["card_number_masked"],
            cheque_number=r["cheque_number"],
        )

    def get_transactions(self, case_id: str) -> List[NormalizedTransaction]:
        return self.get_transactions_with_order_keys(case_id)[0]

    def get_transactions_with_order_keys(
        self, case_id: str, changed_since: Optional[int] = None
    ) -> Tuple[List[NormalizedTransaction], List[tuple], int]:
        """Transactions in case order (txn_date, row_index, rowid) with their order keys and the total row count
        of the case. ``changed_since`` returns only rows written after that case revision (BE-07)."""
        sql = "SELECT rowid AS _rowid, * FROM transactions WHERE case_id = ?"
        params: List[Any] = [case_id]
        if changed_since is not None:
            sql += " AND change_rev > ?"
            params.append(int(changed_since))
        sql += " ORDER BY txn_date ASC, row_index ASC, rowid ASC"
        with self._lock, self._connect() as conn:
            cur = conn.cursor()
            cur.row_factory = None  # plain tuples: name lookups on sqlite3.Row dominated decoding (BE-07)
            cur.execute(sql, params)
            cols = [d[0] for d in cur.description]
            rows = cur.fetchall()
            total = conn.execute("SELECT COUNT(*) FROM transactions WHERE case_id = ?", (case_id,)).fetchone()[0]
        i_date, i_row = cols.index("txn_date"), cols.index("row_index")
        keys = [(r[i_date], r[i_row], r[0]) for r in rows]
        return self._tuples_to_txs(cols, rows), keys, int(total)

    @classmethod
    def _tuples_to_txs(cls, cols: List[str], rows: List[tuple]) -> List[NormalizedTransaction]:
        ix = {c: i for i, c in enumerate(cols)}
        (i_id, i_stmt, i_case, i_row, i_date, i_ts, i_vd, i_narr, i_ref, i_utr, i_chan, i_dir, i_dr, i_cr, i_bal,
         i_cp, i_vpa, i_ifsc, i_ent, i_cat, i_catn, i_src, i_conf, i_top3, i_susp, i_vt, i_led, i_flags, i_ign,
         i_acc, i_holder, i_card, i_chq) = (ix[c] for c in (
            "tx_id", "statement_id", "case_id", "row_index", "txn_date", "txn_timestamp", "value_date",
            "raw_narration", "cheque_or_ref_no", "utr_number", "payment_channel", "direction", "debit", "credit",
            "running_balance", "extracted_counterparty", "counterparty_vpa_or_acc", "counterparty_ifsc",
            "resolved_entity_id", "category_id", "category_name", "categorization_source", "confidence_score",
            "top3_suggestions_json", "is_suspense", "tally_voucher_type", "tally_ledger_name", "anomaly_flags_json",
            "is_duplicate_ignored", "source_account_number", "source_holder_name", "card_number_masked",
            "cheque_number"))
        dirs, vts, dec, top3, flags = cls._DIRECTION_BY_VALUE, cls._VOUCHER_BY_VALUE, cls._dec, cls._top3, cls._flags
        out: List[NormalizedTransaction] = []
        append = out.append
        for r in rows:
            vt = r[i_vt]
            append(NormalizedTransaction(
                tx_id=r[i_id], statement_id=r[i_stmt], case_id=r[i_case], row_index=int(r[i_row]),
                txn_date=r[i_date], txn_timestamp=r[i_ts], value_date=r[i_vd], raw_narration=r[i_narr],
                cheque_or_ref_no=r[i_ref], utr_number=r[i_utr], payment_channel=r[i_chan],
                direction=dirs.get(r[i_dir]) or TxDirection(r[i_dir]),
                debit=dec(r[i_dr]), credit=dec(r[i_cr]), running_balance=dec(r[i_bal]),
                extracted_counterparty=r[i_cp], counterparty_vpa_or_acc=r[i_vpa], counterparty_ifsc=r[i_ifsc],
                resolved_entity_id=r[i_ent], category_id=int(r[i_cat]), category_name=r[i_catn],
                categorization_source=r[i_src], confidence_score=float(r[i_conf]), top3_suggestions=top3(r[i_top3]),
                is_suspense=bool(r[i_susp]),
                tally_voucher_type=(vts.get(vt) or VoucherType(vt)) if vt else None,
                tally_ledger_name=r[i_led], anomaly_flags=flags(r[i_flags]), is_duplicate_ignored=bool(r[i_ign]),
                source_account_number=r[i_acc], source_holder_name=r[i_holder], card_number_masked=r[i_card],
                cheque_number=r[i_chq],
            ))
        return out

    def get_transactions_by_ids(self, case_id: str, tx_ids: Iterable[str]) -> List[NormalizedTransaction]:
        ids = list(dict.fromkeys(tx_ids))
        out: List[NormalizedTransaction] = []
        with self._lock, self._connect() as conn:
            for i in range(0, len(ids), 500):
                chunk = ids[i:i + 500]
                rows = conn.execute(
                    f"SELECT * FROM transactions WHERE case_id = ? AND tx_id IN ({','.join('?' * len(chunk))})",
                    [case_id, *chunk],
                ).fetchall()
                out.extend(self._row_to_tx(r) for r in rows)
        return out

    @staticmethod
    def _tx_filter_sql(case_id: str, filters: Optional[Dict[str, Any]]) -> Tuple[str, List[Any]]:
        f = dict(filters or {})
        where, params = ["case_id = ?"], [case_id]
        q = str(f.get("q") or "").strip()
        if q:
            like = "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
            where.append(
                "(raw_narration LIKE ? ESCAPE '\\' OR IFNULL(extracted_counterparty,'') LIKE ? ESCAPE '\\' "
                "OR IFNULL(utr_number,'') LIKE ? ESCAPE '\\' OR IFNULL(source_holder_name,'') LIKE ? ESCAPE '\\')"
            )
            params += [like] * 4
        if f.get("category_id") not in (None, ""):
            where.append("category_id = ?")
            params.append(int(f["category_id"]))
        if f.get("is_suspense") is not None:
            where.append("is_suspense = ?")
            params.append(1 if f["is_suspense"] else 0)
        if f.get("flagged"):
            where.append(_FLAGGED_SQL)
        if not f.get("include_ignored", True):
            where.append("is_duplicate_ignored = 0")
        for key, col in (("statement_id", "statement_id"), ("entity_id", "resolved_entity_id")):
            if f.get(key):
                where.append(f"{col} = ?")
                params.append(str(f[key]))
        if f.get("channel"):
            # Internal code (TRANSFER) or SRS name (INTERNAL_TRANSFER) both match (NLP-24).
            ch = str(f["channel"]).strip().upper()
            where.append("(payment_channel = ? OR srs_channel(payment_channel) = ?)")
            params += [ch, ch]
        if f.get("direction"):
            where.append("direction = ?")
            params.append(str(f["direction"]).upper())
        if f.get("date_from"):
            where.append("txn_date >= ?")
            params.append(str(f["date_from"]))
        if f.get("date_to"):
            where.append("txn_date <= ?")
            params.append(str(f["date_to"]))
        if f.get("amount_min") is not None:
            where.append("MAX(debit, credit) >= ?")
            params.append(float(f["amount_min"]))
        if f.get("amount_max") is not None:
            where.append("MAX(debit, credit) <= ?")
            params.append(float(f["amount_max"]))
        if f.get("tx_ids") is not None:
            ids = list(f["tx_ids"])
            if not ids:
                where.append("0")
            else:
                where.append(f"tx_id IN ({','.join('?' * len(ids))})")
                params += [str(i) for i in ids]
        return " AND ".join(where), params

    def query_transactions(
        self,
        case_id: str,
        offset: int = 0,
        limit: int = 200,
        filters: Optional[Dict[str, Any]] = None,
        sort: Optional[str] = None,
        order: Optional[str] = None,
    ) -> Tuple[List[NormalizedTransaction], int]:
        """One page of a case's transactions plus the total matching count (BE-08).

        filters: q (narration/counterparty/UTR/holder substring), category_id, is_suspense, flagged (any
        anomaly other than VALID), include_ignored, statement_id, entity_id, channel (internal or SRS name),
        direction, date_from, date_to, amount_min, amount_max (on MAX(debit, credit)) and tx_ids (list, at most
        MAX_TX_IDS). ``sort`` is a TX_SORT_COLUMNS key (default txn_date) and ``order`` 'asc'/'desc'; ties
        break on txn_date, row_index, tx_id. Raises ValueError for an unknown sort key or order.
        """
        sort_key = (sort or "txn_date").strip()
        if sort_key not in TX_SORT_COLUMNS:
            raise ValueError(f"Invalid sort field {sort_key!r}; allowed: {', '.join(sorted(TX_SORT_COLUMNS))}")
        order_key = (order or "asc").strip().lower()
        if order_key not in ("asc", "desc"):
            raise ValueError("Invalid order; use 'asc' or 'desc'")
        ids = (filters or {}).get("tx_ids")
        if ids is not None and len(list(ids)) > MAX_TX_IDS:
            raise ValueError(f"Too many tx_ids ({len(list(ids))}); the limit is {MAX_TX_IDS}")
        where, params = self._tx_filter_sql(case_id, filters)
        direction = "DESC" if order_key == "desc" else "ASC"
        if sort_key in ("txn_date", "date"):
            order_sql = f"txn_date {direction}, row_index {direction}, tx_id {direction}"
        else:
            order_sql = (f"{TX_SORT_COLUMNS[sort_key]} {direction}, txn_date {direction}, "
                         f"row_index {direction}, tx_id {direction}")
        with self._lock, self._connect() as conn:
            total = int(conn.execute(f"SELECT COUNT(*) FROM transactions WHERE {where}", params).fetchone()[0])
            rows = conn.execute(
                f"SELECT * FROM transactions WHERE {where} ORDER BY {order_sql} LIMIT ? OFFSET ?",
                [*params, int(limit), int(offset)],
            ).fetchall()
            return [self._row_to_tx(r) for r in rows], total

    def get_user_labelled_transactions(self, limit: int = 20000) -> List[NormalizedTransaction]:
        """Analyst-labelled rows (categorization_source USER_RULE, not suspense) across all cases, newest
        cases first, for the categoriser's ML tier (NLP-15)."""
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM transactions WHERE categorization_source = 'USER_RULE' AND is_suspense = 0 "
                "AND is_duplicate_ignored = 0 ORDER BY rowid DESC LIMIT ?",
                (int(limit),),
            ).fetchall()
            return [self._row_to_tx(r) for r in rows]

    def transaction_aggregates(self, case_id: str) -> Dict[str, Any]:
        """Case-level counts and totals computed in SQL (no row materialisation)."""
        with self._lock, self._connect() as conn:
            r = conn.execute(
                f"""
                SELECT COUNT(*) AS n,
                       SUM(CASE WHEN is_suspense = 1 AND is_duplicate_ignored = 0 THEN 1 ELSE 0 END) AS suspense,
                       SUM(CASE WHEN {_FLAGGED_SQL} THEN 1 ELSE 0 END) AS flagged,
                       SUM(CASE WHEN is_duplicate_ignored = 0 THEN credit ELSE 0 END) AS credits,
                       SUM(CASE WHEN is_duplicate_ignored = 0 THEN debit ELSE 0 END) AS debits,
                       SUM(is_duplicate_ignored) AS ignored
                FROM transactions WHERE case_id = ?
                """,
                (case_id,),
            ).fetchone()
            return {
                "transactions_count": int(r["n"] or 0),
                "suspense_count": int(r["suspense"] or 0),
                "anomaly_count": int(r["flagged"] or 0),
                "total_credits": round(float(r["credits"] or 0.0), 2),
                "total_debits": round(float(r["debits"] or 0.0), 2),
                "duplicates_ignored": int(r["ignored"] or 0),
            }

    # ------------------------------------------------------------------------------------------ entities
    def _replace_entities_conn(self, conn: sqlite3.Connection, case_id: str, entities: List[ResolvedEntity]) -> None:
        conn.execute("DELETE FROM entities WHERE case_id = ?", (case_id,))
        conn.executemany(
            """
            INSERT INTO entities (
                entity_id, case_id, canonical_name, aliases_json, linked_accounts_json,
                linked_vpas_json, has_statement, is_moved_to_misc, total_credited, total_debited, tx_count, is_system
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    e.entity_id,
                    e.case_id,
                    e.canonical_name,
                    json.dumps(e.aliases),
                    json.dumps(e.linked_account_numbers),
                    json.dumps(e.linked_vpas),
                    1 if e.has_statement else 0,
                    1 if e.is_moved_to_misc else 0,
                    float(e.total_credited),
                    float(e.total_debited),
                    e.tx_count,
                    1 if getattr(e, "is_system", False) else 0,
                )
                for e in entities
            ],
        )

    def replace_entities(self, case_id: str, entities: List[ResolvedEntity]) -> None:
        with self._lock, self._connect(write=True) as conn:
            self._replace_entities_conn(conn, case_id, entities)
            self._bump_revision(conn, [case_id])

    def get_entities(self, case_id: str) -> List[ResolvedEntity]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM entities WHERE case_id = ? ORDER BY (total_credited + total_debited) DESC", (case_id,)
            ).fetchall()
            out: List[ResolvedEntity] = []
            for r in rows:
                ent = ResolvedEntity(
                    entity_id=r["entity_id"],
                    case_id=r["case_id"],
                    canonical_name=r["canonical_name"],
                    aliases=json.loads(r["aliases_json"]),
                    linked_account_numbers=json.loads(r["linked_accounts_json"]),
                    linked_vpas=json.loads(r["linked_vpas_json"]),
                    has_statement=bool(r["has_statement"]),
                    is_moved_to_misc=bool(r["is_moved_to_misc"]),
                    total_credited=Decimal(f"{r['total_credited']:.2f}"),
                    total_debited=Decimal(f"{r['total_debited']:.2f}"),
                    tx_count=int(r["tx_count"]),
                )
                # Runtime attribute (models.ResolvedEntity has no field for it); see nlp.entity_resolver.
                ent.is_system = bool(r["is_system"])  # type: ignore[attr-defined]
                out.append(ent)
            return out

    def save_manual_merge(self, case_id: str, alias_name: str, canonical_target: str) -> None:
        with self._lock, self._connect(write=True) as conn:
            conn.execute(
                "INSERT INTO manual_merges (case_id, alias_name, canonical_target) VALUES (?, ?, ?) "
                "ON CONFLICT(case_id, alias_name) DO UPDATE SET canonical_target = excluded.canonical_target",
                (case_id, alias_name.strip().upper(), canonical_target.strip().upper()),
            )
            self._bump_revision(conn, [case_id])

    def get_manual_merges(self, case_id: str) -> Dict[str, str]:
        with self._lock, self._connect() as conn:
            rows = conn.execute("SELECT alias_name, canonical_target FROM manual_merges WHERE case_id = ?", (case_id,)).fetchall()
            return {r["alias_name"]: r["canonical_target"] for r in rows}

    # ------------------------------------------------------------------------------------------ user rules
    def save_user_rule(
        self,
        pattern: str,
        direction: str,
        category_id: int,
        custom_ledger_name: Optional[str] = None,
        case_id: Optional[str] = None,
        excluded_tx_ids: Optional[Iterable[str]] = None,
    ) -> None:
        """Persist a learned rule. ``case_id=None`` is a global rule; otherwise it applies to that case only.
        ``excluded_tx_ids`` are rows the rule must never apply to (left out by the analyst, NLP-04)."""
        pat, d = pattern.strip().lower(), direction.upper()
        rid = _rule_id(pat, d, case_id)
        with self._lock, self._connect(write=True) as conn:
            if excluded_tx_ids:
                conn.executemany(
                    "INSERT OR IGNORE INTO user_rule_exclusions (rule_id, tx_id) VALUES (?, ?)",
                    [(rid, str(t)) for t in excluded_tx_ids],
                )
            conn.execute(
                """
                INSERT INTO user_rules (rule_id, pattern, direction, category_id, custom_ledger_name, created_at, case_id)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(rule_id) DO UPDATE SET
                    category_id = excluded.category_id,
                    custom_ledger_name = excluded.custom_ledger_name,
                    created_at = excluded.created_at
                """,
                (rid, pat, d, int(category_id), custom_ledger_name, _utc_now(), case_id),
            )

    def get_user_rule_exclusions(self) -> List[Tuple[str, str, Optional[str], List[str]]]:
        """(pattern, direction, case_id, [tx_id, ...]) for every rule with excluded rows."""
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT r.pattern, r.direction, r.case_id, x.tx_id FROM user_rule_exclusions x "
                "JOIN user_rules r ON r.rule_id = x.rule_id ORDER BY r.rule_id, x.tx_id"
            ).fetchall()
        grouped: Dict[Tuple[str, str, Optional[str]], List[str]] = {}
        for r in rows:
            grouped.setdefault((r["pattern"], r["direction"], r["case_id"]), []).append(r["tx_id"])
        return [(p, d, c, ids) for (p, d, c), ids in grouped.items()]

    def get_user_rules(self, case_id: Optional[str] = None) -> List[Tuple[str, str, int, Optional[str], Optional[str]]]:
        """(pattern, direction, category_id, custom_ledger_name, case_id) rows: all rules when ``case_id`` is None,
        otherwise the global rules plus that case's rules."""
        with self._lock, self._connect() as conn:
            if case_id is None:
                rows = conn.execute(
                    "SELECT pattern, direction, category_id, custom_ledger_name, case_id FROM user_rules ORDER BY created_at"
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT pattern, direction, category_id, custom_ledger_name, case_id FROM user_rules "
                    "WHERE case_id IS NULL OR case_id = ? ORDER BY created_at",
                    (case_id,),
                ).fetchall()
            return [
                (r["pattern"], r["direction"], int(r["category_id"]), r["custom_ledger_name"], r["case_id"]) for r in rows
            ]

    # ------------------------------------------------------------------------------------------ layering misc
    def toggle_misc_entity(self, case_id: str, entity_id: str, move_to_misc: bool = True) -> Set[str]:
        with self._lock, self._connect(write=True) as conn:
            if move_to_misc:
                conn.execute("INSERT OR IGNORE INTO layering_misc (case_id, entity_id) VALUES (?, ?)", (case_id, entity_id))
            else:
                conn.execute("DELETE FROM layering_misc WHERE case_id = ? AND entity_id = ?", (case_id, entity_id))
            self._bump_revision(conn, [case_id])
            rows = conn.execute("SELECT entity_id FROM layering_misc WHERE case_id = ?", (case_id,)).fetchall()
            return {r["entity_id"] for r in rows}

    def get_misc_entities(self, case_id: str) -> Set[str]:
        with self._lock, self._connect() as conn:
            rows = conn.execute("SELECT entity_id FROM layering_misc WHERE case_id = ?", (case_id,)).fetchall()
            return {r["entity_id"] for r in rows}

    # ------------------------------------------------------------------------------------------ per-branch misc (TF-06)
    def get_misc_paths(self, case_id: str, source_key: str) -> List[str]:
        """Path keys ('id>id>id') moved to Misc for one layering source ('<entity_id>' or 'tx:<root_tx_id>')."""
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT path_key FROM layering_misc_paths WHERE case_id = ? AND source_key = ? ORDER BY path_key",
                (case_id, source_key),
            ).fetchall()
            return [r["path_key"] for r in rows]

    def get_all_misc_paths(self, case_id: str) -> Dict[str, List[str]]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT source_key, path_key FROM layering_misc_paths WHERE case_id = ? ORDER BY source_key, path_key",
                (case_id,),
            ).fetchall()
        out: Dict[str, List[str]] = {}
        for r in rows:
            out.setdefault(r["source_key"], []).append(r["path_key"])
        return out

    def toggle_misc_path(
        self, case_id: str, source_key: str, path_key: str, move_to_misc: bool = True,
        audit: Optional[Tuple[str, Dict[str, Any]]] = None, actor: str = DEFAULT_ACTOR,
    ) -> List[str]:
        with self._lock, self._connect(write=True) as conn:
            if move_to_misc:
                conn.execute(
                    "INSERT OR IGNORE INTO layering_misc_paths (case_id, source_key, path_key, created_at) "
                    "VALUES (?, ?, ?, ?)", (case_id, source_key, path_key, _utc_now()),
                )
            else:
                conn.execute(
                    "DELETE FROM layering_misc_paths WHERE case_id = ? AND source_key = ? AND path_key = ?",
                    (case_id, source_key, path_key),
                )
            if audit:
                self._log_audit_conn(conn, case_id, audit[0], audit[1], actor)
            self._bump_revision(conn, [case_id])
            rows = conn.execute(
                "SELECT path_key FROM layering_misc_paths WHERE case_id = ? AND source_key = ? ORDER BY path_key",
                (case_id, source_key),
            ).fetchall()
            return [r["path_key"] for r in rows]

    # ------------------------------------------------------------------------------------------ entity decisions
    def get_entity_decisions(self, case_id: str) -> List[Dict[str, Any]]:
        """Active cluster-review decisions (NLP-11) in the order they were made."""
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT decision_json FROM entity_decisions WHERE case_id = ? ORDER BY seq ASC", (case_id,)
            ).fetchall()
            return [json.loads(r["decision_json"]) for r in rows]

    @staticmethod
    def _replace_entity_decisions_conn(conn: sqlite3.Connection, case_id: str, decisions: Sequence[Dict[str, Any]]) -> None:
        conn.execute("DELETE FROM entity_decisions WHERE case_id = ?", (case_id,))
        seen: Set[str] = set()
        rows = []
        for i, d in enumerate(decisions):
            did = str(d.get("decision_id") or "")
            if not did or did in seen:
                raise ValueError(f"Entity decision {i} has a missing or repeated decision_id")
            seen.add(did)
            rows.append((case_id, i, did, json.dumps(d, ensure_ascii=False, sort_keys=True, default=str),
                         str(d.get("created_at") or _utc_now())))
        conn.executemany(
            "INSERT INTO entity_decisions (case_id, seq, decision_id, decision_json, created_at) VALUES (?, ?, ?, ?, ?)",
            rows,
        )

    def replace_entity_decisions(self, case_id: str, decisions: Sequence[Dict[str, Any]]) -> None:
        """Atomically replace the case's active decision list and bump the revision."""
        with self._lock, self._connect(write=True) as conn:
            self._replace_entity_decisions_conn(conn, case_id, decisions)
            self._bump_revision(conn, [case_id])

    @staticmethod
    def _append_decision_history_conn(
        conn: sqlite3.Connection, case_id: str, action: str, decision: Optional[Dict[str, Any]], actor: str
    ) -> None:
        d = decision or {}
        conn.execute(
            "INSERT INTO entity_decision_history (history_id, case_id, action, decision_id, decision_json, actor, "
            "created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (str(uuid.uuid4()), case_id, action, d.get("decision_id"),
             json.dumps(d, ensure_ascii=False, sort_keys=True, default=str), actor, _utc_now()),
        )

    def get_entity_decision_history(self, case_id: str, limit: int = 500) -> List[Dict[str, Any]]:
        """Append-only log of every merge / split / reject / undo (newest first)."""
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM entity_decision_history WHERE case_id = ? ORDER BY rowid DESC LIMIT ?",
                (case_id, int(limit)),
            ).fetchall()
            return [
                {"history_id": r["history_id"], "action": r["action"], "decision_id": r["decision_id"],
                 "decision": json.loads(r["decision_json"]), "actor": r["actor"], "created_at": r["created_at"]}
                for r in rows
            ]

    # ------------------------------------------------------------------------------------------ case settings
    def get_case_setting(self, case_id: str, setting_key: str) -> Optional[Any]:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT setting_value_json FROM case_settings WHERE case_id = ? AND setting_key = ?",
                (case_id, setting_key),
            ).fetchone()
            if not row:
                return None
            try:
                return json.loads(row["setting_value_json"])
            except json.JSONDecodeError:
                return None

    @staticmethod
    def _save_case_setting_conn(conn: sqlite3.Connection, case_id: str, setting_key: str, value: Any) -> None:
        conn.execute(
            "INSERT INTO case_settings (case_id, setting_key, setting_value_json, updated_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(case_id, setting_key) DO UPDATE SET setting_value_json = excluded.setting_value_json, "
            "updated_at = excluded.updated_at",
            (case_id, setting_key, json.dumps(value, ensure_ascii=False, sort_keys=True), _utc_now()),
        )

    def save_case_setting(self, case_id: str, setting_key: str, value: Any) -> None:
        with self._lock, self._connect(write=True) as conn:
            self._save_case_setting_conn(conn, case_id, setting_key, value)
            self._bump_revision(conn, [case_id])

    # ------------------------------------------------------------------------------------------ draw templates
    def save_draw_template(self, tpl: DrawTemplate) -> None:
        with self._lock, self._connect(write=True) as conn:
            conn.execute(
                """
                INSERT INTO draw_templates (
                    template_id, bank_code, template_name, table_bbox_json,
                    column_splits_json, column_names_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(template_id) DO UPDATE SET
                    bank_code = excluded.bank_code,
                    template_name = excluded.template_name,
                    table_bbox_json = excluded.table_bbox_json,
                    column_splits_json = excluded.column_splits_json,
                    column_names_json = excluded.column_names_json
                """,
                (
                    tpl.template_id,
                    tpl.bank_code,
                    tpl.template_name,
                    json.dumps(tpl.table_bbox),
                    json.dumps(tpl.column_splits),
                    json.dumps(tpl.column_names),
                    tpl.created_at or _utc_now(),
                ),
            )

    def get_draw_templates(self) -> List[DrawTemplate]:
        with self._lock, self._connect() as conn:
            rows = conn.execute("SELECT * FROM draw_templates ORDER BY created_at DESC").fetchall()
            return [
                DrawTemplate(
                    template_id=r["template_id"],
                    bank_code=r["bank_code"],
                    template_name=r["template_name"],
                    table_bbox=json.loads(r["table_bbox_json"]),
                    column_splits=json.loads(r["column_splits_json"]),
                    column_names=json.loads(r["column_names_json"]),
                    created_at=r["created_at"],
                )
                for r in rows
            ]

    # ------------------------------------------------------------------------------------------ provenance
    @staticmethod
    def _insert_provenance_conn(conn: sqlite3.Connection, p: ProvenanceRecord) -> None:
        conn.execute(
            "INSERT INTO statement_provenance (provenance_id, statement_id, case_id, source_filename, sha256, "
            "file_size_bytes, parser_version, analyst, ingested_at, details_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                str(uuid.uuid4()), p.statement_id, p.case_id, p.source_filename, p.sha256.lower(), p.file_size_bytes,
                p.parser_version, p.analyst, p.ingested_at or _utc_now(),
                json.dumps(p.details, ensure_ascii=False, default=str, sort_keys=True),
            ),
        )

    def add_statement_provenance(self, record: ProvenanceRecord) -> None:
        with self._lock, self._connect(write=True) as conn:
            self._insert_provenance_conn(conn, record)

    def get_statement_provenance(self, case_id: str, statement_id: Optional[str] = None) -> List[Dict[str, Any]]:
        with self._lock, self._connect() as conn:
            if statement_id:
                rows = conn.execute(
                    "SELECT * FROM statement_provenance WHERE case_id = ? AND statement_id = ? ORDER BY rowid",
                    (case_id, statement_id),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM statement_provenance WHERE case_id = ? ORDER BY rowid", (case_id,)
                ).fetchall()
            return [
                {
                    "provenance_id": r["provenance_id"],
                    "statement_id": r["statement_id"],
                    "case_id": r["case_id"],
                    "source_filename": r["source_filename"],
                    "sha256": r["sha256"],
                    "file_size_bytes": r["file_size_bytes"],
                    "parser_version": r["parser_version"],
                    "analyst": r["analyst"],
                    "ingested_at": r["ingested_at"],
                    "details": json.loads(r["details_json"] or "{}"),
                }
                for r in rows
            ]

    # ------------------------------------------------------------------------------------------ ingest batch
    def commit_ingest_batch(self, batch: IngestBatch) -> str:
        """Write a whole ingest request atomically (BE-11): statements, rows, provenance, re-resolved rows,
        entities, per-file outcomes, audit entries and settings commit together or not at all.

        The duplicate-SHA check is repeated inside the write transaction, so two concurrent uploads of the same
        file cannot both be stored (raises ``ConflictError``). Returns the batch id.
        """
        batch_id = str(uuid.uuid4())
        now = _utc_now()
        with self._lock, self._connect(write=True) as conn:
            if conn.execute("SELECT 1 FROM cases WHERE case_id = ?", (batch.case_id,)).fetchone() is None:
                raise KeyError(f"Case '{batch.case_id}' not found")
            superseded = set(batch.superseded_statement_ids)
            hashes = [s.sha256_hash.lower() for s, _ in batch.statements]
            existing = self._find_by_sha_conn(conn, batch.case_id, sorted(set(hashes)))
            clash = [h for h, st in existing.items() if st.statement_id not in superseded]
            if clash:
                raise ConflictError(
                    "Statement file(s) already ingested in this case (SHA-256 "
                    + ", ".join(sorted(clash)) + "); nothing was written."
                )
            if len(set(hashes)) != len(hashes):
                raise ConflictError("The same statement file appears more than once in this upload.")
            for sid in superseded:
                # Only empty (zero-row) statements are ever superseded; their custody rows remain.
                conn.execute("DELETE FROM transactions WHERE statement_id = ? AND case_id = ?", (sid, batch.case_id))
                conn.execute("DELETE FROM statements WHERE statement_id = ? AND case_id = ?", (sid, batch.case_id))
            for stmt, txns in batch.statements:
                self._save_statement_conn(conn, stmt, txns)
            for p in batch.provenance:
                self._insert_provenance_conn(conn, p)
            if batch.updated_transactions:
                self._upsert_transactions_conn(conn, batch.updated_transactions)
            if batch.entities is not None:
                self._replace_entities_conn(conn, batch.case_id, batch.entities)
            conn.executemany(
                "INSERT INTO ingest_file_results (result_id, batch_id, case_id, filename, sha256, file_size_bytes, status, "
                "statement_id, transactions, error, error_type, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (str(uuid.uuid4()), batch_id, batch.case_id, fr.filename, fr.sha256, fr.file_size_bytes, fr.status,
                     fr.statement_id, int(fr.transactions), fr.error, fr.error_type, now)
                    for fr in batch.file_results
                ],
            )
            for key, value in batch.settings.items():
                self._save_setting_conn(conn, key, value)
            for action, details in batch.audit:
                self._log_audit_conn(conn, batch.case_id, action, {**details, "batch_id": batch_id}, batch.actor)
            self._bump_revision(conn, [batch.case_id])
        return batch_id

    def record_ingest_failure(self, case_id: str, file_results: List[IngestFileResult], reason: str, actor: str = DEFAULT_ACTOR) -> str:
        """Persist per-file outcomes of a batch that could not be committed (nothing else is written)."""
        batch_id = str(uuid.uuid4())
        now = _utc_now()
        with self._lock, self._connect(write=True) as conn:
            conn.executemany(
                "INSERT INTO ingest_file_results (result_id, batch_id, case_id, filename, sha256, file_size_bytes, status, "
                "statement_id, transactions, error, error_type, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (str(uuid.uuid4()), batch_id, case_id, fr.filename, fr.sha256, fr.file_size_bytes, fr.status,
                     None, 0, fr.error, fr.error_type, now)
                    for fr in file_results
                ],
            )
            self._log_audit_conn(
                conn, case_id, "INGEST_BATCH_REJECTED",
                {"batch_id": batch_id, "reason": reason, "files": [fr.filename for fr in file_results]}, actor,
            )
        return batch_id

    def get_ingest_results(self, case_id: str, limit: int = 500) -> List[Dict[str, Any]]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM ingest_file_results WHERE case_id = ? ORDER BY rowid DESC LIMIT ?", (case_id, limit)
            ).fetchall()
            return [
                {k: r[k] for k in ("batch_id", "filename", "sha256", "file_size_bytes", "status", "statement_id",
                                   "transactions", "error", "error_type", "created_at")}
                for r in rows
            ]

    def commit_case_update(
        self,
        case_id: str,
        statements: Optional[List[BankAccountStatement]] = None,
        transactions: Optional[List[NormalizedTransaction]] = None,
        entities: Optional[List[ResolvedEntity]] = None,
        audit: Optional[List[Tuple[str, Dict[str, Any]]]] = None,
        settings: Optional[Dict[str, Dict[str, Any]]] = None,
        actor: str = DEFAULT_ACTOR,
        entity_decisions: Optional[Sequence[Dict[str, Any]]] = None,
        decision_events: Optional[List[Tuple[str, Optional[Dict[str, Any]]]]] = None,
        case_settings: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Atomic multi-table update of one case (re-runs, merges, suspense resolution, cluster review).

        ``entity_decisions`` replaces the active decision list; ``decision_events`` [(action, decision)] are
        appended to the append-only decision history; ``case_settings`` upserts per-case settings."""
        with self._lock, self._connect(write=True) as conn:
            if entity_decisions is not None:
                self._replace_entity_decisions_conn(conn, case_id, entity_decisions)
            for action, decision in decision_events or []:
                self._append_decision_history_conn(conn, case_id, action, decision, actor)
            for key, value in (case_settings or {}).items():
                self._save_case_setting_conn(conn, case_id, key, value)
            for st in statements or []:
                conn.execute(_STMT_UPSERT_SQL, self._stmt_params(st))
            if transactions:
                self._upsert_transactions_conn(conn, transactions)
            if entities is not None:
                self._replace_entities_conn(conn, case_id, entities)
            for key, value in (settings or {}).items():
                self._save_setting_conn(conn, key, value)
            for action, details in audit or []:
                self._log_audit_conn(conn, case_id, action, details, actor)
            self._bump_revision(conn, [case_id])

    # ------------------------------------------------------------------------------------------ settings
    @staticmethod
    def _save_setting_conn(conn: sqlite3.Connection, setting_key: str, settings_dict: Dict[str, Any]) -> None:
        conn.execute(
            """
            INSERT INTO llm_settings (setting_key, setting_value_json, updated_at) VALUES (?, ?, ?)
            ON CONFLICT(setting_key) DO UPDATE SET
                setting_value_json = excluded.setting_value_json, updated_at = excluded.updated_at
            """,
            (setting_key, json.dumps(settings_dict, ensure_ascii=False), _utc_now()),
        )

    def save_llm_settings(self, settings_dict: Dict[str, Any], setting_key: str = "llm_config") -> None:
        with self._lock, self._connect(write=True) as conn:
            self._save_setting_conn(conn, setting_key, settings_dict)

    def get_llm_settings(self, setting_key: str = "llm_config") -> Dict[str, Any]:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT setting_value_json FROM llm_settings WHERE setting_key = ?",
                (setting_key,),
            ).fetchone()
            if not row:
                return {}
            try:
                parsed = json.loads(row["setting_value_json"])
                return parsed if isinstance(parsed, dict) else {}
            except json.JSONDecodeError:
                return {}
