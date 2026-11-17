"""Unified application service layer orchestrating storage, ingestion, NLP, CypherEdge, and CypherTrace."""

from __future__ import annotations

import copy
import datetime as dt
import getpass
import hashlib
import logging
import os
import re
import threading
import time
import uuid
from collections import OrderedDict
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Set, Tuple

from cyphersol import __version__
from cyphersol.demo_data import ensure_demo_cases_seeded
from cyphersol.edge.itr_compliance import generate_itr_compliance_report
from cyphersol.edge.loan_eligibility import compute_loan_eligibility_report
from cyphersol.edge.tally_exporter import TallyLedgerMap, build_tally_export, push_to_tally_http
from cyphersol.ingestion.anomaly_gate import CaseDuplicateIndex, find_duplicate_groups, mark_bulk_duplicates_ignored, run_anomaly_gate
from cyphersol.ingestion.parser import bulk_parse_statements
from cyphersol.jobs import INGEST_PHASE_WEIGHTS, JobManager, UploadStore
from cyphersol.ingestion.pdf_parser import (
    extract_pdf_data,
    generate_password_candidates,
    render_page_png,
    resolve_pdf_password,
    validate_draw_template,
)
from cyphersol.llm_agents import (
    AGENT_DEFINITIONS,
    PROVIDER_CATALOG,
    VALID_EXECUTION_MODES,
    VALID_PROVIDERS,
    HttpTransportFn,
    LLMConfig,
    LLMProviderClient,
    LLMProviderError,
    StageAgentOrchestrator,
    stage1_should_adopt,
)
from cyphersol.models import (
    BankAccountStatement,
    ConflictError,
    DrawTemplate,
    InvestigationCase,
    NormalizedTransaction,
    ResolvedEntity,
    WorkspaceMode,
)
from cyphersol.nlp.categorizer import (
    HybridTransactionCategorizer,
    apply_suspense_resolution,
    preview_suspense_propagation,
)
from cyphersol.nlp import entity_resolver as _er
from cyphersol.nlp.entity_resolver import is_system_entity, preview_clusters, resolve_case_entities
from cyphersol.nlp.narration_parser import (
    SRS_CHANNEL_EXTENSIONS,
    SRS_PAYMENT_CHANNELS,
    enrich_transaction_narration,
    srs_payment_channel,
)
from cyphersol.reporting import (
    PROFILE_EXPORT_FORMATS,
    export_audit_workbook_xlsx,
    export_entity_profile,
    export_forensic_dossier_html,
    export_forensic_dossier_pdf,
    export_itr_workbook_xlsx,
    export_tally_workbook_xlsx,
)
from cyphersol.storage import MAX_TX_IDS, TX_SORT_COLUMNS, CypherVault, IngestBatch, IngestFileResult, ProvenanceRecord
from cyphersol.taxonomy import CATEGORIES_52, INDIAN_BANKS, KNOWLEDGE_BASE_POINT_COUNT, knowledge_base_stats
from cyphersol.trace.bidirectional import analyze_bidirectional_flow, list_all_bidirectional_pairs
from cyphersol.trace.financial_profile import build_individual_financial_profile
from cyphersol.trace.graph_engine import (
    DEFAULT_MAX_DISPLAY_NODES,
    build_network_graph,
    graph_neighbourhood,
    shared_event_cache,
    slice_network_graph,
)
from cyphersol.trace.layering import (
    MAX_TRACE_DEPTH,
    PATH_SEPARATOR,
    build_layering_tree,
    expand_layering_node,
    misc_path_key,
)
from cyphersol.trace.loop_detector import (
    DEFAULT_CYCLE_LENGTH as LOOP_DEFAULT_CYCLE_LENGTH,
    DEFAULT_MAX_INSTANCES_PER_LOOP as LOOP_DEFAULT_MAX_INSTANCES,
    detect_circular_loops,
)
from cyphersol.trace.mule_scorer import evaluate_all_mule_accounts

_LOG = logging.getLogger("cyphersol.service")

ANALYST_ENV = "CYPHERSOL_ANALYST"
OVERVIEW_TX_PAGE = 500          # legacy: rows per embedded list when a caller asks for tx_limit (BE-08)
MAX_TX_PAGE = 2000              # hard cap for one /transactions page
ANALYSIS_CACHE_CASES = 4        # per-case analysis snapshots kept in memory (BE-07)
ANALYSIS_MEMO_ENTRIES = 48      # memoised analytics results per snapshot
EVENT_CACHE_ENTRIES = 16        # distinct transfer-event extractions (filter sets) kept per snapshot
GRAPH_DEFAULT_MAX_NODES = 400   # nodes in a graph view by default; the rest load via expand-on-demand (TG-04)
GRAPH_MAX_NODES_RANGE = (10, 5000)
GRAPH_SERVER_LAYOUT_MAX_NODES = 300  # 'auto' layout: server force layout up to this size, else client layout
GRAPH_LAYOUT_MODES = ("auto", "server", "seed", "none")
GRAPH_EDGE_TX_IDS = 50          # tx_ids listed per graph edge (tx_count is always exact)
GRAPH_EXPAND_LIMIT = (1, 500)
DUPLICATE_POLICIES = ("reject", "skip")
RULE_SCOPES = ("case", "global")
TALLY_MAP_SETTING = "tally_ledger_map"
MAX_LABELLED_EXAMPLES = 20000   # USER_RULE rows fed to the categoriser's ML tier (NLP-15)
DRAW_DPI_RANGE = (36, 300)
MAX_PATH_LEN = 64
# Password-hint keys accepted for the PDF password generator (PDF-13); values stay in memory only.
PASSWORD_HINT_KEYS = (
    "passwords", "password", "name", "holder_name", "account_holder", "customer_name", "dob", "date_of_birth",
    "pan", "account_number", "account_no", "account", "customer_id", "crn", "cif", "mobile", "phone", "bank",
)


def tx_to_json(t: NormalizedTransaction) -> Dict[str, Any]:
    """Transaction JSON for the API: the model dict plus the SRS FR-CAT-01 channel name (NLP-24)."""
    d = t.to_dict()
    d["payment_channel_srs"] = srs_payment_channel(t.payment_channel)
    return d


def clean_password_hints(hints: Any) -> Optional[Dict[str, Any]]:
    """Validate a password-hints object (PDF-13). Returns None for no hints; raises ValueError when malformed."""
    if hints in (None, {}, ""):
        return None
    if not isinstance(hints, dict):
        raise ValueError("password_hints must be an object")
    out: Dict[str, Any] = {}
    for k, v in hints.items():
        key = str(k).strip().lower()
        if key not in PASSWORD_HINT_KEYS:
            raise ValueError(f"Unknown password hint '{k}'; allowed: {', '.join(PASSWORD_HINT_KEYS)}")
        if v is None or v == "":
            continue
        if key in ("passwords",):
            if not isinstance(v, list) or not all(isinstance(x, str) for x in v) or len(v) > 32:
                raise ValueError("password_hints.passwords must be a list of at most 32 strings")
            out[key] = [x[:256] for x in v]
        elif isinstance(v, (str, int)):
            out[key] = str(v)[:256]
        else:
            raise ValueError(f"password_hints.{key} must be a string")
    return out or None

_PARSER_VERSION: Optional[str] = None


def parser_version() -> str:
    """Application version plus a fingerprint of the ingestion code, recorded in statement provenance."""
    global _PARSER_VERSION
    if _PARSER_VERSION is None:
        h = hashlib.sha256()
        ing_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ingestion")
        try:
            for name in sorted(os.listdir(ing_dir)):
                if name.endswith(".py"):
                    h.update(name.encode("utf-8"))
                    with open(os.path.join(ing_dir, name), "rb") as fh:
                        h.update(fh.read())
            _PARSER_VERSION = f"cyphersol-{__version__}+ingest.{h.hexdigest()[:12]}"
        except OSError:
            _PARSER_VERSION = f"cyphersol-{__version__}"
    return _PARSER_VERSION


def _default_analyst() -> str:
    name = os.environ.get(ANALYST_ENV, "").strip()
    if name:
        return name[:120]
    try:
        return getpass.getuser() or "local_operator"
    except Exception:  # noqa: BLE001 - getuser can fail in odd sandboxes
        return "local_operator"


class _CaseSnapshot:
    """Read-only view of one case at one revision, plus memoised analytics computed from it (BE-07)."""

    __slots__ = ("revision", "statements", "transactions", "entities", "misc_ids", "memo", "lock", "misc_paths",
                 "order_keys", "events")

    def __init__(
        self,
        revision: int,
        statements: List[BankAccountStatement],
        transactions: List[NormalizedTransaction],
        entities: List[ResolvedEntity],
        misc_ids: Set[str],
        order_keys: Optional[List[tuple]] = None,
    ) -> None:
        self.revision = revision
        self.statements = statements
        self.transactions = transactions
        self.order_keys: List[tuple] = order_keys or []  # (txn_date, row_index, rowid) per transaction
        self.entities = entities
        self.misc_ids = misc_ids
        self.misc_paths: Dict[str, List[str]] = {}
        self.memo: "OrderedDict[tuple, Any]" = OrderedDict()
        self.lock = threading.Lock()
        self.events: Dict[tuple, Any] = {}  # extracted transfer events shared by the analytics (BE-07)


def _tx_state(t: NormalizedTransaction) -> tuple:
    return (
        t.category_id, t.categorization_source, t.confidence_score, t.is_suspense,
        t.tally_ledger_name, t.tally_voucher_type, t.resolved_entity_id, t.is_duplicate_ignored,
    )


DRAW_COLUMN_FIELDS = ("date", "value_date", "narration", "ref", "debit", "credit", "amount", "type", "balance",
                      "ignore")


def validate_draw_column_names(column_names: List[str], n_splits: int) -> List[str]:
    """FR-ING-04: a draw-template field map must use canonical field names, map each column exactly once (apart
    from 'ignore'), match the divider count and include a date column and an amount column (debit/credit or a
    signed amount). Returns the normalised (lower-case, stripped) names; raises ValueError otherwise."""
    names = [str(c).strip().lower() for c in column_names]
    unknown = [c for c in names if c not in DRAW_COLUMN_FIELDS]
    if unknown:
        raise ValueError(
            f"column_names contains unknown field(s) {unknown}; allowed: {', '.join(DRAW_COLUMN_FIELDS)}"
        )
    dup = sorted({c for c in names if c != "ignore" and names.count(c) > 1})
    if dup:
        raise ValueError(f"column_names maps {dup} to more than one column")
    if len(names) != n_splits + 1:
        raise ValueError(
            f"column_names has {len(names)} name(s) but {n_splits} divider(s) make {n_splits + 1} column(s)"
        )
    if "date" not in names:
        raise ValueError("column_names must include a 'date' column")
    if not ({"debit", "credit", "amount"} & set(names)):
        raise ValueError("column_names must include a 'debit', 'credit' or 'amount' column")
    return names


