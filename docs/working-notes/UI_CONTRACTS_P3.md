# CypherSOL P3 UI — foundation contracts

Root: `src/cyphersol/ui/`. No build step, native ES modules, no CDN, strict CSP. Legacy `app.js`/`styles.css`/old
`index.html` are in `/private/tmp/claude-501/cyphersol-p3/legacy/`; the feature inventory every view must cover is
`/private/tmp/claude-501/cyphersol-p3/legacy_inventory.md`. Reference views: `js/views/overview.js` (case view:
grids, onData, commands) and `js/views/cases.js` (global view). Check JS syntax with
`/private/tmp/claude-501/cyphersol-p3/scripts/check_js.sh`.

```
index.html              shell: skip link, #cloud-ai-banner, aside#sidebar, header.topbar, main#main, #toast-region
styles/tokens.css       design tokens (light + dark)       styles/base.css   reset, shell, layout utilities
styles/components.css   shared components                  styles/views/<view>.css   YOUR view styles (optional)
js/app.js               shell, export menu, theme, palette, global commands, new-case dialog
js/core/{api,store,router,nav,commands,format,dom,constants,results,theme,theme-init}.js
js/components/{toast,modal,drawer,tabs,datagrid,menu,palette,kpi,states,icons}.js
js/views/<name>.js      one module per view
```

## 1. Security rules (tested by `tests/test_p3_ui_server.py`, `tests/test_p0_security.py`)
- CSP on the HTML: `default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data: blob:; font-src 'self'; connect-src 'self'; object-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'`.
  - **No inline handlers** (`onclick=` …) → use `addEventListener` / `delegate()`.
  - **No `style="…"` attributes in markup** (blocked by `style-src 'self'`). Set dynamic values with CSSOM:
    `el.style.setProperty("--value", "42%")`, `setVars(el, {...})`, `el.style.transform = …`. SVG presentation
    attributes (`fill=`, `width=`, `x=`) are fine; prefer the token classes (`.fill-hub`, `.stroke-1`, `.svg-text-2`)
    or `fill="var(--chart-1)"`.
  - No `eval`/`new Function`, no external URLs, no `<style>` injection.
- Network: **only** through `core/api.js` (exactly one `fetch(` in the bundle). Never put the token in a URL.
- Escaping: every server/user string goes through `html```/`esc()` or `textContent`. A tagged-template interpolation
  that is a plain string is escaped, so attribute fragments must themselves be templates:
  `${on ? html`aria-current="page"` : ""}` — **not** `${on ? 'aria-current="page"' : ""}` (renders `&quot;`; a test
  rejects it).
- Never truncate `sha256_hash` (`.slice(` on it fails a test); show it with `.hash` (wraps, mono).

## 2. View module contract (`js/views/<name>.js`)
```js
export const styles = ["views/<name>.css"];   // optional; injected once from ui/styles/
export async function mount(el, ctx) {        // el = fresh <div class="view-root v-<name>" data-view="<name>">
  ...render into el...
  return cleanup;                             // fn | { unmount?(), update?(params) } | undefined
}
```
- `update(params)` (optional) is called instead of a remount when only the query string changes through
  back/forward or a link. `ctx.setParams()` never triggers mount or update.
- The router shows a skeleton until `mount` resolves and renders `errorState` with retry if it throws (do not
  catch-and-swallow fatal load errors; do catch per-panel errors and show `errorState` in that panel).
- Abandoned work: everything is aborted through `ctx.signal` on navigation; check `ctx.isCurrent()` after every
  `await` before touching the DOM.
- Registered names (`core/nav.js` VIEWS): global `cases`, `settings`; case-scoped `overview, ingest, transactions,
  suspense, anomalies, entities, tally, itr, loan, network, bidirectional, layering, loops, mules, profile`;
  internal `notfound`. Sidebar per mode: Edge = overview, ingest, transactions, suspense, anomalies, tally, itr, loan;
  Trace = overview, ingest, transactions, suspense, anomalies, network, bidirectional, layering, loops, mules,
  entities, profile; the other product's views sit under "More". Every view works for both case modes.

