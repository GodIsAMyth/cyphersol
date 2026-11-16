# CypherSOL P2 API changes (performance, NFR-PERF-01)

Covers REVIEW_2026-11 P2 items 26–32. The conventions in `API_P1.md` still apply: every `/api/*` call except
`GET /api/health` needs `X-CypherSOL-Token`, POST bodies are `application/json`, Host / Origin / Sec-Fetch-Site
checks are unchanged, there is no CORS, and the UI CSP is unchanged. Errors are `{"error": "..."}`.

Example payloads below come from a scratch service on a synthetic case (`scripts/bench_p2.py --files 3`).
IDs are shortened.

---

## 1. Background ingest jobs and per-file uploads (new)

The synchronous `POST /api/cases/{id}/upload` is **unchanged** and still supported. The SPA now uses the flow
below, so a 100-file batch is never one 150 MB request, and the browser can show progress.

### POST /api/cases/{id}/uploads → 201
Stages **one** file in server memory until an ingest job consumes it.

Request: `{"filename": "a.csv", "content_b64": "<base64>"}`. Passwords are **not** accepted here.

```json
{"upload_id": "563a4dd7…", "case_id": "CASE_991694D26C", "filename": "a.csv", "size": 6248,
 "sha256": "108232af…", "expires_in_s": 3599}
```

Limits (`jobs.UploadStore`):
* 1 GiB of staged bytes in total.
* 1,000 staged files.
* A 1-hour TTL.
* Each request is still capped at `MAX_REQUEST_BODY_BYTES`.

When a limit is exceeded, the response is **400**. An unknown case gives **404**. Staged files are process-local, so a
restart drops them.

### GET /api/cases/{id}/uploads → 200
Returns the case's staged files: `{"uploads": [<as above>], "max_total_bytes": 1073741824}`.

### POST /api/cases/{id}/uploads/{upload_id}/discard → 200
Returns `{"discarded": "<upload_id>"}`. An unknown, expired or already consumed id gives **404**.

### POST /api/cases/{id}/ingest-jobs → 202
Queues the same pipeline as `/upload` on a background worker.

```json
{"files": [{"upload_id": "563a…", "password": null, "password_hints": {"dob": "01/01/1990"}},
           {"filename": "b.csv", "content_b64": "<base64>"}],
 "template_id": null, "execution_mode": "heuristics", "on_duplicate": "reject", "password_hints": null}
```

* Each `files[]` entry is either `{"upload_id"}` (staged) or an inline `{"filename", "content_b64"}`. Either kind
  can also carry `password` and `password_hints`.
* Passwords and hints are held only in the job's memory until it finishes. They never reach the vault, the
  audit log or the job record.
* Validation errors return immediately: bad `on_duplicate`, empty `files`, malformed hints → **400**; an unknown
  case or staged id → **404**.
* Staged files are claimed atomically. Either all of them are claimed or none are. A claimed file cannot be
  reused.

```json
{"job_id": "99ccb47a…", "kind": "ingest", "case_id": "CASE_991694D26C", "status": "queued", "phase": null,
 "done": 0, "total": 0, "progress": 0.0, "created_at": "2026-11-16T18:09:29.628790+00:00", "started_at": null,
 "finished_at": null, "elapsed_s": null, "error": null, "error_type": null, "http_status": null,
 "files": ["a.csv", "b.csv"], "file_count": 2, "result": null}
```

### GET /api/jobs/{job_id}[?include_result=false] → 200
The job record above, updated as the job runs.

| field | meaning |
|---|---|
| `status` | `queued` → `running` → `succeeded` \| `failed` |
| `phase`, `done`, `total` | Ingest phases: `parse` (files parsed / total), `validate` (statements), `entities`, `commit`. Warm-up jobs report one phase per analytic. |
| `progress` | 0..1 and never decreases. Phase weights are parse 0.55, validate 0.25, entities 0.12, commit 0.08. |
| `result` | On success, the exact payload `POST /upload` returns. It now also includes `timings` (seconds per stage). Omitted when `include_result=false`. |
| `error`, `error_type`, `http_status` | On failure: `http_status` is the status the synchronous endpoint would have returned, for example **409** `ConflictError` for a duplicate SHA-256 and **400** for a `ValueError`. Unexpected errors give **500** with a generic message and an `error_id` that matches the server log. |

An unknown job id gives **404**. The server keeps the newest 200 jobs in memory. Two worker threads serve the queue,
and the per-case lock still serialises ingests into one case.

### GET /api/cases/{id}/jobs → 200
Returns `{"jobs": [<job records without result>, newest first]}`. The list includes `kind: "warmup"` jobs.

