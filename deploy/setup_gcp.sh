#!/bin/bash
# =============================================================================
# GCP Infrastructure Setup for Leasing AI Auditor
# Run this once from Cloud Shell to provision all required GCP resources
# =============================================================================

set -e  # Exit on any error

# --- Config — set these before running ---
PROJECT_ID=$(gcloud config get-value project)
REGION="us-central1"
DB_INSTANCE_NAME="leasing-auditor-db"
DB_NAME="leasing_auditor"
DB_USER="auditor"
DB_PASSWORD=$(openssl rand -base64 24)  # Auto-generated secure password
SERVICE_ACCOUNT="leasing-auditor-sa"

echo "============================================"
echo "  Leasing AI Auditor — GCP Setup"
echo "  Project: $PROJECT_ID"
echo "  Region:  $REGION"
echo "============================================"

# --- Enable required APIs ---
echo ""
echo ">> Enabling GCP APIs..."
gcloud services enable \
    run.googleapis.com \
    cloudbuild.googleapis.com \
    cloudscheduler.googleapis.com \
    sqladmin.googleapis.com \
    secretmanager.googleapis.com \
    containerregistry.googleapis.com \
    aiplatform.googleapis.com \
    gmail.googleapis.com \
    --project=$PROJECT_ID

echo "   APIs enabled."

# --- Service Account ---
echo ""
echo ">> Creating service account..."
gcloud iam service-accounts create $SERVICE_ACCOUNT \
    --display-name="Leasing AI Auditor" \
    --project=$PROJECT_ID 2>/dev/null || echo "   Service account already exists."

SA_EMAIL="$SERVICE_ACCOUNT@$PROJECT_ID.iam.gserviceaccount.com"

# Grant required roles
for ROLE in \
    "roles/aiplatform.user" \
    "roles/cloudsql.client" \
    "roles/secretmanager.secretAccessor" \
    "roles/run.invoker" \
    "roles/storage.objectViewer"
do
    gcloud projects add-iam-policy-binding $PROJECT_ID \
        --member="serviceAccount:$SA_EMAIL" \
        --role="$ROLE" \
        --quiet
done
echo "   Service account configured: $SA_EMAIL"

# --- Cloud SQL ---
echo ""
echo ">> Creating Cloud SQL instance (this takes ~5 minutes)..."
gcloud sql instances create $DB_INSTANCE_NAME \
    --database-version=POSTGRES_15 \
    --tier=db-f1-micro \
    --region=$REGION \
    --storage-auto-increase \
    --backup-start-time=03:00 \
    --project=$PROJECT_ID 2>/dev/null || echo "   SQL instance already exists."

echo "   Creating database and user..."
gcloud sql databases create $DB_NAME \
    --instance=$DB_INSTANCE_NAME \
    --project=$PROJECT_ID 2>/dev/null || echo "   Database already exists."

gcloud sql users create $DB_USER \
    --instance=$DB_INSTANCE_NAME \
    --password=$DB_PASSWORD \
    --project=$PROJECT_ID 2>/dev/null || echo "   User already exists."

DB_INSTANCE_CONNECTION="$PROJECT_ID:$REGION:$DB_INSTANCE_NAME"
echo "   Cloud SQL ready: $DB_INSTANCE_CONNECTION"

# --- Secret Manager ---
echo ""
echo ">> Storing secrets in Secret Manager..."

store_secret() {
    local NAME=$1
    local VALUE=$2
    echo -n "$VALUE" | gcloud secrets create $NAME \
        --data-file=- \
        --project=$PROJECT_ID 2>/dev/null || \
    echo -n "$VALUE" | gcloud secrets versions add $NAME \
        --data-file=- \
        --project=$PROJECT_ID
    echo "   Secret stored: $NAME"
}

store_secret "gcp-project-id"                  "$PROJECT_ID"
store_secret "db-instance-connection-name"     "$DB_INSTANCE_CONNECTION"
store_secret "db-user"                         "$DB_USER"
store_secret "db-password"                     "$DB_PASSWORD"

# --- Container Registry ---
echo ""
echo ">> Configuring Container Registry..."
gcloud auth configure-docker --quiet

# --- Cloud Storage (for report output) ---
echo ""
echo ">> Creating Cloud Storage bucket for reports..."
gsutil mb -p $PROJECT_ID -l $REGION \
    gs://$PROJECT_ID-auditor-reports 2>/dev/null || \
    echo "   Bucket already exists."

# Grant service account access
gsutil iam ch \
    serviceAccount:$SA_EMAIL:objectAdmin \
    gs://$PROJECT_ID-auditor-reports

echo "   Reports bucket: gs://$PROJECT_ID-auditor-reports"

# --- Print Summary ---
echo ""
echo "============================================"
echo "  SETUP COMPLETE"
echo "============================================"
echo ""
echo "  Save these values — you'll need them:"
echo ""
echo "  PROJECT_ID     : $PROJECT_ID"
echo "  SA_EMAIL       : $SA_EMAIL"
echo "  DB_INSTANCE    : $DB_INSTANCE_CONNECTION"
echo "  DB_PASSWORD    : $DB_PASSWORD"
echo "  REPORTS_BUCKET : gs://$PROJECT_ID-auditor-reports"
echo ""
echo "  Next steps:"
echo "  1. Run deploy/build_and_push.sh to build the container"
echo "  2. Run python3 -m agent.pipeline init-db to create tables"
echo "  3. Set up Gmail OAuth credentials for persona inboxes"
echo "============================================"
