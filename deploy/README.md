# Rust VPS deployment

New repositories run the Axum/Tokio/SQLx API, Leptos/WebAssembly static frontend, and a dedicated PostgreSQL 17 database on the VPS. Existing applications keep their deployment paths.

## Provisioning contract

The provisioner configures repository variables `APP_NAME` (lowercase slug, at most 48 characters), `APP_ID` (immutable BugZero UUID), `APP_REPOSITORY` (canonical GitHub URL), `APP_DOMAIN` (plain DNS name), `APP_DISPLAY_NAME`, `RUNNER_LABEL`, `APP_STACK=rust-axum-leptos-v1`, `BUGZERO_APP_KEY` (public widget key), and `BUGZERO_WIDGET_URL` (HTTPS script URL). Deployment uses the actual GitHub repository context for `APP_REPOSITORY` and requires it to match the host identity. The widget settings are public frontend configuration; never put private API tokens there.

The per-app GitHub Actions runner must be Linux x86_64 on the VPS, have Docker Engine and Compose v2, Python 3.10+, and noninteractive sudo. Browser CI additionally requires the Chromium system libraries used by Playwright 1.58.2. These are already installed on the current shared provisioning VPS; new per-app runners reuse that host. When preparing a replacement VPS, install the libraries once with `npx --yes playwright@1.58.2 install-deps chromium` before enabling runners. CI installs its own pinned Node 22 toolchain and browser binary; it does not change shared system packages. VPS CI removes completed debug-profile outputs before the WebAssembly release build, then removes only its checkout's remaining generated `target` directory before installing browser tooling. This bounds retained build artifacts on the shared VPS. The compiled `rust/frontend/dist` bundle and all source files remain available for the Chromium checks; deployment state, images and database volumes are unaffected. Caddy must already import `/etc/caddy/sites/*.caddy`, and the app domain must point to this VPS. Provisioning the runner, DNS, and repository variables does not deploy existing apps.

Dispatch `.github/workflows/deploy.yml` manually with `target_sha` set to a full 40-character commit on `main`. The workflow checks ancestry against `origin/main`, checks out that exact revision, runs deployment safety tests, and builds both Docker images locally. It never reads mutable `latest` images for the app. The template repository has no `APP_NAME` or runner variable and skips the deploy job.

## Data and release ownership

`/opt/bugzero-apps/<slug>` belongs to this app. The script generates the PostgreSQL password once and stores it in a root-only `database.env`; no database password is sent through GitHub variables or logs. PostgreSQL has its own internal Docker network and persistent named volume, with no host port. Only the frontend has a loopback port, which Caddy proxies over HTTPS. The frontend accesses `/api/` on the same origin, so no cross-origin credentials are required.

Each release mounts public `app-config.json` and `qa-release-manifest.json` metadata containing its exact Git revision. WebAssembly files have hashed names; runtime configuration and release metadata are never cached. The public widget key is loaded by the frontend from runtime configuration, while database credentials remain server-side.

Deployment locks serialize one app's release and all Caddy reloads. An app owns only `bugzero-<slug>.caddy`, identified by a matching ownership marker. A preexisting site without that marker is rejected. The main Caddyfile and other applications' site files are never rewritten.

Before registering a managed Rust runner, the authenticated Chat bridge prepares
a root-owned `ownership.json` binding the app UUID, slug, domain and repository.
Deployment requires this exact identity before building images and again under
the app deployment lock. It cannot claim an unregistered state directory.
A durable `retirement.json` permanently prevents deployment, including retries
after a partially completed deletion. Identity and retirement markers remain
after cleanup so an old workflow or deletion request cannot affect a new app.

## App deletion and recovery data

BugZero coordinates app deletion through the trusted Chat host bridge. It first
checks and disables deployment, verifies the repository runner is idle, then
requests app-specific host retirement. The host validates ownership, stops the
runner and app workloads, validates a final PostgreSQL dump and archives private
state. Only verified app-owned resources are removed: both release slots, the
database container and volume, networks and Caddy site. BugZero then removes the
exact owned DNS A record and GitHub runner, archives the workspace and marks the
app and provision deleted. Failed steps remain visible for retry.

Recovery data remains private under `/opt/bugzero-retired-apps/<app-id>` without
automatic expiry and requires an explicit operator cleanup decision. App deletion
removes live service and storage while preserving this recovery archive, its
repository and vault. Never use global Docker prune or remove the shared Caddyfile,
DNS resource group, registry or other applications' resources.

## Promotion and rollback

Before every migration attempt, including first startup, `pg_dump --format=custom` writes a private backup under the app's `backups` directory. A failed or empty backup stops deployment. The API then runs `--migrate-only` before candidate services start. SQL migrations must be additive and compatible with the previous live release; automated rollback never reverses schema changes or restores a database over newer data.

API and frontend releases alternate between blue and green Compose projects. Candidate checks require a working PostgreSQL-backed `/api/health/db`, exact API and frontend revisions, and a serving frontend. Only then is the app's Caddy site replaced atomically, validated against the full VPS configuration, and reloaded. Public HTTPS frontend/API/database endpoints must also report the candidate revision before the release state is committed or the prior slot is stopped. A failed validation, reload, public check, or release state write restores the previous site. A durable `recovery-required.json` marker is written before routing changes and cleared only after a successful commit or safe rollback. If routing rollback or state commit fails, both slots remain running and future deployments are blocked. An operator must reconcile Caddy routing with `current.json`, confirm the healthy slot, and explicitly clear that marker before retrying. A failed candidate leaves the active slot and database running. The prior slot stops only after successful promotion and is retained for review. No production deployment deletes volumes, prunes images, or tears down other apps.

To roll back an application release, dispatch the same workflow with the desired earlier `main` commit. It applies the normal backup, migration, and health gates. Review migration compatibility first. Retained database dumps are operational backups, not automatic schema rollback; schedule additional backups for the app's recovery requirements.

## Verification

Safety unit tests require no infrastructure:

```sh
python3 -m unittest discover -s deploy -p 'test_*.py'
```

The isolated smoke test uses the actual API/frontend Docker images and PostgreSQL. It validates SQLx migrations, persistent data, two revisions, real WebAssembly assets, failed-candidate rollback, readable backup archives, and absence of public PostgreSQL ports. It creates a random `rust-smoke-*` Docker project and uses a temporary Caddy configuration for validation only. It neither touches nor reloads production Caddy. Cleanup explicitly removes only its own randomly named resources.

```sh
docker build -f rust/backend/Dockerfile -t app-api:test .
docker build -f rust/frontend/Dockerfile -t app-web:test .
SMOKE_API_IMAGE=app-api:test SMOKE_WEB_IMAGE=app-web:test python3 deploy/smoke-vps.py
```
