# CypherSOL Deployment & Storage Architecture Guide

CypherSOL is designed as an **offline-first, cloud-ready financial statement analyzer and forensic fund trail intelligence platform**. It can run locally without any external services or be deployed as a containerized web service on **Docker**, **Render**, **Google Cloud Run**, **AWS**, or **Azure**.

---

## 1. Storage Architecture: Inputs, Outputs & Logs

All persistent state is stored under `CYPHERSOL_DATA_DIR` (default: `data/` or `/data` in containers).

```
/data/
├── cyphersol_vault.db          # Encrypted or standard SQLite database (cases, txns, entities, rules)
├── cyphersol_vault.db-wal      # SQLite write-ahead log
├── cyphersol_vault.db-shm      # Shared memory index
├── cases/
│   └── {case_id}/
│       ├── inputs/             # Permanently preserved raw statement documents
│       │   ├── {sha256[:12]}_statement_2024.pdf
│       │   ├── {sha256[:12]}_ledger_import.xlsx
│       │   └── {sha256[:12]}_evidence.json
│       └── outputs/            # Historical generated reports & forensic dossiers
│           ├── 20261117_143000_audit_workbook_CASE_001_audit_report.xlsx
│           ├── 20261117_143210_forensic_dossier_pdf_CASE_001_dossier.pdf
│           └── 20261117_143500_tally_xml_CASE_001_tally_import.xml
└── logs/
    ├── cyphersol.log           # Active rotating application log (max 10MB per file)
    ├── cyphersol.log.1
    └── ...                     # Up to 5 backup log rotations
```

### Key Properties:
- **Input Preservation & Chain of Custody**: Every uploaded file (PDF, Excel, CSV, TXT, JSON) is SHA-256 hashed and stored on disk. Both the hash and original bytes remain permanently retrievable via the frontend UI and REST API.
- **Export Retention**: Every time an analyst exports an Excel audit workbook, Forensic Dossier (PDF/HTML), Tally XML, or ITR report, a timestamped snapshot is archived to disk and registered in the database. Analysts can view the full export history and re-download previously generated files anytime.
- **Rotating File Logs**: Application and server logs are automatically managed by Python's `RotatingFileHandler` (10MB limit with 5 backups). Logs remain safely in the persistent volume without overflowing disk storage and without exposing system logs in the UI.

---

## 2. Generic Deployment Strategy

A single production `Dockerfile` packages all required system utilities and Python libraries:
- **System Utilities**: `qpdf` (AES-256 PDF decryption), `poppler-utils` (`pdftoppm`), `tesseract-ocr` (OCR engine), `libmagic1`.
- **Python Libraries**: `PyMuPDF`, `pdfplumber`, `pikepdf`, `openpyxl`, `xlrd`, `polars`, `duckdb`, `rapidfuzz`, `jellyfish`, `networkx`, `lightgbm`, `scikit-learn`, `fastapi`, `uvicorn`, `pydantic`.
- **Non-root user**: Container runs as unprivileged user `cyphersol` (UID 10001).
- **Persistent mount**: Mounts `/data` volume.
- **Health check**: `GET /api/health` validates container availability.

---

## 3. Deployment Options

### Option A: Local & VM (Docker & Docker Compose)
The simplest way to run on a developer laptop or any Linux virtual machine (Ubuntu, Debian, RHEL, EC2, Compute Engine).

```bash
# 1. Start the service
docker compose up -d

# 2. Open in browser
http://localhost:8080

# 3. View logs
docker compose logs -f
```

---

### Option B: Render (One-Click Blueprint)
Render provides turnkey container hosting with automatic SSL, custom domains, and attached persistent SSD storage.

1. Push your repository to GitHub.
2. In the Render Dashboard, click **New +** -> **Blueprint**.
3. Select your repository. Render reads `render.yaml` automatically:
   - Sets up Docker web service with persistent 10GB disk mounted at `/data`.
   - Binds to internal port `10000` with public HTTPS reverse proxy.
   - Sets `CYPHERSOL_ALLOWED_HOSTS=*` and `CYPHERSOL_NO_BROWSER=1`.