### `ctx`
| member | meaning |
|---|---|
| `view`, `title` | route name and its nav title |
| `caseId`, `case`, `mode` | current case (null for global views); `case` = `{case_id,title,mode,agency_or_firm_name,similarity_threshold,…}`, `mode` = `"cypher_edge"`/`"cypher_trace"`. Snapshot at mount; live values via `ctx.store.get()` |
| `params` | query string as plain object of strings (kept in sync by `setParams`) |
| `signal`, `isCurrent()` | AbortSignal of this mount; false once navigated away |
| `api` | case-scoped client (see §3) bound to `ctx.signal`: `ctx.api.get("trace/mules")`, `ctx.api.post("entities/recluster", body)`, `ctx.api.download("export/xlsx", "name.xlsx")`, `ctx.api.url(sub, params)` |
| `store` | the app store (§4) |
| `href(view, params?)` / `navigate(view, params?, {caseId?, replace?})` | links / navigation within the case |
| `setParams(patch, {push=false})` | merge into URL (null/"" deletes) without remounting — use for filters, tabs, selected entity |
| `setCrumbs([{label, href?}])` | extra breadcrumbs after the view title (e.g. selected entity) |
| `registerCommands([cmd])` | palette commands, auto-removed on unmount (§7) |
| `onData(topics, fn)` | fn(topic) after a mutation anywhere bumped one of the topics; auto-unsubscribed |
| `mutated(topics)` | = `store.mutated`: call after every successful POST |
| `onCleanup(fn)` | teardown registered from anywhere in the view |
| `toast`, `withBusy` | §6 |

URL conventions: `#/case/{id}/{view}?k=v`. Use these param names where they apply so links interoperate:
`entity` (entity_id, profile/network/bidirectional), `a`, `b` (bidirectional pair), `source` (layering source),
`tab` (sub-tab), `q`, `category_id`, `channel`, `direction` (CR/DR), `date_from`, `date_to`, `amount_min`,
`amount_max`, `sort`, `order`, `suspense`, `flagged`. E.g. a graph node → `ctx.navigate("profile", { entity: id })`.

## 3. `core/api.js`
```js
API_TOKEN, TOKEN_HEADER
apiFetch(path, { method, body, params, signal, headers }) -> Response     // throws ApiError | AbortError
api.get(path, { params, signal }) -> json ;  api.post(path, body, { signal }) -> json
caseUrl(caseId, sub) -> "/api/cases/{id}/{sub}" ; qs(params) -> "?a=1" (skips null/"")
caseApi(caseId, signal) -> { get(sub, params, opts), post(sub, body, opts), download(sub, name, opts), url(sub, params) }
downloadBlob(path, fallbackName, { params, signal, open=false }) -> { filename, size, type }   // header token + Blob save
fileToBase64(file) -> Promise<string>
isAbort(err) -> boolean          // never toast aborts
class ApiError { status, message (user-facing), serverMessage, friendly, errorId, body,
                 isAuth, isNotFound, isConflict, isValidation, isServer, isNetwork }
```
Status mapping: 0 network, 400 server text, 403 "session no longer authorised — reload", 404 server text,
409 "changed meanwhile — refresh", 411/413/415, 5xx "internal error" (+ `errorId`). POST bodies are always JSON.

Server additions (P3): `GET /api/cases/{id}/transactions` accepts `sort` ∈ {txn_date|date, debit, credit, amount
(=max(debit,credit)), running_balance|balance, payment_channel|channel, category_id, category_name,
confidence_score, extracted_counterparty|counterparty, source_holder_name, source_account_number, raw_narration,
categorization_source}, `order=asc|desc`, `amount_min`, `amount_max`, alongside the existing `offset, limit (≤2000),
q, category_id, suspense, flagged, include_ignored, statement_id, entity_id, channel, direction, date_from, date_to`.
Response adds `sort`, `order`. Unknown sort/order → 400. `/overview?tx_limit=0` returns the summary-only case payload.

