#!/bin/bash
# Creates Cloud Scheduler jobs for automated engagement runs
# Customize the property IDs and schedule before running

set -e

PROJECT_ID=$(gcloud config get-value project)
REGION="us-central1"
SA_EMAIL="leasing-auditor-sa@$PROJECT_ID.iam.gserviceaccount.com"

echo ">> Creating Cloud Scheduler jobs..."

# Example: Run Maya persona against a property every Tuesday at 10am CT
gcloud scheduler jobs create http audit-tuesday-maya \
    --location=$REGION \
    --schedule="0 10 * * 2" \
    --time-zone="America/Chicago" \
    --uri="https://$REGION-run.googleapis.com/apis/run.googleapis.com/v1/namespaces/$PROJECT_ID/jobs/leasing-ai-auditor:run" \
    --message-body='{"overrides":{"containerOverrides":[{"args":["run","--property-id","PROPERTY_ID_HERE","--persona","maya","--skip-monitor"]}]}}' \
    --oauth-service-account-email=$SA_EMAIL \
    --project=$PROJECT_ID 2>/dev/null || echo "   Job already exists."

echo "   Scheduler jobs created."
echo ""
echo "   Update PROPERTY_ID_HERE with real property IDs before activating."
