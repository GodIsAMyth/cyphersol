# CypherSOL P1 API additions and changes

Captured from a scratch server (`run_app.py --port 19473 --db /private/tmp/claude-501/cyphersol-p1/backend.db`,
seeded demo cases `CASE_TRACE_EOW_041` and `CASE_EDGE_BALAJI_FY25`) with `curl`. Arrays in the examples are
shortened (`"... N more"`) and long strings truncated; field names and types are exact.

## Conventions (unchanged from P0/P3)

* Every `/api/*` call except `GET /api/health` needs the header `X-CypherSOL-Token: <token from index.html meta>`.
  A missing or wrong token gives **403**. Host / Origin / Sec-Fetch-Site checks still apply. No CORS.
* POST and PUT bodies must be `Content-Type: application/json` (else **415**), and must be a JSON object (else **400**).
* Errors are `{"error": "<message>"}` (plus `"code"` for page-render errors). Status mapping:
  **400** bad input (any `ValueError`), **401** PDF password missing or wrong (page raster only), **403** token, host or
  origin rejected, **404** unknown case, entity, transaction or route, **405** `PUT` on anything but `edge/tally-map`,
  **409** duplicate statement (upload), **413** body too large, **415** not JSON, **500** internal (with `error_id`),
  **503** no PDF renderer.
* `PUT` is accepted **only** for `/api/cases/{id}/edge/tally-map`. `OPTIONS` now reports `Allow: GET, POST, PUT, OPTIONS`.
* Downloads use `Content-Disposition: attachment`. HTML downloads also carry
  `Content-Security-Policy: sandbox; default-src 'none'; style-src 'unsafe-inline'; img-src data:`.
* The UI shell CSP (`script-src 'self'; style-src 'self'; img-src 'self' data: blob:` ...) is unchanged. Draw Tool
  page images come back as base64, so the UI can show them through a `data:` URL or a `blob:` URL.
* PDF passwords and password hints exist **in memory for one request only**. They are never written to the vault,
  the audit log or the logs. Responses carry only a formula label (`password_label`).

---

## 1. Transactions (changed)

### GET /api/cases/{id}/transactions
Filtering, sorting and pagination all run in SQL (`storage.query_transactions`). The new index
`idx_tx_case_stmt (case_id, statement_id, row_index)` joins the existing `(case_id, txn_date, row_index)` and
`(case_id, category_id)` indexes.

| param | type | notes |
|---|---|---|
| `offset`, `limit` | int | `limit` is at most 2000 |
| `q`, `category_id`, `suspense`, `flagged`, `include_ignored`, `statement_id`, `entity_id`, `direction`, `date_from`, `date_to` | | unchanged |
| `channel` | str | **changed:** matches either the internal code (`TRANSFER`) or the SRS name (`INTERNAL_TRANSFER`) |
| `sort` | str | one of `txn_date, date, debit, credit, amount, running_balance, balance, payment_channel, channel, category_id, category_name, confidence_score, extracted_counterparty, counterparty, source_holder_name, source_account_number, raw_narration, categorization_source, row_index` (400 otherwise) |
| `order` | `asc`/`desc` | |
| `amount_min`, `amount_max` | float | on `MAX(debit, credit)`; 400 if max < min, or not a finite number |
| `tx_ids` | comma list | exact rows (for mule evidence), at most 500 ids (400 above that) |

Every transaction object (here, in `overview`, and in `resolve-preview`) now has **`payment_channel_srs`**
(the SRS FR-CAT-01 name), **`card_number_masked`** and **`cheque_number`**. Statements have **`pan`**.

`GET /api/cases/CASE_TRACE_EOW_041/transactions?limit=2&sort=amount&order=desc&amount_min=1000`
```json
{
  "offset": 0,
  "limit": 2,
  "total": 33,
  "has_more": true,
  "sort": "amount",
  "order": "desc",
  "transactions": [
    {
      "tx_id": "1b449786-bf00-4c75-ba19-48a64af43596",
      "txn_date": "2026-04-02",
      "payment_channel": "RTGS",
      "payment_channel_srs": "RTGS",
      "debit": 0.0,
      "credit": 450000.0,
      "card_number_masked": null,
      "cheque_number": null
    }
  ]
}
```

### GET /api/meta (additions)
```json
{
  "srs_payment_channels": [
    "UPI",
    "NEFT",
    "RTGS",
    "IMPS",
    "NACH",
    "ATM",
    "AEPS",
    "POS",
    "CHEQUE",
    "INTERNAL_TRANSFER",
    "BANK_CHARGE",
    "INTEREST",
    "CASH_CDM"
  ],
  "srs_channel_extensions": [
    "CASH_WDL",
    "FOREX",
    "OTHER"
  ],
  "profile_export_formats": [
    "html",
    "xlsx",
    "pdf"
  ],
  "password_hint_keys": [
    "passwords",
    "password",
    "name",
    "holder_name",
    "account_holder",
    "customer_name",
    "dob",
    "date_of_birth",
    "pan",
    "account_number",
    "account_no",
    "account",
    "customer_id",
    "crn",
    "cif",
    "mobile",
    "phone",
    "bank"
  ],
  "max_trace_depth": 32,
  "labelled_examples_used": 0
}
```

---

## 2. Entity cluster review (NLP-11 / NLP-12 / UI-S-14)

The decisions are stored in `entity_decisions`, in order. Every apply and every undo is also appended to the
append-only `entity_decision_history`, which keeps a snapshot of the touched entities so that undo restores the
original entity IDs, and to the audit log (`ENTITY_MERGE`, `ENTITY_SPLIT`, `ENTITY_REJECT`, `ENTITY_DECISION_UNDO`).
Every re-cluster (ingest, run-agents, recluster, decisions) passes the previous entities, so IDs stay stable.
Legacy free-text merges (`POST entities/recluster` with `alias_name` and `canonical_target`) still work.

Decision object: `{decision_id, op: "merge"|"split"|"reject", groups: [[aliases...],[aliases...]], entity_ids: [...], actor, note, created_at}`.
* merge: `entity_ids = [source, target]`. The merged entity keeps the **target** ID.
* split: `entity_ids = [entity]`, `groups = [split_off_aliases, remainder]`. The remainder keeps the ID.
* reject: `entity_ids = [a, b]`. The two are never linked again.