## 4. `core/store.js`
```js
store.get() -> { meta, health, llmConfig, cases, caseId, case, mode, caseData, caseStatus, caseError, summary, versions }
store.subscribe(fn(state, changedKeys)) -> off ;  store.select(selector, fn, { fireNow }) -> off
store.loadMeta() / loadHealth() / loadLlmConfig() / loadCases() / createCase({title, mode, agency_or_firm_name})
store.saveLlmConfig(payload) -> cfg        // POST /api/llm/config, bumps "llm", refreshes health (banner)
store.setCase(id) / refreshCaseData() / ensureCaseData() -> caseData   // overview?tx_limit=0, generation-guarded
store.mutated(topics) -> Promise           // refresh caseData, THEN bump topics
store.bump(topics) ; store.onTopics(topics, fn) -> off ; store.version(topic)
store.categories() ; store.category(id) ; store.categoryLabel(id) -> "07 — Name"
store.entities()  // non-system entities of current case ; store.entity(id) ; store.statements()
TOPICS = { transactions, suspense, anomalies, entities, statements, analytics, llm, templates, cases }
MUTATION_TOPICS = { upload, resolveSuspense, toggleDuplicate, recluster, layeringMisc, runAgents, llmConfig, template }
```
`caseData` = overview payload: `case, revision, statements[], entities[], summary{statements_count,
transactions_count, entities_count, suspense_count, anomaly_count, total_credits, total_debits,
duplicates_ignored}, near_merge_candidates[], audit_logs[], recent_ingest_results[], llm_config,
latest_agent_telemetry` (transaction arrays are empty — page them via `/transactions`). Sidebar badges come from
`summary`, so **always `await ctx.mutated(MUTATION_TOPICS.x)` after a mutation**. Prefer patching the affected
rows (`grid.updateRow`) over refetching everything. Analytics views (graph, layering, …) refetch on
`ctx.onData([TOPICS.analytics], …)`. After saving templates call `store.loadMeta()` + `store.bump(["templates"])`.

## 5. `core/format.js`, `core/dom.js`, `core/constants.js`, `core/results.js`, `core/theme.js`
```js
inr(v, {decimals=2, sign, zero}) "₹12,34,567.50" / "−₹50.00" ;  inrCompact(v, {sign}) "₹950" "₹45,200" "₹1.2 L" "₹3.4 Cr"
num(v, d) ; int(v) ; pct(v, {decimals=1, ratio=true}) ; signClass(v) "is-neg|is-pos" ; txAmount(t) ; drCr(t)
date("2026-04-05") "05 Apr 2026" ; dateTime(iso) ; month("2026-04") "Apr 2026" ; relTime ; hours(h) ; plural ; truncate ; bytes ; DASH
esc(s) ; html`…` -> SafeHTML ; raw(trustedString) ; join(parts, sep) ; render(el, SafeHTML|Node|text) ; toNode(SafeHTML)
$(sel, root) ; $$ ; on(target, type, fn) -> off ; delegate(root, type, selector, (e, matched) => …) -> off
trapFocus(container, {initial, restore}) -> release ; debounce(fn, ms) (.cancel/.flush) ; uid(prefix)
cssVar("--chart-1") (canvas colours) ; setVars(el, {"--x": v}) ; formValues(form)
UPLOAD_ACCEPT ; CASE_MODES, caseModeLabel ; EXEC_MODES, execModeLabel ; AGENT_STAGES ; PAYMENT_CHANNELS
RISK_TIERS, tierClass("Critical")->"critical", tierFromScore ; MULE_PARAMS [{key,label,weight}]
GRAPH_ROLES {PRIMARY_HUB:{label,cls:"hub"},BRIDGE_CONDUIT:"conduit",EXIT_ENDPOINT:"exit",PERIPHERAL_LEAF:"leaf"}
LAYER_ROLES {CREDIT_SOURCE:"source",HAS_STATEMENT:"statement",DETAILS_ONLY:"details",MISC:"misc"} ; entityStatus(e)
ANOMALY_FLAGS / flagMeta(flag) -> {label, tone, actions:["toggle-duplicate"]?} ; EXPORTS ; STORAGE_KEYS ; lsGet/lsSet (never throw)
describeTallyPushResult(res) -> {ok, tone, title, text}   // tally view MUST use this (tested)
describeUploadResult(res, mode) ; describeAgentTelemetry(tel) -> {mode, summary, warnings, stagesInvoked}
getThemePref() ; setThemePref("light"|"dark"|"system") ; cycleTheme() ; effectiveTheme() ; onThemeChange(fn) -> off
```
Money: right-align (`.num` / `align:"right"`), tabular numerals, `−` minus, compact in tiles with exact
`inr()` in `title`, `.is-debit` / `.is-credit` for Dr/Cr colour (never colour alone — keep the sign).

