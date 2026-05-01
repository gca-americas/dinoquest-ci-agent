#!/bin/bash
# Sets up Google Model Armor for DinoQuest prompt injection protection.
# Idempotent — safe to run multiple times.

set -e
PROJECT_ID="${1:-$(gcloud config get-value project 2>/dev/null)}"
REGION="us-central1"
TEMPLATE_ID="dinoquest-prompt-guard"
TEMPLATE_NAME="projects/${PROJECT_ID}/locations/${REGION}/templates/${TEMPLATE_ID}"

echo "Project: $PROJECT_ID"
echo "Template: $TEMPLATE_NAME"

# Enable the API
echo "Enabling Model Armor API..."
gcloud services enable modelarmor.googleapis.com --project="$PROJECT_ID" --quiet

# Model Armor requires the regional endpoint (rep) — global endpoint returns PERMISSION_DENIED
echo "Setting regional endpoint override..."
gcloud config set api_endpoint_overrides/modelarmor \
  "https://modelarmor.${REGION}.rep.googleapis.com/"

# Check if template already exists
if gcloud model-armor templates describe "$TEMPLATE_ID" \
    --location="$REGION" --project="$PROJECT_ID" &>/dev/null; then
  echo "Template '$TEMPLATE_ID' already exists — skipping creation."
else
  echo "Creating Model Armor template..."
  gcloud model-armor templates create "$TEMPLATE_ID" \
    --location="$REGION" \
    --project="$PROJECT_ID" \
    --pi-and-jailbreak-filter-settings-enforcement=ENABLED \
    --pi-and-jailbreak-filter-settings-confidence-level=MEDIUM_AND_ABOVE \
    --malicious-uri-filter-settings-enforcement=ENABLED \
    --template-metadata-ignore-partial-invocation-failures \
    --template-metadata-log-operations \
    --template-metadata-log-sanitize-operations
  echo "Template created."
fi

echo ""
echo "MODEL_ARMOR_TEMPLATE=$TEMPLATE_NAME"
echo "Add the above to your Cloud Run service env vars and to backend/.env for local dev."
