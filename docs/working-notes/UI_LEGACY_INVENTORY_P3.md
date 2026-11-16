# Legacy UI inventory (src/cyphersol/ui/index.html + app.js + styles.css, pre-P3)

Moved to /private/tmp/claude-501/cyphersol-p3/legacy/. Every flow below must survive in the new views.
"→ new" names the view/component that now owns it.

## Shell / global
| # | Feature | API | → new |
|---|---|---|---|
| G1 | Token from `<meta name="cyphersol-token">`, sent as `X-CypherSOL-Token` on every fetch; JSON bodies `application/json`; single fetch helper | all | core/api.js `apiFetch` |
| G2 | Error mapping: `err.error` or `Request failed (status)` | – | core/api.js `ApiError` (+friendly per-status text) |
| G3 | Exports fetched with header, saved via Blob + Content-Disposition filename; dossier-html opened in new tab | `GET /api/cases/{id}/export/{xlsx,tally-xml,dossier-pdf,dossier-html}` | Export menu (app.js) + `downloadBlob`; dossier-html is now downloaded (CSP is inherited by blob: tabs) |
| G4 | Cloud-AI banner `#cloud-ai-banner` shown when `/api/health.cloud_egress_possible`; text names provider + effective mode | `GET /api/health` | index.html slot + app.js `refreshCloudBanner()`; also re-run after Settings saves (`store.refreshHealth()`) |
| G5 | Header telemetry line: mode label, banks count, categories count, KB data points | `GET /api/meta`, `/api/llm/config` | Settings view + runtime status chip in top bar |
| G6 | Case selector (`[CypherTrace]/[CypherEdge] title`), auto-select first case, keep preferred case after create | `GET /api/cases` | sidebar case switcher + palette "Switch case" |
| G7 | New case dialog: title (required), mode (cypher_trace / cypher_edge), agency/firm | `POST /api/cases` | app.js `openNewCaseDialog()` (modal component), palette command |
| G8 | KPI strip: mode, statements, transactions, total credits, total debits, entities, suspense pending | overview.summary | overview view (kpi.js) |
| G9 | Step badges: anomaly count, suspense count | overview.summary | sidebar nav badges (from `store.state.summary`) |
| G10 | Global runtime mode segmented control (heuristics/both/llm) persisted via POST | `POST /api/llm/config {execution_mode}` | Settings view (single source of truth, UI-S-17); top-bar status chip shows it |
| G11 | Agent strip S1–S4 with status text (Heuristic Only / Disabled / Needs API Key / Invoked (n) / Primary Active / Backup Ready) from cfg + latest telemetry | overview.latest_agent_telemetry, llm config | Settings view (+ ingest summary) |
| G12 | "Run AI Backup Agents (S2–S4)" button on active case, shows invoked stages | `POST /api/cases/{id}/run-agents {execution_mode}` | ingest view (or settings) – must keep |
| G13 | AI config dialog: default runtime mode, provider (gemini/claude/openai), text model + vision model with datalists from `meta.provider_catalog`, API key (password, blank = keep), saved-key badge from `masked_keys_by_provider`, stage 1–4 toggles, Test Connection, Clear key, Save | `GET/POST /api/llm/config`, `POST /api/llm/test` | settings view |

## 01 Ingest
| I1 | Multi-file input accept `.pdf,.xlsx,.xlsm,.xls,.csv,.tsv,.txt,.png,.jpg,.jpeg,.bmp,.pgm` (test asserts `.xls`) | – | ingest view, constant `UPLOAD_ACCEPT` in core/constants.js |
| I2 | Per-batch mode radio cards (heuristics/both/llm) – default from config | – | ingest view (labelled per-batch override, does NOT change global) |
| I3 | PDF password (optional), draw-template override select (`meta.draw_templates`, "Auto-Detect") | – | ingest view |
| I4 | Upload: files → base64 → `{files:[{filename,content_b64,password}], template_id, execution_mode}`; status: files_ok, total_case_transactions, failed files with error | `POST /api/cases/{id}/upload` (also supports `on_duplicate`) | ingest view; on success `store.mutated(['statements','transactions','entities','analytics'])` |
| I5 | Statements table: file + period, bank + A/C + IFSC, holder, txns, mismatch/dup badges, full SHA-256 (never truncated – test) | overview.statements | ingest view (and overview) |
| I6 | Last pipeline telemetry summary (S1 files recovered, S2 rows repaired, S3 rows enriched, S4 classified/merged, warnings) | upload result `agent_telemetry` / overview.latest_agent_telemetry | ingest view |
| I7 | PDF Draw Tool: preview first page tokens as SVG text, splitter lines from "%" list; template bank code, name, splits, bbox; Save template then refresh meta templates | `POST /api/cases/{id}/pdf-preview {content_b64,password}`, `POST /api/draw-templates` | ingest view (Draw tool tab) + `store.reloadMeta()` |

## 02 Anomalies
| A1 | Flagged rows (overview.anomaly_transactions; server filter `flagged=true`): date, account/holder, narration, debit, credit, balance, flag badges, action | `/transactions?flagged=true` | anomalies view (datagrid) |
| A2 | Toggle duplicate ignore ("Ignore Dup"/"Restore") – should only be offered for duplicate flags (UI-S-15) | `POST /api/cases/{id}/transactions/{tx}/toggle-duplicate` | anomalies view |
| A3 | Empty state "All statements passed…" | – | anomalies view |

