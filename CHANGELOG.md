# Changelog

All notable changes are documented here. Binaries for Windows, Linux (x86), Linux (ARM64), and macOS are attached to each release.

---

## v1.0.8 — 2026-06-26

### Bug Fixes
- `proliant com login --password`: fixed login failure for external (non-`@hpe.com`) HPE Accounts. The Okta IDX introspect/identify calls now use `Accept: application/json` (classic) instead of `ion+json` (OIE), which was silently routing external accounts to MTLS certificate auth instead of password login.

---

## v1.0.7 — 2026-06-25

### Bug Fixes
- `proliant com login --password`: added Pavo SSO broker (`sso-resolve`) step to resolve the Okta state token when authorization redirects to the `/sso/continue` React SPA. Previously the login flow failed to find the state token and aborted.
- `proliant com login --password`: fixed code extraction path for direct HPE Accounts — `success.href` now redirects straight to the `callback?code=` URL without a SAML form.

---

## v1.0.6

### Enhancements
- Initial public release of unified `proliant` CLI combining iLO Redfish and COM cloud management.
- `proliant ilo get firmwares`, `get update-method`, `get network`, `get storage`, `get nic`, `get full`, and related subcommands.
- `proliant com get devices`, `get bundles`, `login`, `logout`.

---
