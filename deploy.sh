#!/usr/bin/env bash
fail() {
  printf 'Deploy failed: %s\n' "$1" >&2
  exit 1
}

docker buildx build --platform linux/amd64 \
  -t us-west1-docker.pkg.dev/rummy-wars/containers/rw-app:latest \
  -f ./Dockerfile --push . ||
  fail "building and pushing image"

DIGEST=$(gcloud artifacts docker images describe \
  us-west1-docker.pkg.dev/rummy-wars/containers/rw-app:latest \
  --format='value(image_summary.digest)') ||
  fail "looking up image digest"

[[ -n "$DIGEST" ]] || fail "image digest was empty"

gcloud run deploy rw-service \
  --image "us-west1-docker.pkg.dev/rummy-wars/containers/rw-app@$DIGEST" \
  --region us-west1 \
  --allow-unauthenticated ||
  fail "deploying to Cloud Run"