### GET /api/cases/{id}/entities/preview?threshold=0.80
A read-only dry run at a threshold, for the slider (memoised per case revision). `threshold` is in [0.5, 1.0] and
defaults to the case's threshold; a value outside that range gives 400.
`clusters[]`: `{entity_id, canonical_name, aliases, has_statement, is_system, linked_account_numbers, linked_vpas, tx_count, total_credited (str), total_debited (str), evidence[{a,b,reason,detail}]}`.
`candidates[]`: near-miss pairs `{entity_a_id, entity_b_id, entity_a, entity_b, name_a, name_b, similarity}`.
`conflicts[]`: `{type: HOLDER_GUARD|CANNOT_LINK|PAN_AMBIGUOUS, a, b, reason, detail}`.
`changes`: `{merged[{entity_id, from_entity_ids}], split[{entity_id, into_entity_ids}], new_entity_ids, retired_entity_ids}`, measured against the stored entities.
```json
{
  "case_id": "CASE_TRACE_EOW_041",
  "threshold": 0.8,
  "entity_count": 31,
  "multi_alias_clusters": 3,
  "clusters": [
    {
      "entity_id": "a386c2dd-63dc-5196-b943-c1ff001b51c2",
      "canonical_name": "POOJAN VIG",
      "aliases": [
        "POOJAN VIG",
        "... 1 more"
      ],
      "has_statement": true,
      "is_system": false,
      "linked_account_numbers": [
        "501002938471"
      ],
      "linked_vpas": [
        "poojan@okhdfc",
        "... 1 more"
      ],
      "tx_count": 6,
      "total_credited": "205300.00",
      "total_debited": "250300.00",
      "evidence": [
        {
          "a": "POOJAN VIG",
          "b": "PUJAN VIJ",
          "reason": "NAME",
          "detail": 0.92
        }
      ]
    }
  ],
  "candidates": [
    {
      "entity_a_id": "08948496-25cb-588b-8187-b5c9af985b08",
      "entity_b_id": "96f76745-150d-5614-861f-3cae9a2da47c",
      "entity_a": "VICTIM TEN",
      "entity_b": "VICTIM ELEVEN",
      "name_a": "VICTIM TEN",
      "name_b": "VICTIM SEVEN",
      "similarity": 0.7984
    }
  ],
  "conflicts": [],
  "changes": {
    "merged": [
      {
        "entity_id": "96f76745-150d-5614-861f-3cae9a2da47c",
        "from_entity_ids": [
          "96f76745-150d-5614-861f-3cae9a2da47c",
          "... 1 more"
        ]
      },
      "... 1 more"
    ],
    "split": [],
    "new_entity_ids": [],
    "retired_entity_ids": [
      "cd338473-b003-5f4f-b51b-0fcf8f05262a",
      "... 1 more"
    ]
  },
  "stats": {
    "names": 34,
    "distinct_normalised": 31,
    "blocks": 136,
    "comparisons": 56,
    "elapsed_ms": 1.4
  }
}
```

The overview's `near_merge_candidates` now come from this preview (no O(n^2) loop) and include the IDs:
```json
[
  {
    "entity_a": "VICTIM ELEVEN",
    "entity_b": "VICTIM SEVEN",
    "similarity": 0.8366,
    "entity_a_id": "96f76745-150d-5614-861f-3cae9a2da47c",
    "entity_b_id": "cf490336-cb83-5a61-b4f3-09d21895f962"
  }
]
```

### GET /api/cases/{id}/entities/decisions
`{decisions: [active, oldest first], history: [newest first: {history_id, action, decision_id, decision (+entities_before), actor, created_at}], legacy_manual_merges: [{alias, canonical}]}`
```json
{
  "decisions": [
    {
      "actor": "manu",
      "created_at": "2026-11-16T10:58:45+00:00",
      "decision_id": "e8489d9ad6734e308516f6c56f34da96",
      "entity_ids": [
        "a386c2dd-63dc-5196-b943-c1ff001b51c2",
        "... 1 more"
      ],
      "groups": [
        [
          "POOJAN VIG",
          "... 1 more"
        ],
        "... 1 more"
      ],
      "note": "same person",
      "op": "merge"
    }
  ],
  "history": [
    {
      "history_id": "ab2a6e4f-a9ff-45bf-85f1-61e1e1082c2a",
      "action": "ENTITY_REJECT",
      "decision_id": "880874bc4331441997321cf1bdecfdae",
      "decision": {
        "actor": "manu",
        "created_at": "2026-11-16T10:58:45+00:00",
        "decision_id": "880874bc4331441997321cf1bdecfdae",
        "entities_before": [
          {
            "aliases": [
              "POOJAN VIG",
              "... 1 more"
            ],
            "canonical_name": "POOJAN VIG",
            "entity_id": "96f76745-150d-5614-861f-3cae9a2da47c"
          },
          "... 1 more"
        ],
        "entity_ids": [
          "a386c2dd-63dc-5196-b943-c1ff001b51c2",
          "... 1 more"
        ],
        "groups": [
          [
            "PUJAN VIJ"
          ],
          "... 1 more"
        ],
        "note": "",
        "op": "reject"
      },
      "actor": "manu",
      "created_at": "2026-11-16T10:58:45.276067+00:00"
    }
  ],
  "legacy_manual_merges": []
}
```

