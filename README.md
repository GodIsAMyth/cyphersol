# CypherSOL — Unified `CypherEdge` & `CypherTrace` Platform

Offline-first Python platform unifying **CypherEdge** (Bank Statement Analyzer for CAs, Tax Professionals, MSMEs & Loan DSAs) and **CypherTrace** (Forensic Fund Trail & Anti-Money Laundering Intelligence Suite) per [Bank_Statement_and_Fund_Trail_Analyzer_SRS.md](Requirement%20Documents/Bank_Statement_and_Fund_Trail_Analyzer_SRS.md).

---

## Runtime Modes & AI API Support (Offline Heuristics + Optional Stages 1–4 LLM Agents)

CypherSOL works **100% offline with zero API keys by default**, and optionally supports **4 Multi-Provider LLM Backup Agents** (with Vision support) across **Stages 1–4**:

- **Zero Required Subscriptions (Heuristics Mode):** Runs locally using the Python `3.11+` standard library (`dependencies = []` in [pyproject.toml](pyproject.toml)), deterministic Indian bank narration parsers, **886-term curated knowledge base** (measured by `get_metadata().knowledge_base`; the SRS FR-CAT-02 target of 7,500+ is not yet met), and local Multinomial Naive Bayes classifier — zero data leaves your workstation.
- **Multi-Provider LLM & Vision Backup (`Gemini`, `Claude`, `OpenAI`):** When configured during deployment (`--configure-llm` / interactive startup wizard) or via the **AI Config** modal in the UI, CypherSOL supports Google Gemini, Anthropic Claude, and OpenAI (using your own BYOK API key stored strictly locally in the git-ignored SQLite vault `data/cyphersol_vault.db`):
  - **Stage 1 (`Stage1VisionExtractionAgent`)**: Multimodal Vision extraction from scanned PDFs and statement photos/images when deterministic OCR returns 0 rows or balance breaks.
  - **Stage 2 (`Stage2ReconciliationAgent`)**: Repairs OCR balance mismatches (`|Δ| > ₹0.05`) and infers missing bank/IFSC/holder metadata.
  - **Stage 3 (`Stage3NarrationAgent`)**: Extracts Indian payment channels (`UPI`/`NEFT`/`RTGS`/`IMPS`), UTR/RRN, IFSC/VPA, and clean counterparties on ambiguous narrations.
  - **Stage 4 (`Stage4CategorizationEntityAgent`)**: Classifies Suspense/low-confidence entries into the **52-category taxonomy** and resolves tricky transliterated Indian entity aliases.
- **Runtime Engine Mode Switcher:** Switch anytime in the header or per upload batch between **Heuristics Only** (100% offline), **Hybrid (Both: Heuristics + LLM Backup)**, or **LLM Primary** (all 4 AI agents).

---

## Documentation

- **[User Guide](docs/USER_GUIDE.md)** — End-to-end walkthrough of launching the UI, demo cases (`CASE_EDGE_BALAJI_FY25` & `CASE_TRACE_EOW_041`), PDF Draw Tool, Anomaly Gate, 52-category reference table, Suspense Workbench, `Merge Similar`, CypherEdge (`Tally XML`, `ITR Compliance`, `Loan ABB/FOIR`), CypherTrace (`Layering Tree`, dual-panel `Force-Directed Network Graph`, `10-Param Mule Scoring`, `Circular Loops`, `Bidirectional Flow`), and Section 63 BSA exports.
- **[Deployment & Administration Guide](docs/DEPLOYMENT_GUIDE.md)** — Installing Python 3.11+, starting/stopping across macOS/Linux/Windows, one-click desktop launchers, air-gapped wheelhouse installs, Git setup, `systemd`/`launchd` services, TLS reverse proxy, SQLite hot backups, Tally `:9000` setup, and REST API reference.
- **[Developer & Architecture Guide](docs/DEVELOPER_AND_ARCHITECTURE_GUIDE.md)** — Layered package architecture, execution sequence diagrams, mathematical & algorithmic specifications across all subsystems, and 6 step-by-step extension playbooks for maintainers.
- **[Data & Variable Dictionary](docs/DATA_AND_VARIABLE_DICTIONARY.md)** — Complete engineering reference of all 5 Enums, 16 domain/engine dataclasses, 9 SQLite WAL tables & indexes, module constants, thresholds, regexes, REST API payload schemas, and UI state variables.

---

## How to Start the Application

### 1. One-Click Desktop Launch (No Terminal Commands Required)

- **macOS (Finder):** Double-click **`Start_CypherSOL.command`** *(on first run, if macOS asks to confirm, Right-Click $\to$ **Open** $\to$ **Open**)*.
- **Windows (File Explorer):** Double-click **`Start_CypherSOL.bat`**.

### 2. Single-Command Terminal Launch

Run **one** command from the project folder — no `.venv` path or extra flags required:

