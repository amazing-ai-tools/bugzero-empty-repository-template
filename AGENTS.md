# Application development

New apps use Rust throughout: Axum 0.8, Tokio 1 and SQLx 0.8 for the API;
Leptos 0.8 with client-side rendering and WebAssembly for the frontend. Rust
1.93.1 and Trunk 0.22.0 are pinned. Keep Cargo.lock committed and use --locked.
Do not replace this stack with Python or TypeScript application scaffolding.

The workspace contains app-api in rust/backend and app-web in rust/frontend.
Production runs on the VPS. PostgreSQL state belongs to the app's persistent
volume. API secrets stay server-side; app-config.json is public configuration.
The frontend uses same-origin /api requests; the web proxy removes /api before
forwarding them to Axum. Liveness is /health; database readiness is /health/db.

Add schema changes as new SQLx migrations in rust/backend/migrations. Preserve
deployed migration files. Startup and --migrate-only both apply embedded
migrations. Deployment takes a database backup before applying migrations.
Never print or commit credentials, env files or database backups.

Before proposing a change, run cargo fmt --all -- --check, the API tests and
Clippy, and a locked release WebAssembly build. Run the ignored integration test
against an isolated PostgreSQL database via TEST_DATABASE_URL. Pull requests
use hosted CI; only trusted main refs use the per-app self-hosted VPS runner.
Deployments are manual with an explicit reviewed main-branch target_sha.

BugZero controls model defaults. Preserve GPT 6.1 as the default for new apps,
chats and threads, and preserve explicit selections and existing conversations.
The application skeleton does not hardcode AI provider or model settings.


The starter UI supports en, fr and pt. Keep every template-owned string in the
typed `rust/frontend/src/i18n.rs` catalog and supply all three translations.
Preserve the header selector, browser/saved preference resolution, document
language and storage-denial fallback. Run frontend native tests with
`cargo test --locked -p app-web --lib --tests` and the compiled browser checks
in `tests/browser.cjs` as documented in README.