## 03 Transactions
| T1 | Search (narration/party/UTR/holder) + category filter (52 cats `NN — name`) | `/transactions?q=&category_id=` | transactions view (datagrid, debounced) |
| T2 | Columns: date, account (holder + number), channel badge, narration + party + UTR, debit, credit, balance, category (suspense badge) + confidence %, source + voucher → ledger | – | transactions view |
| T3 | (bug) silently capped at 300 rows | – | datagrid server paging "X–Y of Z", sort (`sort`/`order` params) |

## 04 Suspense & entities
| S1 | Suspense list (overview.suspense_transactions / `suspense=true&include_ignored=false`), date, narration, signed amount | | suspense view |
| S2 | Top-3 AI suggestion chips → resolve | `POST …/transactions/{tx}/resolve {category_id, custom_ledger_name, auto_refresh_similar, confirmed_tx_ids?, rule_scope?, learn_single_token_party?}` | suspense view |
| S3 | Per-row category select (excl. 52) + custom ledger + "Assign & Learn" | same | suspense view (shared picker) |
| S4 | "Auto-refresh similar & save Auto-Learn rule" checkbox | same | suspense view; also `GET …/transactions/{tx}/resolve-preview` for propagation preview |
| E1 | Entities table: canonical (+ linked accounts), has-statement / details-only, aliases, credited, debited; hides `UNRESOLVED_*` | overview.entities | entities view |
| E2 | Similarity slider 0.50–1.00 (init from case.similarity_threshold) + Re-Cluster | `POST …/entities/recluster {similarity_threshold}` | entities view |
| E3 | Manual merge alias → canonical target | `POST …/entities/recluster {alias_name, canonical_target}` | entities view (+ overview.near_merge_candidates) |

## 05A CypherEdge
| ED1 | Tally stats: ledgers created, vouchers exported, skipped suspense, breakdown; XML preview (first 6000 chars, textContent) | `GET …/edge/tally?include_suspense&company_name&use_trade_vouchers` | tally view (voucher table, include-suspense toggle) |
| ED2 | Push to Tally host/port (default 127.0.0.1:9000); result text branches on `res.success`, `res.connected`, `res.message`, `res.line_errors[0]`, created/altered/errors (test) | `POST …/edge/tally-push {host,port,include_suspense?,company_name?,use_trade_vouchers?}` | tally view uses `describeTallyPushResult()` from core/results.js |
| ED3 | Download Tally XML | `GET …/export/tally-xml` | tally view + Export menu |
| ED4 | ITR: FY, gross credits, net business receipts, interest 80TTA/B, 80C candidates, tax/GST paid; statutory red flags cards (severity, section, title, amount, detail); 52-cat schedule table | `GET …/edge/itr?fy=` | itr view |
| ED5 | Loan: FOIR %, rate %, tenure inputs + Recalculate; KPIs grade, daily ABB, fixed-date ABB, existing EMI, bounces I/W O/W rate, max EMI, max loan; monthly table (credits, net biz credits, debits, net flow, EMI load, daily ABB, fixed-date ABB) | `GET …/edge/loan?foir&rate&tenure` | loan view |

## 05B CypherTrace
| TR1 | Layering: source entity select, window days, Trace; legend counts (credit source / has statement / details only / total); recursive tree cards (role, name, amount, Move to Misc, Restore per misc entity); click name → profile | `GET …/trace/layering?source_entity_id&window_days&max_depth&mode`, `POST …/trace/layering/misc {entity_id, move_to_misc}` | layering view |
| TR2 | Network graph: conduits-only toggle, min ₹, channel (ALL/UPI/NEFT/RTGS/IMPS/ATM/AEPS), keyword, apply, reset zoom; summary KPIs (nodes, edges, conduits, volume, components); full SVG + conduit focus SVG; pan/zoom with drag-vs-click; node click → profile; edge titles; bridge edge labels | `GET …/trace/graph?min_amount&channel&keyword&date_from&date_to` | network view |
| TR3 | Centrality table: entity, role, statement status, risk tier, inflow, outflow, in/out deg, betweenness, pagerank | same | network view |
| TR4 | Mules: card per account, composite score + tier, hold hours, leakage %, 10 weighted parameter bars | `GET …/trace/mules` | mules view |
| TR5 | Loops: max days, min retention %, scan; per loop SVG ring + hop list (date, from→to, amount, channel), retention %, duration | `GET …/trace/loops?max_days&min_retention&max_cycle_length` | loops view |
| TR6 | Bidirectional: A ⇄ B selects + analyze; KPIs A→B, B→A, net, median turnaround, rapid round-trips; txn table with cumulative net | `GET …/trace/bidirectional?a&b` | bidirectional view |

## 06 Profiles & audit
| P1 | Entity select + Load; KPIs subject, credits, debits, net, daily ABB, mule score; monthly table; top senders/beneficiaries | `GET …/trace/profile?entity_id` | profile view (`#/case/{id}/profile?entity=`) |
| P2 | Immutable audit log (timestamp, actor, action, details JSON) | overview.audit_logs | overview view |

## Unused-but-available APIs the new UI should surface
`GET …/provenance` (statement provenance + ingest results), `overview.near_merge_candidates`, `overview.recent_ingest_results`,
`GET …/transactions/{tx}/resolve-preview`, layering `mode=fifo|all`, loops `max_cycle_length`, graph `date_from/date_to`,
mule `weighted_contributions` + `flagged_evidence_tx_ids`, upload `on_duplicate`, tally `company_name`/`use_trade_vouchers`.