### Analysis warm-up (server behaviour)
`run_server` creates the service with `warm_analysis=True`. Set `CYPHERSOL_WARM_ANALYSIS=0` to turn it off.

Warm-up runs after an ingest job and after every successful case-scoped POST that may change data. These POSTs are
excluded: `pdf-preview`, `uploads*`, `ingest-jobs` (it schedules its own warm-up) and `edge/tally-push`.

A background `warmup` job computes the following, so that the first request answers from the cache:
* the case snapshot and the entity preview;
* for CypherTrace cases, the default mules, graph, loops, bidirectional, layering and profile.

Warm-up is debounced, with at most one queued warm-up per case. It stops early if the case changes meanwhile.

`CypherApplicationService(...)` defaults to `warm_analysis=False`, so library callers and tests are unaffected.

---

## 2. Overview (changed)

### GET /api/cases/{id}/overview
The overview now returns **aggregates only**: `case`, `revision`, `statements`, `entities`, `summary`,
`near_merge_candidates`, `audit_logs`, `recent_ingest_results`, `llm_config` and `latest_agent_telemetry`.

It no longer embeds `transactions`, `transactions_page`, `suspense_transactions`, `suspense_truncated`,
`anomaly_transactions` or `anomaly_truncated` unless the caller passes `tx_limit > 0` (legacy behaviour, at most 2,000
rows per list). Use the paginated `GET /api/cases/{id}/transactions` instead (`API_P1.md` §1). The SPA already called
`overview?tx_limit=0` and pages through `/transactions`.

At 100k rows the response is 198 KB, down from 1.6 MB, and a warm call takes 0.08 s, down from 0.73 s.
`service.get_case_overview(cid)` defaults to `tx_limit=0` as well.

---

## 3. Network graph (changed + new)

### GET /api/cases/{id}/trace/graph
New query parameters:

| param | default | notes |
|---|---|---|
| `max_nodes` | 400 | Node cap, 10..5000 (otherwise **400**). The view shows the top N nodes by volume and the edges among them. |
| `layout` | `auto` | `server` (force layout, x/y in 920×520), `seed` (deterministic radial seed positions only), `none` (no x/y) or `auto` (`server` up to 300 displayed nodes, else `seed`). Any other value gives **400**. |

Response changes:
* `summary.layout` reports the effective layout.
* `summary.truncated`, `displayed_nodes` and `max_display_nodes` describe the node cap.
* Centrality, roles and totals (`total_nodes`, `total_edges`, `total_volume`, …) are always computed on the
  **full** filtered graph.
* Each edge lists at most 50 `tx_ids`. `tx_count` stays exact, and `tx_ids_truncated: true` marks capped edges. To
  get every row of an edge, page `GET …/transactions` with filters.
* Filter values `""` and absent are now the same cache key, so the UI's default request is a cache hit after
  warm-up.

Internally, the uncapped graph is computed once per case revision and filter set (`_graph_full`). Every capped view
and every expansion is a slice of it.

### GET /api/cases/{id}/trace/graph/expand?entity_id=…&limit=50 → 200 (new)
Expand-on-demand. It returns one entity's heaviest `limit` edges (1..500), with the neighbour nodes they reach, under
the same filter parameters as `trace/graph`. Nodes come without x/y; the client places them.

```json
{"entity_id": "ad89714d…", "nodes": [{"entity_id": "ad89714d…", "label": "AARAV AGARWAL", "…": "…"}, "…"],
 "edges": ["…"], "total_edges": 22, "total_neighbours": 18, "truncated": true, "limit": 2}
```

`nodes[0]` is the expanded entity. An unknown entity gives **404**, and `limit` out of range gives **400**.

The SPA network view (`views/network.js`) behaves as follows:
* It sends `max_nodes`.
* It offers "Load N more from server" (doubles `max_nodes`, up to 5,000) when the server truncated.
* It adds "Expand neighbours" to the inspector of any node whose flows are not all loaded. The merged nodes stay
  visible beyond the client cap.

---

## 4. Loops (changed)

### GET /api/cases/{id}/trace/loops
* New `hub_degree_cap` parameter. When omitted, the detector default applies: **250** distinct counterparties.
  `0` turns hub exclusion off. Allowed values are 2..100000 (otherwise **400**). Parties above the cap (aggregators,
  merchants, utilities) are skipped by the cycle search.
* New response fields `hub_degree_cap` and `excluded_hubs: [{entity_id, name, degree}]`. The loops view shows them
  in its scan metadata line.
