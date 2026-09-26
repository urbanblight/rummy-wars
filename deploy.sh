#!/usr/bin/env bash

# Avoid applying the script's shell options to the caller when it is sourced.
if [[ "${BASH_SOURCE[0]}" != "$0" ]]; then
    printf '%s\n' 'Run deploy.sh as ./deploy.sh, not with source, so failures do not close your terminal.' >&2
    return 1
fi

set -euo pipefail

# Deployment settings come from the local, untracked dotenv file.
if [[ ! -f .env ]]; then
    printf '%s\n' 'deploy.sh requires a .env file containing the Cloud Run environment variables.' >&2
    exit 1
fi

# Cloud Run provides PORT, and SECRET_KEY is loaded from Secret Manager below.
ENV_VARS=$(awk -F= '
    /^[[:space:]]*#/ || /^[[:space:]]*$/ { next }
    $1 == "SECRET_KEY" || $1 == "PORT" { next }
    { print }
' .env | paste -sd, -)

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

# Deploy the image, public service settings, runtime configuration, and secret.
gcloud run deploy rw-service \
    --image us-west1-docker.pkg.dev/rummy-wars/containers/rw-app@$DIGEST \
    --region us-west1 \
    --set-env-vars "$ENV_VARS" \
    --set-secrets SECRET_KEY=SECRET_KEY:latest \
    --allow-unauthenticated
