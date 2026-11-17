# CypherSOL User Guide (`CypherEdge` + `CypherTrace`)

**Platform:** CypherSOL — Offline-First Bank Statement Analyzer (**CypherEdge**) & Forensic Fund Trail Intelligence Suite (**CypherTrace**) with Optional Stages 1–4 AI Backup Agents  
**Key Guarantee:** Works **100% locally offline** in **Heuristics Only** mode (`zero` internet calls, `zero` subscriptions required), or in **Both (Hybrid)** / **LLM Primary** modes using your own API key (**Google Gemini**, **Anthropic Claude**, or **OpenAI** with Vision).

---

## Table of Contents

1. [How to Start, Open, and Stop the Application](#1-how-to-start-open-and-stop-the-application)
2. [Understanding the Workspace Interface & Global Controls](#2-understanding-the-workspace-interface--global-controls)
3. [Pre-Loaded Demo Cases & Sample Statements Reference](#3-pre-loaded-demo-cases--sample-statements-reference)
4. [Step 01 — Multi-Format Bulk Upload & Interactive PDF Draw Tool](#4-step-01--multi-format-bulk-upload--interactive-pdf-draw-tool)
5. [Step 02 — Automatic Anomaly Gate](#5-step-02--automatic-anomaly-gate)
6. [Step 03 — AI-Categorized Transactions & 52-Category Taxonomy](#6-step-03--ai-categorized-transactions--52-category-taxonomy)
7. [Step 04 — Suspense Reduction Workbench & Entity Resolution (`Merge Similar`)](#7-step-04--suspense-reduction-workbench--entity-resolution-merge-similar)
8. [Step 05A — CypherEdge Guide (`Tally XML`, `ITR Compliance`, `Loan Eligibility`)](#8-step-05a--cypheredge-guide-tally-xml-itr-compliance-loan-eligibility)
9. [Step 05B — CypherTrace Forensic Guide (`Layering`, `Network Graph`, `Mules`, `Loops`, `Bidirectional`)](#9-step-05b--cyphertrace-forensic-guide)
10. [Step 06 — Individual Financial Profiles, Audit Trail & Courtroom Exports](#10-step-06--individual-financial-profiles-audit-trail--courtroom-exports)

---

## 1. How to Start, Open, and Stop the Application

CypherSOL includes ready-to-run launchers so you do **not** have to remember long `.venv` paths or browser flags.

### Option A — One-Click Desktop Launch (No Terminal Commands)

- **macOS (Finder):** Open the CypherSOL folder and double-click **`Start_CypherSOL.command`**.
  *(On first launch, if macOS asks for confirmation, Right-Click `Start_CypherSOL.command` $\to$ **Open** $\to$ **Open**.)*
- **Windows (File Explorer):** Open the CypherSOL folder and double-click **`Start_CypherSOL.bat`**.

On first run, the launcher initializes `.venv` automatically if needed, starts the local engine, prints the startup banner, and **opens `http://127.0.0.1:8080/` in your default web browser automatically**. Keep the launcher terminal window open while working.

> **Tip:** If CypherSOL is already running and you double-click the launcher again, it detects the active server, brings up `http://127.0.0.1:8080/` in your browser cleanly, and exits without throwing an error. If another program is already using port `8080`, CypherSOL automatically binds to the first free port in `8081–8090`.

### Option B — Single-Command Terminal Launch

1. Open **Terminal** (macOS/Linux) or **PowerShell / Command Prompt** (Windows) in the CypherSOL folder:
   ```bash
   cd /path/to/cyphersol          # macOS / Linux
   cd C:\path\to\cyphersol        # Windows
   ```
2. Run **one** command:
   ```bash
   ./start.sh                     # macOS / Linux (or: python3 run_app.py)
   ```
   ```powershell
   .\Start_CypherSOL.bat          # Windows (or: py -3 run_app.py)
   ```
3. You will see confirmation in the terminal:
   ```text
   ====================================================================
   CypherSOL Offline Platform running at http://127.0.0.1:8080/
   Vault DB:          .../data/cyphersol_vault.db
   Sample Statements: .../sample_statements
   Press Ctrl + C in this window to stop the server.
   ====================================================================
   ```
   Your default browser opens automatically to **`http://127.0.0.1:8080/`**.

### Option C — Container & Cloud Deployments (Docker, Render, Google Cloud, AWS, Azure)

For team collaboration or centralized cloud hosting, CypherSOL supports five production container targets with persistent volume storage (`/data`):

1. **Docker / Docker Compose (Local or VM):**
   ```bash
   docker compose up -d
   ```
   Opens at `http://localhost:8080` with data persisted to `./data:/data`.
2. **Render (One-Click Blueprint):** Connect GitHub repo, Render detects `render.yaml`, spins up container with 10GB persistent disk at `/data`.
3. **Google Cloud Run:** Run `./deploy/google-cloud-run/deploy.sh` to deploy with Google Cloud Storage bucket volume mount.
4. **AWS (ECS Fargate / App Runner):** Deploy via `deploy/aws/ecs-task-definition.json` with Amazon EFS persistent storage mount.
5. **Azure Container Apps:** Run `./deploy/azure/deploy-container-app.sh` with Azure Files SMB share volume mount.

*See [DEPLOYMENT_GUIDE.md](DEPLOYMENT_GUIDE.md) for full configuration, environment variables, and security settings.*

### Custom Port, Separate Client Database, or Headless Start

Pass any optional flags directly to `./start.sh`, `Start_CypherSOL.bat`, or `python3 run_app.py`:

- **Use a specific port:** `./start.sh --port 8090` (Windows: `.\Start_CypherSOL.bat --port 8090`)
- **Open a separate encrypted client/case database:**
  ```bash
  ./start.sh --db data/client_acme_2026.db
  ```
- **Start without opening a browser tab automatically:** `./start.sh --no-browser`

### Stopping the Application

Switch back to the terminal window running CypherSOL and press **`Ctrl + C`**. Terminal prints `Shutting down CypherSOL server.` and releases the port.

---

## 2. Understanding the Workspace Interface & Global Controls (Light Contemporary UI)

CypherSOL features a bright, high-clarity frosted-glass workspace with live runtime engine switching and per-stage AI agent observability:

```text
+---------------------------------------------------------------------------------------------------+
| [CS] CypherSOL  CypherEdge + CypherTrace   [Runtime: Heuristics | Both (Hybrid) | LLM Primary]    |
|      Telemetry • 113 Banks • AI Status     [AI Config]  [Case Selector v]  [+ New Case / Client]  |
|                                            [Excel] [Tally] [PDF] [Sec 63 / Dossier]               |
+---------------------------------------------------------------------------------------------------+
| Agent Status Strip: [S1 Vision] [S2 Reconcile] [S3 Narration] [S4 52-Cat & Entity] [Run AI Agents]|
+---------------------------------------------------------------------------------------------------+
| [01 Upload & Draw Tool] -> [02 Anomaly Gate] -> [03 52-Cat Transactions] -> [04 Suspense & Ent.]  |
| -> [05A CypherEdge (Tally / ITR / Loan)] -> [05B CypherTrace (Forensic & Mule)] -> [06 Profiles]  |
+---------------------------------------------------------------------------------------------------+
| [Statements]  [Transactions]  [Total Credits]  [Total Debits]  [Suspense Pending]  [Anomalies]    |
+---------------------------------------------------------------------------------------------------+
```

### Top Bar, Runtime Switcher & AI Agent Controls

- **Telemetry Status (`#telemetry-bar`):** Displays active mode (`Heuristics`, `Hybrid`, or `LLM`), configured provider, supported banks (`113`), categories (`52`), and active knowledge-base data points (`12,777`).
- **Global Runtime Engine Switcher (`#global-mode-switch`):** Switch anytime at runtime between:
  - **`Heuristics`**: 100% offline deterministic + local Naive Bayes execution (`0` external API calls).
  - **`Both (Hybrid)`**: Runs deterministic heuristics first, then invokes **Agents 1–4** only where scans/anomalies/unknown narrations/suspense rows need backup.
  - **`LLM Primary`**: Runs Stages 1–4 AI agents across statements cleanly (falling back safely to heuristics if a provider call fails).
- **`AI Config` Button & Modal (`#llm-config-dialog`):** Configure your provider (**Google Gemini**, **Anthropic Claude**, or **OpenAI**), text model, multimodal **Vision model**, per-stage toggles (Agents 1–4), and masked BYOK API key stored locally in `data/cyphersol_vault.db`. Includes a **Test Live API Connection** button.
- **Stage 1–4 Agent Status Strip & `Run AI Backup Agents (S2–S4)` Button (`#btn-run-case-agents`):** Shows real-time status pills for **Stage 1 Vision**, **Stage 2 Reconcile**, **Stage 3 Narration**, and **Stage 4 Categorize/Entity**, and lets you re-run Agents 2–4 on any already-ingested workspace.
- **Workspace / Case Selector (`#case-selector`):** Switches instantly between stored investigation cases or CA client workspaces.
- **`+ New Case / Client` Button:** Opens a dialog with **Case / Client Title**, **Primary Operating Mode** (`CypherTrace` or `CypherEdge`), and **Agency / CA Firm Name**.
- **Global One-Click Exports:** `Excel (.xlsx)`, `Tally (.xml)`, `Dossier (.pdf)`, and `Sec 63 / Dossier` (printable HTML/SVG report) are always one click away for the active case.

---

## 3. Pre-Loaded Demo Cases & Sample Statements Reference

On first launch against a new database, CypherSOL creates two complete demo workspaces backed by 11 files in `sample_statements/`:

### Demo Case 1: `CASE_EDGE_BALAJI_FY25` (`CypherEdge` Mode)
**Subject:** `Shree Balaji Enterprises` (`SBI Current A/C 382910445566`, `CA2025_SHREE-BALAJI-ENTERPRISES_SBI_382910445566.pdf`, Apr–Jun 2025).  
**Demonstrates:** Multi-page PDF parsing with empty Debit/Credit column preservation, NACH EMI tracking (`₹42,000/mo`), inward return/bounce detection, GST/Advance Tax compliance, **Section 269ST** (`₹2,50,000` single-day cash deposit), **Section 194I/194J** TDS alerts, Tally XML generation (`17` ledgers, `23` vouchers), and Loan Eligibility computation.

### Demo Case 2: `CASE_TRACE_EOW_041` (`CypherTrace` Mode)
**Subject:** 10 interconnected multi-format statements (`PDF`, `XLSX`, `CSV`) representing an organized cyber fraud & laundering network:

| Sample File | Holder & Bank | Forensic Pattern Demonstrated |
| :--- | :--- | :--- |
| `CASE041_POOJAN-VIG_HDFC_501002938471.pdf` | Poojan Vig (`HDFC`) | Mid-layer receiver/distributor (`₹300` split into `₹200` + `₹100`) & rapid bidirectional round-trip with `Apex Shell Pvt Ltd` (`₹2.50L` in $\to$ `₹1.80L` back) |
| `CASE041_AYAZ-QURESHI_SBI_30112233445.csv` | Ayaz Qureshi (`SBI`) | Root `Credit Source` (`₹500` seeded downward) |
| `CASE041_DEEP-PATANGE_ICICI_000401992831.csv` | Deep Patange (`ICICI`) | Alias variant `"Pujan Vij"` tested by fuzzy/phonetic `Merge Similar` |
| `CASE041_MANISH-PATIL_SBI_30981234001.csv` | Manish Patil (`SBI`) | Level-2 uploaded node forwarding to external beneficiary `Bharat Traders` |
| `CASE041_IMRAN-SABLE_AXIS_918010044221.csv` | Imran Sable (`AXIS`) | Level-2 uploaded node with `ATM WDL` exit (`Details Only` leaf) |
| `CASE041_SAMIDA-DEVKAR_KOTAK_7711882201.csv` | Samida Devkar (`KOTAK`) | Low-risk control account (`< 50` score) |
| `CASE041_HAWALA-TRADERS_AXIS_990011223344.xlsx` | Hawala Traders (`AXIS`) | `.xlsx` ingestion + Start of **3-hop circular loop** (`₹4.40L`) |
| `CASE041_APEX-SHELL-PVT-LTD_INDUS_201009988776.csv` | Apex Shell Pvt Ltd (`INDUS`) | High-betweenness **Bridge / Conduit** + Hop 2 (`₹4.32L`) |
| `CASE041_ZENITH-CONSULTANTS_YES_001980077665.csv` | Zenith Consultants (`YES`) | Hop 3 completing loop back to Hawala Traders (`₹4.25L`, `96.6%` retained) + balance break trigger for Step 02 Anomaly Gate |
| `CASE041_RAMESH-KUMAR-MULE_PNB_440011998877.csv` | Ramesh Kumar Mule (`PNB`) | **Critical (`81.4 / 100`) Money Mule**: 52-day dormancy, 8 same-day structured victim credits (`₹48.5k–49.5k`), `< 45 min` pass-through to `CoinDCX` / `AEPS Cash` / `Hawala Traders`, zero lifestyle spend, 3 zero-balance drops |

---

## 4. Step 01 — Multi-Format Bulk Upload & Interactive PDF Draw Tool

1. Click **`01 Upload & Draw Tool`** in the top stepper.
2. Click the file picker under **`01. Multi-Format Bulk Statement Upload`** and select one or more `.pdf`, `.xlsx`, `.csv`, `.tsv`, or `.txt` files (**100+ files** can be processed concurrently).
   - **Recommended Bulk Naming Convention:**
     ```text
     <CaseID>_<AccountHolder>_<BankCode>_<AccountNumber>.<ext>
     Example: CASE099_RAJESH-TRADERS_HDFC_501009988771.pdf
     ```
     When files follow this naming format, CypherSOL populates the Bank Code, Account Number, and Holder Name even if the PDF header is partially unreadable.
3. **Optional Inputs & Runtime Processing Mode:**
   - **Runtime Processing Mode (`#upload-mode-cards`):** Choose **Follow Global Mode**, **Heuristics Only**, **Both (Hybrid)**, or **LLM Primary** for this specific upload batch.
   - **PDF Password:** Enter DOB/PAN if uploading encrypted bank statements (requires optional `pikepdf` accelerator).
   - **Draw Template Override:** Leave on `Auto-Detect (113 Bank Templates)` for standard PDFs/CSVs/XLSXs, or choose a custom template saved with the Draw Tool.
4. Click **`Ingest & Analyze Statements`**.
5. Verify ingestion in the **`Ingested Statements & SHA-256 Chain of Custody`** table on the right: check detected Bank & A/C, Transaction count, Anomalies count, and the immutable **SHA-256** digest calculated on the raw uploaded bytes. Use the **Download file** icon in the table or inside the Statement Provenance drawer to re-download the exact uploaded file.

### Persistent Case Files & Export Archive ("Files & exports" Tab)

Every file uploaded to CypherSOL and every report generated is permanently preserved in the case storage vault (`data/cases/{case_id}/`). Open the **`Files & exports`** sub-tab in Tab **`01 Upload & Ingest`** (or select **"View Stored Files & Exports…"** from the top Export dropdown) to access:

1. **Original Input Documents Datagrid:**
   - **Document Name & Size:** Shows the original filename and exact file size.
   - **SHA-256 Cryptographic Hash:** Computed at upload time for evidentiary integrity and Section 63 BSA / 65B compliance.
   - **Linked Statement:** Displays the detected bank statement or account ID.
   - **1-Click Download (`Download file`):** Instantly downloads the original raw file from the server with proper MIME type.
   - **`+ Upload Document` Button:** Allows investigators and CAs to attach supplementary case documentation (e.g., audit notes, FIR copies, KYC scans, or external JSON evidence) directly to the case vault.

2. **Generated Exports & Reports Datagrid:**
   - **Export History:** Every generated Excel workbook (`.xlsx`), Tally XML (`.xml`), Forensic Dossier (`.pdf`/`.html`), and ITR compliance report is automatically archived with a timestamped snapshot.
   - **Type Badge:** Visual badge distinguishing `audit_workbook`, `tally_xml`, `forensic_dossier_pdf`, `forensic_dossier_html`, and `itr_compliance`.
   - **Cryptographic Hash & Timestamp:** Complete provenance of when each export was produced.
   - **1-Click Download:** Re-download any previously generated report without re-running the export pipeline.

3. **Rotating Audit Log Storage:**
   - System and analysis logs are kept in `data/logs/cyphersol.log` with automatic 10MB x 5 backups rotation. System logs are kept purely in file storage to protect forensic privacy and prevent token leakage in the web UI.

### Using the Interactive PDF Draw Tool (When Auto-Detect Misaligns Columns)

1. Select a `.pdf` file in the file picker.
2. Click **`Open in PDF Draw Tool`**.
3. The SVG canvas (`595 x 842` PDF points) renders Page 1's positioned text tokens (`Date`, `Narration`, `Ref`, `Debit`, `Credit`, `Balance`) with vertical blue column guide lines and a green table bounding box.
4. Calibrate the extraction box:
   - **`Bank Code`** (e.g., `COSMOS`) and **`Template Name`** (e.g., `Cosmos Savings PDF`).
   - **`Splitters (% width)`**: 5 comma-separated percentages separating the 6 columns (default: `15, 51, 64, 75, 87`).
   - **`BBox [x0,y0,x1,y1]`**: Normalized `[left, top, right, bottom]` coordinates (e.g., `0.02, 0.12, 0.98, 0.96`) excluding bank headers/footers.
5. Click **`Save Draw Template`**. Your template appears immediately in the **Draw Template Override** dropdown; select it and click **`Ingest & Analyze Statements`**.

---

## 5. Step 02 — Automatic Anomaly Gate

Open Tab **`02 Anomaly Gate`** whenever the red badge on the tab shows `> 0`:

| Anomaly Flag | Detection Rule | Action Available |
| :--- | :--- | :--- |
| **`BALANCE_MISMATCH`** | $\|Balance_t - (Balance_{t-1} + Credit_t - Debit_t)\| > ₹0.05$ | Inspect row against original statement (possible omitted transaction, parsing offset, or tampered statement). |
| **`DUPLICATE_SUSPECT`** | Same account, date, direction, amount, and normalized narration appearing multiple times across statements | Click **`Ignore / Restore`** to toggle `is_duplicate_ignored`. Ignored duplicates stay visible in the audit log while excluded from balances, Tally, and flow sums. |
| **`DATE_OUT_OF_ORDER`** | Non-monotonic transaction date sequence within a statement | Verify whether statement was reversed or combined from multiple periods. |
| **`OCR_DERIVED`** | Extracted via OCR / Draw fallback rather than clean digital text | Cross-check key amounts visually. |

---

## 6. Step 03 — AI-Categorized Transactions & 52-Category Taxonomy

Open Tab **`03 52-Cat Transactions`** to inspect all normalized entries, search by keyword/UTR/counterparty (`#tx-search`), or filter by category (`#tx-cat-filter`).

### Automatic Extraction Performed Per Row
- **Payment Channel:** `UPI`, `NEFT`, `RTGS`, `IMPS`, `NACH`/`ECS`, `ATM`, `AEPS`, `POS`, `CHEQUE`, `INTEREST`, `CHARGES`, or `OTHER`.
- **Extracted Counterparty & VPA/IFSC:** Parses UPI handles (`name@okicici`), NEFT/RTGS IFSC codes (`UTIB0001122`), and beneficiary names.
- **Tally Voucher & Confidence:** Displays classification source (`USER_RULE`, `RULE_KB`, `ML_NB`, or `SUSPENSE`) and calibrated confidence (`0–100%`).

### Complete 52-Category Taxonomy Reference

| ID | Group | Category Name | Typical Dir | Tally Parent Group | ITR / Forensic Role |
| :---: | :--- | :--- | :---: | :--- | :--- |
| `1` | Income & Receipts | Salary & Wages Received | CR | Direct Incomes | Salary (`Sec 15-17`) |
| `2` | Income & Receipts | Business Sales & Trade Receipts | CR | Sales Accounts | Business Turnover (`PGBP`) |
| `3` | Income & Receipts | Professional & Consultancy Fees | CR | Direct Incomes | `Sec 44ADA` / `PGBP` |
| `4` | Income & Receipts | Interest Income (Savings / FD / Sweep) | CR | Indirect Incomes | `Sec 80TTA` / `80TTB` / `OS` |
| `5` | Income & Receipts | Dividend & Capital Gain Payouts | CR | Indirect Incomes | `Schedule OS / CG` |
| `6` | Income & Receipts | Rental & Lease Income | CR | Indirect Incomes | House Property (`Sec 22`) |
| `7` | Income & Receipts | Tax Refund (Income Tax / GST) | CR | Duties & Taxes | Refund Verification |
| `8` | Income & Receipts | Loan / Credit Line Disbursal Received | CR | Loans (Liability) | Non-Taxable Capital Receipt |
| `9` | Income & Receipts | Capital / Partner / Director Infusion | CR | Capital Account | `Sec 68` Source Verification |
| `10` | Income & Receipts | Commission, Brokerage & Incentive Income | CR | Indirect Incomes | `TDS Sec 194H` |
| `11` | Operating & Business | Vendor & Supplier Payments | DR | Sundry Creditors | `Sec 194C` / `PGBP` |
| `12` | Operating & Business | Inventory & Raw Material Purchase | DR | Purchase Accounts | `COGS` / `PGBP` |
| `13` | Operating & Business | Office / Commercial Rent & Maintenance | DR | Indirect Expenses | `TDS Sec 194I` Check |
| `14` | Operating & Business | Employee Payroll, Wages & Stipends | DR | Indirect Expenses | `Sec 192` Payroll |
| `15` | Operating & Business | Electricity, Water & Gas Utilities | DR | Indirect Expenses | Lifestyle Marker |
| `16` | Operating & Business | Telecom, Broadband & Mobile Bills | DR | Indirect Expenses | Lifestyle Marker |
| `17` | Operating & Business | Software, SaaS & Cloud Subscriptions | DR | Indirect Expenses | OpEx |
| `18` | Operating & Business | Legal, Audit & Professional Consultancy Paid | DR | Indirect Expenses | `TDS Sec 194J` Check |
| `19` | Operating & Business | Marketing, Digital Ads & Promotion | DR | Indirect Expenses | OpEx |
| `20` | Operating & Business | Freight, Logistics, Transport & Courier | DR | Direct Expenses | OpEx |
| `21` | Operating & Business | Printing, Stationery & Office Supplies | DR | Indirect Expenses | OpEx |
| `22` | Operating & Business | Repairs, AMC & Facility Maintenance | DR | Indirect Expenses | OpEx |
| `23` | Banking & Debt | Home Loan / Mortgage EMI | DR | Secured Loans | `Sec 24(b)` & `80C` / FOIR EMI |
| `24` | Banking & Debt | Vehicle / Auto Loan EMI | DR | Secured Loans | FOIR EMI |
| `25` | Banking & Debt | Personal / Business / Unsecured Loan EMI | DR | Unsecured Loans | FOIR EMI |
| `26` | Banking & Debt | Credit Card Bill Settlement | DR | Current Liabilities | `SFT-005` Card Check |
| `27` | Banking & Debt | Bank Charges, SMS & Min Balance Penalties | DR | Bank Charges | Bank Cost |
| `28` | Banking & Debt | Cheque / ECS / NACH Bounce & Return Charges | DR | Bank Charges | Loan Bounce Counter |
| `29` | Banking & Debt | OD / CC / Loan Interest & Processing Fees | DR | Bank Charges | Finance Cost |
| `30` | Banking & Debt | Forex, Remittance & LRS Charges | BOTH | Indirect Expenses | `FEMA / LRS TCS` |
| `31` | Investments & Wealth | Mutual Fund / SIP Investment | DR | Investments | `Sec 80C` (ELSS) |
| `32` | Investments & Wealth | Equity / Stock Broker Settlement (Demat) | BOTH | Investments | `Schedule CG / Trading` |
| `33` | Investments & Wealth | Fixed Deposit / RD Creation (Sweep Out) | DR | Deposits (Asset) | Contra Deposit |
| `34` | Investments & Wealth | Fixed Deposit / Investment Maturity Proceeds | CR | Deposits (Asset) | Non-Turnover Maturity |
| `35` | Investments & Wealth | Insurance Premium (Life / Health / Motor) | DR | Indirect Expenses | `Sec 80C` / `80D` |
| `36` | Investments & Wealth | Provident Fund / Retirement (EPF / PPF / NPS) | DR | Investments | `Sec 80C` / `80CCD` |
| `37` | Investments & Wealth | Crypto / Virtual Digital Asset (VDA) Exchange | BOTH | Investments | **Mule Crypto Leakage** + `Sec 115BBH` |
| `38` | Taxes & Statutory | Income Tax / Advance Tax / TDS Challan | DR | Duties & Taxes | Advance Tax / TDS |
| `39` | Taxes & Statutory | GST Payment (PMT-06 / Challan) | DR | Duties & Taxes | GST Compliance |
| `40` | Taxes & Statutory | Professional Tax / Municipal / Property Tax | DR | Duties & Taxes | Statutory Tax |
| `41` | Taxes & Statutory | Customs Duty & Import/Export Levies | DR | Duties & Taxes | Trade Levy |
| `42` | Taxes & Statutory | Government Fees, Stamp Duty & Penalties | DR | Indirect Expenses | Fee / Penalty |
| `43` | Personal & Lifestyle | Food Delivery, Dining & Restaurants | DR | Drawings / Personal | Lifestyle Marker |
| `44` | Personal & Lifestyle | Groceries, Kirana & Supermarkets | DR | Drawings / Personal | Lifestyle Marker |
| `45` | Personal & Lifestyle | Fuel, EV Charging, Fastag & Local Commute | DR | Drawings / Personal | Lifestyle Marker |
| `46` | Personal & Lifestyle | Travel, Airlines, Railways & Hotels | DR | Drawings / Personal | Lifestyle Marker |
| `47` | Personal & Lifestyle | Healthcare, Hospitals & Pharmacy | DR | Drawings / Personal | Lifestyle Marker + `Sec 80D` |
| `48` | Personal & Lifestyle | E-Commerce, Apparel & Retail Shopping | DR | Drawings / Personal | Lifestyle Marker |
| `49` | Cash, Transfers & Suspense | Self / Inter-Account Contra Transfer | BOTH | Bank Accounts | Excluded from PGBP Turnover |
| `50` | Cash, Transfers & Suspense | Cash Deposit (Branch / CDM) | CR | Cash-in-Hand | `SFT-003` & `Sec 269ST` |
| `51` | Cash, Transfers & Suspense | Cash Withdrawal (ATM / AEPS / Bearer Chq) | DR | Cash-in-Hand | **Mule Cash Leakage** + `Sec 40A(3)` |
| `52` | Cash, Transfers & Suspense | Suspense / Uncategorized | BOTH | Suspense A/c | Routed to Tab `04A` Workbench |

---

## 7. Step 04 — Suspense Reduction Workbench & Entity Resolution (`Merge Similar`)

### 04A. Resolving Suspense Entries (`Suspense & Entities` Tab — Left Card)

1. Any transaction scoring below the confidence gate (`0.65` default) lands in the **Suspense Workbench** (`Category 52`).
2. Each card shows the date, debit/credit amount, narration, counterparty, and the classifier's **Top-3 AI Suggestions** with probability percentages.
3. Click any suggested pill **or** choose a category from the full dropdown and click **`Resolve`** (optionally add a custom Tally Ledger Name).
4. Keep **`Auto-refresh similar counterparties across workspace & save Auto-Learn rule`** checked: CypherSOL immediately updates every matching transaction across the case **and** permanently saves the rule into your local SQLite vault (`user_rules` table) for future uploads.

### 04B. Consolidating Aliases with `Merge Similar` (Right Card)

Bank statements frequently record minor spelling variations of the same person or company (e.g., `POOJAN VIG`, `M/S POOJAN VIG`, `S POOJAN VIG`, and `Pujan Vij`).

1. Adjust the **Similarity Threshold** slider (`0.50` = looser fuzzy/phonetic matching, `1.00` = strict exact matching, default **`0.85`**).
2. Click **`Re-Cluster Entities`** to regroup entities across all statements, Layering trees, and Network graphs simultaneously.
3. **Manual Override:** Type an exact alias in `Alias name to merge...` and the target entity name in `Canonical target name...`, then click **`Merge`**.

---

## 8. Step 05A — CypherEdge Guide (`Tally XML`, `ITR Compliance`, `Loan Eligibility`)

### 1. One-Click Tally Export & HTTP Push

- **Automatic Ledger & Voucher Mapping:** Credits map to `Receipt` (`Contra` for Categories `49`/`50`), Debits map to `Payment` (`Contra` for Categories `33`/`49`/`51`), and **Category `52` (`Suspense`) items are automatically excluded** until resolved in Step `04A`.
- **Method 1 — File Import:** Click **`Download Tally XML`** (`GET /api/cases/{id}/export/tally-xml`), then inside Tally go to **`Gateway of Tally` $\to$ `Import` $\to$ `Transactions / Masters`** and select the downloaded `.xml` file.
- **Method 2 — Direct Push (`localhost:9000`):** Ensure Tally XML Server is active on port `9000`, enter host/port, and click **`Push to Tally (HTTP :9000)`**.

### 2. ITR-Ready Statutory Compliance & Income Summary

Generates an Indian Financial-Year (`April 1 – March 31`) breakdown showing:
- **Turnover Reconciliation:** Gross Credits minus Contra Transfers (`Cat 49`) & Cash Deposits (`Cat 50`) vs Non-Taxable Loans/Capital (`Cat 8`, `9`, `34`) $\to$ **Net Business & Professional Receipts (PGBP)**.
- **Income Heads & Deductions:** Salary (`Sec 15`), PGBP, Rent, Interest (`Sec 80TTA/80TTB`), Dividends, `80C` candidates (`ELSS / LIC / PF`), Home Loan EMI (`Sec 24(b)/80C`), and Advance Tax / GST paid.
- **Automated Statutory Red Flags:**
  - **`Section 269ST` (`CRITICAL`):** Single-day cash deposits $\ge ₹2,00,000$.
  - **`SFT-003 / Rule 114E` (`HIGH`):** Aggregate cash deposits $> ₹10,00,000$.
  - **`Section 115BBH / 194S` (`HIGH`):** Virtual Digital Asset (Crypto) activity.
  - **`Section 40A(3)` (`MEDIUM`):** Daily cash withdrawals $> ₹10,000$.
  - **`Section 194I` (`MEDIUM`):** Annual commercial rent $> ₹2,40,000$.
  - **`Section 194J` (`MEDIUM`):** Annual professional/legal fees $> ₹30,000$.
  - **`SFT-005` (`MEDIUM`):** Credit card payments $\ge ₹10,00,000$.

### 3. Loan Eligibility & Credit Appraisal (`DSA / NBFC`)

Adjust **`FOIR %`** (default `55%`), **`Rate %`** (default `11.5%`), and **`Tenure (mo)`** (default `60`) and click **`Recalculate`**:

- **Daily ABB:** Sum of closing end-of-day balances across every calendar day in the statement period divided by total days.
- **Fixed-Date ABB:** Mean balance sampled on banking surrogate dates (**5th, 10th, 15th, 20th, 25th, and last day of each month**).
- **Assessable Monthly Income:** Uses recurring Salary (`Cat 1`) if salaried, otherwise `max(20% of Monthly Net Business Inflow, 50% of Fixed-Date ABB)`.
- **Max Eligible EMI & Loan:**
  $$\text{Max Eligible EMI} = \max\left(0,\; \text{Assessable Monthly Income} \times \frac{\text{FOIR}}{100} - \text{Existing Monthly EMI}\right)$$
  Standard reducing-balance annuity formula computes `Max Eligible Loan` from `Max Eligible EMI`, `Rate %`, and `Tenure`.
- **Credit Grade:**
  - **`PRIME (A)`:** `0` inward bounces, Fixed-Date ABB $\ge ₹25,000$, and `Max Eligible Loan > 0`.
  - **`STANDARD (B)`:** `<= 2` inward bounces and Fixed-Date ABB $\ge ₹10,000$.
  - **`HIGH RISK / REFER (C)`:** Higher bounce frequency or low ABB.

---

## 9. Step 05B — CypherTrace Forensic Guide

### 1. Layering Tree (Multi-Hop Money Flow)

1. Choose a **`Credit Source`** entity and **`Window (Days)`** (default `14` days; up to 4 hops deep), then click **`Trace Layering`**.
2. Read the color-coded flow cards:
   - **Blue Card (`Credit Source`):** Origin account and total seeded inflow.
   - **Teal Card (`✓ Has Statement`):** Uploaded bank account inside your case; shows subsequent outbound splits (`Click to drill deeper`).
   - **Grey Card (`Details Only`):** External counterparty without an uploaded statement yet.
3. Click **`Move to Misc`** on unrelated personal/utility nodes to group them into a **`Collapsed Misc`** box and unclutter the forensic diagram (`Restore` undoes this anytime). Click any entity name to jump straight to its Financial Profile.

### 2. Forensic Network Graph & Centrality

The **Network Graph** view displays two synchronized interactive SVG panels side-by-side:

1. **Full Multi-Account Fund Trail Topology (Left Panel):**
   - Renders the entire fund-flow graph computed via 70 iterations of deterministic Fruchterman-Reingold force layout.
   - **Color & Size Legend:**
     - **Red (`Primary Account Hub`):** Statement-backed accounts anchoring transaction clusters.
     - **Gold with Amber Border (`Bridge / Conduit Entity`):** Entities sitting in the middle of the network bridging $\ge 2$ hubs or high-betweenness shortest paths.
     - **Blue (`Cash / VDA Exit Endpoint`):** ATM, AEPS Cash, or Crypto Exchange exit nodes.
     - **Grey (`Peripheral Counterparty`):** Single-hub external leaf parties.
2. **Central Conduit & Bridge Sub-Network Focus (Right Panel):**
   - Automatically isolates only the **`Bridge / Conduit`** nodes and their directly connected hubs/exits, zoomed and centered with `₹` amount labels on every transfer edge.
3. **Interactive Navigation:**
   - **Mouse Wheel:** Smooth cursor-centered zoom (`0.35x` to `2.4x`) on either panel.
   - **Click & Drag Background:** Pan anywhere across dense networks.
   - **Click Any Node Circle:** Jumps directly to Tab `06 Individual Financial Profile` for that entity.
   - **Filters & Toggles:** Filter by **`Min ₹`**, **`Channel`** (`UPI`, `NEFT`, `RTGS`, `IMPS`, `ATM`, `AEPS`), **`Keyword`**, check **`Conduits & Bridges Only`** to isolate central intermediaries in the left panel too, or click **`Reset View`**.
4. **Centrality Table:** Displays every entity sorted by volume alongside **In/Out Degree**, **Brandes Betweenness Centrality** (highlights bottlenecks/bridges), and **Weighted PageRank**.

### 3. Mule Account Detection — 10-Parameter Behavioral Scoring Engine

Scores every uploaded bank account from `0` to `100` (`Critical: 80–100`, `Suspicious: 50–79`, `Low Risk: 0–49`) across 10 behavioral signals:

| # | Parameter | Weight | Behavioral Signal Measured |
| :---: | :--- | :---: | :--- |
| **1** | Flow-Through Ratio | **25%** | $1 - \|\sum Cr - \sum Dr\| / \max(\sum Cr, \sum Dr)$ gated by ₹50k turnover (measures pass-through accounts retaining near-zero balance). |
| **2** | Velocity / Hold Time | **20%** | Exponential decay $\exp(-\text{Median FIFO Hold Hours} / 12)$; funds drained within minutes score near `100%`. |
| **3** | Lifestyle Spending Absence | **15%** | Inverse frequency of groceries, utilities, dining, fuel, healthcare, and shopping txns per active month. |
| **4** | Counterparty Concentration (HHI) | **10%** | Herfindahl-Hirschman Index $\sum (V_i / V_{\text{total}})^2$; detects handler concentration. |
| **5** | Fan-In / Fan-Out Pattern | **10%** | Sliding 24-hour window ratio of distinct senders vs distinct receivers (detects 1-to-many dispersion or many-to-1 victim collection). |
| **6** | Cash / Crypto / Wallet Leakage | **5%** | Share of debits exiting through `ATM`, `AEPS`, `Cat 51`, `Cat 37` Crypto/VDA, or digital wallets. |
| **7** | Structuring / Smurfing | **5%** | Frequency of amounts clustered just below Indian CTR/PAN thresholds (`₹45k–49,999`, `₹1.80L–1.999L`, `₹9.0L–9.999L`). |
| **8** | Zero Balance Oscillation | **5%** | Count of `> ₹10k` inflows followed by a drain down to `<= max(₹500, 1% of peak credit)`. |
| **9** | Burst / Dormancy Pattern | **3%** | Dormancy gap `>= 30` days followed by a sudden volume spike. |
| **10** | Round Number Anomalies | **2%** | Proportion of electronic transfers (`UPI/NEFT/RTGS/IMPS`) that are exact multiples of `₹5,000`. |

### 4. Circular Loop Detection ($A \to B \to C \to A$)

- Set **`Max Days`** (default `30`) and **`Min Retention %`** (default `50%`) and click **`Scan Loops`**.
- Discovers chronological cycles (`2` to `5` hops) where money leaves Entity $A$ and returns to Entity $A$ while retaining at least the configured fraction of the starting transfer.
- Displays a circular arrow SVG per loop plus hop-by-hop timestamps, amounts, channels, duration in hours, and retention percentage.

### 5. Bidirectional Flow Analysis ($A \rightleftarrows B$)

- Choose any pair of entities and click **`Analyze Pair`**.
- Displays total $A \to B$, total $B \to A$, net flow, median turnaround hours, count of rapid round-trips ($\le 48\text{h}$), and a chronological ledger showing running **Cumulative Net Balance**.

---

## 10. Step 06 — Individual Financial Profiles, Audit Trail & Courtroom Exports

Open Tab **`06 Financial Profiles & Audit`** (or click any node in CypherTrace) to examine:
- **Per-Entity Profile:** Total Credits, Debits, Net Flow, Daily ABB, Mule Score/Tier, Month-by-Month cash flow, and Top Senders vs. Beneficiaries.
- **Immutable Operator Audit Log:** Chronological trail of every ingestion batch, Suspense Auto-Learn rule update, and entity merge.

### Export Deliverables Reference (Top Header Buttons)

| Button | Output Format | Contents Included |
| :--- | :--- | :--- |
| **`Excel (.xlsx)`** | Styled Multi-Sheet Workbook (`*_audit_report.xlsx`) | 6 worksheets: `Executive Summary`, `Transactions` (with Category ID, Tally Voucher, Anomaly Flags), `Category Schedule`, `Mule Risk Scorecard`, `Circular Loops`, and `Chain of Custody`. |
| **`Tally (.xml)`** | Tally Prime / ERP 9 XML (`*_tally_import.xml`) | Auto-created Master Ledgers + balanced `Receipt`, `Payment`, and `Contra` vouchers. |
| **`Dossier (.pdf)`** | Native PDF 1.4 Report (`*_forensic_dossier.pdf`) | Tabular executive report covering Mule Risk assessments, Circular Loops, ITR Statutory Flags, and SHA-256 Chain of Custody. |
| **`Sec 63 / Dossier`** | Opens printable HTML Dossier in new tab (`Ctrl/Cmd+P` ready) | Case KPIs, Mule Scorecard, Circular Loops, inline **Multi-Account Fund Trail Network Topology SVG**, ITR Red Flags, SHA-256 file manifest, and **Section 63 BSA / Section 65B Evidence Certificate**. |