- **macOS & Linux (`zsh` / `bash`):**
  ```bash
  ./start.sh
  ```
  *(Or run `python3 run_app.py` directly — `run_app.py` automatically switches into `.venv` if present.)*
- **Windows 10 / 11 (PowerShell or CMD):**
  ```powershell
  .\Start_CypherSOL.bat
  ```
  *(Or run `py -3 run_app.py` directly.)*

**What happens automatically when you launch:**
- Bootstraps `.venv` on first launch if needed and opens **`http://127.0.0.1:8080/`** in your default browser automatically.
- If CypherSOL is **already running**, re-launching opens your active browser tab cleanly instead of failing.
- If another program occupies port `8080`, it automatically picks the next free port (`8081–8090`).
- Press **`Ctrl + C`** in the launcher terminal window to stop the server cleanly.

### CLI Options (`run_app.py` / `start.sh` / `cyphersol`)

| Flag | Default | Purpose |
| :--- | :--- | :--- |
| `--host` | `127.0.0.1` | Bind host interface |
| `--port` | `8080` | Preferred HTTP port (auto-falls back to `8081–8090` if occupied by another app) |
| `--db` | `data/cyphersol_vault.db` | Path to local SQLite WAL vault |
| `--samples` | `sample_statements` | Folder for auto-generated demo statements |
| `--open-browser` | Enabled by default | Kept for backward compatibility; browser opens automatically |
| `--no-browser` | `False` | Suppress automatic browser launch (for headless / `systemd` / `launchd` services) |
| `--configure-llm` | `False` | Run the interactive deployment setup wizard (runtime mode, Gemini/Claude/OpenAI provider, models, masked API key) |
| `--non-interactive` | `False` | Skip interactive first-run LLM configuration prompt (for CI, `systemd`, `launchd`) |
| `--execution-mode` | Saved vault setting | Override runtime mode at startup (`heuristics`, `both`, or `llm`) |

---

## Running Tests & Optional Accelerators

```bash
# Run all 14 automated unit, integration, & LLM backup agent tests
.venv/bin/python -B -W default -m unittest discover -s tests -v

# Optional performance / encrypted-PDF packages (not required for core features)
.venv/bin/pip install pdfplumber pikepdf openpyxl rapidfuzz
```

---

## Implemented Modules

- **Module A (`cyphersol.ingestion`)**: Multi-format parser (native `.pdf`, `.xlsx`, `.csv`, `.tsv`, `.txt`) with automatic table-header column inference, **113 Indian bank fingerprints**, standardized bulk naming, interactive **PDF Draw Tool** coordinate extraction, SHA-256 chain-of-custody hashing, and **Step 02 Anomaly Gate** (running balance checks & duplicate detection).
- **Module B (`cyphersol.nlp`)**: Indian payment rail parser (`UPI`, `NEFT`, `RTGS`, `IMPS`, `NACH/ECS`, `ATM`, `AEPS`, `POS`), **886-term curated knowledge base** (measured by `get_metadata().knowledge_base`; the SRS FR-CAT-02 target of 7,500+ is not yet met), hybrid deterministic + Multinomial Naive Bayes classifier across **52 standardized categories**, **Suspense Reduction & Auto-Learn Workbench**, and fuzzy + Indian phonetic **Entity Resolution (`Merge Similar`)** (`0.50–1.00`).
- **Module C (`cyphersol.edge`)**: Smart Tally Ledger/Voucher mapping, One-Click **Tally XML** export & `127.0.0.1:9000` Upload Utility, **ITR-Ready Compliance Report** (`Sec 269ST`, `Sec 40A(3)`, `Sec 194I/194J`, `Sec 115BBH`, `80C/80TTA`), and **Loan Eligibility / Credit Appraisal** (`Daily/Fixed-Date ABB`, `EMI Load`, `I/W & O/W Bounces`, `FOIR`, `Max Eligible Loan`).
- **Module D (`cyphersol.trace`)**: Dual-panel force-directed **Forensic Network Graph** (`Full Multi-Account Fund Trail Topology` + `Central Conduit & Bridge Sub-Network Focus`, Brandes betweenness & PageRank), interactive **Layering Detection Tree** (`Credit Source`, `✓ Has Statement`, `Details Only`, `Move to Misc`), temporal **Circular Loop Detection** ($A \to B \to C \to A$), **Bidirectional Flow Analysis**, **10-Parameter Behavioral Mule Account Scorer** (`Critical 80–100`, `Suspicious 50–79`, `Low Risk 0–49`), and **Individual Financial Profiles**.
- **Reporting & Persistence (`cyphersol.storage`, `cyphersol.reporting`)**: Thread-safe SQLite WAL vault (`CypherVault`), 6-sheet styled `.xlsx` workbook exporter, Tally `.xml` exporter, native `.pdf` dossier, and Section 63 BSA printable `.html` dossier with embedded network SVG.
