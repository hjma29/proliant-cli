# Changelog

All notable changes are documented here. Binaries for Windows, Linux (x86), Linux (ARM64), and macOS are attached to each release.

---

## v1.0.8 — 2026-06-26

### Bug Fixes
- `proliant com login --password`: fixed login failure for external HPE Accounts (non-`@hpe.com`).

---

## v1.0.7 — 2026-06-25

### Bug Fixes
- `proliant com login --password`: fixed login failure when HPE authorization redirects through the Pavo SSO broker (`/sso/continue`).
- `proliant com login --password`: fixed auth code extraction for direct HPE Accounts (no SAML redirect after password).

---

## v1.0.6

### New Features
- Initial public release of unified `proliant` CLI combining iLO Redfish and COM cloud management.
- `proliant ilo`: firmware inventory, update-method classification, network/storage/NIC/CPU/memory inspection, firmware upgrade.
- `proliant com`: device listing, firmware bundles, login/logout.

---
