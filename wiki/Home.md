# Welcome to the CypherSOL Wiki

**CypherSOL** is an offline-first financial statement analyzer and forensic fund trail intelligence platform unifying:
- **CypherEdge**: Automated bank statement processing for CAs, tax professionals, MSMEs, and loan DSAs (Smart Tally Prime/ERP 9 XML exports, Section 269ST/40A(3)/194I/194J ITR compliance, and loan ABB/FOIR underwriting).
- **CypherTrace**: Forensic fund trail intelligence suite for financial fraud investigators, cyber crime cells, and AML analysts (interactive layering detection trees, force-directed network graphs, circular loop discovery, bidirectional flow analysis, and 10-parameter behavioral mule scoring).

---

## Documentation Quick Links

| Guide | Description |
| :--- | :--- |
| **[User Guide](User-Guide)** | End-to-end user manual covering launching, bulk statement upload, interactive PDF Draw Tool, Anomaly Gate, 52-category taxonomy, Suspense Workbench, entity resolution, Tally XML generation, forensic fund trails, and courtroom exports. |
| **[Deployment Guide](Deployment-Guide)** | Production deployment architecture for **Docker**, **Render**, **Google Cloud Run**, **AWS (ECS / App Runner)**, and **Azure (Container Apps)**, including persistent storage mounts and rotating logs. |
| **[Developer & Architecture Guide](Developer-and-Architecture-Guide)** | Software engineering reference covering package topology, SQLite WAL persistence, sequence diagrams, subsystem specifications, and maintenance playbooks. |
| **[Data & Variable Dictionary](Data-and-Variable-Dictionary)** | Comprehensive technical dictionary covering all 5 Enums, 16 domain dataclasses, SQLite schema, regexes, thresholds, and REST API schemas. |

---

## Core Guarantees

1. **100% Offline Capability**: Runs locally on Python standard library primitives with zero internet connectivity required.
2. **Bring-Your-Own-Key (BYOK) AI**: Optional Stages 1–4 AI Backup Agents (Google Gemini, Anthropic Claude, OpenAI with Vision) stored strictly locally in the encrypted/protected vault.
3. **Chain of Custody & Section 63 BSA / 65B Compliance**: Every uploaded statement and evidence document is SHA-256 hashed at ingestion, with raw input preservation and timestamped export history.
