# CI/CD

GitHub Actions runs the test suite on every pull request and push. A push to `main` deploys that commit to VAULT after the tests pass. Until the secrets below exist, the deploy job logs a warning and succeeds without touching the NAS, so CI stays green.

The workflow is [`.github/workflows/ci.yml`](../.github/workflows/ci.yml). It does not call the DSM API and it does not create or edit the Task Scheduler task `hearth-recreate`.

## Tests

Same command as local development, on Python 3.12 (the version in the `Dockerfile`):

```bash
python -m pip install -r requirements-dev.txt
pytest -q
```

`requirements-dev.txt` pulls in `requirements.txt` plus pytest. No extra package manager.

## What a deploy does

From the GitHub-hosted runner, over Tailscale:

1. SSH to VAULT and overlay the git archive for the commit onto `/volume2/media/docker/hearth`.
2. Leave these host-local files alone: `docker-compose.yml`, `boot_serve.py`, `.env`, and `./data`. `hearth/` is updated to match the commit (removed modules are deleted). Other tracked trees (`workspace/`, `ha/`, …) are updated without deleting host-only files such as `ha/.storage` or `workspace/.cache`.
3. If `docker-compose.yml` already bind-mounts `./hearth:/app/hearth`, that line stays. The workflow never writes a new compose file.
4. Write `deployed-sha.txt` (the 40-character commit SHA only).
5. Run the same recreate the DSM task runs:

   ```bash
   cd /volume2/media/docker/hearth && docker compose up -d --force-recreate --build
   ```

6. When Tailscale is up, `GET https://vault.taileff393.ts.net/health` and require HTTP 200. The body is printed only when it looks like Hearth's `/health` JSON. `.env` is never printed. Set `VAULT_HEALTHCHECK_URL` to `skip` if that HTTPS name is not ready.

Logs must not contain `.env`. The scripts do not run `docker compose config` (that interpolates secrets into the output).

## Secrets

Repository → Settings → Secrets and variables → Actions → Repository secrets.

Required. If any of these is missing, deploy skips:

| Secret | Value |
| --- | --- |
| `TAILSCALE_AUTHKEY` | Reusable, ephemeral, pre-approved auth key tagged `tag:ci`. This is how the GitHub runner reaches VAULT. An auth key is the supported path (the Tailscale action also accepts OAuth; this workflow uses the auth key). |
| `VAULT_SSH_PRIVATE_KEY` | OpenSSH private key for the deploy user (the `BEGIN`/`END` lines included). |
| `VAULT_SSH_USER` | SSH username, for example `hearth-deploy`. |

Optional:

| Secret | Default | Value |
| --- | --- | --- |
| `VAULT_SSH_HOST` | `vault.taileff393.ts.net` | Tailscale hostname or `100.67.187.109`. |
| `VAULT_SSH_PORT` | `22` | SSH port. |
| `VAULT_SSH_KNOWN_HOSTS` | unset | One line from `ssh-keyscan`. Without it, each job accepts the host key it sees on the tailnet (`accept-new`). Pin this once you can. |
| `VAULT_DEPLOY_PATH` | `/volume2/media/docker/hearth` | Absolute path ending in `/hearth`. |
| `VAULT_HEALTHCHECK_URL` | `https://vault.taileff393.ts.net/health` | `skip` disables the probe. Any other value must be `https://…`. |

Nothing in this list belongs in the git repo.

## One-time VAULT setup

Do this once over SSH. It does not change the `hearth-recreate` task. Leave that task as it is; after secrets are set, pushes to `main` recreate the stack on their own.

### Tailscale

1. MagicDNS stays on so `vault.taileff393.ts.net` resolves.
2. In the tailnet ACL, add a tag `tag:ci` and allow it to reach VAULT on TCP 22 and TCP 443:

   ```json
   {
     "grants": [
       {"src": ["tag:ci"], "dst": ["vault"], "ip": ["tcp:22", "tcp:443"]}
     ],
     "tagOwners": {"tag:ci": ["autogroup:admin"]}
   }
   ```

   Merge that into the existing policy. Do not drop rules the house already needs.

3. Create an auth key: reusable, ephemeral, tagged `tag:ci`, and pre-approved if device approval is on. Store it as `TAILSCALE_AUTHKEY`.

The runner's node name is `hearth-ci-<run id>`. Ephemeral nodes are removed when the job ends.

### Deploy user

1. Create a user (the name you store in `VAULT_SSH_USER`). Do not use the DSM admin account.
2. Install the matching public key in that user's `authorized_keys`.
3. Give the user read/write on `/volume2/media/docker/hearth` (the File Station share `/media/docker/hearth`). The user must be able to update `hearth/` and create `deployed-sha.txt`, and must not need to rewrite `.env`.
4. Confirm non-interactive SSH has `bash`, `rsync`, `tar`, and `sha256sum`.
5. Confirm docker works without a password. Either the user can already run `docker info`, or add a sudoers drop-in (this persists across Docker restarts; the socket group on DSM often does not):

   ```sudoers
   hearth-deploy ALL=(root) NOPASSWD: /usr/local/bin/docker
   ```

   Adjust the username. The deploy script tries `docker` first, then `sudo -n docker`. It never prompts.

6. Capture the host key from a machine that already trusts VAULT:

   ```bash
   ssh-keyscan -t ed25519 vault.taileff393.ts.net
   ```

   Store that line as `VAULT_SSH_KNOWN_HOSTS`.

A manual check, from a tailnet machine, before relying on Actions:

```bash
ssh hearth-deploy@vault.taileff393.ts.net 'cd /volume2/media/docker/hearth && docker compose version'
```

## Manual deploy

Actions → CI → Run workflow, branch `main`. The test job runs first. The deploy job then runs the same path as a push. Dispatch from any other branch does not deploy.

## Health check

`GET /health` is unauthenticated liveness (`{"ok": true, ...}`). The probe uses the existing Tailscale HTTPS name, not a new router port-forward. The certificate must verify (Tailscale HTTPS certificates do). If the name is not served yet, set `VAULT_HEALTHCHECK_URL=skip` and use `deployed-sha.txt` plus `docker compose ps` on VAULT instead.