4. Click **Apply**. Your app is live at `https://<your-service>.onrender.com`.

---

### Option C: Google Cloud (Cloud Run)
Cloud Run provides scalable serverless containers with Google Cloud Storage (GCS) volume integration.

Run the bundled deploy script:
```bash
./deploy/google-cloud-run/deploy.sh
```

**What it does:**
1. Creates a Cloud Storage bucket `gs://<project-id>-cyphersol-data`.
2. Builds the container image via Google Cloud Build.
3. Deploys to Cloud Run with Gen2 execution environment, mounting the GCS bucket directly to `/data`.
4. Returns the public HTTPS Cloud Run URL.

---

### Option D: AWS (ECS Fargate or App Runner)

#### Option D1: Amazon ECS Fargate with Amazon EFS (Recommended for Multi-AZ)
1. Push the Docker image to Amazon Elastic Container Registry (ECR).
2. Create an Amazon EFS file system for `/data`.
3. Register the task definition in [deploy/aws/ecs-task-definition.json](file:///Users/manu/Library/CloudStorage/OneDrive-Personal/AML/deploy/aws/ecs-task-definition.json).
4. Launch an ECS Fargate service behind an Application Load Balancer (ALB).

#### Option D2: AWS App Runner (Fastest)
1. In AWS App Runner console, create a new service from your ECR image.
2. Set Port to `8080` and CPU/Memory to `1 vCPU / 2 GB`.
3. Add environment variables: `HOST=0.0.0.0`, `PORT=8080`, `CYPHERSOL_DATA_DIR=/data`, `CYPHERSOL_ALLOWED_HOSTS=*`.

---

### Option E: Azure (Azure Container Apps)
Azure Container Apps provides fully managed serverless containers with persistent Azure Files storage.

Run the bundled deployment script:
```bash
./deploy/azure/deploy-container-app.sh
```

**What it does:**
1. Creates Azure Container Registry (ACR) and builds the container image.
2. Creates an Azure Storage Account and Azure File Share (`cyphersol-data`).
3. Connects Azure Files to the Container Apps Environment.
4. Deploys CypherSOL with `/data` mapped to the persistent Azure File Share.

---

## 4. Environment Variables Reference

| Variable | Default | Purpose |
| :--- | :--- | :--- |
| `HOST` | `127.0.0.1` | Network interface to bind (`0.0.0.0` for containers/cloud) |
| `PORT` | `8080` | HTTP port to listen on (Render uses `10000`, Cloud Run/AWS use `8080`) |
| `CYPHERSOL_DATA_DIR` | `data` | Directory where SQLite vault, case inputs, outputs, and logs are stored |
| `CYPHERSOL_DB_PATH` | `data/cyphersol_vault.db` | Explicit SQLite vault file path |
| `CYPHERSOL_DB_KEY` | *(None)* | SQLCipher database encryption passphrase |
| `CYPHERSOL_ALLOWED_HOSTS` | *(Loopback only)* | Comma-separated list or `*` for cloud domains (e.g. `*.onrender.com,*.run.app`) |
| `CYPHERSOL_NO_BROWSER` | `0` | Set `1` in headless container and cloud environments |
| `CYPHERSOL_LOG_LEVEL` | `INFO` | Logging level (`DEBUG`, `INFO`, `WARNING`, `ERROR`) |
| `GEMINI_API_KEY` | *(None)* | Optional Google Gemini API key for hybrid vision/LLM parsing |
| `ANTHROPIC_API_KEY` | *(None)* | Optional Anthropic API key |
| `OPENAI_API_KEY` | *(None)* | Optional OpenAI API key |

---

## 5. Backup & Disaster Recovery

Because all cases, inputs, outputs, and vault data reside within `CYPHERSOL_DATA_DIR` (`/data`), backing up your system requires only archiving this directory:

```bash
# Backup
tar -czvf cyphersol_backup_$(date +%Y%m%d).tar.gz /data

# Restore
tar -xzvf cyphersol_backup_YYYYMMDD.tar.gz -C /
```