### POST /api/cases/{id}/entities/merge  `{source_entity_id, target_entity_id, note?}`
Response for all four POSTs: `{decision, decisions, entities[ResolvedEntity], new_entity_ids, retired_entity_ids, conflicts, changed}`.
```json
{
  "decision": {
    "decision_id": "e8489d9ad6734e308516f6c56f34da96",
    "op": "merge",
    "groups": [
      [
        "POOJAN VIG",
        "... 1 more"
      ],
      "... 1 more"
    ],
    "entity_ids": [
      "a386c2dd-63dc-5196-b943-c1ff001b51c2",
      "... 1 more"
    ],
    "actor": "manu",
    "note": "same person",
    "created_at": "2026-11-16T10:58:45+00:00"
  },
  "decisions": "[... active decisions, oldest first]",
  "entities": [
    {
      "entity_id": "96f76745-150d-5614-861f-3cae9a2da47c",
      "case_id": "CASE_TRACE_EOW_041",
      "canonical_name": "POOJAN VIG",
      "aliases": [
        "POOJAN VIG",
        "... 2 more"
      ],
      "linked_account_numbers": [
        "501002938471"
      ],
      "linked_vpas": [
        "poojan@okhdfc",
        "... 1 more"
      ],
      "has_statement": true,
      "is_moved_to_misc": false,
      "total_credited": 305300.0,
      "total_debited": 250300.0,
      "tx_count": 7
    }
  ],
  "new_entity_ids": [],
  "retired_entity_ids": [
    "a386c2dd-63dc-5196-b943-c1ff001b51c2"
  ],
  "conflicts": [],
  "changed": true
}
```
Self-merge, unknown ID or missing field gives 400:
```json
{
  "error": "Cannot merge an entity with itself."
}
```

### POST /api/cases/{id}/entities/split  `{entity_id, aliases: [str, ...], note?}`
`aliases` must be a non-empty subset of the entity's aliases that leaves at least one alias behind (else 400).
```json
{
  "decision": {
    "decision_id": "a23be65b65ae455593f7e3681f4b97b6",
    "op": "split",
    "groups": [
      [
        "PUJAN VIJ"
      ],
      "... 1 more"
    ],
    "entity_ids": [
      "96f76745-150d-5614-861f-3cae9a2da47c"
    ],
    "actor": "manu",
    "note": "",
    "created_at": "2026-11-16T10:58:45+00:00"
  },
  "new_entity_ids": [
    "a386c2dd-63dc-5196-b943-c1ff001b51c2"
  ],
  "retired_entity_ids": [],
  "changed": true
}
```

### POST /api/cases/{id}/entities/reject  `{entity_a_id, entity_b_id, note?}`
```json
{
  "decision": {
    "decision_id": "880874bc4331441997321cf1bdecfdae",
    "op": "reject",
    "groups": [
      [
        "PUJAN VIJ"
      ],
      "... 1 more"
    ],
    "entity_ids": [
      "a386c2dd-63dc-5196-b943-c1ff001b51c2",
      "... 1 more"
    ],
    "actor": "manu",
    "note": "",
    "created_at": "2026-11-16T10:58:45+00:00"
  }
}
```

### POST /api/cases/{id}/entities/undo  `{decision_id?}`
With no `decision_id`, the most recent decision is undone. Nothing to undo gives `{"decision": null, "changed": false, ...}`.
An unknown `decision_id` gives 400. Example: undoing the merge above brings back the original IDs.
```json
{
  "decision": {
    "actor": "manu",
    "created_at": "2026-11-16T10:58:45+00:00",
    "decision_id": "e8489d9ad6734e308516f6c56f34da96",
    "entity_ids": [
      "a386c2dd-63dc-5196-b943-c1ff001b51c2",
      "... 1 more"
    ],
    "groups": [
      [
        "POOJAN VIG",
        "... 1 more"
      ],
      "... 1 more"
    ],
    "note": "same person",
    "op": "merge"
  },
  "decisions": [],
  "entities": [
    {
      "entity_id": "a386c2dd-63dc-5196-b943-c1ff001b51c2",
      "case_id": "CASE_TRACE_EOW_041",
      "canonical_name": "POOJAN VIG",
      "aliases": [
        "POOJAN VIG",
        "... 1 more"
      ],
      "linked_account_numbers": [
        "501002938471"
      ],
      "linked_vpas": [
        "poojan@okhdfc",
        "... 1 more"
      ],
      "has_statement": true,
      "is_moved_to_misc": false,
      "total_credited": 205300.0,
      "total_debited": 250300.0,
      "tx_count": 6
    }
  ],
  "new_entity_ids": [
    "a386c2dd-63dc-5196-b943-c1ff001b51c2"
  ],
  "retired_entity_ids": [],
  "changed": true
}
```

---

## 3. Draw Tool and PDF passwords (PDF-13 / PDF-21 / PDF-22)

### POST /api/draw/page-image  `{content_b64, page=1, dpi=110 (36..300), password?}`
Renders one page as a PNG. To map overlay pixels to PDF points:
`x_pt = px * page_width_pt / width_px`, `y_pt = page_height_pt - py * page_height_pt / height_px`.
```json
{
  "content_type": "image/png",
  "page": 1,
  "page_count": 1,
  "dpi": 72,
  "width_px": 595,
  "height_px": 842,
  "page_width_pt": 595.0,
  "page_height_pt": 842.0,
  "png_b64": "iVBORw0KGgoAAAANSUhEUgAAAlMAAANK...",
  "data_url": "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAl..."
}
```
Errors: `{error, code}`. `page_out_of_range` and `corrupt` give 400, `password_required` and `wrong_password` give 401,
`renderer_unavailable` gives 503, `render_failed` gives 500. A bad `dpi` or `page` (not an integer, or out of range) gives 400.
```json
{
  "error": "Page 9 does not exist (the PDF has 1).",
  "code": "page_out_of_range"
}
```

### POST /api/pdf/password-check  `{content_b64, password?, password_hints?}`
Tries the supplied password, then the generated candidates. It never returns a password.
`status` is one of `ok`, `no_match`, `not_encrypted` or `unavailable`.
```json
{
  "status": "ok",
  "label": "dob_ddmmyyyy",
  "tried": 2,
  "candidates": 15
}
```
`password_hints` keys (any subset; an unknown key gives 400): `passwords` (list of up to 32 strings), `password`, `name`,
`holder_name`, `account_holder`, `customer_name`, `dob`, `date_of_birth`, `pan`, `account_number`, `account_no`, `account`,
`customer_id`, `crn`, `cif`, `mobile`, `phone`, `bank`. These keys are also listed in `/api/meta` as `password_hint_keys`.

