#!/usr/bin/env bash
# ==============================================================================
# Google Cloud Run Deployment Script for CypherSOL
# Builds image with Cloud Build and deploys to Cloud Run with persistent storage
# ==============================================================================
set -euo pipefail

PROJECT_ID="${PROJECT_ID:-$(gcloud config get-value project 2>/dev/null || echo '')}"
REGION="${REGION:-asia-south1}" # Default: Mumbai (or us-central1 / europe-west1)
SERVICE_NAME="${SERVICE_NAME:-cyphersol}"
IMAGE_TAG="gcr.io/${PROJECT_ID}/${SERVICE_NAME}:latest"
BUCKET_NAME="${BUCKET_NAME:-${PROJECT_ID}-cyphersol-data}"

if [ -z "${PROJECT_ID}" ]; then
  echo "Error: PROJECT_ID is not set. Run 'gcloud config set project <your-project-id>' first."
  exit 1
fi

echo "==> Configuring Google Cloud Project: ${PROJECT_ID} (${REGION})"

# Enable required Google Cloud APIs
gcloud services enable \
  run.googleapis.com \
  cloudbuild.googleapis.com \
  containerregistry.googleapis.com \
  storage.googleapis.com

# Create persistent Cloud Storage bucket if it doesn't already exist
if ! gsutil ls -b "gs://${BUCKET_NAME}" >/dev/null 2>&1; then
  echo "==> Creating Cloud Storage Bucket for persistent case data: gs://${BUCKET_NAME}"
  gsutil mb -p "${PROJECT_ID}" -l "${REGION}" -b on "gs://${BUCKET_NAME}"
fi

# Build and push container using Google Cloud Build
echo "==> Building and pushing container image: ${IMAGE_TAG}"
gcloud builds submit --tag "${IMAGE_TAG}" .

# Deploy container to Cloud Run with GCS volume mount for /data
echo "==> Deploying service '${SERVICE_NAME}' to Cloud Run..."
gcloud run deploy "${SERVICE_NAME}" \
  --image "${IMAGE_TAG}" \
  --region "${REGION}" \
  --platform managed \
  --execution-environment gen2 \
  --memory 2Gi \
  --cpu 2 \
  --port 8080 \
  --min-instances 0 \
  --max-instances 1 \
  --set-env-vars "HOST=0.0.0.0,PORT=8080,CYPHERSOL_DATA_DIR=/data,CYPHERSOL_ALLOWED_HOSTS=*,CYPHERSOL_NO_BROWSER=1" \
  --add-volume "name=cyphersol-storage,type=cloud-storage,bucket=${BUCKET_NAME}" \
  --add-volume-mount "volume=cyphersol-storage,mount-path=/data" \
  --allow-unauthenticated

URL=$(gcloud run services describe "${SERVICE_NAME}" --region "${REGION}" --format="value(status.url)")
echo "=================================================================="
echo "CypherSOL successfully deployed to Google Cloud Run!"
echo "Service URL: ${URL}"
echo "Persistent storage bucket: gs://${BUCKET_NAME} mounted at /data"
echo "=================================================================="
