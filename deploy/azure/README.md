# Azure Container Apps Deployment

Deploy CypherSOL as an auto-managed container application on Azure Container Apps with persistent Azure Files storage.

## Features
- Scalable serverless micro-billing.
- TLS certificates automatically provisioned for your custom or `.azurecontainerapps.io` domain.
- Persistent Azure Files SMB share mounted directly to `/data` (persisting SQLite vault, raw statement documents, generated reports, and rotating logs).

## Prerequisites
- [Azure CLI (`az`)](https://learn.microsoft.com/en-us/cli/azure/install-azure-cli) installed and logged in (`az login`).
- Azure Subscription.

## Quick Automated Deploy
Run the automated deployment script from repository root:
```bash
./deploy/azure/deploy-container-app.sh
```

## Environment Variables
- `HOST=0.0.0.0`
- `PORT=8080`
- `CYPHERSOL_DATA_DIR=/data`
- `CYPHERSOL_ALLOWED_HOSTS=*`
- `CYPHERSOL_NO_BROWSER=1`