class CypherApplicationService:
    """High-level application controller backing both the HTTP API and CLI."""

    def __init__(
        self,
        db_path: str,
        sample_dir: str,
        seed_demo: bool = True,
        llm_transport_fn: Optional[HttpTransportFn] = None,
        analyst: Optional[str] = None,
        warm_analysis: bool = False,
    ) -> None:
        """``warm_analysis=True`` precomputes the default analytics in the background after every ingest or
        mutation (see :meth:`schedule_warmup`); the HTTP server enables it, library callers and tests do not."""
        self.vault = CypherVault(db_path)
        self.sample_dir = sample_dir
        self.analyst = (analyst or _default_analyst()).strip() or "local_operator"
        self._llm_transport_fn = llm_transport_fn
        if seed_demo:
            ensure_demo_cases_seeded(self.vault, sample_dir)
        # Shared categoriser: holds the KB index and learned rules. Requests never mutate it; they work on
        # a cheap per-call copy carrying the case's threshold (BE-10).
        self.categorizer = HybridTransactionCategorizer()
        self._rules_lock = threading.Lock()
        self._refresh_rules()
        self._labelled_examples_used = 0
        self._refit_from_labelled_rows()
        self.llm_config = LLMConfig.from_dict(self.vault.get_llm_settings())
        self._locks_guard = threading.Lock()
        self._case_locks: Dict[str, threading.RLock] = {}
        self._cache_lock = threading.Lock()
        self._analysis_cache: "OrderedDict[str, _CaseSnapshot]" = OrderedDict()
        self.jobs = JobManager(workers=2)
        self.uploads = UploadStore()
        self.warm_analysis = bool(warm_analysis)

        # Persistent rotating file logging in data volume (logs/cyphersol.log)
        logs_dir = os.path.join(self.vault.data_dir, "logs")
        try:
            os.makedirs(logs_dir, exist_ok=True)
            log_file = os.path.join(logs_dir, "cyphersol.log")
            if not os.path.exists(log_file):
                with open(log_file, "a", encoding="utf-8"):
                    pass
            cypher_logger = logging.getLogger("cyphersol")
            if cypher_logger.level == logging.NOTSET:
                cypher_logger.setLevel(logging.INFO)
            from logging.handlers import RotatingFileHandler
            abs_log_file = os.path.abspath(log_file)
            has_this_rfh = any(
                isinstance(h, RotatingFileHandler) and getattr(h, "baseFilename", None) == abs_log_file
                for h in cypher_logger.handlers
            )
            if not has_this_rfh:
                rfh = RotatingFileHandler(log_file, maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8")
                rfh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
                cypher_logger.addHandler(rfh)
        except Exception as _e:
            _LOG.debug("Could not initialize file log handler: %s", _e)

    # ------------------------------------------------------------------------------------------ helpers
    def _case_lock(self, case_id: str) -> threading.RLock:
        """Per-case lock serialising read-modify-write sequences within this process (BE-10)."""
        with self._locks_guard:
            lock = self._case_locks.get(case_id)
            if lock is None:
                lock = self._case_locks[case_id] = threading.RLock()
            return lock

    def _require_case(self, case_id: str) -> InvestigationCase:
        case_obj = self.vault.get_case(case_id)
        if not case_obj:
            raise KeyError(f"Case '{case_id}' not found")
        return case_obj

    def _refresh_rules(self) -> None:
        with self._rules_lock:
            self.categorizer.load_user_rules(self.vault.get_user_rules())
            self.categorizer.set_rule_exclusions(self.vault.get_user_rule_exclusions())

    def _refit_from_labelled_rows(self) -> int:
        """Teach the ML tier the analyst-labelled rows (USER_RULE, not suspense) of every case (NLP-15).
        A no-op while there are none, so the KB + curated priors model stays as shipped."""
        rows = self.vault.get_user_labelled_transactions(limit=MAX_LABELLED_EXAMPLES)
        if not rows and not self._labelled_examples_used:
            return 0
        fit = getattr(self.categorizer, "fit_labelled_examples", None)
        if fit is None:
            return 0
        try:
            with self._rules_lock:
                used = int(fit([(t, int(t.category_id)) for t in rows]))
        except Exception:  # noqa: BLE001 - a bad label must never stop the service
            _LOG.exception("Refitting the categoriser on labelled rows failed")
            return 0
        self._labelled_examples_used = used
        return used

    # ------------------------------------------------------------------------------------------ entity helpers
    def _resolve_entities(
        self,
        case_id: str,
        statements: List[BankAccountStatement],
        txns: List[NormalizedTransaction],
        similarity_threshold: float,
        manual_merges: Optional[Dict[str, str]] = None,
        decisions: Optional[Sequence[Dict[str, Any]]] = None,
        previous_entities: Optional[Sequence[ResolvedEntity]] = None,
        diagnostics: Optional[Dict[str, Any]] = None,
    ) -> Tuple[List[ResolvedEntity], List[NormalizedTransaction]]:
        """resolve_case_entities with the case's analyst decisions and previous entities, so cluster IDs stay
        stable across re-clusters (NLP-12) and split / merge / reject decisions stay in force (NLP-11)."""
        return resolve_case_entities(
            case_id, statements, txns,
            similarity_threshold=similarity_threshold,
            manual_merges=manual_merges if manual_merges is not None else self.vault.get_manual_merges(case_id),
            decisions=decisions if decisions is not None else self.vault.get_entity_decisions(case_id),
            previous_entities=previous_entities if previous_entities is not None else self.vault.get_entities(case_id),
            diagnostics=diagnostics,
        )

    def _categorizer_for(self, case_obj: InvestigationCase) -> HybridTransactionCategorizer:
        """Request-local categoriser sharing the read-only KB/ML indexes, with the case's threshold."""
        with self._rules_lock:
            cat = copy.copy(self.categorizer)
        cat._cache = {}  # own memo: rule changes on the shared instance must not race with this request
        cat.confidence_threshold = float(case_obj.confidence_threshold)
        return cat

    def _audit(self, case_id: Optional[str], action: str, details: Dict[str, Any]) -> None:
        self.vault.log_audit(case_id, action, details, actor=self.analyst)

    # ------------------------------------------------------------------------------------------ analysis cache
    def _snapshot(self, case_id: str) -> _CaseSnapshot:
        rev = self.vault.get_case_revision(case_id)
        if rev is None:
            raise KeyError(f"Case '{case_id}' not found")
        with self._cache_lock:
            snap = self._analysis_cache.get(case_id)
            if snap is not None and snap.revision == rev:
                self._analysis_cache.move_to_end(case_id)
                return snap
        with self._cache_lock:
            prev = self._analysis_cache.get(case_id)
        txns, keys = self._load_transactions(case_id, prev)
        snap = _CaseSnapshot(
            rev,
            self.vault.get_statements(case_id),
            txns,
            self.vault.get_entities(case_id),
            self.vault.get_misc_entities(case_id),
            order_keys=keys,
        )
        snap.misc_paths = self.vault.get_all_misc_paths(case_id)
        if self.vault.get_case_revision(case_id) == rev:  # not mutated while loading -> cacheable
            with self._cache_lock:
                self._analysis_cache[case_id] = snap
                self._analysis_cache.move_to_end(case_id)
                while len(self._analysis_cache) > ANALYSIS_CACHE_CASES:
                    self._analysis_cache.popitem(last=False)
        return snap

    def _load_transactions(
        self, case_id: str, prev: Optional[_CaseSnapshot]
    ) -> Tuple[List[NormalizedTransaction], List[tuple]]:
        """Case transactions in (txn_date, row_index, rowid) order. With a cached snapshot of an older revision,
        only rows written since that revision are read and merged (BE-07); a row-count mismatch (deleted rows)
        falls back to a full load."""
        if prev is not None and prev.order_keys and len(prev.order_keys) == len(prev.transactions):
            changed, ckeys, total = self.vault.get_transactions_with_order_keys(case_id, changed_since=prev.revision)
            by_id: Dict[str, Tuple[tuple, NormalizedTransaction]] = {
                t.tx_id: (k, t) for k, t in zip(prev.order_keys, prev.transactions)
            }
            for k, t in zip(ckeys, changed):
                by_id[t.tx_id] = (k, t)
            if len(by_id) == total:
                if not changed:
                    return list(prev.transactions), list(prev.order_keys)
                merged = sorted(by_id.values(), key=lambda kt: kt[0])
                return [t for _k, t in merged], [k for k, _t in merged]
        txns, keys, _total = self.vault.get_transactions_with_order_keys(case_id)
        return txns, keys

    @staticmethod
    def _memo(snap: _CaseSnapshot, key: tuple, compute: Callable[[], Any]) -> Any:
        with snap.lock:
            if key in snap.memo:
                snap.memo.move_to_end(key)
                return snap.memo[key]
        if len(snap.events) > EVENT_CACHE_ENTRIES:
            snap.events.clear()
        with shared_event_cache(snap.events):
            value = compute()
        with snap.lock:
            snap.memo[key] = value
            while len(snap.memo) > ANALYSIS_MEMO_ENTRIES:
                snap.memo.popitem(last=False)
        return value

    def _mules(self, snap: _CaseSnapshot) -> list:
        return self._memo(
            snap, ("mules",), lambda: evaluate_all_mule_accounts(snap.statements, list(snap.transactions), snap.entities)
        )

    def _loops(
        self,
        snap: _CaseSnapshot,
        max_days: int = 30,
        min_retention: float = 0.50,
        max_cycle_length: Optional[int] = None,
        time_budget_s: Optional[float] = None,
        max_instances_per_loop: Optional[int] = None,
        hub_degree_cap: Any = "default",
    ):
        # Defaults are filled in so the UI's default scan, the exports and the warm-up share one memo entry.
        max_days = int(max_days)
        min_retention = float(min_retention)
        if max_cycle_length is None:
            max_cycle_length = LOOP_DEFAULT_CYCLE_LENGTH
        if max_instances_per_loop is None:
            max_instances_per_loop = LOOP_DEFAULT_MAX_INSTANCES
        kwargs: Dict[str, Any] = {"max_cycle_days": max_days, "min_retention_ratio": min_retention}
        if hub_degree_cap != "default":
            kwargs["hub_degree_cap"] = hub_degree_cap
        if max_instances_per_loop is not None:
            kwargs["max_instances_per_loop"] = int(max_instances_per_loop)
        if max_cycle_length is not None:
            kwargs["max_cycle_length"] = int(max_cycle_length)
        if time_budget_s is not None:
            kwargs["time_budget_s"] = float(time_budget_s)
        key = ("loops", max_days, float(min_retention), max_cycle_length, time_budget_s, max_instances_per_loop,
               hub_degree_cap)
        return self._memo(
            snap, key, lambda: detect_circular_loops(snap.statements, list(snap.transactions), snap.entities, **kwargs)
        )

    def _graph_full(self, snap: _CaseSnapshot, **filters: Any) -> Dict[str, Any]:
        """Uncapped, un-laid-out graph for one filter set; every node-capped view and expansion slices it."""
        key = ("graph_full",) + tuple(sorted(filters.items()))
        max_amount = filters.pop("max_amount", None)
        ifsc = filters.pop("ifsc", None)

        def compute() -> Dict[str, Any]:
            mule_map = {m.entity_id: m.to_dict() for m in self._mules(snap) if m.entity_id}
            # IFSC (full codes or bank prefixes, comma-separated) and the per-transaction amount ceiling are
            # applied by the engine, which also echoes them in summary.filters (TG-12).
            return build_network_graph(
                snap.statements, list(snap.transactions), snap.entities, mule_scores=mule_map,
                ifsc_filter=ifsc, max_amount=max_amount, max_display_nodes=0, layout="none", **filters,
            )

        return self._memo(snap, key, compute)

    def _graph(
        self,
        snap: _CaseSnapshot,
        max_nodes: Optional[int] = None,
        layout: Optional[str] = None,
        max_edge_tx_ids: Optional[int] = GRAPH_EDGE_TX_IDS,
        **filters: Any,
    ) -> Dict[str, Any]:
        """Node-capped graph view (TG-04): top ``max_nodes`` by volume, laid out on the server only when small
        (``layout='auto'``); larger views carry seed positions that the client's force layout refines."""
        cap = GRAPH_DEFAULT_MAX_NODES if max_nodes is None else int(max_nodes)
        mode = (layout or "auto").strip().lower()
        if mode not in GRAPH_LAYOUT_MODES:
            raise ValueError(f"layout must be one of {', '.join(GRAPH_LAYOUT_MODES)}")
        key = ("graph", cap, mode, max_edge_tx_ids) + tuple(sorted(filters.items()))

        def compute() -> Dict[str, Any]:
            full = self._graph_full(snap, **filters)
            shown = min(cap, len(full["nodes"])) if cap > 0 else len(full["nodes"])
            eff = mode if mode != "auto" else ("server" if shown <= GRAPH_SERVER_LAYOUT_MAX_NODES else "seed")
            return slice_network_graph(full, cap, layout=eff, max_edge_tx_ids=max_edge_tx_ids)

        return self._memo(snap, key, compute)

    # ------------------------------------------------------------------------------------------ LLM config
    def _reload_llm_config(self) -> LLMConfig:
        self.llm_config = LLMConfig.from_dict(self.vault.get_llm_settings())
        return self.llm_config

    def get_llm_config_public(self) -> Dict[str, Any]:
        return {
            **self.llm_config.to_public_dict(),
            "provider_catalog": PROVIDER_CATALOG,
            "agent_definitions": AGENT_DEFINITIONS,
        }

    def update_llm_config(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        cur_cfg = self.llm_config
        cur = cur_cfg.to_storage_dict()  # vault-held keys only; env keys are never written back (LLM-03)
        provider = str(payload.get("provider") or cur["provider"]).strip().lower()
        if provider not in VALID_PROVIDERS:
            raise ValueError(f"Unsupported LLM provider '{provider}'. Choose one of {VALID_PROVIDERS}.")
        p_meta = PROVIDER_CATALOG[provider]

        exec_mode = str(payload.get("execution_mode") or cur["execution_mode"]).strip().lower()
        if exec_mode not in VALID_EXECUTION_MODES:
            raise ValueError(f"Unsupported execution_mode '{exec_mode}'. Choose one of {VALID_EXECUTION_MODES}.")

        api_keys = dict(cur.get("api_keys") or {})
        disabled_env = set(cur.get("disabled_env_providers") or [])
        changed_keys: List[str] = []
        cleared = False
        raw_key = payload.get("api_key")
        if payload.get("clear_api_key"):
            api_keys.pop(provider, None)
            disabled_env.add(provider)  # also suppress an environment fallback for this provider (LLM-11)
            cleared = True
        elif isinstance(raw_key, str):
            stripped = raw_key.strip()
            if stripped and not stripped.startswith("••••"):
                api_keys[provider] = stripped
                disabled_env.discard(provider)
                changed_keys.append(provider)

        if isinstance(payload.get("api_keys"), dict):
            for k_prov, k_val in payload["api_keys"].items():
                if k_prov in VALID_PROVIDERS and isinstance(k_val, str):
                    sv = k_val.strip()
                    if sv and not sv.startswith("••••"):
                        api_keys[k_prov] = sv
                        disabled_env.discard(k_prov)
                        changed_keys.append(k_prov)

        default_model = p_meta["default_model"] if provider != cur["provider"] and not payload.get("model") else cur["model"]
        default_vision = (
            p_meta["default_vision_model"]
            if provider != cur["provider"] and not payload.get("vision_model")
            else cur["vision_model"]
        )

        new_cfg = LLMConfig(
            execution_mode=exec_mode,
            provider=provider,
            model=str(payload.get("model") or default_model).strip(),
            vision_model=str(payload.get("vision_model") or default_vision).strip(),
            api_key=api_keys.get(provider, ""),
            api_keys=api_keys,
            enable_stage1_vision=bool(payload.get("enable_stage1_vision", cur["enable_stage1_vision"])),
            enable_stage2_reconciliation=bool(payload.get("enable_stage2_reconciliation", cur["enable_stage2_reconciliation"])),
            enable_stage3_narration=bool(payload.get("enable_stage3_narration", cur["enable_stage3_narration"])),
            enable_stage4_categorization=bool(payload.get("enable_stage4_categorization", cur["enable_stage4_categorization"])),
            timeout_sec=max(5, int(payload.get("timeout_sec", cur.get("timeout_sec", 45)))),
            configured=True,
            disabled_env_providers=sorted(disabled_env),
        )
        self.vault.save_llm_settings(new_cfg.to_storage_dict())
        # Re-read so env-sourced keys (only with the explicit opt-in) and key_sources are correct.
        self.llm_config = LLMConfig.from_dict(self.vault.get_llm_settings())
        cfg = self.llm_config
        self._audit(None, "LLM_CONFIG_UPDATED", {
            "execution_mode": cfg.execution_mode,
            "provider": cfg.provider,
            "model": cfg.model,
            "vision_model": cfg.vision_model,
            "key_source": cfg.key_source(),
            "keys_changed_for": sorted(set(changed_keys)),
            "key_cleared_for": provider if cleared else None,
            "stages_enabled": [
                i for i, on in enumerate(
                    (cfg.enable_stage1_vision, cfg.enable_stage2_reconciliation,
                     cfg.enable_stage3_narration, cfg.enable_stage4_categorization), start=1) if on
            ],
            "cloud_egress_possible": cfg.cloud_egress_possible(),
        })
        return self.get_llm_config_public()

    def test_llm_connection(self, payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        payload = payload or {}
        base_cfg = self.llm_config
        base = base_cfg.to_storage_dict()
        provider = str(payload.get("provider") or base["provider"]).strip().lower()
        if provider not in VALID_PROVIDERS:
            return {"ok": False, "error": f"Unsupported provider: {provider}"}

        # Use the keys this config actually holds (vault keys, plus env keys only when opted in).
        api_keys = {p: base_cfg.active_api_key(p) for p in VALID_PROVIDERS if base_cfg.active_api_key(p)}
        raw_key = payload.get("api_key")
        if isinstance(raw_key, str):
            stripped = raw_key.strip()
            if stripped and not stripped.startswith("••••"):
                api_keys[provider] = stripped

        p_meta = PROVIDER_CATALOG[provider]
        test_cfg = LLMConfig(
            execution_mode=base["execution_mode"],
            provider=provider,
            model=str(payload.get("model") or (p_meta["default_model"] if provider != base["provider"] else base["model"])).strip(),
            vision_model=str(payload.get("vision_model") or (p_meta["default_vision_model"] if provider != base["provider"] else base["vision_model"])).strip(),
            api_key=api_keys.get(provider, ""),
            api_keys=api_keys,
            timeout_sec=int(payload.get("timeout_sec", base.get("timeout_sec", 20))),
            configured=True,
        )
        if not test_cfg.has_valid_key():
            return {
                "ok": False,
                "provider": provider,
                "model": test_cfg.model,
                "error": f"No API key provided or saved for {p_meta['label']}.",
            }
        client = LLMProviderClient(test_cfg, transport_fn=self._llm_transport_fn)
        try:
            return client.test_connection()
        except LLMProviderError as exc:
            return {
                "ok": False,
                "provider": provider,
                "model": test_cfg.model,
                "error": str(exc),
            }

    def get_metadata(self) -> Dict[str, Any]:
        return {
            "version": __version__,
            "parser_version": parser_version(),
            "categories_count": len(CATEGORIES_52),
            "banks_supported_count": len(INDIAN_BANKS),
            "knowledge_base_data_points": KNOWLEDGE_BASE_POINT_COUNT,
            "knowledge_base": knowledge_base_stats(),
            "vault_encrypted": self.vault.encrypted,
            "llm_config": self.llm_config.to_public_dict(),
            "provider_catalog": PROVIDER_CATALOG,
            "agent_definitions": AGENT_DEFINITIONS,
            "categories": [
                {
                    "category_id": c.category_id,
                    "group": c.group,
                    "name": c.name,
                    "typical_direction": c.typical_direction,
                    "tally_voucher": c.tally_voucher.value,
                    "tally_parent_group": c.tally_parent_group,
                    "is_lifestyle": c.is_lifestyle,
                    "is_cash_crypto_leakage": c.is_cash_crypto_leakage,
                    "itr_section": c.itr_section,
                }
                for c in CATEGORIES_52
            ],
            "banks": [
                {
                    "bank_code": b.bank_code,
                    "bank_name": b.bank_name,
                    "ifsc_prefix": b.ifsc_prefix,
                }
                for b in INDIAN_BANKS
            ],
            "draw_templates": [t.to_dict() for t in self.vault.get_draw_templates()],
            # P1 additions
            "srs_payment_channels": list(SRS_PAYMENT_CHANNELS),
            "srs_channel_extensions": list(SRS_CHANNEL_EXTENSIONS),
            "profile_export_formats": list(PROFILE_EXPORT_FORMATS),
            "password_hint_keys": list(PASSWORD_HINT_KEYS),
            "max_trace_depth": MAX_TRACE_DEPTH,
            "labelled_examples_used": self._labelled_examples_used,
        }

    # ------------------------------------------------------------------------------------------ cases
    def list_cases(self) -> List[Dict[str, Any]]:
        return [c.to_dict() for c in self.vault.list_cases()]

    def create_case(
        self, title: str, mode: str, agency_or_firm_name: Optional[str] = None
    ) -> Dict[str, Any]:
        case_obj = InvestigationCase(
            case_id=f"CASE_{uuid.uuid4().hex[:10].upper()}",
            title=title.strip() or "Untitled Investigation",
            mode=WorkspaceMode(mode),
            agency_or_firm_name=agency_or_firm_name,
            created_at=dt.datetime.now(dt.timezone.utc).isoformat(),
        )
        self.vault.save_case(case_obj)
        self._audit(case_obj.case_id, "CREATE_CASE", {"title": case_obj.title, "mode": mode})
        return case_obj.to_dict()

    def get_case_overview(self, case_id: str, tx_offset: int = 0, tx_limit: int = 0) -> Dict[str, Any]:
        """Case header, statements, entities and SQL aggregates only (BE-08). Transactions are paged through
        :meth:`list_transactions`; ``tx_limit > 0`` (legacy callers) additionally embeds one page of each list
        (all rows / suspense / flagged), capped at MAX_TX_PAGE."""
        case_obj = self._require_case(case_id)
        limit = max(0, min(int(tx_limit), MAX_TX_PAGE))
        stmts = self.vault.get_statements(case_id)
        ents = self.vault.get_entities(case_id)
        agg = self.vault.transaction_aggregates(case_id)
        lists: Dict[str, Any] = {}
        if limit:
            page, total = self.vault.query_transactions(case_id, offset=max(0, int(tx_offset)), limit=limit)
            suspense, suspense_total = self.vault.query_transactions(
                case_id, limit=limit, filters={"is_suspense": True, "include_ignored": False}
            )
            flagged, flagged_total = self.vault.query_transactions(case_id, limit=limit, filters={"flagged": True})
            lists = {
                "transactions": [tx_to_json(t) for t in page],
                "transactions_page": {
                    "offset": max(0, int(tx_offset)),
                    "limit": limit,
                    "total": total,
                    "has_more": max(0, int(tx_offset)) + len(page) < total,
                },
                "suspense_transactions": [tx_to_json(t) for t in suspense],
                "suspense_truncated": len(suspense) < suspense_total,
                "anomaly_transactions": [tx_to_json(t) for t in flagged],
                "anomaly_truncated": len(flagged) < flagged_total,
            }

        # Near-miss merge candidates come from the blocked resolver preview (no O(n^2) loop), memoised per
        # case revision. Rows keep entity_a / entity_b / similarity and add entity_a_id / entity_b_id.
        try:
            preview = self._cluster_preview(case_id, case_obj.similarity_threshold)
            merge_candidates = [
                {"entity_a": c.get("entity_a"), "entity_b": c.get("entity_b"), "similarity": c.get("similarity"),
                 "entity_a_id": c.get("entity_a_id"), "entity_b_id": c.get("entity_b_id")}
                for c in preview.get("candidates") or []
            ]
        except Exception:  # noqa: BLE001 - the overview must render even if the preview fails
            _LOG.exception("Entity cluster preview failed for %s", case_id)
            merge_candidates = []

        return {
            "case": case_obj.to_dict(),
            "revision": self.vault.get_case_revision(case_id),
            "statements": [s.to_dict() for s in stmts],
            **lists,
            "entities": [e.to_dict() for e in ents],
            "summary": {
                "statements_count": len(stmts),
                "transactions_count": agg["transactions_count"],
                "entities_count": len(ents),
                "suspense_count": agg["suspense_count"],
                "anomaly_count": agg["anomaly_count"],
                "total_credits": agg["total_credits"],
                "total_debits": agg["total_debits"],
                "duplicates_ignored": agg["duplicates_ignored"],
            },
            "near_merge_candidates": merge_candidates[:20],
            "audit_logs": self.vault.get_audit_logs(case_id, limit=50),
            "recent_ingest_results": self.vault.get_ingest_results(case_id, limit=50),
            "llm_config": self.llm_config.to_public_dict(),
            "latest_agent_telemetry": self.vault.get_llm_settings(f"case_telemetry:{case_id}") or None,
        }

    def list_transactions(
        self, case_id: str, offset: int = 0, limit: int = 200, **filters: Any
    ) -> Dict[str, Any]:
        """Paginated, filtered transactions (BE-08). Filters: q, category_id, is_suspense, flagged,
        include_ignored, statement_id, entity_id, channel, direction, date_from, date_to."""
        self._require_case(case_id)
        offset = max(0, int(offset))
        limit = max(1, min(int(limit), MAX_TX_PAGE))
        sort = filters.pop("sort", None)
        order = filters.pop("order", None)
        amount_min = filters.pop("amount_min", None)
        amount_max = filters.pop("amount_max", None)
        tx_ids = self._clean_tx_ids(filters.pop("tx_ids", None))
        if amount_min is not None and amount_max is not None and float(amount_max) < float(amount_min):
            raise ValueError("amount_max must be greater than or equal to amount_min")
        clean = {k: v for k, v in filters.items() if v is not None and v != ""}
        if amount_min is not None:
            clean["amount_min"] = float(amount_min)
        if amount_max is not None:
            clean["amount_max"] = float(amount_max)
        if tx_ids is not None:
            clean["tx_ids"] = tx_ids
        sort_key = (sort or "txn_date").strip()
        order_key = (order or "asc").strip().lower()
        # Sorting, amount range and the tx_ids filter all run in SQL (storage.query_transactions).
        rows, total = self.vault.query_transactions(
            case_id, offset=offset, limit=limit, filters=clean, sort=sort_key, order=order_key
        )
        return {
            "transactions": [tx_to_json(t) for t in rows],
            "offset": offset,
            "limit": limit,
            "total": total,
            "has_more": offset + len(rows) < total,
            "sort": sort_key,
            "order": order_key,
        }

    MAX_TX_IDS = MAX_TX_IDS
    TX_SORT_COLUMNS = TX_SORT_COLUMNS  # whitelist lives in storage (SQL ORDER BY); kept here for callers

    @classmethod
    def _clean_tx_ids(cls, raw: Any) -> Optional[List[str]]:
        """Normalise the ``tx_ids`` filter (comma-separated string or list) to unique ids, capped at MAX_TX_IDS."""
        if raw is None or raw == "":
            return None
        parts = raw.split(",") if isinstance(raw, str) else list(raw)
        ids: List[str] = []
        seen: Set[str] = set()
        for part in parts:
            v = str(part).strip()
            if v and v not in seen:
                seen.add(v)
                ids.append(v)
        if len(ids) > cls.MAX_TX_IDS:
            raise ValueError(f"Too many tx_ids ({len(ids)}); the limit is {cls.MAX_TX_IDS}")
        return ids

    def get_statement_provenance(self, case_id: str) -> Dict[str, Any]:
        self._require_case(case_id)
        # ING-10: per-row OCR self-healing before/after values (from the OCR_AUTOCORRECT audit entries), keyed by
        # tx_id, so the transaction drawer can show what was changed on an OCR_AUTOCORRECTED row.
        ocr: Dict[str, Dict[str, Any]] = {}
        for entry in self.vault.get_audit_details_by_action(case_id, "OCR_AUTOCORRECT"):
            for c in entry.get("corrections") or []:
                if isinstance(c, dict) and c.get("tx_id"):
                    ocr[str(c["tx_id"])] = {
                        "statement_id": entry.get("statement_id"),
                        "filename": entry.get("filename"),
                        "row_index": c.get("row_index"),
                        "before": c.get("before"),
                        "after": c.get("after"),
                        "recorded_at": entry.get("_created_at"),
                    }
        return {
            "provenance": self.vault.get_statement_provenance(case_id),
            "ingest_results": self.vault.get_ingest_results(case_id),
            "ocr_corrections": ocr,
        }

    # ------------------------------------------------------------------------------------------ draw tool
    @staticmethod
    def _draw_password(pdf_bytes: bytes, password: Optional[str], hints: Optional[Mapping[str, Any]]) -> Optional[str]:
        """Resolve the Draw Tool password from the supplied password and/or holder-detail hints (PDF-13), in
        memory only, so a PDF unlocked through hints at upload can also be calibrated. Never returned."""
        clean = clean_password_hints(dict(hints) if hints else None)
        if not clean:
            return password or None
        cands: List[Any] = [("supplied", password)] if password else []
        cands.extend(generate_password_candidates(clean))
        res = resolve_pdf_password(pdf_bytes, cands)
        if res.get("status") == "ok":
            return res.get("password") or None
        return password or None

    def preview_pdf_for_draw_tool(
        self, pdf_bytes: bytes, password: Optional[str] = None, hints: Optional[Mapping[str, Any]] = None
    ) -> Dict[str, Any]:
        if hints and pdf_bytes:
            password = self._draw_password(pdf_bytes, password, hints)
        res = extract_pdf_data(pdf_bytes, password=password)
        err = getattr(res, "error", None)
        if err == "corrupt":
            raise ValueError("The PDF is damaged or is not a PDF.")
        if res.is_encrypted:
            return {"is_encrypted": True, "error": err, "tokens": [], "page_sizes": []}
        return {
            "is_encrypted": False,
            "error": err,
            "needs_ocr": res.needs_ocr,
            "ocr_pages": list(getattr(res, "ocr_pages", []) or []),
            "text_source": getattr(res, "text_source", None),
            "page_sizes": res.page_sizes,
            "tokens": [
                {"page": t.page, "x": t.x, "x_end": getattr(t, "x_end", None), "y": t.y, "text": t.text}
                for t in res.tokens[:400]
            ],
            "auto_rows_preview": res.rows[:25],
        }

    def render_draw_page(
        self, pdf_bytes: bytes, page: int = 1, dpi: int = 110, password: Optional[str] = None,
        hints: Optional[Mapping[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Raster one PDF page for the Draw Tool overlay (PDF-21). Raises pdf_parser.PageRenderError (mapped to
        HTTP status codes by the server) or ValueError for bad arguments. The password is used in memory only."""
        if not pdf_bytes:
            raise ValueError("content_b64 is required")
        try:
            page_i, dpi_i = int(page), int(dpi)
        except (TypeError, ValueError) as exc:
            raise ValueError("page and dpi must be integers") from exc
        if page_i < 1:
            raise ValueError("page must be 1 or greater")
        if not DRAW_DPI_RANGE[0] <= dpi_i <= DRAW_DPI_RANGE[1]:
            raise ValueError(f"dpi must be between {DRAW_DPI_RANGE[0]} and {DRAW_DPI_RANGE[1]}")
        if hints:
            password = self._draw_password(pdf_bytes, password, hints)
        return render_page_png(pdf_bytes, page=page_i, dpi=dpi_i, password=password or None)

    def check_pdf_password(
        self, pdf_bytes: bytes, password: Optional[str] = None, hints: Optional[Mapping[str, Any]] = None
    ) -> Dict[str, Any]:
        """Try the supplied password and the generated candidates (PDF-13) against an encrypted PDF.
        Returns {status, label, tried, candidates}; never the password itself."""
        if not pdf_bytes:
            raise ValueError("content_b64 is required")
        clean = clean_password_hints(dict(hints) if hints else None)
        cands: List[Any] = [("supplied", password)] if password else []
        if clean:
            cands.extend(generate_password_candidates(clean))
        res = resolve_pdf_password(pdf_bytes, cands)
        return {
            "status": res.get("status"),
            "label": res.get("label"),
            "tried": int(res.get("tried") or 0),
            "candidates": len(cands),
        }

    def save_draw_template(
        self,
        bank_code: str,
        template_name: str,
        table_bbox: List[float],
        column_splits: List[float],
        column_names: List[str],
        case_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        # PDF-22: reject NaN / out-of-range / unsorted geometry at the boundary; persist the normalised splits.
        norm_splits, norm_bbox = validate_draw_template(column_splits, table_bbox)
        if norm_splits is not None:
            column_splits = norm_splits
        if norm_bbox is not None:
            table_bbox = norm_bbox
        if not isinstance(column_names, list) or not all(isinstance(c, str) for c in column_names):
            raise ValueError("column_names must be a list of strings")
        column_names = validate_draw_column_names(column_names, len(column_splits or []))
        tpl = DrawTemplate(
            template_id=f"TPL_{uuid.uuid4().hex[:8].upper()}",
            bank_code=bank_code.upper(),
            template_name=template_name,
            table_bbox=table_bbox,
            column_splits=column_splits,
            column_names=column_names,
            created_at=dt.datetime.now(dt.timezone.utc).isoformat(),
        )
        self.vault.save_draw_template(tpl)
        self._audit(case_id, "DRAW_TEMPLATE_SAVED", {
            "template_id": tpl.template_id,
            "bank_code": tpl.bank_code,
            "template_name": tpl.template_name,
            "table_bbox": tpl.table_bbox,
            "column_splits": tpl.column_splits,
            "column_names": tpl.column_names,
        })
        return tpl.to_dict()

    # ------------------------------------------------------------------------------------------ ingestion
    def ingest_statements(
        self,
        case_id: str,
        files: List[Tuple[str, bytes, Optional[str]]],
        template_id: Optional[str] = None,
        execution_mode: Optional[str] = None,
        on_duplicate: str = "reject",
        password_hints: Optional[Mapping[str, Any]] = None,
        file_password_hints: Optional[Sequence[Optional[Mapping[str, Any]]]] = None,
        progress: Optional[Callable[[str, int, int], None]] = None,
    ) -> Dict[str, Any]:
        """Parse, validate, categorise and persist a batch of statement files.

        * ``progress(phase, done, total)`` is called as work advances (phases: parse, validate, entities,
          commit); the background job API uses it for polling (P2 item 27).

        * ``password_hints`` (batch) and ``file_password_hints`` (per file, same order as ``files``) feed the PDF
          password generator (PDF-13). Passwords and hints stay in memory for this request only; the response,
          provenance and audit carry only the label of the formula that opened each file.

        * Each file is parsed in isolation; a failing file is reported (and persisted as a per-file result)
          without stopping the others (ING-14/PDF-15).
        * Everything the batch writes commits in one transaction (BE-11).
        * Re-uploading a file whose SHA-256 is already in the case raises ``ConflictError`` (HTTP 409) with
          ``on_duplicate='reject'`` (default), or is skipped and reported with ``on_duplicate='skip'`` (BE-12).
          A previous upload of the same file that yielded zero rows is superseded instead.
        * ``execution_mode=None`` uses the saved LLM configuration's mode.
        """
        if on_duplicate not in DUPLICATE_POLICIES:
            raise ValueError(f"on_duplicate must be one of {DUPLICATE_POLICIES}")
        if not files:
            raise ValueError("No statement files were provided.")
        self._require_case(case_id)
        batch_hints = clean_password_hints(dict(password_hints) if password_hints else None)
        per_file = list(file_password_hints or [])
        if per_file and len(per_file) != len(files):
            raise ValueError("file_password_hints must have one entry per file")
        if batch_hints or any(per_file):
            prepared: List[Tuple[str, bytes, Any]] = []
            for idx, (fn, raw, pwd) in enumerate(files):
                hints = dict(batch_hints or {})
                hints.update(clean_password_hints(dict(per_file[idx])) if idx < len(per_file) and per_file[idx] else {})
                if hints and str(fn).lower().endswith(".pdf"):
                    cands: List[Any] = [("supplied", pwd)] if pwd else []
                    cands.extend(generate_password_candidates(hints))
                    prepared.append((fn, raw, cands))
                else:
                    prepared.append((fn, raw, pwd))
            files = prepared  # type: ignore[assignment]
        with self._case_lock(case_id):
            return self._ingest_locked(case_id, files, template_id, execution_mode, on_duplicate, progress)

    # ------------------------------------------------------------------------------------------ jobs / staged uploads
    def stage_upload(self, case_id: str, filename: str, raw: bytes) -> Dict[str, Any]:
        """Stage one statement file for a later ingest job (per-file upload, P2 item 27)."""
        self._require_case(case_id)
        return self.uploads.put(case_id, filename, raw).to_dict()

    def list_staged_uploads(self, case_id: str) -> Dict[str, Any]:
        self._require_case(case_id)
        return {"uploads": self.uploads.list(case_id), "max_total_bytes": self.uploads.max_total_bytes}

    def discard_upload(self, case_id: str, upload_id: str) -> Dict[str, Any]:
        self._require_case(case_id)
        self.uploads.discard(case_id, upload_id)
        return {"discarded": upload_id}

    def start_ingest_job(
        self,
        case_id: str,
        files: Sequence[Mapping[str, Any]],
        template_id: Optional[str] = None,
        execution_mode: Optional[str] = None,
        on_duplicate: str = "reject",
        password_hints: Optional[Mapping[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Queue :meth:`ingest_statements` as a background job and return the job record (HTTP 202).

        ``files`` entries are ``{"upload_id", "password"?, "password_hints"?}`` (staged with :meth:`stage_upload`)
        or ``{"filename", "content": bytes, "password"?, "password_hints"?}``. Arguments are validated and staged
        files are claimed before the job is queued, so a bad request fails immediately. The job's ``result`` is
        the same payload the synchronous upload returns.
        """
        if on_duplicate not in DUPLICATE_POLICIES:
            raise ValueError(f"on_duplicate must be one of {DUPLICATE_POLICIES}")
        if not files:
            raise ValueError("No statement files were provided.")
        self._require_case(case_id)
        batch_hints = clean_password_hints(dict(password_hints) if password_hints else None)
        staged_ids = [str(f["upload_id"]) for f in files if f.get("upload_id")]
        for f in files:
            if not f.get("upload_id") and not (f.get("filename") and f.get("content")):
                raise ValueError("Each file needs an upload_id, or a filename and content")
            clean_password_hints(dict(f["password_hints"]) if f.get("password_hints") else None)
        staged = {u.upload_id: u for u in self.uploads.take(case_id, staged_ids)} if staged_ids else {}
        batch: List[Tuple[str, bytes, Optional[str]]] = []
        per_file_hints: List[Optional[Mapping[str, Any]]] = []
        for f in files:
            if f.get("upload_id"):
                it = staged[str(f["upload_id"])]
                batch.append((it.filename, it.data, f.get("password") or None))
            else:
                batch.append((str(f["filename"]), bytes(f["content"]), f.get("password") or None))
            per_file_hints.append(f.get("password_hints") or None)
        names = [b[0] for b in batch]

        def run(progress: Callable[[str, int, int], None]) -> Dict[str, Any]:
            res = self.ingest_statements(
                case_id, batch, template_id=template_id, execution_mode=execution_mode, on_duplicate=on_duplicate,
                password_hints=batch_hints, file_password_hints=per_file_hints if any(per_file_hints) else None,
                progress=progress,
            )
            self.schedule_warmup(case_id)
            return res

        job = self.jobs.submit(
            "ingest", case_id, run, meta={"files": names, "file_count": len(names)}, weights=INGEST_PHASE_WEIGHTS,
        )
        return job.to_dict()

    def get_job(self, job_id: str, include_result: bool = True) -> Dict[str, Any]:
        return self.jobs.get(job_id).to_dict(include_result=include_result)

    def list_jobs(self, case_id: str) -> Dict[str, Any]:
        self._require_case(case_id)
        return {"jobs": [j.to_dict(include_result=False) for j in self.jobs.list(case_id=case_id)]}

    def schedule_warmup(self, case_id: str) -> Optional[Dict[str, Any]]:
        """Queue a background warm-up of the case's default analytics (debounced: at most one queued per case).
        No-op unless the service was created with ``warm_analysis=True``."""
        if not self.warm_analysis:
            return None
        pending = self.jobs.active(case_id, "warmup")
        if pending is not None and pending.status == "queued":
            return pending.to_dict(include_result=False)
        job = self.jobs.submit("warmup", case_id, lambda progress: self.warm_case_analysis(case_id, progress))
        return job.to_dict(include_result=False)

    def warm_case_analysis(
        self, case_id: str, progress: Optional[Callable[[str, int, int], None]] = None
    ) -> Dict[str, Any]:
        """Compute the snapshot and the analytics the UI requests by default, so they answer from the cache
        (NFR-PERF-01: < 500 ms warm). Stops early if the case changes meanwhile. Returns per-step seconds."""
        case_obj = self._require_case(case_id)
        rev = self.vault.get_case_revision(case_id)
        steps: List[Tuple[str, Callable[[], Any]]] = [
            ("snapshot", lambda: self._snapshot(case_id)),
            ("entity_preview", lambda: self._cluster_preview(case_id, case_obj.similarity_threshold)),
        ]
        if WorkspaceMode(case_obj.mode) == WorkspaceMode.CYPHER_TRACE:
            steps += [
                ("mules", lambda: self.get_mule_assessments(case_id)),
                ("graph", lambda: self.get_network_graph(case_id)),
                ("loops", lambda: self.get_loops_report(case_id)),
                ("bidirectional", lambda: self.get_bidirectional(case_id)),
                ("layering", lambda: self.get_layering(case_id)),
                ("profile", lambda: self.get_financial_profile(case_id)),
            ]
        timings: Dict[str, float] = {}
        for i, (name, fn) in enumerate(steps):
            if self.vault.get_case_revision(case_id) != rev:
                return {"revision": rev, "superseded": True, "timings": timings}
            t0 = time.perf_counter()
            try:
                fn()
            except Exception:  # noqa: BLE001 - warm-up is best effort; the request path reports real errors
                _LOG.debug("warm-up step %s failed for %s", name, case_id, exc_info=True)
            timings[name] = round(time.perf_counter() - t0, 3)
            if progress is not None:
                progress(name, i + 1, len(steps))
        return {"revision": rev, "superseded": False, "timings": timings}

    def _ingest_locked(
        self,
        case_id: str,
        files: List[Tuple[str, bytes, Optional[str]]],
        template_id: Optional[str],
        execution_mode: Optional[str],
        on_duplicate: str,
        progress: Optional[Callable[[str, int, int], None]] = None,
    ) -> Dict[str, Any]:
        case_obj = self._require_case(case_id)
        t_start = dt.datetime.now(dt.timezone.utc)
        timings: Dict[str, float] = {}
        t_phase = time.perf_counter()

        def report(phase: str, done: int, total: int) -> None:
            if progress is not None:
                try:
                    progress(phase, done, total)
                except Exception:  # noqa: BLE001 - progress reporting never breaks an ingest
                    _LOG.debug("ingest progress callback failed", exc_info=True)

        def lap(name: str) -> None:
            nonlocal t_phase
            now = time.perf_counter()
            timings[name] = round(timings.get(name, 0.0) + now - t_phase, 4)
            t_phase = now

        def on_parse_progress(done: int, total: int) -> None:
            report("parse", done, total)

        # 1. Evidence identity and duplicate detection before any parsing.
        digests = [hashlib.sha256(raw).hexdigest() for _fn, raw, _pwd in files]
        existing_by_sha = self.vault.find_statements_by_sha(case_id, digests)
        superseded: List[str] = []
        file_status: List[Optional[IngestFileResult]] = [None] * len(files)
        duplicates: List[Dict[str, Any]] = []
        seen: Dict[str, int] = {}
        for idx, ((fn, raw, _pwd), sha) in enumerate(zip(files, digests)):
            prior = existing_by_sha.get(sha)
            if sha in seen:
                duplicates.append({"filename": fn, "sha256": sha, "duplicate_of": files[seen[sha]][0]})
                file_status[idx] = IngestFileResult(fn, "duplicate", sha, len(raw), error=f"Same file as '{files[seen[sha]][0]}' in this upload.", error_type="DuplicateInBatch")
            elif prior is not None and prior.total_transactions > 0:
                duplicates.append({"filename": fn, "sha256": sha, "existing_statement_id": prior.statement_id,
                                   "existing_filename": prior.source_filename})
                file_status[idx] = IngestFileResult(
                    fn, "duplicate", sha, len(raw), statement_id=prior.statement_id,
                    error=f"Already ingested in this case as '{prior.source_filename}' ({prior.statement_id}).",
                    error_type="DuplicateStatement",
                )
            elif prior is not None:
                superseded.append(prior.statement_id)  # earlier upload produced no rows: allow a retry
            seen.setdefault(sha, idx)
        if duplicates and on_duplicate == "reject":
            names = ", ".join(f"'{d['filename']}'" for d in duplicates)
            raise ConflictError(
                f"Duplicate statement file(s) {names}: the same SHA-256 is already in this case or repeated in the "
                "upload. Nothing was ingested; remove the duplicates or retry with on_duplicate='skip'."
            )

        draw_tpl = None
        if template_id:
            draw_tpl = next((t for t in self.vault.get_draw_templates() if t.template_id == template_id), None)
            if draw_tpl is None:
                raise ValueError(f"Draw template '{template_id}' not found")

        self._refresh_rules()
        categorizer = self._categorizer_for(case_obj)
        orchestrator = StageAgentOrchestrator(self.llm_config, transport_fn=self._llm_transport_fn)
        eff_mode, use_llm, mode_warning = orchestrator.should_use_llm(execution_mode)
        telemetry = orchestrator.make_telemetry(eff_mode, warning=mode_warning)
        s1_info: Dict[str, Any] = {"invoked": False, "files_recovered": 0, "rejected": 0}
        s2_info: Dict[str, Any] = {"invoked": False, "rows_repaired": 0, "repairs_rejected": 0, "metadata_updated": False}
        s3_info: Dict[str, Any] = {"invoked": False, "rows_enriched": 0}
        s4_info: Dict[str, Any] = {"invoked": False, "transactions_classified": 0, "entities_merged": 0}

        def mark(stage: int) -> None:
            if stage not in telemetry.stages_invoked:
                telemetry.stages_invoked.append(stage)

        lap("prepare")
        existing_txns = self.vault.get_transactions(case_id)
        existing_ids = {t.tx_id for t in existing_txns}
        all_case_txns: List[NormalizedTransaction] = list(existing_txns)
        dup_index = CaseDuplicateIndex(existing_txns)  # built once, extended per accepted statement (BE-06)
        audit_entries: List[Tuple[str, Dict[str, Any]]] = []

        # 2. Parse every non-duplicate file in isolation (process pool for large batches).
        to_parse = [i for i, st in enumerate(file_status) if st is None]
        parsed = bulk_parse_statements(
            [files[i] for i in to_parse], case_id, draw_template=draw_tpl, return_status=True,
            enrich_narrations=True, on_progress=on_parse_progress,
        ) if to_parse else []
        parse_by_idx = dict(zip(to_parse, parsed))
        lap("parse")

        new_statements: List[Tuple[BankAccountStatement, List[NormalizedTransaction]]] = []
        provenance: List[ProvenanceRecord] = []
        reports: List[Optional[Dict[str, Any]]] = [None] * len(files)
        analyst = self.analyst
        p_version = parser_version()

        for idx, (filename, raw_bytes, _pwd) in enumerate(files):
            report("validate", idx, len(files))
            sha, size = digests[idx], len(raw_bytes)
            if file_status[idx] is not None:
                fr = file_status[idx]
                reports[idx] = {"filename": filename, "status": fr.status, "sha256_hash": sha,
                                "error": fr.error, "error_type": fr.error_type,
                                "existing_statement_id": fr.statement_id}
                continue
            res = parse_by_idx[idx]
            agent_warnings: List[str] = []
            try:
                if res.ok:
                    stmt, txns, meta = res.statement, list(res.transactions), dict(res.meta)
                else:
                    stmt, txns, meta = None, [], {"warnings": []}
                # Rows enriched by the parse worker skip Stage 3's deterministic pass unless an LLM stage
                # replaced or repaired them (BE-06).
                pre_enriched = bool(meta.pop("narration_enriched", False))

                # STAGE 1: vision / LLM extraction, never replacing a better deterministic parse (LLM-12)
                if use_llm and self.llm_config.enable_stage1_vision:
                    stmt, txns, s1_note, s1_meta = self._run_stage1(
                        orchestrator, eff_mode, filename, raw_bytes, case_id, stmt, txns, all_case_txns, s1_info
                    )
                    if s1_meta is not None:
                        pre_enriched = False
                        # The LLM rows replaced the deterministic parse: provenance and the file report must
                        # describe where the evidence rows actually came from (BE-04 chain of custody).
                        meta = {
                            "warnings": [],
                            "deterministic_warnings": list(meta.get("warnings") or []),
                            "deterministic_text_source": meta.get("pdf_text_source"),
                            "pdf_text_source": "llm_stage1",
                            "stage1_llm_adopted": True,
                            "stage1_provider": self.llm_config.provider,
                            "stage1_model": self.llm_config.vision_model or self.llm_config.model,
                            "stage1_meta": s1_meta,
                        }
                    if s1_note:
                        agent_warnings.append(s1_note)
                    if s1_info["invoked"]:
                        mark(1)

                if stmt is None:
                    err = res.error or "No statement could be extracted."
                    file_status[idx] = IngestFileResult(filename, "failed", sha, size, error=err, error_type=res.error_type or "ParseError")
                    reports[idx] = {"filename": filename, "status": "failed", "sha256_hash": sha,
                                    "error": err, "error_type": res.error_type or "ParseError",
                                    "warnings": agent_warnings}
                    continue

                # Evidence identity always reflects the uploaded bytes.
                stmt.sha256_hash = sha
                stmt.case_id = case_id
                for t in txns:
                    t.case_id = case_id
                    t.statement_id = stmt.statement_id

                # STAGE 2: anomaly gate + reconciliation agent
                stmt, txns, anom_counts = run_anomaly_gate(stmt, txns, existing_index=dup_index)
                if use_llm and self.llm_config.enable_stage2_reconciliation:
                    try:
                        stmt, txns, r2 = orchestrator.stage2_agent.reconcile(
                            stmt, txns, existing_case_txns=all_case_txns, header_hint="",
                            force_llm=(eff_mode == "llm"),
                        )
                        if r2.get("invoked"):
                            pre_enriched = False
                            mark(2)
                            s2_info["invoked"] = True
                            s2_info["rows_repaired"] += int(r2.get("rows_repaired", 0))
                            s2_info["repairs_rejected"] += int(r2.get("repairs_rejected", 0) or 0)
                            s2_info["metadata_updated"] = s2_info["metadata_updated"] or bool(r2.get("metadata_updated"))
                            stmt, txns, anom_counts = run_anomaly_gate(stmt, txns, existing_index=dup_index)
                        if r2.get("repairs_audit") or r2.get("repairs_rejected"):
                            audit_entries.append(("LLM_STAGE2_REPAIR", {
                                "statement_id": stmt.statement_id,
                                "filename": filename,
                                "provider": self.llm_config.provider,
                                "repairs": r2.get("repairs_audit") or [],
                                "rejected": int(r2.get("repairs_rejected", 0) or 0),
                            }))
                    except Exception as exc:  # noqa: BLE001 - LLM output is untrusted; never half-ingest
                        agent_warnings.append(f"Stage 2 Reconciliation Agent skipped: {exc}")

                # STAGE 3: narration rail and counterparty extraction
                if not pre_enriched:
                    txns = [enrich_transaction_narration(t) for t in txns]
                if use_llm and self.llm_config.enable_stage3_narration:
                    try:
                        txns, r3 = orchestrator.stage3_agent.enrich(txns, force_all=(eff_mode == "llm"))
                        if r3.get("invoked"):
                            mark(3)
                            s3_info["invoked"] = True
                            s3_info["rows_enriched"] += int(r3.get("rows_enriched", 0))
                    except Exception as exc:  # noqa: BLE001
                        agent_warnings.append(f"Stage 3 Narration Agent skipped: {exc}")

                # STAGE 4A baseline: deterministic categorisation
                lap("validate")
                txns = categorizer.classify_batch(txns)
                lap("categorise")
            except Exception as exc:  # noqa: BLE001 - one bad file must not abort the batch
                _LOG.exception("Ingest of '%s' failed", filename)
                err = str(exc) or exc.__class__.__name__
                file_status[idx] = IngestFileResult(filename, "failed", sha, size, error=err, error_type=exc.__class__.__name__)
                reports[idx] = {"filename": filename, "status": "failed", "sha256_hash": sha, "error": err,
                                "error_type": exc.__class__.__name__, "warnings": agent_warnings}
                continue

            all_case_txns.extend(txns)
            dup_index.extend(txns)
            new_statements.append((stmt, txns))
            telemetry.warnings.extend(agent_warnings)
            if meta.get("ocr_corrections"):
                # ING-10: OCR self-healing is an evidence change; record every before/after value.
                audit_entries.append(("OCR_AUTOCORRECT", {
                    "statement_id": stmt.statement_id,
                    "filename": filename,
                    "corrections": meta.get("ocr_corrections"),
                }))
            provenance.append(ProvenanceRecord(
                statement_id=stmt.statement_id,
                case_id=case_id,
                source_filename=filename,
                sha256=sha,
                file_size_bytes=size,
                parser_version=p_version,
                analyst=analyst,
                ingested_at=stmt.ingested_at or t_start.isoformat(),
                details={
                    "execution_mode": eff_mode,
                    "stage1_llm_adopted": bool(meta.get("stage1_llm_adopted")),
                    "stage1_provider": meta.get("stage1_provider"),
                    "stage1_model": meta.get("stage1_model"),
                    "deterministic_text_source": meta.get("deterministic_text_source"),
                    "is_ocr_derived": bool(stmt.is_ocr_derived),
                    "text_source": meta.get("pdf_text_source"),
                    "ocr_pages": meta.get("ocr_pages"),
                    "date_order": meta.get("date_order"),
                    "rows": len(txns),
                    "filename_crosscheck": meta.get("filename_crosscheck"),
                    "period_coverage": meta.get("period_coverage"),
                    "password_label": meta.get("password_label"),
                    "low_confidence_pages": meta.get("low_confidence_pages"),
                    "ocr_confidence_by_page": meta.get("ocr_confidence_by_page"),
                    "superseded_statement_id": (
                        existing_by_sha[sha].statement_id if sha in existing_by_sha else None
                    ),
                },
            ))
            file_status[idx] = IngestFileResult(filename, "ok", sha, size, statement_id=stmt.statement_id, transactions=len(txns))
            reports[idx] = {
                "status": "ok",
                "statement_id": stmt.statement_id,
                "filename": stmt.source_filename,
                "bank_name": stmt.bank_name,
                "account_number": stmt.account_number,
                "sha256_hash": sha,
                "file_size_bytes": size,
                "transactions": len(txns),
                "anomalies": anom_counts,
                "warnings": list(meta.get("warnings", [])) + agent_warnings,
                "deterministic_warnings": meta.get("deterministic_warnings"),
                "date_order": meta.get("date_order"),
                "sheets": meta.get("sheets"),
                "ocr_corrections": meta.get("ocr_corrections"),
                "missing_metadata": anom_counts.get("missing_metadata") if isinstance(anom_counts, dict) else None,
                "statement_flags": anom_counts.get("statement_flags") if isinstance(anom_counts, dict) else None,
                "filename_crosscheck": meta.get("filename_crosscheck"),
                "period_coverage": meta.get("period_coverage"),
                "password_label": meta.get("password_label"),
                "ocr_confidence_by_page": meta.get("ocr_confidence_by_page"),
                "low_confidence_pages": meta.get("low_confidence_pages"),
                "ocr_rejected_pages": meta.get("ocr_rejected_pages"),
                "low_ocr_confidence": bool(meta.get("low_ocr_confidence")),
                "pan": getattr(stmt, "pan", None),
            }

        lap("validate")
        ok_count = sum(1 for st in file_status if st is not None and st.status == "ok")
        # An empty earlier upload is replaced only when its retry actually succeeded.
        ok_shas = {st.sha256 for st in file_status if st is not None and st.status == "ok"}
        superseded = [
            existing_by_sha[h].statement_id for h in ok_shas
            if h in existing_by_sha and existing_by_sha[h].statement_id in superseded
        ]

        # STAGE 4: categorisation agent and entity resolution over the whole case
        entities: Optional[List[ResolvedEntity]] = None
        updated_txns: List[NormalizedTransaction] = all_case_txns
        if ok_count:
            all_stmts = self.vault.get_statements(case_id)
            superseded_set = set(superseded)
            all_stmts = [s for s in all_stmts if s.statement_id not in superseded_set] + [s for s, _ in new_statements]
            manual_merges = self.vault.get_manual_merges(case_id)
            entities, updated_txns = self._run_stage4(
                orchestrator, use_llm, eff_mode, case_obj, all_stmts, all_case_txns, manual_merges,
                s4_info, telemetry, audit_entries, mark,
                decisions=self.vault.get_entity_decisions(case_id),
                previous_entities=self.vault.get_entities(case_id),
            )

        lap("entities")
        report("entities", 1, 1)
        telemetry.stage_details = {"stage1": s1_info, "stage2": s2_info, "stage3": s3_info, "stage4": s4_info}
        tel_dict = telemetry.to_dict()
        final_reports = [r for r in reports if r is not None]
        failed = [r for r in final_reports if r["status"] == "failed"]
        skipped = [r for r in final_reports if r["status"] == "duplicate"]

        by_id = {t.tx_id: t for t in updated_txns}
        batch = IngestBatch(
            case_id=case_id,
            statements=[(s, [by_id.get(t.tx_id, t) for t in txns]) for s, txns in new_statements],
            provenance=provenance,
            updated_transactions=[t for t in updated_txns if t.tx_id in existing_ids],
            entities=entities,
            file_results=[st for st in file_status if st is not None],
            audit=audit_entries + [("INGEST_STATEMENTS", {
                "count": ok_count,
                "files": [r["filename"] for r in final_reports if r["status"] == "ok"],
                "sha256": {r["filename"]: r["sha256_hash"] for r in final_reports if r["status"] == "ok"},
                "failed": [{"filename": r["filename"], "error": r.get("error"), "error_type": r.get("error_type")} for r in failed],
                "duplicates_skipped": [r["filename"] for r in skipped],
                "superseded_statement_ids": superseded,
                "execution_mode": eff_mode,
                "parser_version": p_version,
                "agent_telemetry": tel_dict,
            })],
            settings={f"case_telemetry:{case_id}": tel_dict} if ok_count else {},
            superseded_statement_ids=superseded,
            actor=analyst,
        )
        try:
            self.vault.commit_ingest_batch(batch)
            # Persist raw uploaded input files to permanent case storage
            for fn, raw, _pwd in files:
                sha = hashlib.sha256(raw).hexdigest()
                stmt_id = None
                for fr in batch.file_results:
                    if fr.sha256 == sha:
                        stmt_id = fr.statement_id
                        break
                try:
                    self.vault.save_case_input(case_id, fn, raw, statement_id=stmt_id)
                except Exception as _e:
                    _LOG.warning("Could not persist input file %s: %s", fn, _e)
            lap("commit")
            report("commit", 1, 1)
        except ConflictError:
            raise
        except Exception as exc:
            self.vault.record_ingest_failure(
                case_id,
                [IngestFileResult(fr.filename, "failed" if fr.status == "ok" else fr.status, fr.sha256, fr.file_size_bytes,
                                  error=fr.error or f"Batch not committed: {exc.__class__.__name__}",
                                  error_type=fr.error_type or "BatchCommitError")
                 for fr in batch.file_results],
                reason=f"{exc.__class__.__name__}: {exc}",
                actor=analyst,
            )
            raise

        return {
            "ingested": final_reports,
            "files_ok": ok_count,
            "files_failed": len(failed),
            "files_duplicate": len(skipped),
            "total_case_transactions": len(updated_txns) if ok_count else len(existing_txns),
            "total_resolved_entities": len(entities) if entities is not None else len(self.vault.get_entities(case_id)),
            "agent_telemetry": tel_dict,
            "timings": timings,
        }

    def _run_stage1(
        self,
        orchestrator: StageAgentOrchestrator,
        eff_mode: str,
        filename: str,
        raw_bytes: bytes,
        case_id: str,
        stmt: Optional[BankAccountStatement],
        txns: List[NormalizedTransaction],
        all_case_txns: List[NormalizedTransaction],
        s1_info: Dict[str, Any],
    ) -> Tuple[Optional[BankAccountStatement], List[NormalizedTransaction], Optional[str], Optional[Dict[str, Any]]]:
        """Stage 1 per file. Returns (stmt, txns, note, adopted_meta). The deterministic parse is kept unless the
        LLM result passes ``stage1_should_adopt`` (at least as many rows, no more balance breaks);
        ``adopted_meta`` is the Stage 1 agent's meta when its rows were adopted, else None."""
        if eff_mode == "llm":
            needs = True
        else:  # "both": only as a backup for empty or inconsistent scans, or a failed parse
            if stmt is None:
                needs = True
            else:
                prelim_stmt, prelim_txns, _ = run_anomaly_gate(
                    copy.copy(stmt), [copy.copy(t) for t in txns], existing_case_txns=all_case_txns
                )
                needs = len(prelim_txns) == 0 or (prelim_stmt.is_ocr_derived and prelim_stmt.balance_mismatch_count > 0)
        if not needs:
            return stmt, txns, None, None
        try:
            baseline = (stmt, txns) if stmt is not None else None
            l_stmt, l_txns, l_meta = orchestrator.stage1_agent.extract_statement(
                filename, raw_bytes, case_id, baseline=baseline
            )
        except Exception as exc:  # noqa: BLE001 - LLM failure falls back to the deterministic parse
            return stmt, txns, f"Stage 1 Vision Agent skipped ({exc}); kept the deterministic parse.", None
        if not l_txns:
            s1_info["rejected"] += 1
            reason = (l_meta or {}).get("adoption_reason") or "Stage 1 LLM extraction returned no rows"
            return stmt, txns, reason, None
        if stmt is not None:
            ok, reason = stage1_should_adopt(stmt, txns, l_stmt, l_txns)
            if not ok:
                s1_info["rejected"] += 1
                return stmt, txns, reason, None
        s1_info["invoked"] = True
        s1_info["files_recovered"] += 1
        return l_stmt, list(l_txns), (
            f"Stage 1 Vision Agent extracted {len(l_txns)} rows"
            + (f" (deterministic parse had {len(txns)})." if stmt is not None else " (deterministic parse failed).")
        ), dict(l_meta or {})

    def _run_stage4(
        self,
        orchestrator: StageAgentOrchestrator,
        use_llm: bool,
        eff_mode: str,
        case_obj: InvestigationCase,
        statements: List[BankAccountStatement],
        txns: List[NormalizedTransaction],
        manual_merges: Dict[str, str],
        s4_info: Dict[str, Any],
        telemetry: Any,
        audit_entries: List[Tuple[str, Dict[str, Any]]],
        mark: Callable[[int], None],
        decisions: Optional[Sequence[Dict[str, Any]]] = None,
        previous_entities: Optional[Sequence[ResolvedEntity]] = None,
    ) -> Tuple[List[ResolvedEntity], List[NormalizedTransaction]]:
        if decisions is None:
            decisions = self.vault.get_entity_decisions(case_obj.case_id)
        if previous_entities is None:
            previous_entities = self.vault.get_entities(case_obj.case_id)
        if use_llm and self.llm_config.enable_stage4_categorization and txns:
            try:
                updated, entities, llm_merges, r4 = orchestrator.stage4_agent.classify_and_resolve(
                    case_id=case_obj.case_id,
                    statements=statements,
                    txns=txns,
                    similarity_threshold=case_obj.similarity_threshold,
                    manual_merges=manual_merges,
                    force_all=(eff_mode == "llm"),
                    confidence_threshold=case_obj.confidence_threshold,
                    decisions=decisions,
                    previous_entities=previous_entities,
                )
                if r4.get("invoked"):
                    mark(4)
                    s4_info["invoked"] = True
                    s4_info["transactions_classified"] = int(r4.get("transactions_classified", 0))
                    s4_info["transactions_suggested"] = int(r4.get("transactions_suggested", 0) or 0)
                    s4_info["entities_merged"] = int(r4.get("entities_merged", 0))
                if llm_merges:
                    audit_entries.append(("LLM_SUGGESTED_MERGES", {
                        "provider": self.llm_config.provider,
                        "merges": [{"alias": a, "canonical": c} for a, c in sorted(dict(llm_merges).items())],
                    }))
                return entities, updated
            except Exception as exc:  # noqa: BLE001 - fall back to deterministic resolution
                telemetry.warnings.append(f"Stage 4 Categorization/Entity Agent skipped: {exc}")
        entities, updated = self._resolve_entities(
            case_obj.case_id, statements, txns, case_obj.similarity_threshold,
            manual_merges=manual_merges, decisions=decisions, previous_entities=previous_entities,
        )
        return entities, updated

    def run_case_llm_agents(
        self,
        case_id: str,
        execution_mode: Optional[str] = None,
        stages: Optional[List[int]] = None,
    ) -> Dict[str, Any]:
        """Re-run Stage 2–4 agents on an already-ingested case. ``execution_mode=None`` uses the saved mode."""
        self._require_case(case_id)
        with self._case_lock(case_id):
            return self._run_agents_locked(case_id, execution_mode, stages)

    def _run_agents_locked(
        self, case_id: str, execution_mode: Optional[str], stages: Optional[List[int]]
    ) -> Dict[str, Any]:
        case_obj = self._require_case(case_id)
        selected_stages = {int(s) for s in (stages or [2, 3, 4])}
        if not selected_stages <= {2, 3, 4}:
            raise ValueError("stages must be a subset of [2, 3, 4]")
        stmts = self.vault.get_statements(case_id)
        txns = self.vault.get_transactions(case_id)
        if not txns:
            raise ValueError("No transactions available in this case. Upload statements first.")

        orchestrator = StageAgentOrchestrator(self.llm_config, transport_fn=self._llm_transport_fn)
        eff_mode, use_llm, mode_warning = orchestrator.should_use_llm(execution_mode)
        telemetry = orchestrator.make_telemetry(eff_mode, warning=mode_warning)
        s2_info: Dict[str, Any] = {"invoked": False, "rows_repaired": 0, "repairs_rejected": 0, "metadata_updated": False}
        s3_info: Dict[str, Any] = {"invoked": False, "rows_enriched": 0}
        s4_info: Dict[str, Any] = {"invoked": False, "transactions_classified": 0, "entities_merged": 0}
        audit_entries: List[Tuple[str, Dict[str, Any]]] = []

        def mark(stage: int) -> None:
            if stage not in telemetry.stages_invoked:
                telemetry.stages_invoked.append(stage)

        txns_by_stmt: Dict[str, List[NormalizedTransaction]] = {}
        for t in txns:
            txns_by_stmt.setdefault(t.statement_id, []).append(t)

        rebuilt_txns: List[NormalizedTransaction] = []
        updated_stmts: List[BankAccountStatement] = []
        for stmt in stmts:
            st_txns = sorted(txns_by_stmt.get(stmt.statement_id, []), key=lambda x: x.row_index)
            if 2 in selected_stages:
                # Earlier statements are the duplicate-detection context, as during ingest (BE-13).
                stmt, st_txns, _ = run_anomaly_gate(stmt, st_txns, existing_case_txns=list(rebuilt_txns))
                if use_llm and self.llm_config.enable_stage2_reconciliation:
                    try:
                        stmt, st_txns, r2 = orchestrator.stage2_agent.reconcile(
                            stmt, st_txns, existing_case_txns=list(rebuilt_txns), force_llm=(eff_mode == "llm")
                        )
                        if r2.get("invoked"):
                            pre_enriched = False
                            mark(2)
                            s2_info["invoked"] = True
                            s2_info["rows_repaired"] += int(r2.get("rows_repaired", 0))
                            s2_info["repairs_rejected"] += int(r2.get("repairs_rejected", 0) or 0)
                            s2_info["metadata_updated"] = s2_info["metadata_updated"] or bool(r2.get("metadata_updated"))
                        if r2.get("repairs_audit") or r2.get("repairs_rejected"):
                            audit_entries.append(("LLM_STAGE2_REPAIR", {
                                "statement_id": stmt.statement_id,
                                "filename": stmt.source_filename,
                                "provider": self.llm_config.provider,
                                "repairs": r2.get("repairs_audit") or [],
                                "rejected": int(r2.get("repairs_rejected", 0) or 0),
                            }))
                    except Exception as exc:  # noqa: BLE001
                        telemetry.warnings.append(f"Stage 2 Agent failed on {stmt.source_filename}: {exc}")
            updated_stmts.append(stmt)
            rebuilt_txns.extend(st_txns)

        if 3 in selected_stages:
            rebuilt_txns = [enrich_transaction_narration(t) for t in rebuilt_txns]
            if use_llm and self.llm_config.enable_stage3_narration:
                try:
                    rebuilt_txns, r3 = orchestrator.stage3_agent.enrich(rebuilt_txns, force_all=(eff_mode == "llm"))
                    if r3.get("invoked"):
                        mark(3)
                        s3_info["invoked"] = True
                        s3_info["rows_enriched"] = int(r3.get("rows_enriched", 0))
                except Exception as exc:  # noqa: BLE001
                    telemetry.warnings.append(f"Stage 3 Agent failed: {exc}")

        manual_merges = self.vault.get_manual_merges(case_id)
        if 4 in selected_stages:
            self._refresh_rules()
            categorizer = self._categorizer_for(case_obj)
            # Analyst decisions (USER_RULE rows) are never re-categorised.
            baseline_targets = [t for t in rebuilt_txns if t.categorization_source != "USER_RULE"]
            if baseline_targets:
                categorizer.classify_batch(baseline_targets)
            entities, rebuilt_txns = self._run_stage4(
                orchestrator, use_llm, eff_mode, case_obj, updated_stmts, rebuilt_txns, manual_merges,
                s4_info, telemetry, audit_entries, mark,
            )
        else:
            entities, rebuilt_txns = self._resolve_entities(
                case_id, updated_stmts, rebuilt_txns, case_obj.similarity_threshold, manual_merges=manual_merges,
            )

        telemetry.stage_details = {
            "stage1": {"invoked": False, "note": "Run during file upload/ingestion for Vision scans"},
            "stage2": s2_info,
            "stage3": s3_info,
            "stage4": s4_info,
        }
        tel_dict = telemetry.to_dict()
        self.vault.commit_case_update(
            case_id,
            statements=updated_stmts,
            transactions=rebuilt_txns,
            entities=entities,
            audit=audit_entries + [("RUN_LLM_AGENTS", tel_dict)],
            settings={f"case_telemetry:{case_id}": tel_dict},
            actor=self.analyst,
        )
        return {
            "updated_transactions": len(rebuilt_txns),
            "resolved_entities": len(entities),
            "agent_telemetry": tel_dict,
        }

    # ------------------------------------------------------------------------------------------ suspense
    def preview_resolution(self, case_id: str, tx_id: str) -> Dict[str, Any]:
        """Suspense rows a resolution of ``tx_id`` would propagate to, for analyst confirmation (NLP-04)."""
        self._require_case(case_id)
        txns = self.vault.get_transactions(case_id)
        if not any(t.tx_id == tx_id for t in txns):
            raise KeyError(f"Transaction '{tx_id}' not found")
        key, ids = preview_suspense_propagation(txns, tx_id)
        by_id = {t.tx_id: t for t in txns}
        return {
            "tx_id": tx_id,
            "rule_key": key,
            "tx_ids": ids,
            "count": len(ids),
            "transactions": [tx_to_json(by_id[i]) for i in ids[:MAX_TX_PAGE]],
        }

    def resolve_suspense(
        self,
        case_id: str,
        tx_id: str,
        category_id: int,
        custom_ledger_name: Optional[str] = None,
        auto_refresh_similar: bool = True,
        confirmed_tx_ids: Optional[Sequence[str]] = None,
        rule_scope: str = "case",
        learn_single_token_party: bool = False,
    ) -> Dict[str, Any]:
        """Assign a category to a row and optionally to matching *Suspense* rows. ``confirmed_tx_ids`` limits
        propagation to the analyst-confirmed subset of :meth:`preview_resolution`. The learned rule is scoped
        to this case unless ``rule_scope='global'`` (BE-17)."""
        if rule_scope not in RULE_SCOPES:
            raise ValueError(f"rule_scope must be one of {RULE_SCOPES}")
        case_obj = self._require_case(case_id)
        with self._case_lock(case_id):
            txns = self.vault.get_transactions(case_id)
            anchor = next((t for t in txns if t.tx_id == tx_id), None)
            if anchor is None:
                raise KeyError(f"Transaction '{tx_id}' not found")
            before = {t.tx_id: _tx_state(t) for t in txns}
            rule_case = case_id if rule_scope == "case" else None
            categorizer = self._categorizer_for(case_obj)
            updated_cnt, learned_pat = apply_suspense_resolution(
                txns,
                target_tx_id=tx_id,
                new_category_id=int(category_id),
                custom_ledger_name=custom_ledger_name,
                auto_refresh_similar=auto_refresh_similar,
                categorizer=categorizer,
                confirmed_tx_ids=confirmed_tx_ids,
                case_id=rule_case,
                learn_single_token_party=learn_single_token_party,
            )
            changed = [t for t in txns if _tx_state(t) != before[t.tx_id]]
            excluded_ids: List[str] = []
            if learned_pat:
                excluded_ids = categorizer.rule_exclusions_for(learned_pat, anchor.direction.value, rule_case)
                self.vault.save_user_rule(
                    learned_pat, anchor.direction.value, int(category_id), custom_ledger_name, case_id=rule_case,
                    excluded_tx_ids=excluded_ids,
                )
                self._refresh_rules()
            self.vault.commit_case_update(
                case_id,
                transactions=changed,
                audit=[("RESOLVE_SUSPENSE", {
                    "tx_id": tx_id,
                    "category_id": int(category_id),
                    "custom_ledger_name": custom_ledger_name,
                    "updated_count": updated_cnt,
                    "updated_tx_ids": [t.tx_id for t in changed],
                    "learned_pattern": learned_pat,
                    "rule_scope": rule_scope if learned_pat else None,
                    "confirmed_subset": confirmed_tx_ids is not None,
                    "rule_excluded_tx_ids": excluded_ids,
                })],
                actor=self.analyst,
            )
        self._refit_from_labelled_rows()
        return {
            "updated_count": updated_cnt,
            "learned_pattern": learned_pat,
            "rule_scope": rule_scope if learned_pat else None,
            "updated_tx_ids": [t.tx_id for t in changed],
        }

    def toggle_duplicate_ignore(self, case_id: str, tx_id: str) -> Dict[str, Any]:
        self._require_case(case_id)
        with self._case_lock(case_id):
            found = self.vault.get_transactions_by_ids(case_id, [tx_id])
            if not found:
                raise KeyError(f"Transaction '{tx_id}' not found")
            target = found[0]
            target.is_duplicate_ignored = not target.is_duplicate_ignored
            self.vault.commit_case_update(
                case_id,
                transactions=[target],
                audit=[("TOGGLE_DUPLICATE_IGNORE", {"tx_id": tx_id, "ignored": target.is_duplicate_ignored})],
                actor=self.analyst,
            )
        return {"tx_id": tx_id, "is_duplicate_ignored": target.is_duplicate_ignored}

    def recluster_entities(
        self,
        case_id: str,
        similarity_threshold: Optional[float] = None,
        manual_merge: Optional[Tuple[str, str]] = None,
    ) -> Dict[str, Any]:
        case_obj = self._require_case(case_id)
        with self._case_lock(case_id):
            if similarity_threshold is not None:
                case_obj.similarity_threshold = max(0.50, min(1.00, float(similarity_threshold)))
                self.vault.update_case_thresholds(case_id, similarity_threshold=case_obj.similarity_threshold)
            merge_pair = None
            if manual_merge:
                alias, canonical = str(manual_merge[0]).strip(), str(manual_merge[1]).strip()
                if not alias or not canonical:
                    raise ValueError("Both alias_name and canonical_target are required for a merge.")
                self.vault.save_manual_merge(case_id, alias, canonical)
                merge_pair = {"alias": alias.upper(), "canonical": canonical.upper()}

            stmts = self.vault.get_statements(case_id)
            txns = self.vault.get_transactions(case_id)
            manual_merges = self.vault.get_manual_merges(case_id)
            entities, updated_txns = self._resolve_entities(
                case_id, stmts, txns, case_obj.similarity_threshold, manual_merges=manual_merges,
            )
            audit: List[Tuple[str, Dict[str, Any]]] = []
            if merge_pair:
                audit.append(("MANUAL_MERGE", merge_pair))
            audit.append(("ENTITY_RESOLUTION", {
                "similarity_threshold": case_obj.similarity_threshold,
                "entity_count": len(entities),
                "manual_merge": merge_pair,
            }))
            self.vault.commit_case_update(
                case_id, transactions=updated_txns, entities=entities, audit=audit, actor=self.analyst
            )
        return {
            "similarity_threshold": case_obj.similarity_threshold,
            "entities": [e.to_dict() for e in entities],
        }

    # ------------------------------------------------------------------------------------------ cluster review
    @staticmethod
    def _check_threshold(value: Any) -> float:
        try:
            thr = float(value)
        except (TypeError, ValueError) as exc:
            raise ValueError("threshold must be a number between 0.5 and 1.0") from exc
        if not 0.5 <= thr <= 1.0 or thr != thr:
            raise ValueError("threshold must be a number between 0.5 and 1.0")
        return thr

    def _cluster_preview(self, case_id: str, threshold: float) -> Dict[str, Any]:
        snap = self._snapshot(case_id)
        thr = round(float(threshold), 4)

        def compute() -> Dict[str, Any]:
            return preview_clusters(
                case_id, snap.statements, list(snap.transactions), similarity_threshold=thr,
                manual_merges=self.vault.get_manual_merges(case_id),
                decisions=self.vault.get_entity_decisions(case_id),
                previous_entities=snap.entities,
            )

        return self._memo(snap, ("cluster_preview", thr), compute)

    def preview_entity_clusters(self, case_id: str, similarity_threshold: Optional[float] = None) -> Dict[str, Any]:
        """Dry run of entity resolution at a threshold (NLP-11 slider preview). Read-only: nothing is saved."""
        case_obj = self._require_case(case_id)
        thr = case_obj.similarity_threshold if similarity_threshold is None else self._check_threshold(similarity_threshold)
        return self._cluster_preview(case_id, thr)

    def get_entity_decisions(self, case_id: str) -> Dict[str, Any]:
        self._require_case(case_id)
        return {
            "decisions": self.vault.get_entity_decisions(case_id),
            "history": self.vault.get_entity_decision_history(case_id),
            "legacy_manual_merges": [
                {"alias": a, "canonical": c} for a, c in sorted(self.vault.get_manual_merges(case_id).items())
            ],
        }

    def _apply_entity_decision(
        self, case_id: str, action: str, op: Callable[[List[Dict[str, Any]], List[ResolvedEntity]], Tuple[List[Dict[str, Any]], Optional[Dict[str, Any]]]],
    ) -> Dict[str, Any]:
        case_obj = self._require_case(case_id)
        with self._case_lock(case_id):
            decisions = self.vault.get_entity_decisions(case_id)
            current = self.vault.get_entities(case_id)
            new_decisions, decision = op(decisions, current)
            if decision is None:  # undo with nothing to undo
                return {"decision": None, "decisions": decisions,
                        "entities": [e.to_dict() for e in current], "changed": False}
            stmts = self.vault.get_statements(case_id)
            txns = self.vault.get_transactions(case_id)
            previous: List[Any] = list(current)
            touched = set(decision.get("entity_ids") or [])
            entities_before = [
                {"entity_id": e.entity_id, "canonical_name": e.canonical_name, "aliases": list(e.aliases)}
                for e in current if e.entity_id in touched
            ]
            if action == "ENTITY_DECISION_UNDO":
                # Hand the pre-decision versions of the touched entities back to the resolver, so undoing a
                # merge or split restores the original entity IDs (NLP-12) instead of minting new ones.
                before = self._entities_before(case_id, decision.get("decision_id"))
                if before:
                    ids = {b["entity_id"] for b in before}
                    previous = list(before) + [e for e in current if e.entity_id not in ids]
            diagnostics: Dict[str, Any] = {}
            entities, updated_txns = self._resolve_entities(
                case_id, stmts, txns, case_obj.similarity_threshold,
                decisions=new_decisions, previous_entities=previous, diagnostics=diagnostics,
            )
            before_ids = {e.entity_id for e in current}
            after_ids = {e.entity_id for e in entities}
            self.vault.commit_case_update(
                case_id,
                transactions=updated_txns,
                entities=entities,
                entity_decisions=new_decisions,
                decision_events=[(action, {**decision, "entities_before": entities_before})],
                audit=[(action, {
                    "decision": decision,
                    "entity_count_before": len(current),
                    "entity_count_after": len(entities),
                    "new_entity_ids": sorted(after_ids - before_ids),
                    "retired_entity_ids": sorted(before_ids - after_ids),
                })],
                actor=self.analyst,
            )
        return {
            "decision": decision,
            "decisions": new_decisions,
            "entities": [e.to_dict() for e in entities],
            "new_entity_ids": sorted(after_ids - before_ids),
            "retired_entity_ids": sorted(before_ids - after_ids),
            "conflicts": diagnostics.get("conflicts", []),
            "changed": True,
        }

    def _entities_before(self, case_id: str, decision_id: Optional[str]) -> List[Dict[str, Any]]:
        """Snapshot of the touched entities taken when ``decision_id`` was applied (from the history log)."""
        if not decision_id:
            return []
        for row in self.vault.get_entity_decision_history(case_id, limit=100_000):
            if row["decision_id"] == decision_id and row["action"] != "ENTITY_DECISION_UNDO":
                return [b for b in (row["decision"].get("entities_before") or []) if b.get("entity_id")]
        return []

    @staticmethod
    def _note(note: Any) -> str:
        if note is None:
            return ""
        if not isinstance(note, str):
            raise ValueError("note must be a string")
        return note.strip()[:500]

    def split_entity(self, case_id: str, entity_id: str, aliases: Sequence[str], note: Any = None) -> Dict[str, Any]:
        if not entity_id or not isinstance(entity_id, str):
            raise ValueError("entity_id is required")
        if not isinstance(aliases, (list, tuple)) or not aliases or not all(isinstance(a, str) for a in aliases):
            raise ValueError("aliases must be a non-empty list of alias names")
        n = self._note(note)
        return self._apply_entity_decision(
            case_id, "ENTITY_SPLIT",
            lambda d, ents: _er.split_entity(d, ents, entity_id, aliases, actor=self.analyst, note=n),
        )

    def merge_entities(self, case_id: str, source_entity_id: str, target_entity_id: str, note: Any = None) -> Dict[str, Any]:
        if not source_entity_id or not target_entity_id:
            raise ValueError("source_entity_id and target_entity_id are required")
        n = self._note(note)
        return self._apply_entity_decision(
            case_id, "ENTITY_MERGE",
            lambda d, ents: _er.merge_entities(d, ents, str(source_entity_id), str(target_entity_id), actor=self.analyst, note=n),
        )

    def reject_entity_merge(self, case_id: str, entity_a_id: str, entity_b_id: str, note: Any = None) -> Dict[str, Any]:
        if not entity_a_id or not entity_b_id:
            raise ValueError("entity_a_id and entity_b_id are required")
        n = self._note(note)
        return self._apply_entity_decision(
            case_id, "ENTITY_REJECT",
            lambda d, ents: _er.reject_merge(d, ents, str(entity_a_id), str(entity_b_id), actor=self.analyst, note=n),
        )

    def undo_entity_decision(self, case_id: str, decision_id: Optional[str] = None) -> Dict[str, Any]:
        if decision_id is not None and not isinstance(decision_id, str):
            raise ValueError("decision_id must be a string")

        def op(decisions: List[Dict[str, Any]], _ents: List[ResolvedEntity]):
            new_decisions, removed = _er.undo_decision(decisions, decision_id or None)
            if removed is not None:
                # A later decision recorded alias groups that already include this decision's effect, so undoing
                # this one alone would change nothing while the history says it was undone: refuse (409).
                idx = next(i for i, d in enumerate(decisions) if d is removed)
                names = {str(a).strip().upper() for g in (removed.get("groups") or []) for a in g if str(a).strip()}
                later = [
                    d for d in decisions[idx + 1:]
                    if str(d.get("op") or "").lower() == "merge" and names & {str(a).strip().upper() for g in (d.get("groups") or []) for a in g}
                ]
                if later:
                    raise ConflictError(
                        "Later review decisions depend on this one; undo them first (most recent first): "
                        + ", ".join(str(d.get("decision_id")) for d in reversed(later))
                    )
            return new_decisions, removed

        return self._apply_entity_decision(case_id, "ENTITY_DECISION_UNDO", op)

    # ------------------------------------------------------------------------------------------ CypherEdge
    # Tally ledger map (EDGE-10): stored per case; the defaults apply until the analyst saves one.
    def _stored_tally_map(self, case_id: str) -> TallyLedgerMap:
        stored = self.vault.get_case_setting(case_id, TALLY_MAP_SETTING)
        try:
            return TallyLedgerMap.coerce(stored if isinstance(stored, dict) else None)
        except ValueError:
            _LOG.warning("Stored Tally ledger map for %s is invalid; using defaults", case_id)
            return TallyLedgerMap.defaults()

    def get_tally_ledger_map(self, case_id: str) -> Dict[str, Any]:
        self._require_case(case_id)
        stored = self.vault.get_case_setting(case_id, TALLY_MAP_SETTING)
        return {"ledger_map": self._stored_tally_map(case_id).to_dict(), "is_default": stored is None}

    def set_tally_ledger_map(self, case_id: str, data: Any) -> Dict[str, Any]:
        self._require_case(case_id)
        if data is None:
            # Reset to the defaults.
            # Stored as null (read back as "not set") so is_default is true again and the case follows future
            # default changes; setting and audit commit together.
            with self._case_lock(case_id):
                self.vault.commit_case_update(
                    case_id,
                    case_settings={TALLY_MAP_SETTING: None},
                    audit=[("TALLY_MAP_UPDATE", {"reset": True})],
                    actor=self.analyst,
                )
            return self.get_tally_ledger_map(case_id)
        if not isinstance(data, dict):
            raise ValueError("ledger_map must be an object")
        ledger_map = TallyLedgerMap.from_dict(data)  # raises ValueError listing every problem
        stored = ledger_map.to_dict()
        with self._case_lock(case_id):
            self.vault.commit_case_update(
                case_id,
                case_settings={TALLY_MAP_SETTING: stored},
                audit=[("TALLY_MAP_UPDATE", {"ledger_map": stored})],
                actor=self.analyst,
            )
        return {"ledger_map": stored, "is_default": False}

    def _tally_export(
        self,
        case_id: str,
        include_suspense: bool,
        company_name: Optional[str],
        use_trade_vouchers: Optional[bool],
    ) -> Dict[str, Any]:
        case_obj = self._require_case(case_id)
        snap = self._snapshot(case_id)
        return build_tally_export(
            case_obj.title, snap.statements, list(snap.transactions), include_suspense,
            company_name=(company_name or None),
            use_trade_vouchers=None if use_trade_vouchers is None else bool(use_trade_vouchers),
            ledger_map=self._stored_tally_map(case_id),
        )

    def get_tally_preview(
        self,
        case_id: str,
        include_suspense: bool = False,
        company_name: Optional[str] = None,
        use_trade_vouchers: Optional[bool] = None,
        include_xml: bool = True,
    ) -> Dict[str, Any]:
        """Structured preview (EDGE-28): stats, vouchers, ledgers, groups, xml_preview / xml_truncated /
        xml_sha256 / xml_bytes and the effective ledger_map. ``include_xml=False`` (the HTTP default) leaves the
        full XML out. ``use_trade_vouchers=None`` keeps the stored ledger map's setting."""
        res = self._tally_export(case_id, include_suspense, company_name, use_trade_vouchers)
        out = {k: v for k, v in res.items() if k != "xml"}
        if include_xml:
            out["xml"] = res["xml"]
        return out

    @staticmethod
    def _scalar_stats(stats: Dict[str, Any]) -> Dict[str, Any]:
        return {k: v for k, v in stats.items() if v is None or isinstance(v, (int, float, str, bool))}

    def export_tally_xml(
        self,
        case_id: str,
        include_suspense: bool = False,
        company_name: Optional[str] = None,
        use_trade_vouchers: Optional[bool] = None,
    ) -> Dict[str, Any]:
        """Tally XML for download; the export (with the XML's SHA-256) is written to the audit log."""
        res = self._tally_export(case_id, include_suspense, company_name, use_trade_vouchers)
        self._audit(case_id, "TALLY_XML_EXPORT", {
            "include_suspense": include_suspense,
            "company_name": company_name,
            "use_trade_vouchers": res["stats"].get("use_trade_vouchers", use_trade_vouchers),
            "xml_sha256": hashlib.sha256(res["xml"].encode("utf-8")).hexdigest(),
            "stats": self._scalar_stats(res["stats"]),
        })
        try:
            self.vault.save_case_output(
                case_id, "tally_xml", f"{case_id}_tally_import.xml", res["xml"].encode("utf-8"),
                metadata={"include_suspense": include_suspense, "company_name": company_name},
            )
        except Exception as _e:
            _LOG.warning("Could not persist export output for case %s: %s", case_id, _e)
        return res

    def push_tally(
        self,
        case_id: str,
        host: str = "127.0.0.1",
        port: int = 9000,
        include_suspense: bool = False,
        company_name: Optional[str] = None,
        use_trade_vouchers: Optional[bool] = None,
    ) -> Dict[str, Any]:
        preview = self._tally_export(case_id, include_suspense, company_name, use_trade_vouchers)
        res = push_to_tally_http(preview["xml"], host=host, port=port)
        self._audit(case_id, "TALLY_PUSH", {
            "host": host,
            "port": port,
            "connected": res.get("connected"),
            "success": res.get("success"),
            "created": res.get("created"),
            "altered": res.get("altered"),
            "errors": res.get("errors"),
            "exceptions": res.get("exceptions"),
            "line_errors": (res.get("line_errors") or [])[:50],
            "xml_sha256": hashlib.sha256(preview["xml"].encode("utf-8")).hexdigest(),
            "stats": self._scalar_stats(preview["stats"]),
        })
        return {**res, "stats": preview["stats"]}

    def export_tally_xlsx(
        self,
        case_id: str,
        include_suspense: bool = False,
        company_name: Optional[str] = None,
        use_trade_vouchers: Optional[bool] = None,
    ) -> bytes:
        """Excel-to-Tally workbook (EDGE-13), built with the case's stored ledger map."""
        case_obj = self._require_case(case_id)
        snap = self._snapshot(case_id)
        lmap = self._stored_tally_map(case_id)
        utv = lmap.use_trade_vouchers if use_trade_vouchers is None else bool(use_trade_vouchers)
        body = export_tally_workbook_xlsx(
            case_obj.title, snap.statements, list(snap.transactions), include_suspense,
            company_name=(company_name or lmap.company_name or None), use_trade_vouchers=utv,
            voucher_prefix=lmap.voucher_prefix or "CS", ledger_map=lmap,
        )
        self._audit(case_id, "EXPORT_TALLY_XLSX", {
            "include_suspense": include_suspense, "company_name": company_name, "use_trade_vouchers": utv,
            "sha256": hashlib.sha256(body).hexdigest(),
        })
        try:
            self.vault.save_case_output(
                case_id, "tally_import", f"{case_id}_tally_import.xlsx", body,
                metadata={"include_suspense": include_suspense, "company_name": company_name},
            )
        except Exception as _e:
            _LOG.warning("Could not persist export output for case %s: %s", case_id, _e)
        return body

    def export_itr_xlsx(self, case_id: str, fy: Optional[str] = None) -> bytes:
        """FY compliance workbook (EDGE-19). ValueError (HTTP 400) for an FY with no transactions."""
        self._require_case(case_id)
        snap = self._snapshot(case_id)
        body = export_itr_workbook_xlsx(list(snap.transactions), snap.statements, fy=fy or None)
        self._audit(case_id, "EXPORT_ITR_XLSX", {"fy": fy, "sha256": hashlib.sha256(body).hexdigest()})
        try:
            self.vault.save_case_output(
                case_id, "itr_compliance", f"{case_id}_itr_{fy or 'latest'}.xlsx", body,
                metadata={"fy": fy},
            )
        except Exception as _e:
            _LOG.warning("Could not persist export output for case %s: %s", case_id, _e)
        return body

    def get_itr_report(self, case_id: str, fy: Optional[str] = None) -> Dict[str, Any]:
        snap = self._snapshot(case_id)
        return generate_itr_compliance_report(list(snap.transactions), fy=fy or None, statements=snap.statements)

    def get_loan_report(
        self,
        case_id: str,
        foir_pct: float = 55.0,
        annual_rate: float = 11.5,
        tenure_months: int = 60,
        business_income_factor: float = 0.20,
        abb_income_factor: float = 0.50,
        monthly_income_override: Optional[float] = None,
    ) -> Dict[str, Any]:
        snap = self._snapshot(case_id)
        return compute_loan_eligibility_report(
            list(snap.transactions), foir_pct=foir_pct, annual_interest_rate=annual_rate,
            tenure_months=tenure_months, statements=snap.statements,
            business_income_factor=business_income_factor, abb_income_factor=abb_income_factor,
            monthly_income_override=monthly_income_override,
        )

    # ------------------------------------------------------------------------------------------ bulk dedup
    def dedup_transactions(self, case_id: str, apply: bool = False) -> Dict[str, Any]:
        """FR-ING-06.2 one-click deduplication across the case's statements. ``apply=False`` previews the
        duplicate groups; ``apply=True`` marks every non-kept row ignored (DUPLICATE_SUSPECT), persists only the
        changed rows and audits BULK_DEDUP with the groups."""
        self._require_case(case_id)
        with self._case_lock(case_id):
            stmts = self.vault.get_statements(case_id)
            txns = self.vault.get_transactions(case_id)
            acct = {s.statement_id: s.account_number for s in stmts}
            groups = find_duplicate_groups(txns, acct)
            changed: List[str] = []
            if apply and groups:
                changed = mark_bulk_duplicates_ignored(txns, acct)
                if changed:
                    ids = set(changed)
                    self.vault.commit_case_update(
                        case_id,
                        transactions=[t for t in txns if t.tx_id in ids],
                        audit=[("BULK_DEDUP", {"groups": groups, "changed_tx_ids": changed})],
                        actor=self.analyst,
                    )
        return {
            "applied": bool(apply),
            "groups": groups,
            "group_count": len(groups),
            "duplicate_count": sum(len(g.get("duplicate_tx_ids") or []) for g in groups),
            "changed_tx_ids": changed,
        }

    # ------------------------------------------------------------------------------------------ CypherTrace
    def get_mule_assessments(self, case_id: str) -> List[Dict[str, Any]]:
        snap = self._snapshot(case_id)
        return [m.to_dict() for m in self._mules(snap)]

    def get_network_graph(
        self,
        case_id: str,
        date_from: Optional[str] = None,
        date_to: Optional[str] = None,
        min_amount: float = 0.0,
        channel: Optional[str] = None,
        keyword: Optional[str] = None,
        max_amount: Optional[float] = None,
        ifsc: Optional[str] = None,
        max_nodes: Optional[int] = None,
        layout: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Node-capped network graph (``max_nodes``, default GRAPH_DEFAULT_MAX_NODES; ``layout`` auto|server|
        seed|none). Centrality and summary totals always describe the full filtered graph."""
        snap = self._snapshot(case_id)
        filters = self._graph_filters(min_amount, max_amount, ifsc)
        if max_nodes is not None and not GRAPH_MAX_NODES_RANGE[0] <= int(max_nodes) <= GRAPH_MAX_NODES_RANGE[1]:
            raise ValueError(f"max_nodes must be between {GRAPH_MAX_NODES_RANGE[0]} and {GRAPH_MAX_NODES_RANGE[1]}")
        return self._graph(
            snap, max_nodes=max_nodes, layout=layout, date_from=date_from or None, date_to=date_to or None,
            channel_filter=(channel or "").strip() or None, keyword=(keyword or "").strip() or None, **filters,
        )

    @staticmethod
    def _graph_filters(min_amount: float, max_amount: Optional[float], ifsc: Optional[str]) -> Dict[str, Any]:
        if max_amount is not None and float(max_amount) < float(min_amount or 0.0):
            raise ValueError("max_amount must be greater than or equal to min_amount")
        parts = [p.strip().upper() for p in (ifsc or "").split(",") if p.strip()]
        if len(parts) > 20 or any(not re.fullmatch(r"[A-Z0-9]{4,11}", p) for p in parts):
            raise ValueError(
                "Invalid IFSC filter; use full IFSCs (e.g. SBIN0001234) or bank prefixes, comma-separated (max 20)"
            )
        return {
            "min_amount": float(min_amount or 0.0),
            "max_amount": float(max_amount) if max_amount is not None else None,
            "ifsc": ",".join(parts) or None,
        }

    def expand_network_node(
        self,
        case_id: str,
        entity_id: str,
        limit: int = 50,
        date_from: Optional[str] = None,
        date_to: Optional[str] = None,
        min_amount: float = 0.0,
        channel: Optional[str] = None,
        keyword: Optional[str] = None,
        max_amount: Optional[float] = None,
        ifsc: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Expand-on-demand (TG-04): one node's heaviest ``limit`` edges and their neighbours, under the same
        filters as the graph view. Unknown entity -> KeyError (404)."""
        if not entity_id:
            raise ValueError("entity_id is required")
        if not GRAPH_EXPAND_LIMIT[0] <= int(limit) <= GRAPH_EXPAND_LIMIT[1]:
            raise ValueError(f"limit must be between {GRAPH_EXPAND_LIMIT[0]} and {GRAPH_EXPAND_LIMIT[1]}")
        snap = self._snapshot(case_id)
        filters = self._graph_filters(min_amount, max_amount, ifsc)
        full = self._graph_full(
            snap, date_from=date_from or None, date_to=date_to or None,
            channel_filter=(channel or "").strip() or None, keyword=(keyword or "").strip() or None, **filters,
        )
        return graph_neighbourhood(full, entity_id, limit=int(limit), max_edge_tx_ids=GRAPH_EDGE_TX_IDS)

    def get_bidirectional(
        self,
        case_id: str,
        entity_a_id: Optional[str] = None,
        entity_b_id: Optional[str] = None,
        rapid_window_hours: float = 48.0,
        round_trip_window_hours: float = 720.0,
        amount_tolerance: float = 0.10,
        date_from: Optional[str] = None,
        date_to: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Pair list plus the selected pair's analysis (TG-05/06/18). Bad parameters raise ValueError."""
        snap = self._snapshot(case_id)
        pairs = self._memo(
            snap, ("bi_pairs",), lambda: list_all_bidirectional_pairs(snap.statements, list(snap.transactions), snap.entities)
        )
        if not entity_a_id or not entity_b_id:
            if pairs:
                entity_a_id = pairs[0]["entity_a_id"]
                entity_b_id = pairs[0]["entity_b_id"]
        detail = None
        if entity_a_id and entity_b_id:
            ids = {e.entity_id for e in snap.entities}
            for eid in (entity_a_id, entity_b_id):
                if eid not in ids:
                    raise KeyError(f"Entity '{eid}' not found in this case")
            a, b = entity_a_id, entity_b_id
            params = (float(rapid_window_hours), float(round_trip_window_hours), float(amount_tolerance),
                      date_from or None, date_to or None)
            detail = self._memo(
                snap, ("bi_detail", a, b) + params,
                lambda: analyze_bidirectional_flow(
                    snap.statements, list(snap.transactions), snap.entities, a, b,
                    rapid_window_hours=params[0], round_trip_window_hours=params[1], amount_tolerance=params[2],
                    date_from=params[3], date_to=params[4],
                ).to_dict(),
            )
        return {"pairs": pairs, "selected_detail": detail}

    @staticmethod
    def _layering_source_key(source_entity_id: Optional[str], root_tx_id: Optional[str]) -> Optional[str]:
        if root_tx_id:
            return f"tx:{root_tx_id}"
        return source_entity_id or None

    @staticmethod
    def _check_trace_depth(trace_depth: Optional[int], max_depth: Optional[int] = None) -> None:
        if trace_depth is not None and not 1 <= int(trace_depth) <= MAX_TRACE_DEPTH:
            raise ValueError(f"trace_depth must be between 1 and {MAX_TRACE_DEPTH}")
        if max_depth is not None and not 1 <= int(max_depth) <= MAX_TRACE_DEPTH:
            raise ValueError(f"max_depth must be between 1 and {MAX_TRACE_DEPTH}")

    def get_layering(
        self,
        case_id: str,
        source_entity_id: Optional[str] = None,
        window_days: int = 14,
        max_depth: int = 4,
        mode: str = "fifo",
        root_tx_id: Optional[str] = None,
        date_from: Optional[str] = None,
        date_to: Optional[str] = None,
        trace_depth: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Layering tree (TF-03..07). ``root_tx_id`` roots the trace at one credit; per-branch Misc paths stored
        for this source are applied. Bad parameters raise ValueError (HTTP 400)."""
        self._check_trace_depth(trace_depth, max_depth)
        if not 0 < int(window_days) <= 366:
            raise ValueError("window_days must be between 1 and 366")
        snap = self._snapshot(case_id)
        candidates = [e for e in snap.entities if not is_system_entity(e)]
        if not source_entity_id and not root_tx_id and candidates:
            # Default credit source (unchanged behaviour): the demo's named sender, else the top entity.
            ayaz = next((e for e in candidates if "AYAZ" in e.canonical_name), None)
            source_entity_id = ayaz.entity_id if ayaz else candidates[0].entity_id
        misc = frozenset(snap.misc_ids)
        source_key = self._layering_source_key(source_entity_id, root_tx_id)
        paths = tuple(snap.misc_paths.get(source_key or "", []))

        def compute() -> Dict[str, Any]:
            return build_layering_tree(
                snap.statements, list(snap.transactions), snap.entities, source_entity_id,
                window_days=window_days, max_depth=max_depth, misc_entity_ids=set(misc), mode=mode,
                root_tx_id=root_tx_id or None, misc_paths=list(paths), date_from=date_from or None,
                date_to=date_to or None, trace_depth=trace_depth,
            )

        key = ("layering", source_entity_id, int(window_days), int(max_depth), mode, misc, root_tx_id or None,
               paths, date_from or None, date_to or None, trace_depth)
        tree = self._memo(snap, key, compute) if (source_entity_id or root_tx_id) else {"root": None, "legend_counts": {}}
        return {
            **tree,
            "source_candidates": [
                {"entity_id": e.entity_id, "name": e.canonical_name, "has_statement": e.has_statement}
                for e in candidates
            ],
            "selected_source_id": source_entity_id,
            "source_key": source_key,
        }

    def expand_layering(
        self,
        case_id: str,
        source_entity_id: Optional[str],
        node_path: Any,
        window_days: int = 14,
        mode: str = "fifo",
        root_tx_id: Optional[str] = None,
        date_from: Optional[str] = None,
        date_to: Optional[str] = None,
        trace_depth: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Children of one layering node, for lazy drill-down (TF-03/05)."""
        self._check_trace_depth(trace_depth)
        if not node_path:
            raise ValueError("path is required")
        if not 0 < int(window_days) <= 366:
            raise ValueError("window_days must be between 1 and 366")
        path_key = node_path if isinstance(node_path, str) else misc_path_key([str(x) for x in node_path])
        if len(path_key.split(PATH_SEPARATOR)) > MAX_PATH_LEN:
            raise ValueError(f"path is longer than {MAX_PATH_LEN} nodes")
        snap = self._snapshot(case_id)
        if not source_entity_id and not root_tx_id:
            source_entity_id = path_key.split(PATH_SEPARATOR)[0]
        misc = frozenset(snap.misc_ids)
        source_key = self._layering_source_key(source_entity_id, root_tx_id)
        paths = tuple(snap.misc_paths.get(source_key or "", []))

        def compute() -> Dict[str, Any]:
            return expand_layering_node(
                snap.statements, list(snap.transactions), snap.entities, source_entity_id, path_key,
                window_days=window_days, mode=mode, misc_entity_ids=set(misc), misc_paths=list(paths),
                root_tx_id=root_tx_id or None, date_from=date_from or None, date_to=date_to or None,
                trace_depth=trace_depth,
            )

        key = ("layering_expand", source_entity_id, path_key, int(window_days), mode, misc, root_tx_id or None,
               paths, date_from or None, date_to or None, trace_depth)
        return self._memo(snap, key, compute)

    def toggle_layering_misc(
        self,
        case_id: str,
        entity_id: Optional[str] = None,
        move_to_misc: bool = True,
        path: Any = None,
        source_entity_id: Optional[str] = None,
        root_tx_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Move an entity to Misc everywhere (``entity_id``) or one tree branch only (``path``, TF-06)."""
        self._require_case(case_id)
        known = {e.entity_id for e in self.vault.get_entities(case_id)}
        if path:
            if isinstance(path, str):
                parts = [p for p in path.split(PATH_SEPARATOR) if p]
            elif isinstance(path, list) and all(isinstance(p, str) for p in path):
                parts = [p for p in path if p]
            else:
                raise ValueError("path must be a list of entity ids or a 'id>id>id' path key")
            if not parts or len(parts) > MAX_PATH_LEN:
                raise ValueError(f"path must have between 1 and {MAX_PATH_LEN} nodes")
            if parts[-1] == "MISC":
                parts = parts[:-1]
            unknown = [p for p in parts if p not in known]
            if unknown:
                raise KeyError(f"Entity '{unknown[0]}' not found in this case")
            if len(parts) < 2:
                raise ValueError("A branch path needs the root and at least one child; use entity_id for the root")
            source_key = self._layering_source_key(source_entity_id or parts[0], root_tx_id)
            path_key = misc_path_key(parts)
            with self._case_lock(case_id):
                paths = self.vault.toggle_misc_path(
                    case_id, source_key, path_key, move_to_misc=move_to_misc,
                    audit=("LAYERING_MISC_TOGGLE", {"path": path_key, "source_key": source_key,
                                                    "move_to_misc": move_to_misc, "scope": "path"}),
                    actor=self.analyst,
                )
            return {"scope": "path", "source_key": source_key, "misc_paths": paths,
                    "misc_entity_ids": sorted(self.vault.get_misc_entities(case_id))}
        entity_id = str(entity_id or "").strip()
        if not entity_id:
            raise ValueError("entity_id or path is required")
        if entity_id not in known:
            raise KeyError(f"Entity '{entity_id}' not found in this case")
        with self._case_lock(case_id):
            misc_ids = self.vault.toggle_misc_entity(case_id, entity_id, move_to_misc=move_to_misc)
            self._audit(case_id, "LAYERING_MISC_TOGGLE", {"entity_id": entity_id, "move_to_misc": move_to_misc})
        return {"scope": "entity", "misc_entity_ids": sorted(misc_ids)}

    def get_loops(
        self,
        case_id: str,
        max_days: int = 30,
        min_retention: float = 0.50,
        max_cycle_length: Optional[int] = None,
        time_budget_s: Optional[float] = None,
    ) -> List[Dict[str, Any]]:
        return self.get_loops_report(case_id, max_days, min_retention, max_cycle_length, time_budget_s)["loops"]

    def get_loops_report(
        self,
        case_id: str,
        max_days: int = 30,
        min_retention: float = 0.50,
        max_cycle_length: Optional[int] = None,
        time_budget_s: Optional[float] = None,
        max_instances_per_loop: Optional[int] = None,
        hub_degree_cap: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Loops plus detection metadata (truncated, truncation_reason, elapsed_s, max_cycle_length,
        hub_degree_cap, excluded_hubs...). ``hub_degree_cap``: None keeps the detector default, 0 disables hub
        exclusion. The serialised report is memoised per case revision, and the exports reuse the same loops."""
        if max_instances_per_loop is not None and not 1 <= int(max_instances_per_loop) <= 500:
            raise ValueError("max_instances_per_loop must be between 1 and 500")
        if hub_degree_cap is not None and int(hub_degree_cap) != 0 and not 2 <= int(hub_degree_cap) <= 100_000:
            raise ValueError("hub_degree_cap must be 0 (off) or between 2 and 100000")
        cap: Any = "default" if hub_degree_cap is None else (None if int(hub_degree_cap) == 0 else int(hub_degree_cap))
        snap = self._snapshot(case_id)
        args = (
            int(max_days), float(min_retention),
            LOOP_DEFAULT_CYCLE_LENGTH if max_cycle_length is None else int(max_cycle_length),
            time_budget_s,
            LOOP_DEFAULT_MAX_INSTANCES if max_instances_per_loop is None else int(max_instances_per_loop),
            cap,
        )

        def compute() -> Dict[str, Any]:
            result = self._loops(snap, *args)
            meta = result.meta() if hasattr(result, "meta") else {}
            return {"loops": [lp.to_dict() for lp in result], **meta}

        return self._memo(snap, ("loops_report",) + args, compute)

    def get_financial_profile(self, case_id: str, entity_id: Optional[str] = None) -> Dict[str, Any]:
        snap = self._snapshot(case_id)
        ents = snap.entities
        if not entity_id and ents:
            with_stmt = next((e for e in ents if e.has_statement), ents[0])
            entity_id = with_stmt.entity_id
        if not entity_id:
            return {"error": "No entities available"}
        eid = entity_id
        return self._memo(
            snap, ("profile", eid),
            lambda: build_individual_financial_profile(eid, snap.statements, list(snap.transactions), ents, self._mules(snap)),
        )

    # ------------------------------------------------------------------------------------------ exports
    def _llm_provenance(self, case_id: str) -> Tuple[Optional[bool], Optional[str]]:
        """Whether cloud AI processed this case, from recorded telemetry and the audit log (BE-19).
        Returns (None, None) when no processing record exists (e.g. cases ingested before telemetry)."""
        recorded = False
        provider: Optional[str] = None
        tel = self.vault.get_llm_settings(f"case_telemetry:{case_id}")
        records: List[Dict[str, Any]] = [tel] if tel else []
        for row in self.vault.get_audit_logs(case_id, limit=100_000):
            d = row.get("details") or {}
            if row["action"] == "INGEST_STATEMENTS" and isinstance(d.get("agent_telemetry"), dict):
                records.append(d["agent_telemetry"])
            elif row["action"] == "RUN_LLM_AGENTS":
                records.append(d)
            elif row["action"] in ("LLM_STAGE2_REPAIR", "LLM_SUGGESTED_MERGES"):
                records.append({"stages_invoked": [2], "provider": d.get("provider")})
        for rec in records:
            if "stages_invoked" in rec:
                recorded = True
                if rec.get("stages_invoked"):
                    provider = provider or rec.get("provider")
                    return True, provider
        return (False, None) if recorded else (None, None)

    def export_xlsx(self, case_id: str) -> bytes:
        case_obj = self._require_case(case_id)
        snap = self._snapshot(case_id)
        loops = self._loops(snap)
        pairs = self._memo(
            snap, ("bi_pairs",), lambda: list_all_bidirectional_pairs(snap.statements, list(snap.transactions), snap.entities)
        )
        self._audit(case_id, "EXPORT_XLSX", {"statements": len(snap.statements), "loops_truncated": bool(getattr(loops, "truncated", False))})
        body = export_audit_workbook_xlsx(
            case_obj, snap.statements, list(snap.transactions), snap.entities, self._mules(snap), loops=list(loops),
            bidirectional_pairs=pairs,
        )
        try:
            self.vault.save_case_output(case_id, "audit_workbook", f"{case_id}_audit_report.xlsx", body)
        except Exception as _e:
            _LOG.warning("Could not persist export output for case %s: %s", case_id, _e)
        return body

    def export_profile(self, case_id: str, entity_id: Optional[str], fmt: str) -> Tuple[bytes, str, str]:
        """Entity financial profile as html / xlsx / pdf (TG-17). Returns (body, content_type, filename).
        Unknown format -> ValueError (400); unknown entity -> KeyError (404)."""
        fmt_key = str(fmt or "").strip().lower()
        if fmt_key not in PROFILE_EXPORT_FORMATS:
            raise ValueError(f"format must be one of {', '.join(PROFILE_EXPORT_FORMATS)}")
        case_obj = self._require_case(case_id)
        if entity_id:
            snap = self._snapshot(case_id)
            if not any(e.entity_id == entity_id for e in snap.entities):
                raise KeyError(f"Entity '{entity_id}' not found in this case")
        prof = self.get_financial_profile(case_id, entity_id)
        if isinstance(prof, dict) and prof.get("error"):
            raise KeyError(f"Profile not found: {prof['error']}")
        llm_assisted, llm_provider = self._llm_provenance(case_id)
        body, ctype, filename = export_entity_profile(prof, fmt_key, case_obj, llm_assisted, llm_provider)
        self._audit(case_id, f"EXPORT_PROFILE_{fmt_key.upper()}", {
            "entity_id": prof.get("entity_id") if isinstance(prof, dict) else entity_id,
            "sha256": hashlib.sha256(body).hexdigest(),
        })
        try:
            self.vault.save_case_output(
                case_id, "entity_profile", filename, body,
                metadata={"entity_id": prof.get("entity_id") if isinstance(prof, dict) else entity_id, "format": fmt_key},
            )
        except Exception as _e:
            _LOG.warning("Could not persist export output for case %s: %s", case_id, _e)
        return body, ctype, filename

    def export_dossier_html(self, case_id: str) -> str:
        case_obj = self._require_case(case_id)
        snap = self._snapshot(case_id)
        llm_assisted, llm_provider = self._llm_provenance(case_id)
        loops = self._loops(snap)
        graph = self._graph(
            snap, max_nodes=DEFAULT_MAX_DISPLAY_NODES, layout="server", max_edge_tx_ids=None, date_from=None, date_to=None,
            channel_filter=None, keyword=None, **self._graph_filters(0.0, None, None),
        )
        self._audit(case_id, "EXPORT_DOSSIER_HTML", {"statements": len(snap.statements), "llm_assisted": llm_assisted})
        doc = export_forensic_dossier_html(
            case_obj, snap.statements, list(snap.transactions), snap.entities, self._mules(snap),
            llm_assisted=llm_assisted, llm_provider=llm_provider, loops=list(loops), graph=graph,
        )
        try:
            self.vault.save_case_output(case_id, "forensic_dossier_html", f"{case_id}_forensic_dossier.html", doc.encode("utf-8"))
        except Exception as _e:
            _LOG.warning("Could not persist export output for case %s: %s", case_id, _e)
        return doc

    def export_dossier_pdf(self, case_id: str) -> bytes:
        case_obj = self._require_case(case_id)
        snap = self._snapshot(case_id)
        llm_assisted, llm_provider = self._llm_provenance(case_id)
        loops = self._loops(snap)
        self._audit(case_id, "EXPORT_DOSSIER_PDF", {"statements": len(snap.statements), "llm_assisted": llm_assisted})
        body = export_forensic_dossier_pdf(
            case_obj, snap.statements, list(snap.transactions), snap.entities, self._mules(snap),
            llm_assisted=llm_assisted, llm_provider=llm_provider, loops=list(loops),
        )
        try:
            self.vault.save_case_output(case_id, "forensic_dossier_pdf", f"{case_id}_forensic_dossier.pdf", body)
        except Exception as _e:
            _LOG.warning("Could not persist export output for case %s: %s", case_id, _e)
        return body

    # ------------------------------------------------------------------------------------------ inputs and outputs
    def list_case_inputs(self, case_id: str) -> List[Dict[str, Any]]:
        self._require_case(case_id)
        return self.vault.get_case_inputs(case_id)

    def get_case_input_file(self, case_id: str, identifier: str) -> Optional[Tuple[str, str, bytes]]:
        self._require_case(case_id)
        return self.vault.get_case_input_file(case_id, identifier)

    def list_case_outputs(self, case_id: str) -> List[Dict[str, Any]]:
        self._require_case(case_id)
        return self.vault.get_case_outputs(case_id)

    def get_case_output_file(self, case_id: str, identifier: str) -> Optional[Tuple[str, str, bytes]]:
        self._require_case(case_id)
        return self.vault.get_case_output_file(case_id, identifier)

    def upload_case_document(
        self, case_id: str, filename: str, content: bytes, metadata: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        self._require_case(case_id)
        return self.vault.save_case_input(case_id, filename, content, metadata=metadata)