## 6. Components (`js/components/*`) — all return/accept SafeHTML
```js
toast(msg, {type:"info|success|warn|error", title, timeout(0=sticky), action:{label, run}}) -> dismiss
toast.success / .warn / .info / .error(err, {title})   // replaces alert(); aborts ignored
withBusy(buttonOrNull, asyncFn, {success, error, silent}) -> result|undefined   // busy state, no double submit, error toast
openModal({title, description, body, size:"sm|md|lg|xl", actions:[{label, value, variant, type:"submit", spacer}],
           onSubmit:async(values,{el,close,form}) => false keeps open, onOpen, initialFocus, dismissible}) -> {el, body, form, close, closed}
confirmDialog({title, message, confirmLabel, danger}) -> Promise<boolean>
openDrawer({title, body, footer, width=520, side, onClose}) -> {el, body, setTitle, setBody, setFooter, close, closed}; closeDrawer()
createTabs(el, {tabs:[{id,label,badge,icon}], selected, onChange(id, panelEl), label, fireInitial=true})
   -> {panel(id), select(id), selected, setBadge(id,n), setLabel(id,text)}    // ARIA tabs, arrow keys
createDataGrid(el, opts) -> grid   // see below
createMenu(button, {items:[{group}|{separator}|{id,label,description,icon,hint,disabled,checked,run|href}] | () => items, align, label}) -> {open, close, isOpen, destroy}
openPalette(query?) ; initPalette()  (shell only)
kpiGrid([{label, value, sub, tone:"success|danger|warn|critical|accent", title, icon, href}], {compact, label}) ; kpiTile(item)
emptyState({icon, title, message, action:{label, href}|{label, id}, tone, compact}) ; errorState(err, {retryId, title, compact})
skeleton({kind:"text|table|kpis|cards|block", rows}) ; viewSkeleton() ; spinner() ; pageHead({title, sub, actions})
icon(name, {size:"sm|lg|xl", label, className})   // names: ICON_NAMES in icons.js
```
**Data grid** `createDataGrid(el, {...})`:
`columns:[{key, label, width:"120px"|"minmax(200px,2fr)", align:"left|right|center", sortable, sortKey, defaultOrder,
format(value,row)->string, render(row,i)->SafeHTML, className:string|fn(row), title(row)->tooltip, wrap, sortValue(row)}]`;
server mode `fetchPage({offset,limit,sort,order,filters,signal}) -> {rows,total}` or client mode `rows:[]` +
`clientFilter(row, filters)`; `filters:[{key,type:"search"|"select"|"toggle", label, placeholder, options}, {type:"date-range"|"number-range", label, keys:[from,to]}]`;
`initialFilters, initialSort:{key,order}, pageSize=200, rowHeight=36, height (CSS), rowKey, onRowClick(row,{index,event}),
onStateChange({sort,order,filters}), emptyState, ariaLabel, countNoun:[one,many], toolbarExtra (SafeHTML), showToolbar`.
API: `reload({keepScroll}) refresh() setFilters(patch,{replace,silent}) getFilters() setSort(key,order) getState()
setRows(rows) getRow(i) rowFromEvent(e) updateRow(key, patch|fn) focus() destroy() el toolbar scroller`.
Virtualised, "X–Y of Z" counter, aria-sort headers, ↑/↓/PgUp/PgDn/Home/End/Enter row navigation, debounced search,
skeleton / error+retry / empty / "no matching rows + clear filters" states. Buttons inside rows: give them
`data-action` and use `delegate(grid.el, "click", "[data-action]", (e, b) => { const row = grid.rowFromEvent(e); … })`
(row click is suppressed for buttons/links/inputs). Destroy grids in cleanup (`ctx.onCleanup(() => grid.destroy())`).

