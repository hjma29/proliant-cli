# Changelog

All notable changes are documented here. Binaries for Windows, Linux (x86), Linux (ARM64), and macOS are attached to each release.

---

## v1.0.9 — 2026-06-26

### New Features
- `proliant setting telemetry on|off`: enable or disable Sentry error telemetry via marker files.
- `proliant setting uninstall`: remove all proliant-cli config and cache directories.
- `proliant ilo init` now creates `inventory.ini` in `~/.config/proliant-cli/` instead of the current directory.

### Enhancements
- Renamed `proliant config` subcommand to `proliant setting`.
- Standardised config and cache directories to `~/.config/proliant-cli/` and `~/.cache/proliant-cli/` across all platforms.
- SPP and QuickSpecs caches now stored under `~/.cache/proliant-cli/spp/` and `~/.cache/proliant-cli/qs/`.
- Telemetry now controlled by marker files (`telemetry-enabled`/`telemetry-disabled`) in addition to `PROLIANT_TELEMETRY` env var.

---

## v1.0.8 — 2026-06-26

### Bug Fixes
- `proliant com login --password`: fixed login failure for external HPE Accounts (non-`@hpe.com`).

---

## v1.0.7 — 2026-06-25

### Bug Fixes
- `proliant com login --password`: fixed login failure on accounts that use the HPE GreenLake SSO flow.

---

## v1.0.6

### New Features
- Initial public release of unified `proliant` CLI combining iLO Redfish and COM cloud management.
- `proliant ilo`: firmware inventory, update-method classification, network/storage/NIC/CPU/memory inspection, firmware upgrade.
- `proliant com`: device listing, firmware bundles, login/logout.

---
