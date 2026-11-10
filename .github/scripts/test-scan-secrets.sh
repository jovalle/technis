#!/usr/bin/env bash
set -euo pipefail

scanner="$(cd "$(dirname "$0")" && pwd)/scan-secrets.sh"
version_script="$(cd "$(dirname "$0")" && pwd)/gitleaks-version.py"
tmp=$(mktemp -d)
trap 'rm -rf "${tmp}"' EXIT
cd "${tmp}"
printf 'repos:\n  - repo: https://github.com/gitleaks/gitleaks\n    rev: v8.30.0\n    hooks:\n      - id: gitleaks\n' >.pre-commit-config.yaml
[[ $(python3 "${version_script}") == 8.30.0 ]]
printf 'repos:\n  - hooks:\n      - id: gitleaks\n    repo: https://github.com/gitleaks/gitleaks\n    rev: v8.30.0\n' >.pre-commit-config.yaml
[[ $(python3 "${version_script}") == 8.30.0 ]]
printf 'repos: []\n' >.pre-commit-config.yaml
if python3 "${version_script}" >/dev/null 2>&1; then
  echo 'accepted missing scanner version' >&2
  exit 1
fi
rm .pre-commit-config.yaml
git init -q -b main
git config user.name 'Test'
git config user.email 'test@example.invalid'
printf 'safe\n' >file
git add file
git commit -qm base
base=$(git rev-parse HEAD)
git switch -qc side
printf 'token="%s%s" # gitleaks:allow\n' 'gh' 'p_A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8' >secret
git add secret
git commit -qm add
rm secret
git add -u
git commit -qm remove
git switch -q main
printf '[allowlist]\npaths = [".*"]\n' >.gitleaks.toml
printf 'safe\n' >other
git add other
git commit -qm diverge
git merge -q --no-ff side -m merge
head=$(git rev-parse HEAD)
if SCAN_BASE=${base} SCAN_HEAD=${head} bash "${scanner}" >/dev/null 2>&1; then
  echo 'failed to detect secret on merged branch' >&2
  exit 1
fi
SCAN_BASE=${head} SCAN_HEAD=${head} bash "${scanner}" >/dev/null 2>&1
if SCAN_BASE=invalid SCAN_HEAD=${head} bash "${scanner}" >/dev/null 2>&1; then
  echo 'accepted invalid commit range' >&2
  exit 1
fi
git switch -qc resolution
printf 'side\n' >side-file
git add side-file
git commit -qm side
git switch -q main
printf 'main\n' >main-file
git add main-file
git commit -qm main
git merge -q --no-commit --no-ff resolution >/dev/null 2>&1
printf 'token="%s%s"\n' 'gh' 'p_Z9y8X7w6V5u4T3s2R1q0P9o8N7m6L5k4J3i2' >secret
git add secret
git commit -qm resolution
rm secret
git add -u
git commit -qm remove-resolution
final_head=$(git rev-parse HEAD)
if SCAN_BASE=${head} SCAN_HEAD=${final_head} bash "${scanner}" >/dev/null 2>&1; then
  echo 'failed to detect secret introduced in merge resolution' >&2
  exit 1
fi