## 7. Palette commands
`ctx.registerCommands([{ id:"<view>.<action>", title, group:"<View title>", icon, keywords:[], hint, when:()=>bool, run }])`.
Global ones already exist: "Go to <view>", "Switch to <case>", "New case…", "Refresh case data", "Export …",
"Theme: …", "Toggle sidebar". Shell events: `window.dispatchEvent(new CustomEvent("cyphersol:new-case"))`.

## 8. CSS conventions
- Shared classes only from base/components.css. View-specific classes **must** be prefixed `.v-<view>-…`
  (e.g. `.v-network-canvas`, `.v-layering-node`) and live in `styles/views/<view>.css` declared via `export const styles`.
  Never restyle shared classes globally from a view file; scope under `.v-<view>` (the view root has that class).
- Layout utilities: `.stack .stack-sm .stack-lg .row .row-wrap .row-between .spacer .grid-2 .grid-3 .grid-auto .min-w-0`;
  text: `.num .tabular .mono .hash .muted .text-2 .small .xsmall .strong .truncate .nowrap .caps .is-debit .is-credit .is-neg .is-pos .sr-only`.
- Components: `.card(__head|__title|__sub|__body|__body--flush|__foot) .card--interactive`, `.btn(--primary|--danger|--ghost|--subtle|--sm|--lg|--icon|--block) .btn-group`,
  `.seg/.seg__btn[aria-checked]`, `.field .field__label .field__hint .field__error .form-grid .input .select .textarea .input--sm/--num/--w-xs|sm|md .input-group .check .switch[role=switch][aria-checked]`,
  `.toolbar`, `.callout(--info|success|warn|danger)`, `.badge(--neutral|accent|info|success|warn|danger|critical|suspicious|low|source|statement|details|misc|system|outline|lg|dot)`,
  `.chip[aria-pressed]`, `.legend .legend__item .legend__swatch(--round) .sw-<role|tier|1..8|credit|debit>`, `.kpi-grid .kpi`,
  `.table-wrap(--max) .table(--compact)` for small static tables, `.meter(--critical|suspicious|low) > .meter__fill` (width via `--value`),
  `.kv` (dl), `.code`, `.tooltip` (position via CSSOM), `.empty`, `.skeleton`, `.spinner`.
- SVG: `.fill-<source|statement|details|misc|system|hub|conduit|exit|leaf|critical|suspicious|low|1..8|credit|debit>`,
  `.stroke-<1|2|3|credit|debit|muted|strong|critical>`, `.svg-text .svg-text-2 .svg-text-3 .svg-grid .svg-axis .svg-surface`. No hex in views.
- Responsive: shell drawer < 768 px; use `minmax(min(100%, Npx), 1fr)` grids; wide content scrolls inside its own
  container (`.table-wrap`, grid frame) — never page-level horizontal scroll. Check 375 / 768 / 1440 in light and dark.
- Focus: the global 2 px `:focus-visible` ring; don't remove outlines. Every input has a `<label>` or `aria-label`.

