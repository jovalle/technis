#!/usr/bin/env bash
set -e

RUNNER_NAME="${GITHUB_RUNNER_NAME:-nexus-runner}"
RUNNER_LABELS="${GITHUB_RUNNER_LABELS:-nexus,self-hosted,linux,x64}"
TOKEN="${GITHUB_RUNNER_TOKEN:-}"
ORG="${GITHUB_RUNNER_ORG:-}"
REPO_URL="${GITHUB_RUNNER_REPO_URL:-https://github.com/jovalle/plate-pantry}"

if [ -z "$TOKEN" ]; then
  echo "Error: GITHUB_RUNNER_TOKEN is required." >&2
  exit 1
fi

# Determine target URL and API endpoint
if [ -n "$ORG" ]; then
  TARGET_URL="https://github.com/${ORG}"
  REG_API="https://api.github.com/orgs/${ORG}/actions/runners/registration-token"
  REM_API="https://api.github.com/orgs/${ORG}/actions/runners/remove-token"
else
  # Strip protocol and domain to get OWNER/REPO
  REPO_PATH="${REPO_URL#https://github.com/}"
  REPO_PATH="${REPO_PATH#http://github.com/}"
  REPO_PATH="${REPO_PATH%/}"
  TARGET_URL="https://github.com/${REPO_PATH}"
  REG_API="https://api.github.com/repos/${REPO_PATH}/actions/runners/registration-token"
  REM_API="https://api.github.com/repos/${REPO_PATH}/actions/runners/remove-token"
fi

PAT_TOKEN=""
REG_TOKEN=""

# If TOKEN looks like a PAT (ghp_, github_pat_, gho_, ghu_, or standard 40-char token), request a registration token from the API
if [[ "$TOKEN" =~ ^(ghp_|github_pat_|gho_|ghu_) ]] || [ ${#TOKEN} -eq 40 ]; then
  PAT_TOKEN="$TOKEN"
  echo "Requesting registration token from GitHub API for ${TARGET_URL}..."
  RESPONSE=$(curl -sS -X POST \
    -H "Authorization: Bearer ${PAT_TOKEN}" \
    -H "Accept: application/vnd.github+json" \
    -H "X-GitHub-Api-Version: 2022-11-28" \
    "${REG_API}")

  REG_TOKEN=$(echo "$RESPONSE" | jq -r '.token // empty')
  if [ -z "$REG_TOKEN" ]; then
    echo "Failed to obtain registration token from GitHub API. Response:" >&2
    echo "$RESPONSE" >&2
    exit 1
  fi
else
  # Direct runner registration token provided (e.g. from GitHub UI)
  REG_TOKEN="$TOKEN"
fi

cleanup() {
  echo "Signal received, shutting down runner..."
  if [ -n "$PAT_TOKEN" ]; then
    echo "Requesting removal token from GitHub API..."
    REM_RESP=$(curl -sS -X POST \
      -H "Authorization: Bearer ${PAT_TOKEN}" \
      -H "Accept: application/vnd.github+json" \
      -H "X-GitHub-Api-Version: 2022-11-28" \
      "${REM_API}" || true)
    REM_TOKEN=$(echo "$REM_RESP" | jq -r '.token // empty')
    if [ -n "$REM_TOKEN" ]; then
      ./config.sh remove --token "$REM_TOKEN" || true
    fi
  fi
}

trap 'cleanup; exit 130' INT
trap 'cleanup; exit 143' TERM

# Configure the runner if not already configured
if [ ! -f .runner ]; then
  echo "Registering runner ${RUNNER_NAME} with labels ${RUNNER_LABELS} to ${TARGET_URL}..."
  ./config.sh \
    --url "${TARGET_URL}" \
    --token "${REG_TOKEN}" \
    --name "${RUNNER_NAME}" \
    --labels "${RUNNER_LABELS}" \
    --work "_work" \
    --unattended \
    --replace
fi

echo "Starting runner ${RUNNER_NAME}..."
./run.sh &
wait $!
