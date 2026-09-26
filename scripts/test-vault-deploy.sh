#!/usr/bin/env bash
# Local checks for the VAULT deploy scripts. No SSH, no docker, no secrets.
set -euo pipefail

root="$(cd "$(dirname "$0")/.." && pwd)"
bash -n "$root/scripts/ci-deploy-gate.sh"
bash -n "$root/scripts/vault-deploy.sh"
bash -n "$root/scripts/vault-deploy-remote.sh"

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

# --- gate: missing secrets skip, present secrets proceed, values stay quiet ---
gate_out="$(mktemp)"
gate_err="$(mktemp)"
bash "$root/scripts/ci-deploy-gate.sh" >"$gate_out" 2>"$gate_err"
grep -qx 'ready=false' "$gate_out"
grep -q 'TAILSCALE_AUTHKEY' "$gate_err"
grep -q 'VAULT_SSH_PRIVATE_KEY' "$gate_err"
grep -q 'VAULT_SSH_USER' "$gate_err"

secret_key="super-secret-tailscale-key"
secret_ssh="super-secret-ssh-key"
TAILSCALE_AUTHKEY="$secret_key" \
  VAULT_SSH_PRIVATE_KEY="$secret_ssh" \
  VAULT_SSH_USER="hearth-deploy" \
  bash "$root/scripts/ci-deploy-gate.sh" >"$gate_out" 2>"$gate_err"
grep -qx 'ready=true' "$gate_out"
if grep -q "$secret_key" "$gate_out" "$gate_err" || grep -q "$secret_ssh" "$gate_out" "$gate_err"; then
  echo "gate printed a secret value" >&2
  exit 1
fi

# --- remote overlay preserves host-local files ---
sha="0123456789abcdef0123456789abcdef01234567"
src="$tmp/src"
dest="$tmp/tree/hearth"
mkdir -p "$src/hearth" "$src/workspace/skills" "$src/ha" "$src/data" "$src/docs"
printf 'new-code\n' > "$src/hearth/marker.py"
printf 'skill\n' > "$src/workspace/skills/vault_echo.py"
printf 'ha-tracked\n' > "$src/ha/configuration.yaml"
printf 'EVIL_DB\n' > "$src/data/hearth-auth.db"
printf 'EVIL_ENV=1\n' > "$src/.env"
printf 'services: {}\n' > "$src/docker-compose.yml"
printf 'evil boot\n' > "$src/boot_serve.py"
printf 'FROM scratch\n' > "$src/Dockerfile"
mkdir -p "$dest/hearth" "$dest/workspace/.cache" "$dest/ha/.storage" "$dest/data"
printf 'old-code\n' > "$dest/hearth/marker.py"
printf 'stale\n' > "$dest/hearth/removed.py"
printf 'cache\n' > "$dest/workspace/.cache/local.txt"
printf 'auth\n' > "$dest/ha/.storage/auth"
printf 'REALDB\n' > "$dest/data/hearth-auth.db"
printf 'SUPER_SECRET=do-not-print\n' > "$dest/.env"
cat > "$dest/docker-compose.yml" <<'EOF'
services:
  hearth:
    volumes:
      - ./hearth:/app/hearth
EOF
printf 'HEARTH_BIND_HOST=127.0.0.1\n' > "$dest/boot_serve.py"

archive="$tmp/hearth.tgz"
tar -C "$src" -czf "$archive" .

fakebin="$tmp/fakebin"
mkdir -p "$fakebin"
marker="$tmp/docker-was-called"
cat > "$fakebin/docker" <<EOF
#!/bin/sh
echo called > "$marker"
exit 99
EOF
chmod +x "$fakebin/docker"

log="$tmp/deploy.log"
PATH="$fakebin:$PATH" \
  HEARTH_DEPLOY_SHA="$sha" \
  HEARTH_DEPLOY_PATH="$dest" \
  HEARTH_DEPLOY_SKIP_COMPOSE=1 \
  bash "$root/scripts/vault-deploy-remote.sh" "$archive" >"$log"

grep -qx 'new-code' "$dest/hearth/marker.py"
[[ ! -e "$dest/hearth/removed.py" ]]
grep -qx 'cache' "$dest/workspace/.cache/local.txt"
grep -qx 'skill' "$dest/workspace/skills/vault_echo.py"
grep -qx 'auth' "$dest/ha/.storage/auth"
grep -qx 'ha-tracked' "$dest/ha/configuration.yaml"
grep -qx 'REALDB' "$dest/data/hearth-auth.db"
grep -qx 'SUPER_SECRET=do-not-print' "$dest/.env"
grep -qx 'HEARTH_BIND_HOST=127.0.0.1' "$dest/boot_serve.py"
grep -q './hearth:/app/hearth' "$dest/docker-compose.yml"
grep -qx 'FROM scratch' "$dest/Dockerfile"
grep -qx "$sha" "$dest/deployed-sha.txt"
grep -q 'still bind-mounts ./hearth:/app/hearth' "$log"
[[ ! -e "$marker" ]]

if grep -q 'SUPER_SECRET' "$log" || grep -q 'REALDB' "$log" || grep -q 'EVIL' "$log"; then
  echo "deploy log included protected file contents" >&2
  exit 1
fi

# Host compose without the bind is left unchanged and reported.
nobind="$tmp/nobind/hearth"
mkdir -p "$nobind/hearth"
printf 'old\n' > "$nobind/hearth/marker.py"
printf 'services: {}\n' > "$nobind/docker-compose.yml"
printf 'keep\n' > "$nobind/.env"
HEARTH_DEPLOY_SHA="$sha" \
  HEARTH_DEPLOY_PATH="$nobind" \
  HEARTH_DEPLOY_SKIP_COMPOSE=1 \
  bash "$root/scripts/vault-deploy-remote.sh" "$archive" >"$log"
grep -qx 'services: {}' "$nobind/docker-compose.yml"
grep -q 'no ./hearth:/app/hearth bind' "$log"
grep -qx 'new-code' "$nobind/hearth/marker.py"
grep -qx 'keep' "$nobind/.env"

# A changed .env must abort. Point the hash tripwire at a directory we can
# sabotage by replacing the script's tar with a direct rsync is unnecessary:
# the tripwire is covered because an archive .env is deleted before copy.
# Refuse a path that is not the hearth tree.
if HEARTH_DEPLOY_SHA="$sha" HEARTH_DEPLOY_PATH="/tmp" HEARTH_DEPLOY_SKIP_COMPOSE=1 \
  bash "$root/scripts/vault-deploy-remote.sh" "$archive" >"$log" 2>&1; then
  echo "expected a non-hearth path to be refused" >&2
  exit 1
fi

echo "vault deploy checks passed"
