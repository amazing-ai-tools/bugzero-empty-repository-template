# BugZero Rust application template

The starter for newly provisioned BugZero apps uses the finance.app technology
stack: an Axum/Tokio/SQLx API and a Leptos/WebAssembly frontend. It contains no
finance-specific features and does not migrate existing applications.

| Component | Location | Toolchain |
| --- | --- | --- |
| API (`app-api`) | `rust/backend` | Axum 0.8, Tokio 1, SQLx 0.8 |
| Web (`app-web`) | `rust/frontend` | Leptos 0.8 CSR, Trunk 0.22.0 |
| Database | VPS persistent volume | PostgreSQL 17 |
| Builds | Workspace `Cargo.lock` | Rust 1.93.1 |

## Local development

Install the toolchain from `rust-toolchain.toml` and Trunk:

```sh
rustup toolchain install 1.93.1 --profile minimal --component rustfmt,clippy --target wasm32-unknown-unknown
cargo install trunk --version 0.22.0 --locked
```

Start an isolated local PostgreSQL database. Copy `.env.example` to `.env`, set
`DATABASE_URL`, then load the local values and start the API:

```sh
set -a
. ./.env
set +a
cargo run --locked -p app-api
```

In another terminal, run `trunk serve --config rust/frontend/Trunk.toml`. Trunk
proxies `/api/` to the local API. The frontend reads its display name from
`app-config.json` and makes a real same-origin database readiness request.

The API binds to `BIND_ADDR` (default `0.0.0.0:8000`). `GET /health` checks
liveness. `GET /health/db` verifies PostgreSQL and returns HTTP 200 when ready
or 503 when unavailable. Both return `status` and `revision`; `APP_REVISION`
overrides the revision embedded during the build. No credentials enter the
browser bundle.

SQLx migrations in `rust/backend/migrations` run on startup. Use
`cargo run --locked -p app-api -- --migrate-only` to migrate and exit. Add new
numbered migration files for schema changes instead of editing deployed files.

## Verification

```sh
cargo fmt --all -- --check
cargo clippy --locked -p app-api --all-targets -- -D warnings
cargo test --locked -p app-api
cargo test --locked -p app-web --lib --tests
# TEST_DATABASE_URL must point to an isolated test database, never production.
TEST_DATABASE_URL=postgres://app:local-development-only@127.0.0.1:5432/app_test cargo test --locked -p app-api --test health -- --ignored
python3 -m unittest discover -s deploy -p 'test_*.py'
cargo build --locked --release -p app-api
cargo clippy --locked -p app-web --target wasm32-unknown-unknown -- -D warnings
trunk build --config rust/frontend/Trunk.toml --release --locked
```

CI runs pull requests on hosted runners with read-only repository access and
no deployment credentials. Trusted `main` pushes and manual `main` CI runs use
the app's provisioned VPS runner (`RUNNER_LABEL`). CI includes PostgreSQL-backed
migration and readiness checks. CI never deploys on push.
Trusted CI starts once provisioning configures the app's `RUNNER_LABEL`; the
public seed repository skips that job while it has no runner configured.

## Provisioning and production

BugZero provisions new app repositories from this template, installs a dedicated
runner label, and deploys to the VPS with PostgreSQL persistence and Caddy TLS.
The API and frontend run in separate containers. The frontend serves the
WebAssembly bundle, public runtime `app-config.json`, and same-origin `/api/`.
Production configuration and secrets come from the app's vault.
The public runtime configuration includes `display_name`, `domain`, `revision`
and the optional `bugzero_app_key`/`bugzero_widget_url`. The widget uses its
public app key and loads only an HTTPS script URL. API tokens and vault secrets
must never be placed in that public configuration.

Release promotion uses the manual deployment workflow with an explicit reviewed
`main` commit SHA. Existing applications retain their current runtimes and
deployment configuration. See `deploy/` for the deployment implementation and
operational safeguards.

GPT 6.1 remains the BugZero default for new apps, chats and threads. Explicit
model choices and existing conversations retain their configured models; no
model setting is baked into this application template.

## Languages

Every new app includes English (`en`), French (`fr`) and Portuguese (`pt`).
The accessible selector sits in the page header. An explicit choice persists
in local browser storage; otherwise the first supported browser language is
used (including regional variants such as `pt-BR`), with English as fallback.
The interface also works when local storage is unavailable. All starter copy,
health states, revision labels and no-script messages are translated. Runtime
app display names remain the app's own name. The document language and fallback
title update when the user switches languages.

Add template text to the typed catalog in `rust/frontend/src/i18n.rs` and supply
all three translations. API status is kept separately from translated text so
late responses and language changes cannot restore the wrong language.

CI tests the actual compiled WebAssembly UI in Chromium: every language,
loading/ready/error states, keyboard interaction, reload persistence, browser
preferences, denied storage, no-script fallback and the 320px layout. To run
locally after the release Trunk build (Node 18+ required only for tests):

```sh
npm install --prefix /tmp/template-browser --no-package-lock --no-save playwright@1.58.2
/tmp/template-browser/node_modules/.bin/playwright install chromium
NODE_PATH=/tmp/template-browser/node_modules node tests/browser.cjs
```