## 9. Tokens (`styles/tokens.css`)
Surfaces `--bg --surface-1 --surface-2 --surface-3 --surface-sunken --surface-overlay --surface-inverse --scrim`;
text `--text-1 --text-2 --text-3 --text-disabled --text-inverse --text-link`; borders `--border-1 --border-2 --border-control --border-strong`;
accent `--accent --accent-hover --accent-active --accent-fg --accent-bg --accent-text --accent-border`; focus `--focus-ring --focus-width --focus-offset`;
status `--success|--warn|--danger|--info` + `-bg` + `-text`; money `--debit-text --credit-text`;
risk `--risk-critical|--risk-suspicious|--risk-low` + `-bg` + `-text` (thresholds 80/50);
layering roles `--node-source --node-statement --node-details --node-misc --node-system` + `-bg`;
graph roles `--node-hub --node-conduit --node-exit --node-leaf --edge-muted --edge-strong --node-stroke` (deliberately not red/amber/green);
charts `--chart-1..8` (blue, orange, aqua, yellow, magenta, green, violet, red — CVD-validated order; assign in order, never cycle;
scatter-like charts max 3 series), `--chart-credit --chart-debit --chart-seq-1..7 --chart-neutral --chart-grid --chart-axis --chart-surface`;
type `--font-sans --font-mono --fs-2xs..--fs-3xl --fw-* --lh-*`; space `--sp-0..--sp-12`; radius `--r-xs..--r-full`;
shadow `--shadow-xs..--shadow-lg`; layout `--sidebar-w --topbar-h --control-h --control-h-sm --row-h`; z `--z-sticky --z-sidebar --z-popover --z-modal --z-toast`.
Dark values live in two identical blocks (OS preference and `[data-theme="dark"]`); canvas renderers read colours with
`cssVar()` and redraw on `onThemeChange()`. Text tokens verified ≥ 4.5:1, marks/borders ≥ 3:1 in both themes.

## 10. Shell behaviour views can rely on
Single Export menu (mode-filtered: Edge xlsx/tally-xml/dossier-pdf/dossier-html, Trace without tally-xml; Blob
downloads; dossier-html is downloaded because blob tabs inherit the CSP); runtime chip → `#/settings` (Settings is
the single source of truth for execution mode; use `store.saveLlmConfig`); Cloud-AI banner driven by
`store.health.cloud_egress_possible` (re-polled on tab focus and after `saveLlmConfig`); theme toggle (light/dark/system,
persisted); ⌘K/Ctrl+K palette; `[` toggles the sidebar; case switcher keeps the current view when the target mode has it.

## 11. Integration additions (P3 integration pass)
- `GET …/transactions` also takes `tx_ids` (comma-separated, ≤ 500, else 400) and `sort=row_index` (statement order; use with `statement_id`).
- `GET …/trace/graph` also takes `max_amount` (per transaction) and `ifsc` (full IFSC or bank prefix; matches the statement account's IFSC or the narration's counterparty IFSC). `max_amount < min_amount` or a malformed ifsc → 400.
- `GET …/edge/loan` validates foir ∈ (0,100], rate ∈ [0,50], tenure ∈ [1,480] (400 otherwise). `POST …/trace/layering/misc` 404s for an entity not in the case.
- Transactions URL param `ids` (→ `tx_ids`) shows a dismissible "Filtered to N evidence rows" chip; mules/profile/bidirectional link rows that way.
- `core/files.js`: `csvCell, toCsv, safeFilename, saveBlob, downloadText, downloadCsv` (the only client-side file savers).
- `core/api.js`: `linkAbort(parentSignal, controller)`. `core/format.js`: `catNo(id)`, `clockTime(iso)`; `hours()` keeps one decimal below 24 h.
- `core/constants.js`: `STORAGE_KEYS.tally`, `getTallyConnection()`, `setTallyConnection(patch|null)` (Settings and Tally share them).
- Data grid: `rowClassName(row)`, `onRowToggle(row)` (Space), grouped select options `{group, options}`, `getVisibleRows()`, `getActiveIndex()`.
- `.subhead` (components.css) for h3/h4 sub-section headings. `renderStub` and `.stub-list` removed.
