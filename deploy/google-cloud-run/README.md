# Google Cloud Run Deployment

Deploy CypherSOL as a scalable, serverless container on Google Cloud Run with persistent storage mounted via Google Cloud Storage (GCS) or Cloud Filestore (NFS).

## Prerequisites
- [Google Cloud SDK (`gcloud`)](https://cloud.google.com/sdk/docs/install) installed and authenticated.
- A GCP project with billing enabled.

## 1-Command Deployment
Run the automated deployment script from repository root:
```bash
./deploy/google-cloud-run/deploy.sh
```

## Manual Steps
1. **Set Project & Region**:
   ```bash
   gcloud config set project YOUR_PROJECT_ID
   export REGION="asia-south1" # or us-central1
   ```

2. **Create Storage Bucket for Case Data & Vault**:
   ```bash
   gsutil mb -p YOUR_PROJECT_ID -l $REGION -b on gs://YOUR_PROJECT_ID-cyphersol-data
   ```

3. **Build & Push Container**:
   ```bash
   gcloud builds submit --tag gcr.io/YOUR_PROJECT_ID/cyphersol:latest .
   ```

4. **Deploy to Cloud Run**:
   ```bash
   gcloud run deploy cyphersol \
     --image gcr.io/YOUR_PROJECT_ID/cyphersol:latest \
     --region $REGION \
     --platform managed \
     --execution-environment gen2 \
     --memory 2Gi \
     --cpu 2 \
     --port 8080 \
     --set-env-vars "HOST=0.0.0.0,PORT=8080,CYPHERSOL_DATA_DIR=/data,CYPHERSOL_ALLOWED_HOSTS=*,CYPHERSOL_NO_BROWSER=1" \
     --add-volume "name=cyphersol-storage,type=cloud-storage,bucket=YOUR_PROJECT_ID-cyphersol-data" \
     --add-volume-mount "volume=cyphersol-storage,mount-path=/data" \
     --allow-unauthenticated
   ```
