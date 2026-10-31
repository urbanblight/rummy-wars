#!/usr/bin/env bash

# Avoid applying the script's shell options to the caller when it is sourced.
if [[ "${BASH_SOURCE[0]}" != "$0" ]]; then
    printf '%s\n' 'Run deploy.sh as ./deploy.sh, not with source, so failures do not close your terminal.' >&2
    return 1
fi

set -euo pipefail

# Usage: ./deploy.sh [--beta]
SERVICE=rw-service
case "${1:-}" in
    "") ;;
    --beta|beta) SERVICE=rw-service-beta ;;
    *) printf '%s\n' 'Usage: deploy.sh [--beta]' >&2; exit 1 ;;
esac

# Deployment settings come from the local, untracked dotenv file.
if [[ ! -f .env ]]; then
    printf '%s\n' 'deploy.sh requires a .env file containing the Cloud Run environment variables.' >&2
    exit 1
fi

# Cloud Run provides PORT; SECRET_KEY and DATABASE_URL are loaded from Secret Manager below.
ENV_VARS=$(awk -F= '
    /^[[:space:]]*#/ || /^[[:space:]]*$/ { next }
    $1 == "SECRET_KEY" || $1 == "DATABASE_URL" || $1 == "PORT" || $1 == "CLOUDSQL_INSTANCE" { next }
    { print }
' .env | paste -sd'|' -)

# CLOUDSQL_INSTANCE is the connection name (PROJECT:REGION:INSTANCE) mounted at /cloudsql/.
CLOUDSQL_INSTANCE=$(awk -F= '$1 == "CLOUDSQL_INSTANCE" { sub(/^[^=]*=/, ""); gsub(/^["'\'']|["'\'']$/, ""); print; exit }' .env)
if [[ -z "$CLOUDSQL_INSTANCE" ]]; then
    printf '%s\n' 'CLOUDSQL_INSTANCE is not set in .env (expected PROJECT:REGION:INSTANCE).' >&2
    exit 1
fi

if [[ -z "$ENV_VARS" ]]; then
    printf '%s\n' 'No deployable environment variables were found in .env.' >&2
    exit 1
fi

# Build and publish an amd64 image for Cloud Run.
docker buildx build \
    --platform linux/amd64 \
    -t us-west1-docker.pkg.dev/rummy-wars/containers/rw-app:latest \
    -f ./Dockerfile \
    --push .

# Resolve the pushed tag to a digest so the service deploys the exact image built above.
DIGEST=$(gcloud artifacts docker images describe us-west1-docker.pkg.dev/rummy-wars/containers/rw-app:latest --format='value(image_summary.digest)')
[[ -n "$DIGEST" ]] || { printf '%s\n' 'Unable to resolve the pushed image digest.' >&2; exit 1; }
# Deploy the image, public service settings, runtime configuration, and secret.
gcloud run deploy "$SERVICE" \
    --image us-west1-docker.pkg.dev/rummy-wars/containers/rw-app@$DIGEST \
    --region us-west1 \
    --set-env-vars "^|^$ENV_VARS" \
    --add-cloudsql-instances "$CLOUDSQL_INSTANCE" \
    --set-secrets SECRET_KEY=SECRET_KEY:latest,DATABASE_URL=DATABASE_URL:latest \
    --min-instances=1 \
    --max-instances=1 \
    --allow-unauthenticated
