# Backend deployment

The API and the single check worker run as UID/GID `10001:10001`. Code is baked
into the image; only `uploads/` is mounted writable. Containers have a read-only
root filesystem, a bounded temporary filesystem, dropped capabilities, and CPU,
memory, process, and log limits. The image starts Uvicorn without reload or
automatic trust in forwarded headers.

## Before replacing a running deployment

1. Inspect the running Compose project name, database image, database volume,
   upload mount, and systemd working directory. Keep their identities unchanged.
   The repository-root Compose file uses `pgdata`; the backend-only Compose file
   uses `pgvector_data`. These names are not interchangeable on an existing VM.
2. Wait for active checks to finish and pause admission during the maintenance
   window. Take a PostgreSQL dump and a protected backup of the deployment files
   and uploads. Validate the dump with `pg_restore --list`; regularly verify a
   restore into a separate temporary database as well.
3. Keep the existing database credentials in the ignored deployment `.env`.
   `POSTGRES_PASSWORD` is required by Compose. Changing this variable alone does
   not rotate the password stored in an initialized PostgreSQL volume.
4. For an existing Linux upload bind mount, verify the absolute path is the
   backend's upload directory before changing permissions. Grant UID/GID 10001
   access to that directory and its PDF/highlight files only. Do not change
   ownership of the repository, `.env`, database data, or other VM directories.
   A new installation can create the directory with
   `sudo install -d -o 10001 -g 10001 -m 0750 uploads` from the backend directory.
5. Set `APP_ENV=production`, a unique JWT secret, HTTPS cookie settings, and the
   exact allowed frontend origin. Keep `BOOTSTRAP_ADMIN_PASSWORD` empty when
   admin `cihuy` already exists. No sample accounts or passwords are created.
6. Validate with `docker compose config --quiet`, build the API/worker images,
   and recreate the affected services using the existing project and volumes.
   Do not run `down -v`. Start exactly one worker after the API is healthy.
7. Verify the public health endpoint, authenticated access, a bounded login
   throttle test, and document upload/check behavior. Keep the prior images and
   backups for rollback. Use the deployment's existing `backend.service` only
   after confirming what its start/stop commands manage.

## Network and resource settings

`BACKEND_BIND_HOST` and `POSTGRES_BIND_HOST` default to `127.0.0.1`. Host
Cloudflared can reach `127.0.0.1:8000`; the worker/API reach PostgreSQL using the
Docker service name `db`. A separate host client can reach PostgreSQL on its
loopback port. Remove the database `ports` entry entirely if no host client
needs it. These bindings do not require touching a hypervisor or its firewall.

### Frontend development over Tailscale

A local frontend that proxies to `http://<tailscale-ip>:8000` needs an explicit
API binding on that private IP. A loopback-only binding refuses that connection;
Next.js may report its proxy connection failure as an internal server error.

For the backend-only Linux VM deployment, prepare these ignored `.env` values
after the change is reviewed and pushed:

```env
BACKEND_BIND_HOST=127.0.0.1
BACKEND_TAILNET_IP=<the-VM-Tailscale-IPv4>
COMPOSE_FILE=docker-compose.yml:docker-compose.tailnet.yml
```

The override adds only the exact Tailscale API address alongside loopback. Keep
PostgreSQL on loopback. `COMPOSE_FILE` lets the existing `backend.service`
working in the backend directory load both files on its next requested restart.
Ensure the Tailscale interface/address exists before starting the containers.
Check the merged mapping with `docker compose config --quiet`; this command does
not deploy or restart services. For an explicit one-off file selection, use
`docker compose -f docker-compose.yml -f docker-compose.tailnet.yml config --quiet`.
Do not deploy until the user has pushed the local changes and requests the pull.

The frontend developer can alternatively keep the API loopback-only and use the
existing public API through Next's server-side proxy. The existing frontend
`next.config.ts` supports this local `.env.local` configuration:

```env
NEXT_PUBLIC_API_URL=
BACKEND_API_URL=https://api-evo.juliasta.my.id
```

Restart the local Next dev process after changing these values. Browser calls
then remain on the local frontend origin and Next forwards them to the API.
If calling the public API directly from the browser instead, its CORS allowlist
must explicitly include the local frontend origin; avoid wildcard credentialed
CORS. Tailscale membership alone does not change browser CORS rules.

API defaults are 1 CPU and 768 MiB memory; worker defaults are 1.5 CPUs and
1536 MiB. Set `BACKEND_*_LIMIT` and `CHECK_WORKER_*_LIMIT` in `.env` after
measuring the VM's available capacity and representative documents. A container
memory limit includes its temporary filesystem. A task that exceeds the limit
can be killed by the runtime; monitor failed checks and OOM/restart events.

Only `uploads/` remains writable on disk. `BACKEND_TMPFS_SIZE` (128 MiB) and
`CHECK_WORKER_TMPFS_SIZE` (256 MiB) bound temporary files; their contents disappear
when the container is recreated. Increase these deliberately if measured
concurrent uploads require it.

## Login limits and proxy identity

Login protection is implemented in the backend's existing database, so it
works without additional Cloudflare rules. Defaults are 5 failed attempts per
account and 30 attempts per client IP over 900 seconds. Configure
`LOGIN_ACCOUNT_MAX_FAILURES`, `LOGIN_IP_MAX_ATTEMPTS`, and `LOGIN_WINDOW_SECONDS`
as needed. Cloudflare rate limiting can be added as a second layer.

`LOGIN_TRUSTED_PROXY_CIDRS` defaults to empty, so client-supplied forwarded-IP
headers are ignored. For host Cloudflared plus Docker port publishing, identify
the immediate socket peer and trust only its necessary address/CIDR. The
container command disables Uvicorn proxy-header rewriting so the backend can
make that trust decision itself. Do not trust all networks or arbitrary proxy
headers. A missing trusted-proxy configuration can aggregate users behind the
same proxy under one IP limit, so validate it before enabling a public login.

## Local development

To retire only the former built-in demo accounts while keeping their document,
check, and profile references, first run
`python -m app.workers.retire_demo_accounts` for a preview, then add `--apply`.
The command requires an active admin `cihuy`; it never changes that admin's
password or other real users. Retired accounts lose access and old credentials.

After recreating the Docker network, verify that its gateway still matches the
exact trusted proxy address configured in `LOGIN_TRUSTED_PROXY_CIDRS`.

For Python development, use `.venv` and run
`uvicorn app.main:app --reload --port 8000` explicitly. For local HTTP cookies,
set `SESSION_COOKIE_SECURE=false` in your ignored local `.env` only.

Compose forces `APP_ENV=production` for the API/worker and uses the same hardened
defaults locally. Run it from `backend/` with
`docker compose up --build`, or from the repository root with
`docker compose --env-file backend/.env up --build`. Code edits require a rebuild
because the application directory is no longer bind-mounted. Do not copy a
development `.env` into an image; `.dockerignore` excludes environment files,
virtual environments, uploads, dumps, and Git metadata.
