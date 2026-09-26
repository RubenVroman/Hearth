#!/usr/bin/env bash
# Runner-side deploy: send the git archive to VAULT and recreate the stack.
# Requires the GitHub Actions secrets documented in docs/ci-cd.md.
# Never prints the SSH key or .env.
set -euo pipefail

umask 077

: "${VAULT_SSH_PRIVATE_KEY:?VAULT_SSH_PRIVATE_KEY is required}"
: "${VAULT_SSH_USER:?VAULT_SSH_USER is required}"
: "${HEARTH_DEPLOY_SHA:?HEARTH_DEPLOY_SHA is required}"

HOST="${VAULT_SSH_HOST:-vault.taileff393.ts.net}"
PORT="${VAULT_SSH_PORT:-22}"
DEST="${VAULT_DEPLOY_PATH:-/volume2/media/docker/hearth}"
HEALTH_URL="${VAULT_HEALTHCHECK_URL:-https://vault.taileff393.ts.net/health}"
SHA="$HEARTH_DEPLOY_SHA"
USER_NAME="$VAULT_SSH_USER"

if [[ ! "$SHA" =~ ^[0-9a-fA-F]{40}$ ]]; then
  echo "HEARTH_DEPLOY_SHA must be a 40-character git sha" >&2
  exit 1
fi
SHA="${SHA,,}"
if [[ ! "$HOST" =~ ^[A-Za-z0-9._-]+$ ]]; then
  echo "invalid VAULT_SSH_HOST" >&2
  exit 1
fi
if [[ ! "$PORT" =~ ^[0-9]+$ ]] || ((PORT < 1 || PORT > 65535)); then
  echo "invalid VAULT_SSH_PORT" >&2
  exit 1
fi
if [[ ! "$USER_NAME" =~ ^[A-Za-z0-9._-]+$ ]]; then
  echo "invalid VAULT_SSH_USER" >&2
  exit 1
fi
if [[ "$DEST" != /* || "$DEST" == *..* || "$DEST" != */hearth ]]; then
  echo "invalid VAULT_DEPLOY_PATH" >&2
  exit 1
fi
if [[ "$HEALTH_URL" != "skip" && ! "$HEALTH_URL" =~ ^https://[A-Za-z0-9._:-]+(/[A-Za-z0-9._~:/?#@!$\&\'\(\)*+,;=%-]*)?$ ]]; then
  echo "invalid VAULT_HEALTHCHECK_URL" >&2
  exit 1
fi

install -d -m 700 "${HOME}/.ssh"
key_file="${HOME}/.ssh/vault_deploy"
known_file="${HOME}/.ssh/vault_known_hosts"
printf '%s\n' "$VAULT_SSH_PRIVATE_KEY" | tr -d '\r' > "$key_file"
chmod 600 "$key_file"
unset VAULT_SSH_PRIVATE_KEY

strict="accept-new"
if [[ -n "${VAULT_SSH_KNOWN_HOSTS:-}" ]]; then
  printf '%s\n' "$VAULT_SSH_KNOWN_HOSTS" | tr -d '\r' > "$known_file"
  strict="yes"
  echo "using VAULT_SSH_KNOWN_HOSTS"
else
  : > "$known_file"
  echo "VAULT_SSH_KNOWN_HOSTS is unset; accepting the tailnet host key for this job"
fi
chmod 600 "$known_file"
unset VAULT_SSH_KNOWN_HOSTS

ssh_base=(
  ssh
  -i "$key_file"
  -p "$PORT"
  -o BatchMode=yes
  -o IdentitiesOnly=yes
  -o ForwardAgent=no
  -o ForwardX11=no
  -o StrictHostKeyChecking="$strict"
  -o UserKnownHostsFile="$known_file"
  -o ConnectTimeout=20
  -o ServerAliveInterval=15
  -o ServerAliveCountMax=8
)

remote_tar="/tmp/hearth-${SHA}.tgz"
remote_script="/tmp/hearth-deploy-${SHA}.sh"
archive="$(mktemp)"
cleanup() {
  rm -f "$archive" "$key_file"
}
trap cleanup EXIT

git archive --format=tar.gz -o "$archive" HEAD
echo "sending $(basename "$archive") to ${USER_NAME}@${HOST}:${DEST}"

"${ssh_base[@]}" "${USER_NAME}@${HOST}" "umask 077; cat > '${remote_tar}'" < "$archive"
"${ssh_base[@]}" "${USER_NAME}@${HOST}" "umask 077; cat > '${remote_script}'" < "$(dirname "$0")/vault-deploy-remote.sh"

# printf %q keeps the remote command free of operator-controlled quoting.
# bash -s so a DSM user whose login shell is ash still runs bash.
remote_sha="$(printf '%q' "$SHA")"
remote_dest="$(printf '%q' "$DEST")"
remote_script_q="$(printf '%q' "$remote_script")"
remote_tar_q="$(printf '%q' "$remote_tar")"
"${ssh_base[@]}" "${USER_NAME}@${HOST}" bash -s -- <<EOF
set -euo pipefail
trap 'rm -f ${remote_tar_q} ${remote_script_q}' EXIT
export HEARTH_DEPLOY_SHA=${remote_sha}
export HEARTH_DEPLOY_PATH=${remote_dest}
bash ${remote_script_q} ${remote_tar_q}
EOF

if [[ "$HEALTH_URL" == "skip" ]]; then
  echo "health check skipped"
  exit 0
fi

echo "checking ${HEALTH_URL}"
body="$(mktemp)"
trap 'rm -f "$archive" "$key_file" "$body"' EXIT
set +e
code="$(
  curl -sS -o "$body" -w '%{http_code}' \
    --proto '=https' \
    --tlsv1.2 \
    --retry 10 \
    --retry-delay 3 \
    --retry-all-errors \
    --retry-max-time 90 \
    --connect-timeout 10 \
    --max-time 20 \
    "$HEALTH_URL"
)"
curl_rc=$?
set -e
echo "health HTTP ${code:-000}"
if grep -q '"ok"' "$body"; then
  head -c 200 "$body"
  echo
else
  echo "health body omitted"
fi
if [[ "$curl_rc" -ne 0 || "$code" != "200" ]]; then
  echo "health check failed" >&2
  exit 1
fi
echo "health check ok"
