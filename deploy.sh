set -e

docker buildx build --platform linux/amd64 -t us-west1-docker.pkg.dev/rummy-wars/containers/rw-app:latest -f ./Dockerfile --push .
DIGEST=$(gcloud artifacts docker images describe us-west1-docker.pkg.dev/rummy-wars/containers/rw-app:latest --format='value(image_summary.digest)')
gcloud run deploy rw-service --image us-west1-docker.pkg.dev/rummy-wars/containers/rw-app@$DIGEST --region us-west1 --allow-unauthenticated