* Unchanged: the time budget (`time_budget_s`, 5 s), the cycle cap (`max_cycles`, 1,000), `truncated` and
  `truncation_reason`. Temporal pruning (bisect on per-source, time-sorted events, within the window) and SCC pruning
  were already in place.
* The serialised report is memoised per case revision.
* The defaults `max_cycle_length=8` and `max_instances_per_loop=50` are normalised, so the UI's default scan, the
  warm-up and the exports (`export/xlsx`, `export/dossier-html|pdf`) share **one** loop computation.

---

## 5. Ingest (internal, no API shape change except `timings`)

* `run_anomaly_gate(..., existing_index=CaseDuplicateIndex)`: the case-wide duplicate signatures are built once per
  ingest and then extended per accepted statement. Before this change, statement k rescanned every earlier row, which
  was 5M signature computations at 100 × 1k rows. Flags are identical; see `tests/test_p2_perf.py`.
* `bulk_parse_statements(..., enrich_narrations=True, on_progress=cb)`: narration parsing runs inside the
  process-pool workers. Results come back in input order, and progress is reported as files complete. Stage 3
  re-enriches only rows that an LLM stage replaced or repaired.
* Categoriser: Tier 1 was already a token n-gram hash index per direction, which is equivalent to Aho-Corasick at
  token level. The ML tier is now **lazy**. It is evaluated only on a Tier 1 / rule miss, or when the top-3
  suggestions need it. The memo key is the normalised narration. Output is byte-identical to eager evaluation (test
  `test_lazy_ml_tier_matches_eager_evaluation`).
* The upload result gains `timings: {prepare, parse, validate, categorise, entities, commit}` in seconds.

## 6. Analysis cache (internal)

* `transactions.change_rev` is a new column (migration plus index `idx_tx_case_change`). It holds the case revision
  each row was last written at. When the snapshot is stale, the service reads only the rows with
  `change_rev > cached revision` and merges them in `(txn_date, row_index, rowid)` order. If the row count does not
  match (for example, deleted rows), it falls back to a full load.
* Full loads decode rows from plain tuples, with caches for repeated amounts, flag lists and suggestion lists. A
  100k-row load fell from 4.1 s to 2.1 s.
* Transfer events are extracted once per snapshot and filter set, and shared by the graph, loops, bidirectional,
  layering and profile (`graph_engine.shared_event_cache`). Before this change, every analytic re-extracted them from
  every row.

## 7. OCR (item 32)

`ocr_engine.image_dimensions()` reads the image size from the header (PNG IHDR, BMP, PNM or JPEG SOF). The
pure-Python pixel decode now runs only for the fixtures-only glyph matcher, and never for rasters above
`MAX_IMAGE_PIXELS`. Before this change, every Tesseract/Vision image was fully decoded just to read its size. The
300-DPI grayscale per-page rendering with per-page timeouts was already in place.

---

## 8. Benchmark

`python scripts/bench_p2.py [--files 100 --rows 1000 --seed 2026 --json out.json --profile-ingest]`

The script generates a seeded synthetic case, with byte-identical files for the same arguments: P2P
UPI/NEFT/IMPS transfers between holders, merchants, salary, ATM, EMI, charges and planted 3- and 4-hop loops. It then
ingests the case through the service and times each endpoint three ways: cold, warm, and warm plus the JSON encoding
the server adds. It also times the exports, and the re-warm after a one-row mutation.

Measured in this container (4 vCPU, Python 3.13), at 100 statements × 1,000 rows (100k transactions):

| step | main (s) | P2 branch (s) | target |
|---|---|---|---|
| ingest | 59.5 | **18.4** | < 60 |
| overview, cold / warm+json | 4.05 / 0.73 | 2.44 / **0.08** | < 0.5 warm |
| graph, cold / warm+json | 2.28 / 0.08 | 2.41 / **0.08** | < 0.5 warm |
| layering, cold / warm+json | 2.59 / 0.03 | 1.06 / **0.03** | < 0.5 warm |
| profile, cold / warm+json | 0.03 / 0.002 | 0.03 / **0.003** | < 0.5 warm |
| loops, cold / warm+json | 1.83 / 0.18 | 0.33 / **0.05** | < 0.5 warm, always finishes |
| bidirectional, cold / warm+json | 3.87 / 0.02 | 0.13 / 0.02 | |
| mules, cold / warm+json | 2.38 / 0.04 | 2.31 / 0.04 | |
| export xlsx / dossier html | 7.77 / 5.87 | 6.94 / 3.60 | always finishes |
| re-warm after a 1-row mutation (background) | n/a | 7.2 | |

"Cold" means the first request after a change and before the background warm-up has finished. With the server's
warm-up, requests after the warm-up completes are cache hits.
