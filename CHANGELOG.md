# Changelog

All notable changes are documented here. Binaries for Windows, Linux (x86), Linux (ARM64), and macOS are attached to each release.

---

## v1.0.19 — 2026-07-07

### New Features
- `proliant oneview upgrade readiness`: read-only pre-upgrade check. Reports the appliance version, the supported Synergy Composer upgrade path (recommended next hop + full milestone chain to the latest release), and a PASS/WARN/FAIL assessment of disk space, memory/CPU, active alerts, backup freshness, logical interconnect consistency, and interconnect redundancy.
- `proliant oneview upgrade cleanup`: reclaim appliance disk by removing unused firmware baselines (SPP/SSP) not assigned to any logical enclosure, logical interconnect, or server profile. Newer unused baselines are kept as upgrade targets. Dry-run preview by default; `--yes` performs the deletion. Repository-only — never touches running enclosures or interconnects.

### Enhancements
- `proliant oneview upgrade cleanup`: prunable and external-repository baseline tables are now sorted oldest -> newest by release date, instead of the API's arbitrary member order, making it easier to scan chronologically.

### Bug Fixes
- `proliant oneview upgrade cleanup`/`readiness`: firmware baselines that only exist in an external repository (e.g. an SPP repository added under Firmware Bundles > External Repositories) are no longer counted as reclaimable or attempted for deletion. OneView always rejects deleting these (HTTP 400 "exists only in the external repository...") and their reported size isn't appliance disk at all, so `cleanup` used to promise disk it could never free and spam a failed-deletion line per baseline. They're now listed separately as informational "not deletable via OneView" entries.

---

## v1.0.18 — 2026-07-06

### New Features
- `proliant setup`: new guided menu for managing your iLO servers and OneView appliance in `inventory.ini` — view, add, edit, or delete entries, with each connection live-tested before it's saved. Merges into any existing config instead of overwriting it. `proliant ilo init` still works as a shortcut to the same wizard.