### POST /api/draw-templates (changed)
`column_splits` and `table_bbox` are validated: NaN, infinite, mixed units, out-of-range or degenerate values give 400.
The splits are saved sorted. `column_names` must be a list of strings.
```json
{
  "error": "Column dividers mix normalised (0..1) and point coordinates."
}
```

### POST /api/cases/{id}/upload (changed)
Adds a batch-level `password_hints` object and a per-file `files[i].password_hints` object (the per-file hints
override the batch hints). For a `.pdf` with hints, the per-file `password` is tried first, then the generated candidates.
New keys in each `ingested[]` report: `password_label`, `pan`, `filename_crosscheck`, `period_coverage`,
`ocr_confidence_by_page` (`{"1": 0.93}`), `low_confidence_pages`, `ocr_rejected_pages` and `low_ocr_confidence`. OCR self-healing is
audited as `OCR_AUTOCORRECT` (before and after values per row). Provenance `details` gains `filename_crosscheck`,
`period_coverage`, `password_label`, `low_confidence_pages` and `ocr_confidence_by_page`.
An image upload with no OCR engine installed now warns "No OCR engine available; install Tesseract ..."
and keeps the empty statement, so a retry can supersede it (BE-12). It is not rejected.
```json
{"files": [{"filename": "HDFC_locked.pdf", "content_b64": "...", "password_hints": {"name": "POOJAN VIG"}}],
 "password_hints": {"dob": "1990-05-17"}}
```
```json
{
  "status": "ok",
  "filename": "HDFC_locked.pdf",
  "transactions": 8,
  "password_label": "dob_ddmmyyyy",
  "pan": null,
  "filename_crosscheck": {
    "bank": "header_only",
    "account": "header_only",
    "holder": "header_only",
    "header_account_number": "501002938471",
    "filename_account_number": null,
    "header_holder_name": "POOJAN VIG",
    "filename_holder_name": null,
    "filename_period": null,
    "period": "header_only"
  },
  "period_coverage": {
    "stated_start": "2026-04-01",
    "stated_end": "2026-04-30",
    "first_txn_date": "2026-04-01",
    "last_txn_date": "2026-04-20",
    "rows_outside": 0,
    "days_without_rows_at_start": 0,
    "days_without_rows_at_end": 10
  },
  "ocr_confidence_by_page": null,
  "low_confidence_pages": null,
  "ocr_rejected_pages": null,
  "low_ocr_confidence": false
}
```

---

## 4. Bulk deduplication (FR-ING-06.2)

### POST /api/cases/{id}/dedup  `{apply: bool}`
`apply: false` (or omitted) returns a preview. `apply: true` marks every non-kept row `is_duplicate_ignored` and
`DUPLICATE_SUSPECT`, saves only the changed rows, and audits `BULK_DEDUP`. Running it again changes nothing. A non-boolean `apply` gives 400.
```json
{
  "applied": true,
  "groups": [
    {
      "keep_tx_id": "f0b4625d-ad04-4ca8-969b-5239c00ab86e",
      "duplicate_tx_ids": [
        "3ac8d4da-6dbf-42e9-9670-ef47dec9c499"
      ],
      "statement_ids": [
        "2e15fe92-4257-419d-af9c-ca20d156b3df",
        "... 1 more"
      ],
      "account_number": "123456789012",
      "txn_date": "2026-04-01",
      "amount": "500.00",
      "direction": "CR"
    },
    "... 1 more"
  ],
  "group_count": 2,
  "duplicate_count": 2,
  "changed_tx_ids": [
    "3ac8d4da-6dbf-42e9-9670-ef47dec9c499",
    "... 1 more"
  ]
}
```

---

## 5. CypherEdge

### GET /api/cases/{id}/edge/tally-map
### PUT or POST /api/cases/{id}/edge/tally-map  `{ledger_map: {...}}` or `{reset: true}`
(A body without `ledger_map` or `reset` is treated as the map itself.) A partial map is overlaid on the defaults.
Validation errors list every problem (400). Saved per case and audited as `TALLY_MAP_UPDATE`. Map fields:
`version, category_groups{"1".."52": group}, category_ledgers{"<cid>": ledger}, counterparty_ledgers{"<NORMALISED CP>": {ledger, group|null}}, custom_groups{name: predefined_parent}, company_name, use_trade_vouchers, voucher_prefix, sales_categories[int], purchase_categories[int]`.
```json
{
  "ledger_map": {
    "version": 1,
    "counterparty_ledgers": {},
    "custom_groups": {},
    "company_name": null,
    "use_trade_vouchers": false,
    "voucher_prefix": "CS",
    "sales_categories": [
      2
    ],
    "purchase_categories": [
      11,
      12
    ],
    "category_groups": {
      "1": "Direct Incomes",
      "2": "Sales Accounts",
      "3": "Direct Incomes",
      "...": "52 entries"
    },
    "category_ledgers": {}
  },
  "is_default": true
}
```
```json
{
  "error": "Invalid Tally ledger map: category_groups[1]: group 5 is neither predefined nor declared in custom_groups; ..."
}
```

