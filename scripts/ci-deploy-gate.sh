#!/usr/bin/env bash
# Print ready=true or ready=false for the deploy job. Missing secrets skip
# the deploy; they do not fail CI. Secret values are never printed.
set -euo pipefail

missing=()
[[ -n "${TAILSCALE_AUTHKEY:-}" ]] || missing+=(TAILSCALE_AUTHKEY)
[[ -n "${VAULT_SSH_PRIVATE_KEY:-}" ]] || missing+=(VAULT_SSH_PRIVATE_KEY)
[[ -n "${VAULT_SSH_USER:-}" ]] || missing+=(VAULT_SSH_USER)

if ((${#missing[@]} > 0)); then
  echo "::warning::Deploy skipped. Missing GitHub Actions secrets: ${missing[*]}. See docs/ci-cd.md." >&2
  echo "ready=false"
  exit 0
fi

echo "ready=true"
