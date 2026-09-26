#!/usr/bin/env bash
# Apply a git archive onto the VAULT hearth tree, then recreate containers.
#
# Host-local files are left untouched: docker-compose.yml, boot_serve.py,
# .env, and ./data. Code under hearth/ is replaced to match the archive.
# Nothing in this script prints .env or file contents from ./data.
#
# Usage: HEARTH_DEPLOY_SHA=... HEARTH_DEPLOY_PATH=... vault-deploy-remote.sh <tarball>
# HEARTH_DEPLOY_SKIP_COMPOSE=1 skips docker (used by scripts/test-vault-deploy.sh).
set -euo pipefail

export PATH="/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:${PATH:-}"
umask 077

DEST="${HEARTH_DEPLOY_PATH:-/volume2/media/docker/hearth}"
SHA="${HEARTH_DEPLOY_SHA:-}"
TARBALL="${1:-}"

if [[ ! "$SHA" =~ ^[0-9a-fA-F]{40}$ ]]; then
  echo "HEARTH_DEPLOY_SHA must be a 40-character git sha" >&2
  exit 1
fi
SHA="${SHA,,}"

if [[ "$DEST" != /* || "$DEST" == *..* || "$DEST" != */hearth ]]; then
  echo "refusing HEARTH_DEPLOY_PATH (must be an absolute path ending in /hearth)" >&2
  exit 1
fi
if [[ ! -d "$DEST" ]]; then
  echo "deploy path does not exist: $DEST" >&2
  exit 1
fi
if [[ ! -f "$TARBALL" ]]; then
  echo "deploy tarball not found" >&2
  exit 1
fi

stage=""
cleanup() {
  if [[ -n "$stage" ]]; then
    rm -rf "$stage"
  fi
}
trap cleanup EXIT
stage="$(mktemp -d)"

file_hash() {
  local path="$1"
  if [[ -f "$path" ]]; then
    sha256sum "$path" | awk '{print $1}'
  fi
}

tree_hash() {
  local root="$1"
  if [[ ! -d "$root" ]]; then
    return 0
  fi
  (
    cd "$root"
    find . -type f | LC_ALL=C sort | while IFS= read -r file; do
      sha256sum "$file"
    done
  ) | sha256sum | awk '{print $1}'
}

env_before="$(file_hash "$DEST/.env")"
data_before="$(tree_hash "$DEST/data")"

tar -xzf "$TARBALL" -C "$stage" --no-same-owner
rm -f "$stage/.env" "$stage/docker-compose.yml" "$stage/boot_serve.py" "$stage/deployed-sha.txt"
rm -rf "$stage/data" "$stage/.git"

if [[ ! -d "$stage/hearth" ]]; then
  echo "archive has no hearth/ directory" >&2
  exit 1
fi
if ! command -v rsync >/dev/null 2>&1; then
  echo "rsync is required on the deploy host" >&2
  exit 1
fi

shopt -s dotglob nullglob
for item in "$stage"/*; do
  base="$(basename "$item")"
  case "$base" in
    .env | data | docker-compose.yml | boot_serve.py | deployed-sha.txt | .git)
      continue
      ;;
    hearth)
      mkdir -p "$DEST/hearth"
      # --checksum: a same-size edit must land even when mtimes match or the
      # host copy looks newer. --delete drops modules removed from the commit.
      rsync -a --checksum --delete --no-owner --no-group "$item"/ "$DEST/hearth"/
      ;;
    *)
      if [[ -d "$item" ]]; then
        mkdir -p "$DEST/$base"
        rsync -a --checksum --no-owner --no-group "$item"/ "$DEST/$base"/
      else
        cp -a "$item" "$DEST/$base"
      fi
      ;;
  esac
done
shopt -u dotglob nullglob

env_after="$(file_hash "$DEST/.env")"
data_after="$(tree_hash "$DEST/data")"
if [[ "$env_before" != "$env_after" ]]; then
  echo "deploy aborted: .env changed" >&2
  exit 1
fi
if [[ "$data_before" != "$data_after" ]]; then
  echo "deploy aborted: ./data changed" >&2
  exit 1
fi

compose="$DEST/docker-compose.yml"
if [[ ! -f "$compose" ]]; then
  echo "host docker-compose.yml is missing; left the tree without writing one" >&2
  exit 1
fi
if grep -q -- './hearth:/app/hearth' "$compose"; then
  echo "host docker-compose.yml still bind-mounts ./hearth:/app/hearth"
else
  echo "host docker-compose.yml has no ./hearth:/app/hearth bind; file left unchanged"
fi

printf '%s\n' "$SHA" > "$DEST/deployed-sha.txt"
echo "updated $DEST to $SHA"

if [[ "${HEARTH_DEPLOY_SKIP_COMPOSE:-}" == "1" ]]; then
  echo "skipping docker compose"
  exit 0
fi

if docker info >/dev/null 2>&1; then
  compose_cmd=(docker compose)
elif sudo -n docker info >/dev/null 2>&1; then
  compose_cmd=(sudo -n docker compose)
else
  echo "deploy user cannot run docker. Add them to the docker group or a NOPASSWD sudoers rule. See docs/ci-cd.md." >&2
  exit 1
fi

(
  cd "$DEST"
  "${compose_cmd[@]}" up -d --force-recreate --build
)
echo "docker compose recreate finished"