### Enhancements
- `proliant setup`: the entries table now has a live "Status" column (Reachable / Timeout / Unreachable / Auth failed) instead of guessing from config alone. All entries are tested in parallel when the wizard starts (so total wait time doesn't scale with the number of servers), and re-tested automatically right after you add, edit, or delete an entry.
- Windows installer: the "Finished" page now has a checked-by-default "Launch a new terminal" option, so you can jump straight into using `proliant` instead of having to go find/open a shell yourself. Prefers Windows Terminal, falling back to PowerShell if Windows Terminal isn't installed.

### Bug Fixes
- Windows installer: the post-install confirmation message is now a simple "installed successfully" + install location — dropped the extra getting-started/tab-completion text, which isn't needed now that completion is set up automatically.
- `proliant qs` (QuickSpecs browser) is temporarily disabled — rendering wasn't reliable enough across HPE's HTML and PDF QuickSpec formats. The command now prints a clean "currently unavailable" message instead of a broken table, and no longer appears in `--help`, tab completion, or the README/docs. The underlying module isn't deleted (may return once rendering is more reliable) but its dependencies are no longer bundled into the release binaries, shrinking their size.

---

## v1.0.17 — 2026-07-05

### Bug Fixes
- Fixed tab completion not working after a fresh install, even in a brand-new PowerShell window. The GUI installer (`proliant-cli-windows-setup.exe`) never wrote anything into `$PROFILE` itself — completion was only ever set up as a side effect of running a `proliant` command for the first time, so a user who installed and went straight to `proliant i<Tab>` without running any command first got nothing. The installer now triggers that one-time setup itself right after install, so tab completion is already working the first time you open a terminal.

---

## v1.0.16 — 2026-07-05

### New Features
- `proliant update` (Windows): before installing, now shows the target version, install directory, and how to uninstall later, and asks for confirmation. Use `-y`/`--yes` to skip the prompt for scripted/unattended use.

### Bug Fixes
- Fixed a rare crash (`ValueError: I/O operation on closed file`) that could happen if the CLI's internal startup routine ran more than once in the same process — hardened the Windows UTF-8 output setup to reconfigure the existing stream instead of creating a duplicate one.
- `proliant ilo`/`proliant oneview`: commands no longer appear to hang with no feedback when given a wrong or unreachable host IP. A "Connecting to..." hint now shows while logging in (it disappears once a real response comes back), the initial connection now fails within ~8 seconds instead of up to 60, and a connect-timeout error is now reported cleanly instead of leaking as an unhandled traceback. Also fixed a bug where iLO requests issued after login had no timeout at all, so a server that stopped responding mid-session could hang indefinitely.
- Fresh installs on a clean machine now tell you what to do next: the installer (both the interactive GUI and `install.ps1`) shows an install location and a "getting started" checklist (`proliant --help`, `proliant ilo init`) instead of just disappearing, and the one-time "tab completion enabled" message now also mentions running `. $PROFILE` to load it in the *current* window instead of only suggesting a new one.

---

## v1.0.15 — 2026-07-03

### Enhancements
- Tab completion is significantly faster:
  - Top-level completion (e.g. `proliant i<TAB>` → `ilo`) now answers instantly from PowerShell itself instead of launching a new `proliant` process every keystroke — cut from ~700-850ms to well under 50ms.
  - Completions that look up live data (OneView/iLO/COM object names, SPP versions) are now cached for a few seconds, so repeatedly pressing `<TAB>` while typing the same command doesn't re-fetch from the network or device each time.

### Bug Fixes
- PowerShell profile setup: fixed a bug where re-running `proliant update` could leave a duplicate copy of the "show completion menu" tweak in your PowerShell profile. Existing profiles are automatically cleaned up the next time completion is refreshed.
- `proliant update` (Windows): tab-completion improvements now reach existing installs automatically after an update, instead of only applying to brand-new installs.

---

## v1.0.14 — 2026-07-02

### Bug Fixes
- `proliant --help`: no longer lists `install-completion` as a runnable command — it never existed as a subcommand, so running it failed with "unknown namespace". Tab completion is set up by the installer instead.
- `proliant com` (devices, bundles, etc.): a revoked or rotated auto-managed GLP API credential from a previous `proliant com login` now shows "Session expired ... run 'proliant com login'" instead of a raw HPE JSON error.
- `proliant update` (Windows): the installer now shows a confirmation dialog with the installed version and location when finishing a silent update, instead of just disappearing with no feedback.

---

## v1.0.13 — 2026-07-02

### Bug Fixes
- `proliant com login --password`: the password prompt now masks input — every character you type **or paste** shows a matching `*`. Previously nothing appeared as you typed, making the prompt look frozen.
- `proliant com workspaces`: listing workspaces now works right after an OAuth/email login instead of failing with "requires a user OAuth token session".

### Enhancements
- `proliant com login`: when your account has only one workspace, the CLI now logs in directly and tells you which workspace it selected instead of showing a single-item picker.

---

## v1.0.12 — 2026-07-02

### Enhancements
- Windows now installs via a signed GUI installer (`proliant-cli-windows-setup.exe`) into `C:\Program Files\proliant-cli`, with an Add/Remove Programs entry and machine PATH setup. This replaces the single self-extracting `.exe`, which some endpoint security tools (Defender, CrowdStrike Falcon) flagged.
- `proliant update` on Windows now downloads and runs the installer instead of swapping the running binary in place.

### Bug Fixes
- `proliant oneview`: missing config now shows a clean "run init" message instead of a raw Python traceback, and reads inventory from the same `~/.config/proliant-cli` location as the other commands.

---

## v1.0.11 — 2026-07-01

### New Features
- `proliant oneview enclosures describe`: show GUI-like enclosure bay layout and hardware detail tables.
- `proliant oneview server-profiles describe`: show detailed profile, firmware, connection, boot, BIOS, and address settings.
- `proliant oneview mac describe`: trace a MAC with a diagram focused on the learned endpoint or uplink.

### Bug Fixes
- PowerShell completion now handles namespace delegation, trailing spaces, and values containing spaces or commas more reliably.
- Sentry telemetry now drops expected user/environment errors such as authentication failures, timeouts, missing config, and invalid input.
- OneView requests now report connection and timeout failures as clean CLI errors.

### Enhancements
- OneView MAC list output hides server profile columns when entries are not related to a server profile.
- OneView `--json` can be used before or after subcommand arguments.
- OneView output uses cleaner status coloring, compact server names, and richer network, enclosure, and profile details.

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
