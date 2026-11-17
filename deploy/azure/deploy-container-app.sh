#!/usr/bin/env bash
# ==============================================================================
# Azure Container Apps Deployment Script for CypherSOL
# Builds image to Azure Container Registry (ACR) and mounts Azure Files share
# ==============================================================================
set -euo pipefail

RESOURCE_GROUP="${RESOURCE_GROUP:-rg-cyphersol}"
LOCATION="${LOCATION:-centralindia}" # or eastus / westeurope
ACR_NAME="${ACR_NAME:-acrcyphersol$RANDOM}"
ENVIRONMENT_NAME="${ENVIRONMENT_NAME:-env-cyphersol}"
APP_NAME="${APP_NAME:-cyphersol}"
STORAGE_ACCOUNT="${STORAGE_ACCOUNT:-stcyphersol$RANDOM}"
FILE_SHARE_NAME="cyphersol-data"

echo "==> Creating Azure Resource Group: ${RESOURCE_GROUP} (${LOCATION})"
az group create --name "${RESOURCE_GROUP}" --location "${LOCATION}"

echo "==> Creating Azure Container Registry: ${ACR_NAME}"
az acr create --resource-group "${RESOURCE_GROUP}" --name "${ACR_NAME}" --sku Basic --admin-enabled true

echo "==> Building container image in ACR..."
az acr build --registry "${ACR_NAME}" --image cyphersol:latest .

echo "==> Creating Storage Account and Azure File Share for /data..."
az storage account create --resource-group "${RESOURCE_GROUP}" --name "${STORAGE_ACCOUNT}" --location "${LOCATION}" --sku Standard_LRS
STORAGE_KEY=$(az storage account keys list --resource-group "${RESOURCE_GROUP}" --account-name "${STORAGE_ACCOUNT}" --query "[0].value" --output tsv)
az storage share create --name "${FILE_SHARE_NAME}" --account-name "${STORAGE_ACCOUNT}" --account-key "${STORAGE_KEY}"

echo "==> Creating Container Apps Environment..."
az containerapp env create --name "${ENVIRONMENT_NAME}" --resource-group "${RESOURCE_GROUP}" --location "${LOCATION}"

echo "==> Linking Azure Files storage to Container Apps Environment..."
az containerapp env storage set \
  --name "${ENVIRONMENT_NAME}" \
  --resource-group "${RESOURCE_GROUP}" \
  --storage-name "${FILE_SHARE_NAME}" \
  --azure-file-account-name "${STORAGE_ACCOUNT}" \
  --azure-file-account-key "${STORAGE_KEY}" \
  --azure-file-share-name "${FILE_SHARE_NAME}" \
  --access-mode ReadWrite

echo "==> Deploying CypherSOL Container App..."
ACR_SERVER=$(az acr show --name "${ACR_NAME}" --query "loginServer" --output tsv)
ACR_PASSWORD=$(az acr credential show --name "${ACR_NAME}" --query "passwords[0].value" --output tsv)

az containerapp create \
  --name "${APP_NAME}" \
  --resource-group "${RESOURCE_GROUP}" \
  --environment "${ENVIRONMENT_NAME}" \
  --image "${ACR_SERVER}/cyphersol:latest" \
  --registry-server "${ACR_SERVER}" \
  --registry-username "${ACR_NAME}" \
  --registry-password "${ACR_PASSWORD}" \
  --target-port 8080 \
  --ingress external \
  --cpu 1.0 \
  --memory 2.0Gi \
  --min-replicas 1 \
  --max-replicas 1 \
  --env-vars "HOST=0.0.0.0" "PORT=8080" "CYPHERSOL_DATA_DIR=/data" "CYPHERSOL_ALLOWED_HOSTS=*" "CYPHERSOL_NO_BROWSER=1" \
  --volume-name cyphersol-storage \
  --volume-mount-path /data \
  --storage-name "${FILE_SHARE_NAME}"

FQDN=$(az containerapp show --name "${APP_NAME}" --resource-group "${RESOURCE_GROUP}" --query "properties.configuration.ingress.fqdn" --output tsv)
echo "=================================================================="
echo "CypherSOL deployed successfully on Azure Container Apps!"
echo "Access URL: https://${FQDN}"
echo "Persistent storage share: ${FILE_SHARE_NAME} mounted to /data"
echo "=================================================================="
