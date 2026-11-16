# Trilingual Rust template continuation

Goal: every newly generated Rust application includes English, French and Portuguese.
The approved Rust/VPS architecture and existing app/model compatibility remain intact.

Design: a typed Rust locale catalog owns all template UI strings, including the
fallback app name, status states, revision prefix and accessible language label.
A native select in a responsive page header uses language autonyms. Choose a
supported saved preference first, then the browser's ordered language list,
then English. Store user changes locally, update document language/title, and
keep the selected locale when asynchronous health/config responses arrive.
Storage denial must not prevent rendering. Names provided by the app remain
proper names. The no-script fallback is present in all three languages.

Alternatives considered: a runtime translation dependency adds unnecessary
requests and failure states for a small fixed catalog; three separate pages
would duplicate application state and reload on every selection. Use a typed
catalog and one reactive Leptos view instead.

- [x] Add native behavioral tests for preference resolution and each UI state.
- [x] Run failing tests, implement the catalog and Leptos header integration.
- [x] Verify formatting, native tests, wasm Clippy and locked Trunk release.
- [x] Exercise the real WASM in a browser for loading/ready/error, all locales,
  storage denial, keyboard changes, reload persistence and 320px layout.
- [ ] Review, link a continuation issue/PR targeting main from this thread branch,
  merge and verify trusted main CI. Confirm the published generation source.
- [ ] Complete control-plane deployment verification and exact release evidence.

Validation before publication: six frontend native tests, wasm Clippy and
locked Trunk release passed. Twelve real Chromium/WASM scenarios passed with
no page errors; deployment safety tests passed 15/15. Actionlint and diff checks
passed. Independent review has no remaining blocking findings.