### GET /api/cases/{id}/edge/tally?include_suspense=&company_name=&use_trade_vouchers=&include_xml= (changed)
**The full `xml` is no longer returned by default (EDGE-28).** Use `vouchers`, `ledgers`, `groups` and `xml_preview`,
or pass `include_xml=true` (kept for the transition) or download `export/tally-xml`. If `use_trade_vouchers` is
omitted, the stored map's setting applies. The stored ledger map is applied to the preview, `export/tally-xml`,
`export/tally-xlsx` and `edge/tally-push` (whose `use_trade_vouchers` is now optional too).
`vouchers[]`: `{voucher_number, voucher_type, date, guid, amount, party_ledger, bank_ledger, narration, tx_id, mirror_tx_id, statement_id, category_id, direction, utr, entries[{ledger, is_deemed_positive, amount (signed)}]}`.
`ledgers[]`: `{name, parent, kind: bank|cash|suspense|transfer_clearing|party, voucher_count, amount}`.
```json
{
  "stats": {
    "company_name": "Balaji Books",
    "case_title": "Shree Balaji Enterprises — FY 2025-26 Audit & Loan Appraisal (Demo)",
    "groups_created": 0,
    "ledgers_created": 15,
    "vouchers_exported": 21,
    "skipped_suspense": 3,
    "skipped_zero_amount": 0,
    "mirror_legs_deduped": 0,
    "unmatched_own_transfers": 1,
    "counterparty_overrides_applied": 0,
    "use_trade_vouchers": true,
    "voucher_prefix": "CS",
    "voucher_breakdown": {
      "Sales": 3,
      "Payment": 15,
      "Contra": 2,
      "Receipt": 1
    }
  },
  "vouchers": [
    {
      "voucher_number": "CS-000001",
      "voucher_type": "Sales",
      "date": "2025-04-02",
      "guid": "f36dd152-4504-5399-8ce5-04d921c07ecd",
      "amount": 320000.0,
      "party_ledger": "RAZORPAY",
      "bank_ledger": "SBI - 5566",
      "narration": "RAZORPAY/SETTLEMENT/INV-401 | Cat: Business Sales & Trade Receipts | UTR: NA",
      "tx_id": "75c0279b-3239-4255-8ade-ff704a2016b7",
      "mirror_tx_id": null,
      "statement_id": "66ecd95e-b560-4439-b5c2-a4ba71c3fa34",
      "category_id": 2,
      "direction": "CR",
      "utr": null,
      "entries": [
        {
          "ledger": "RAZORPAY",
          "is_deemed_positive": false,
          "amount": 320000.0
        },
        "... 1 more"
      ]
    }
  ],
  "ledgers": [
    {
      "name": "AWS CLOUD SUBSCRIPTION",
      "parent": "Indirect Expenses",
      "kind": "party",
      "voucher_count": 1,
      "amount": 12400.0
    }
  ],
  "groups": [],
  "xml_preview": "<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n<ENVELOPE>\n  <HEADER>\n    <TALLYREQUEST>I...",
  "xml_truncated": true,
  "xml_sha256": "d920513e7ef695668da436b0ecbaa097257b9e91862b46f35325756a05403f34",
  "xml_bytes": 23830,
  "ledger_map": "{... effective map, same shape as edge/tally-map}"
}
```

### GET /api/cases/{id}/edge/loan?foir=&rate=&tenure=&income_factor=&abb_factor=&income_override= (changed)
`income_factor` and `abb_factor` must be in [0, 1] (defaults 0.20 and 0.50). `income_override` must be at least 0. Anything else gives 400.
```json
{
  "income_basis": {
    "method": "banking_surrogate",
    "salary_monthly_avg": 0.0,
    "avg_monthly_net_business_inflow": 445816.67,
    "business_income_factor": 0.3,
    "business_income_proxy": 133745.0,
    "fixed_date_abb": 625236.67,
    "abb_income_factor": 0.4,
    "abb_income_proxy": 250094.67,
    "monthly_income_override": null,
    "note": "Indicative banking surrogate, not verified income; lenders apply their own programme norms."
  },
  "consolidated_coverage": {
    "basis": "Consolidated EOD balance for a day is the sum of the accounts whose statement period covers that day; days ...",
    "accounts": [
      "SBI - 5566"
    ],
    "segments": [
      {
        "from": "2025-04-02",
        "to": "2025-06-30",
        "days": 90,
        "accounts": [
          "SBI - 5566"
        ]
      }
    ],
    "full_coverage_days": 90,
    "partial_coverage_days": 0,
    "uncovered_days": 0
  },
  "penal_event_count": 0,
  "overdrawn_days": 0,
  "cash_withdrawal_pct": 1.82
}
```
```json
{
  "error": "income_factor must be between 0 and 1"
}
```

### GET /api/cases/{id}/edge/itr?fy= (domain change, same route)
`checks_run[]` replaces the UI's hard-coded thresholds. Also returned: `deductions[]`, `deduction_note`,
`reconciliation_waterfall[]`, `reconciliation.reconciles`, `reconciliation.unexplained_credits`, and `statutory_red_flags[].check_id`.
```json
{
  "checks_run": [
    {
      "id": "194C",
      "group": "TDS",
      "section": "Sec 194C",
      "title": "Contractor / Vendor Payments",
      "rule": "Per payee: single payment > ₹30,000 or FY aggregate > ₹1,00,000 (TDS 1% (individual/HUF) / 2% (others)).",
      "thresholds": {
        "period": "fy",
        "aggregate_limit": 100000,
        "single_limit": 30000,
        "rate_note": "1% (individual/HUF) / 2% (others)",
        "applies_from_fy_start": 2000,
        "applies_to_fy_start": null
      },
      "status": "pass",
      "flag_count": 0,
      "severity": null,
      "amount": 0,
      "caveats": [
        "Provisional: uncategorised (suspense) rows are not yet assessed."
      ]
    }
  ],
  "deductions": [
    {
      "key": "sec_80c_elss_lic_pf",
      "section": "Sec 80C",
      "title": "ELSS, life insurance, PPF / EPF / SSY / NSC",
      "amount": 48000.0,
      "tx_count": 1,
      "statutory_cap": 150000,
      "eligible_upto_cap": 48000.0,
      "transactions": [
        {
          "date": "2025-05-18",
          "narration": "UPI/DR/LIC OF INDIA PREMIUM",
          "amount": 48000.0,
          "category_id": 35
        }
      ]
    }
  ],
  "reconciliation_waterfall": [
    {
      "step": "gross_credits",
      "label": "Gross credits in the FY",
      "kind": "start",
      "categories": [],
      "amount": 1587450.0,
      "running_balance": 1587450.0
    },
    "... 1 more"
  ],
  "statutory_red_flags": [
    {
      "section": "Section 269ST",
      "severity": "MEDIUM",
      "verify_source": true,
      "title": "Cash Deposits ≥ ₹2,00,000 in a Day With No Identified Depositor",
      "amount": 250000.0,
      "detail": "1 day(s) with large cash deposits whose depositor is not named. Section 269ST applies only if the cash was ...",
      "occurrences": [
        {
          "date": "2025-05-02",
          "amount": 250000.0
        }
      ],
      "check_id": "269ST"
    }
  ]
}
```

