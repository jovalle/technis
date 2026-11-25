#!/usr/bin/env bash
set -euo pipefail

[[ ${SCAN_HEAD:-} =~ ^[0-9a-f]{40}$ ]] || exit 1
[[ ${SCAN_BASE:-} =~ ^[0-9a-f]{40}$ ]] || exit 1
git cat-file -e "${SCAN_HEAD}^{commit}"
if [[ ${SCAN_BASE} =~ ^0{40}$ ]]; then
  range=${SCAN_HEAD}
else
  if ! git cat-file -e "${SCAN_BASE}^{commit}" 2>/dev/null; then
    git fetch --no-tags --filter=blob:none origin "${SCAN_BASE}"
  fi
  git cat-file -e "${SCAN_BASE}^{commit}"
  range="${SCAN_BASE}..${SCAN_HEAD}"
fi

tmp=$(mktemp -d)
trap 'rm -rf "${tmp}"' EXIT
printf '[extend]\nuseDefault = true\n' >"${tmp}/gitleaks.toml"
gitleaks git --no-banner --redact=100 --ignore-gitleaks-allow \
  --config "${tmp}/gitleaks.toml" --gitleaks-ignore-path "${tmp}" \
  --log-opts="--diff-merges=first-parent ${range}"
