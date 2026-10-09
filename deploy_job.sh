#!/usr/bin/env bash
# Local Docker build -> Artifact Registry -> Job -> Scheduler.
set -euo pipefail
cd "$(dirname "$0")"
PROJECT_ID="${PROJECT_ID:-ggn-nmfs-swfscerddap-dev-1}"
REGION="${REGION:-us-east4}"
REPO_NAME="${REPO_NAME:-gtspp-cloud-run}"
IMAGE_NAME="gtspp-monthly"
TAG="${TAG:-v0.2.0}"
SERVICE_ACCOUNT="${SERVICE_ACCOUNT:-swfscerddap-run-sa1@${PROJECT_ID}.iam.gserviceaccount.com}"
JOB_MODE="${JOB_MODE:-production}"
BUCKET="${BUCKET:-erddap-swfsc-erd}"
START_MONTH="${START_MONTH:-202608}"
END_MONTH="${END_MONTH:-202608}"
ENABLE_SCHEDULE="${ENABLE_SCHEDULE:-false}"
FLAG_SECRET="${FLAG_SECRET:-erddap-flag-erd-gtspp-best}"
FLAG_SECRET_SCOPE="${FLAG_SECRET_SCOPE:-regional}"
SCHEDULE="${SCHEDULE:-0 10 10 * *}"
TIME_ZONE="${TIME_ZONE:-America/Los_Angeles}"
case "$JOB_MODE" in
  staging) JOB_NAME="gtspp-monthly-staging"; PREFIX="_staging/gtspp/points/gtsppNcCF" ;;
  production) JOB_NAME="gtspp-monthly-processor"; PREFIX="points/gtsppNcCF"; START_MONTH=""; END_MONTH="" ;;
  *) echo "JOB_MODE must be staging or production" >&2; exit 1 ;;
esac
for tool in gcloud docker; do command -v "$tool" >/dev/null || { echo "Missing $tool" >&2; exit 1; }; done
secret_resource=""
if [[ "$JOB_MODE" == production ]]; then
  case "$FLAG_SECRET_SCOPE" in
    regional)
      gcloud secrets versions describe latest --secret "$FLAG_SECRET" --location "$REGION" --project "$PROJECT_ID" >/dev/null
      secret_resource="projects/${PROJECT_ID}/locations/${REGION}/secrets/${FLAG_SECRET}/versions/latest" ;;
    global)
      gcloud secrets versions describe latest --secret "$FLAG_SECRET" --project "$PROJECT_ID" >/dev/null
      secret_resource="projects/${PROJECT_ID}/secrets/${FLAG_SECRET}/versions/latest" ;;
    *) echo "FLAG_SECRET_SCOPE must be regional or global" >&2; exit 1 ;;
  esac
fi
IMAGE_PATH="${REGION}-docker.pkg.dev/${PROJECT_ID}/${REPO_NAME}/${IMAGE_NAME}:${TAG}"
docker buildx build --platform "${PLATFORM:-linux/amd64}" -t "$IMAGE_PATH" --load .
# This repository was created during staging. Surface auth/IAM errors directly.
gcloud artifacts repositories describe "$REPO_NAME" --location "$REGION" --project "$PROJECT_ID" >/dev/null
gcloud auth configure-docker "${REGION}-docker.pkg.dev" --quiet
docker push "$IMAGE_PATH"
envs="JOB_MODE=${JOB_MODE},BUCKET=${BUCKET},PREFIX=${PREFIX},STATE_PREFIX=_processing/gtspp/${JOB_MODE},LOOKBACK_MONTHS=4,MAX_MONTHS=8,START_MONTH=${START_MONTH},END_MONTH=${END_MONTH}"
if [[ "$JOB_MODE" == production ]]; then
  envs+=",ERDDAP_BASE_URL=${ERDDAP_BASE_URL:-https://155.206.244.135/erddap},ERDDAP_INSECURE=${ERDDAP_INSECURE:-true},ERDDAP_FLAG_SECRET=${secret_resource}"
fi
gcloud run jobs deploy "$JOB_NAME" --project "$PROJECT_ID" --region "$REGION" \
  --image "$IMAGE_PATH" --service-account "$SERVICE_ACCOUNT" --tasks 1 --parallelism 1 \
  --max-retries 0 --task-timeout 14400s --memory "${MEMORY:-8Gi}" --cpu "${CPU:-4}" \
  --set-env-vars "$envs" --clear-secrets
if [[ "$JOB_MODE" == production ]]; then
  scheduler_name="gtspp-monthly-schedule"
  project_number="$(gcloud projects describe "$PROJECT_ID" --format='value(projectNumber)')"
  if gcloud scheduler jobs describe "$scheduler_name" --project "$PROJECT_ID" --location "$REGION" >/dev/null 2>&1; then
    gcloud scheduler jobs delete "$scheduler_name" --project "$PROJECT_ID" --location "$REGION" --quiet
  fi
  gcloud scheduler jobs create http "$scheduler_name" --project "$PROJECT_ID" --location "$REGION" \
    --schedule "$SCHEDULE" --time-zone "$TIME_ZONE" \
    --uri "https://${REGION}-run.googleapis.com/apis/run.googleapis.com/v1/namespaces/${project_number}/jobs/${JOB_NAME}:run" \
    --http-method POST --oauth-service-account-email "$SERVICE_ACCOUNT" \
    --oauth-token-scope https://www.googleapis.com/auth/cloud-platform
  if [[ "$ENABLE_SCHEDULE" != true ]]; then
    gcloud scheduler jobs pause "$scheduler_name" --project "$PROJECT_ID" --location "$REGION"
  fi
fi
echo "Deployed ${JOB_NAME}; schedule enabled: ${ENABLE_SCHEDULE}."