### GET /api/cases/{id}/export/tally-xlsx?include_suspense=&company_name=&trade_vouchers=  (new)
### GET /api/cases/{id}/export/itr-xlsx?fy=  (new; an FY with no data gives 400)
The ITR workbook sheets are ITR Summary, ITR Checks Run, ITR Red Flags (with Check ID), ITR Flag Details, ITR Deductions,
ITR Reconciliation and ITR Category Schedule. The Tally workbook sheets are Vouchers, Ledger Entries, Ledger Masters, Groups and Summary.
Both exports are audited as `EXPORT_TALLY_XLSX` and `EXPORT_ITR_XLSX`, with the file's SHA-256.
```json
{
  "error": "FY 1999-00 has no transactions; available: FY 2025-26"
}
```

---

## 6. Profiles and exports

### GET /api/cases/{id}/export/profile?entity_id=&format=html|xlsx|pdf  (new; `fmt=` is also accepted)
`entity_id` defaults to the first entity with a statement. An unknown format gives 400; an unknown entity gives 404.
Audited as `EXPORT_PROFILE_<FMT>`. `export/xlsx` (the audit workbook) now includes the bidirectional pairs.
```
profile_pdf: 200 application/pdf 27683 | Content-Disposition: attachment; filename="profile_POOJAN_VIG.pdf"
profile_xlsx: 200 application/vnd.openxmlformats-officedocument.spreadsheetml.sheet 10057 | Content-Disposition: attachment; filename="profile_POOJAN_VIG.xlsx"
profile_html: 200 text/html; charset=utf-8 16789 | Content-Disposition: attachment; filename="profile_POOJAN_VIG.html"
tallyxlsx: 200 application/vnd.openxmlformats-officedocument.spreadsheetml.sheet 8661 | Content-Disposition: attachment; filename="CASE_EDGE_BALAJI_FY25_tally_import.xlsx"
itrxlsx: 200 application/vnd.openxmlformats-officedocument.spreadsheetml.sheet 10459 | Content-Disposition: attachment; filename="CASE_EDGE_BALAJI_FY25_itr_latest.xlsx"
```
```json
{
  "error": "format must be one of html, xlsx, pdf"
}
```

---

## 7. CypherTrace

### GET /api/cases/{id}/trace/layering (changed)
New params: `root_tx_id` (root the tree at one credit; a non-credit or unknown ID gives 400), `date_from`, `date_to`, and
`trace_depth` (attribution depth 1..32, default 8, never below `max_depth`). `max_depth` must be 1..32 and
`window_days` 1..366. The per-branch Misc paths stored for this source are applied automatically.
Nodes gain `path`, `path_key` ("id>id>id"), `expandable`, `has_more_children` and `misc_members[]` (Misc nodes only:
`{entity_id, entity_name, has_statement, attributed_amount, debit_amount, path, path_key, scope}`).
The response adds `trace_depth`, `misc_paths`, `root_tx_id`, `root_credit`, `date_from`, `date_to` and **`source_key`**
(the root entity ID, or `tx:<root_tx_id>`).
```json
{
  "legend_counts": {
    "credit_source": 1,
    "has_statement": 5,
    "details_only": 0,
    "total_entities": 6
  },
  "window_days": 14,
  "max_depth": 2,
  "trace_depth": 4,
  "mode": "fifo",
  "node_count": 6,
  "truncated": false,
  "misc_entity_ids": [],
  "misc_paths": [],
  "root_tx_id": null,
  "root_credit": null,
  "date_from": null,
  "date_to": null,
  "selected_source_id": "c3dc5367-a004-5e0b-b972-a32c3178e6db",
  "source_key": "c3dc5367-a004-5e0b-b972-a32c3178e6db",
  "root": {
    "node_id": "L0_c3dc5367",
    "entity_id": "c3dc5367-a004-5e0b-b972-a32c3178e6db",
    "entity_name": "AYAZ QURESHI",
    "role": "CREDIT_SOURCE",
    "amount": 500.0,
    "depth": 0,
    "has_statement": true,
    "is_misc": false,
    "misc_count": 0,
    "tx_ids": [
      "d8d10bca-59a0-45e0-b316-da7ac995b3ac",
      "... 1 more"
    ],
    "attributed_amount": 500.0,
    "debit_amount": 500.0,
    "is_system": false,
    "path": [
      "c3dc5367-a004-5e0b-b972-a32c3178e6db"
    ],
    "path_key": "c3dc5367-a004-5e0b-b972-a32c3178e6db",
    "expandable": true,
    "has_more_children": true,
    "misc_members": [],
    "children": "[... child nodes, same shape]"
  },
  "source_candidates": [
    {
      "entity_id": "99e0bc12-c63d-5a1e-903c-b8718a8aa6ae",
      "name": "APEX SHELL PVT LTD",
      "has_statement": true
    }
  ]
}
```
With `?root_tx_id=...`:
```json
{
  "root_tx_id": "f38d110d-f09c-4c7b-af7c-0ae88ed15793",
  "root_credit": {
    "tx_id": "f38d110d-f09c-4c7b-af7c-0ae88ed15793",
    "entity_id": "0f03c21b-926b-57f1-8848-124ca952d66d",
    "amount": 25.0,
    "date": "2026-02-15",
    "timestamp": "2026-02-15T10:00:00",
    "utr": null,
    "narration": "INT.PD/SB INTEREST",
    "counterparty_entity_id": "a4453bd0-aa9d-52f7-98a5-21d92b2db083"
  },
  "source_key": "tx:f38d110d-f09c-4c7b-af7c-0ae88ed15793",
  "selected_source_id": null,
  "trace_depth": 8
}
```

