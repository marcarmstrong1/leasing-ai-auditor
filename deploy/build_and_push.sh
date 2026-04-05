#!/bin/bash
# Builds the Docker image and pushes to GCR
# Run from the project root

set -e

PROJECT_ID=$(gcloud config get-value project)
IMAGE="gcr.io/$PROJECT_ID/leasing-ai-auditor"

echo ">> Building image: $IMAGE"
docker build -t $IMAGE:latest .

echo ">> Pushing to Container Registry..."
docker push $IMAGE:latest

echo ">> Image pushed: $IMAGE:latest"
echo ""
echo "To create the Cloud Run job:"
echo "  gcloud run jobs create leasing-ai-auditor \\"
echo "    --image=$IMAGE:latest \\"
echo "    --region=us-central1 \\"
echo "    --service-account=leasing-auditor-sa@$PROJECT_ID.iam.gserviceaccount.com \\"
echo "    --memory=4Gi \\"
echo "    --cpu=2 \\"
echo "    --max-retries=1 \\"
echo "    --task-timeout=7200"
