# proliant — Copilot Instructions

> Engineering context for contributors and AI agents working on this repo.
> Update whenever a new coding gotcha or schema difference is discovered.
> User-facing CLI reference: `README.md` (update when commands or flags change).
> Deep debugging narratives, incident write-ups, and troubleshooting procedures
> live in `~/work/work-notes/notes-proliant-cli.md` (private notes repo) — do
> not duplicate them here. This file stays a short, current reference for
> facts that affect how code must be written, not how to diagnose problems.

---

## What this project is

`proliant` — Unified Python CLI for HPE ProLiant server management combining:
- **`proliant ilo`** — Direct iLO Redfish management (firmware inventory, upgrade via HPE SDR)
- **`proliant com`** — HPE Compute Ops Management (COM) cloud API
- **`proliant oneview`** — HPE OneView appliance management
- **`proliant spp`** — HPE Service Pack for ProLiant (SPP) release inspection/diff

---

## Repo layout

```
src/proliant/
  cli.py                Top-level entry point — dispatches to ilo/com/oneview/spp/qs/setting sub-CLIs
                        Sets _ARGCOMPLETE=2 before delegating for correct tab completion
  common/                Shared helpers (config_dir(), inventory_errors.py, etc.)
  ilo/
    cli.py              All proliant ilo commands: servers/firmware/nic/storage/... subparsers
    client.py           Async Redfish client (httpx, HTTP/2, session management)
    inventory.py        All read-only Redfish fetches; classify_update_method() BMC/UEFI/OS
    firmware.py         Stage, queue, wait helpers for iLO firmware operations
    sdr.py              HPE SDR fetch, fwpkg parsing, find_upgrades() version matching
    config.py           inventory.ini loader: PCLI_CONFIG env → ./inventory.ini → ~/.config/proliant-cli/inventory.ini
  com/
    cli.py              All proliant com commands: devices/servers/bundles/workspaces; login/logout
    client.py           Async HTTP COM client (httpx, HTTP/2, pagination)
    auth.py / login.py  COMSession — load/save token.json, Okta + GLP client-credentials auth
    devices.py          GLP devices API, resolve_user_ids() UUID→email
    firmware.py         FirmwareBundle dataclass, fetch_bundles() from COM API
  oneview/
    cli.py              proliant oneview commands: servers/firmware/networks/profiles/appliances
    config.py           OneView appliance sections in the same inventory.ini ([oneview] or type = oneview)
  spp/                  proliant spp list/inspect/diff — SPP catalog + fwpkg inspection
  setup/
    wizard.py           `proliant setup` interactive inventory.ini wizard + malformed-file recovery
  qs/                    proliant qs — HPE QuickSpecs lookup
  setting/               proliant setting — local CLI settings
tests/                  pytest — run with: pytest tests/ -q  (must pass before commit)
sample-inventory.ini    Working example inventory.ini, linked from parse-error messages
```

---

## CLI commands (current)

Full command reference lives in `README.md`. Top-level groups:

```bash
proliant setup                          # Interactive inventory.ini wizard

proliant ilo servers|firmware|nic|storage|cpu|memory|power|boot ...
proliant ilo firmware upgrade <host> [--dry-run] [--reboot]

proliant com login [--api-client] / logout
proliant com devices|servers|bundles|workspaces|reports ...

proliant oneview servers|firmware|networks|networksets|uplinksets|server-profiles|enclosures|mac|reports ...
proliant oneview upgrade readiness|cleanup
proliant oneview appliances list|use <name>

proliant spp list|inspect|diff
proliant version
```

---

## Critical gotchas & reference details

Component-level Redfish/COM schema quirks, firmware task-queue rules, job template IDs,
API base URLs, HPE SDR/Redfish doc links, and other implementation-detail reference
material live in `~/work/work-notes/notes-proliant-cli.md` — check that file before
touching `ilo/`, `com/`, `oneview/`, or `common/inventory_errors.py` code. It is kept
current and is the source of truth for these facts, not this document. Key sections
there: "4. COM API Implementation", "9. classify_update_method() Rules", "10. Lessons
Learned".

Two facts worth remembering without opening that file:
- COM token cache: `~/.config/proliant-cli/com/token.json`.
- `proliant ilo` upgrade order is iLO first, then BIOS, then everything else, with a
  ~90s wait after an iLO flash for it to restart before continuing.

---

## Coding conventions

- Navigate URIs from Redfish root helpers (`get_system_uri`, `get_chassis_uri`) — never hardcode paths
- Always run `pytest tests/ -q` before committing — all tests must pass
- Use `--dry-run` when testing upgrade paths against live servers
- httpx timeout: connect=10s, read=60s

## Release process

Before tagging a release, always update `CHANGELOG.md` first:
- Add a new `## vX.Y.Z — YYYY-MM-DD` section at the top (above previous releases).
- Sections: **New Features** first (if any), then **Bug Fixes**, then **Enhancements** — only include non-empty sections.
- One brief user-facing bullet per change — what broke / what's new, no internal implementation details.
- CI extracts the section automatically and uses it as the GitHub Release body.

Example entry:
```markdown
## v1.0.9 — 2026-07-01

### New Features
- `proliant ilo get disk-map`: show physical disk slot layout per server.

### Bug Fixes
- `proliant com login`: fixed token refresh when ccs-session expires.
```