### GET /api/cases/{id}/trace/layering/expand?source=&path=&window_days=&mode=&root_tx_id=&date_from=&date_to=&trace_depth=  (new)
Children of one node, for lazy drill-down. `path` is the node's `path_key` and is required (400 if missing).
`source` defaults to the first ID in the path. Each child has `children: []`; use `has_more_children` to decide whether to expand it again.
```json
{
  "path": [
    "c3dc5367-a004-5e0b-b972-a32c3178e6db"
  ],
  "path_key": "c3dc5367-a004-5e0b-b972-a32c3178e6db",
  "entity_id": "c3dc5367-a004-5e0b-b972-a32c3178e6db",
  "entity_name": "AYAZ QURESHI",
  "depth": 0,
  "children_total": 500.0,
  "mode": "fifo",
  "window_days": 14,
  "trace_depth": 8,
  "truncated": false,
  "root_tx_id": null,
  "children": [
    {
      "node_id": "L1_a386c2dd_1",
      "entity_id": "a386c2dd-63dc-5196-b943-c1ff001b51c2",
      "entity_name": "POOJAN VIG",
      "role": "HAS_STATEMENT",
      "amount": 300.0,
      "depth": 1,
      "has_statement": true,
      "is_misc": false,
      "misc_count": 0,
      "tx_ids": [
        "d8d10bca-59a0-45e0-b316-da7ac995b3ac"
      ],
      "attributed_amount": 300.0,
      "debit_amount": 300.0,
      "is_system": false,
      "path": [
        "c3dc5367-a004-5e0b-b972-a32c3178e6db",
        "... 1 more"
      ],
      "path_key": "c3dc5367-a004-5e0b-b972-a32c3178e6db>a386c2dd-63dc-5196-b943-c1ff001b51c2",
      "expandable": true,
      "has_more_children": true,
      "misc_members": []
    }
  ]
}
```

### POST /api/cases/{id}/trace/layering/misc (changed)
* Global (unchanged): `{entity_id, move_to_misc}` returns `{"scope": "entity", "misc_entity_ids": [...]}`
* Per branch (new): `{path: [ids] | "id>id>id", move_to_misc, source_entity_id?, root_tx_id?}` returns
  `{"scope": "path", "source_key", "misc_paths": [...], "misc_entity_ids": [...]}`.
  The path needs the root and at least one child. A trailing `"MISC"` element is ignored. An unknown entity gives 404.
  To restore a Misc member with `scope: "path"`, send its `path_key` with `move_to_misc: false`.
```json
{
  "scope": "path",
  "source_key": "c3dc5367-a004-5e0b-b972-a32c3178e6db",
  "misc_paths": [
    "c3dc5367-a004-5e0b-b972-a32c3178e6db>a386c2dd-63dc-5196-b943-c1ff001b51c2"
  ],
  "misc_entity_ids": []
}
```
```json
{
  "scope": "path",
  "source_key": "c3dc5367-a004-5e0b-b972-a32c3178e6db",
  "misc_paths": [],
  "misc_entity_ids": []
}
```

### GET /api/cases/{id}/trace/bidirectional?a=&b=&rapid_window_hours=48&round_trip_window_hours=720&amount_tolerance=0.10&date_from=&date_to= (changed)
Bad parameters give 400; an unknown `a` or `b` gives 404. **Semantic change:** `rapid_round_trip_count` now counts
amount-matched A to B to A round trips. Use `rapid_reversal_count` and `direction_reversal_count` for the old count of direction flips.
```json
{
  "pairs": [
    {
      "entity_a_id": "99e0bc12-c63d-5a1e-903c-b8718a8aa6ae",
      "entity_a_name": "APEX SHELL PVT LTD",
      "entity_b_id": "a386c2dd-63dc-5196-b943-c1ff001b51c2",
      "entity_b_name": "POOJAN VIG",
      "total_a_to_b": 250000.0,
      "total_b_to_a": 180000.0,
      "net_flow": 70000.0,
      "tx_count_a_to_b": 1,
      "tx_count_b_to_a": 1,
      "total_mutual_volume": 430000.0
    }
  ],
  "selected_detail": {
    "entity_a_id": "99e0bc12-c63d-5a1e-903c-b8718a8aa6ae",
    "entity_a_name": "APEX SHELL PVT LTD",
    "entity_b_id": "a386c2dd-63dc-5196-b943-c1ff001b51c2",
    "entity_b_name": "POOJAN VIG",
    "total_a_to_b": 250000.0,
    "total_b_to_a": 180000.0,
    "net_flow": 70000.0,
    "min_turnaround_hours": 19.5,
    "median_turnaround_hours": 19.5,
    "max_turnaround_hours": 19.5,
    "rapid_round_trip_count": 0,
    "rapid_round_trip_count_b": 0,
    "direction_reversal_count": 1,
    "rapid_reversal_count": 1,
    "round_trips": [],
    "round_trip_stats": {
      "anchor_a": {
        "count": 0,
        "min_hours": null,
        "median_hours": null,
        "max_hours": null,
        "rapid_count": 0,
        "matched_volume": 0
      },
      "anchor_b": {
        "count": 0,
        "min_hours": null,
        "median_hours": null,
        "max_hours": null,
        "rapid_count": 0,
        "matched_volume": 0
      },
      "direction_flips": {
        "count": 1,
        "min_hours": 19.5,
        "median_hours": 19.5,
        "max_hours": 19.5
      }
    },
    "parameters": {
      "rapid_window_hours": 48.0,
      "round_trip_window_hours": 720.0,
      "amount_tolerance": 0.1,
      "date_from": null,
      "date_to": null
    },
    "ledger_a_to_b": [
      {
        "tx_id": "030ea9b8-4dd1-40cb-ae52-b26091a2ad4c",
        "date": "2026-04-16",
        "timestamp": "2026-04-16T14:00:00",
        "amount": 250000.0,
        "channel": "NEFT",
        "utr": null,
        "narration": "NEFT-HDFC0000240-4093001-POOJAN VIG",
        "cumulative": 250000.0
      }
    ],
    "ledger_b_to_a": [
      {
        "tx_id": "60297822-2ec5-4d5a-87bc-57bd49bad1d4",
        "date": "2026-04-17",
        "timestamp": "2026-04-17T09:30:00",
        "amount": 180000.0,
        "channel": "RTGS",
        "utr": "UTR4094002",
        "narration": "RTGS/UTR4094002/APEX SHELL PVT LTD",
        "cumulative": 180000.0
      }
    ],
    "cumulative_series": [
      {
        "timestamp": "2026-04-16T14:00:00",
        "date": "2026-04-16",
        "cumulative_a_to_b": 250000.0,
        "cumulative_b_to_a": 0.0,
        "net_a_to_b": 250000.0
      }
    ],
    "daily_series": [
      {
        "date": "2026-04-16",
        "a_to_b": 250000.0,
        "b_to_a": 0.0,
        "cumulative_a_to_b": 250000.0,
        "cumulative_b_to_a": 0.0,
        "net_a_to_b": 250000.0
      }
    ],
    "transactions": "[... rows with direction_code, cumulative_a_to_b, cumulative_b_to_a]"
  }
}
```

### GET /api/cases/{id}/trace/loops?max_days=&min_retention=&max_cycle_length=&max_instances_per_loop= (changed)
`max_instances_per_loop` must be 1..500 (default 50). Loops gain per-hop dates, UTRs and leakage, `intermediary_leakage`,
`total_leakage`, `instance_count`, `disjoint_instance_count` and `instances[]`.
```json
{
  "truncated": false,
  "truncation_reason": null,
  "elapsed_s": 0.0,
  "max_cycle_length": 6,
  "candidate_events": 5,
  "loop_count": 2,
  "loops": [
    {
      "loop_id": "27226e04-07e6-5859-baa7-5a9227d456fb",
      "hop_count": 3,
      "entities": [
        "HAWALA TRADERS",
        "... 3 more"
      ],
      "entity_ids": [
        "50c5419f-5843-5e24-8008-7d5e5c4ffe2d",
        "... 3 more"
      ],
      "min_amount": 425000.0,
      "max_amount": 440000.0,
      "retention_ratio": 0.9659,
      "duration_hours": 31.0,
      "start_date": "2026-04-02",
      "end_date": "2026-04-03",
      "total_leakage": 15000.0,
      "leakage_pct": 3.41,
      "instance_count": 1,
      "disjoint_instance_count": 1,
      "instances_truncated": false,
      "total_instance_volume": 440000.0,
      "hops": [
        {
          "hop_index": 1,
          "tx_id": "727f9a64-b8c2-49c5-a1aa-062fca0429a0",
          "from": "HAWALA TRADERS",
          "to": "APEX SHELL PVT LTD",
          "from_entity_id": "50c5419f-5843-5e24-8008-7d5e5c4ffe2d",
          "to_entity_id": "99e0bc12-c63d-5a1e-903c-b8718a8aa6ae",
          "amount": 440000.0,
          "date": "2026-04-02",
          "timestamp": "2026-04-02T11:00:00",
          "channel": "RTGS",
          "utr": null,
          "hours_since_previous_hop": null,
          "leakage": null,
          "leakage_pct": null
        }
      ],
      "intermediary_leakage": [
        {
          "entity_id": "99e0bc12-c63d-5a1e-903c-b8718a8aa6ae",
          "entity_name": "APEX SHELL PVT LTD",
          "received": 440000.0,
          "forwarded": 432000.0,
          "leakage": 8000.0,
          "leakage_pct": 1.82
        }
      ],
      "instances": [
        {
          "tx_ids": [
            "727f9a64-b8c2-49c5-a1aa-062fca0429a0",
            "... 2 more"
          ],
          "amounts": [
            440000.0,
            "... 2 more"
          ],
          "utrs": [
            null,
            "... 2 more"
          ],
          "start_date": "2026-04-02",
          "end_date": "2026-04-03",
          "start_timestamp": "2026-04-02T11:00:00",
          "end_timestamp": "2026-04-03T18:00:00",
          "min_amount": 425000.0,
          "max_amount": 440000.0,
          "retention_ratio": 0.9659,
          "duration_hours": 31.0,
          "leakage": 15000.0
        }
      ]
    }
  ]
}
```

### GET /api/cases/{id}/trace/graph?date_from=&date_to=&min_amount=&max_amount=&channel=&keyword=&ifsc= (changed)
`ifsc` now takes a comma list (up to 20) of full IFSCs or bank prefixes, applied by the engine. An invalid value, or
`max_amount` < `min_amount`, gives 400. `summary.filters` echoes the filters, and `summary.colour_by` is the legend.
Nodes have `colour_keys{role, statement, risk_tier, betweenness}` and `betweenness_percentile`.
```json
{
  "summary": {
    "total_nodes": 6,
    "total_edges": 5,
    "filters": {
      "date_from": "2026-04-01",
      "date_to": null,
      "min_amount": 1000.0,
      "max_amount": 500000.0,
      "channel": null,
      "keyword": null,
      "ifsc": [
        "SBIN",
        "... 1 more"
      ]
    },
    "colour_by": {
      "role": [
        "PRIMARY_HUB",
        "... 3 more"
      ],
      "statement": [
        "HAS_STATEMENT",
        "... 2 more"
      ],
      "risk_tier": [
        "CRITICAL",
        "... 4 more"
      ],
      "betweenness": [
        "none",
        "... 4 more"
      ]
    }
  },
  "nodes": [
    {
      "entity_id": "a386c2dd-63dc-5196-b943-c1ff001b51c2",
      "label": "POOJAN VIG",
      "node_role": "BRIDGE_CONDUIT",
      "risk_tier": "Low Risk",
      "betweenness_percentile": 1.0,
      "colour_keys": {
        "role": "BRIDGE_CONDUIT",
        "statement": "HAS_STATEMENT",
        "risk_tier": "LOW_RISK",
        "betweenness": "top"
      }
    }
  ]
}
```

---

## 8. Persistence and migration summary
New tables, created by an idempotent migration in one transaction on open: `entity_decisions`,
`entity_decision_history` (append-only triggers), `layering_misc_paths (case_id, source_key, path_key)` and
`case_settings (case_id, setting_key)`, which holds the Tally ledger map under `tally_ledger_map`. New columns:
`statements.pan`, `transactions.card_number_masked` and `transactions.cheque_number`. New index: `idx_tx_case_stmt`.
This was checked against a copy of a vault created by the pre-P1 code (opened twice, rows and audit chain intact,
entity IDs unchanged by a re-cluster) and against a fresh vault.
