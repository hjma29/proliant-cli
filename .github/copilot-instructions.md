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

## Critical gotchas — will cause bugs if ignored

**1. Gen12 (iLO 7) OEM actions path differs from Gen11 (iLO 6)**
- Gen11: `svc["Actions"]["Oem"]["Hpe"]` — Gen12: `svc["Oem"]["Hpe"]["Actions"]`
- Handled in `ilo/firmware.py::_oem_actions()` — tries Gen12 path first, falls back to Gen11.

**2. NIC firmware is NOT in FirmwareInventory — must use NetworkAdapters**
- Path: `GET /redfish/v1/Chassis/1/NetworkAdapters/{id}` → `Controllers[0].FirmwarePackageVersion`
- `inventory.py::fetch_nic_firmware_inventory()` returns FirmwareInventory-style dicts.

**2b. Gen12 NIC labels can differ between Redfish fields for the same Broadcom family**
- `NetworkAdapters[].Model` may be a generic silicon name (e.g. `BCM57414`) while the GUI shows an
  HPE marketing name (e.g. `Broadcom P225p`). Preserve the raw Redfish `Model`/`Name` and use
  `PartNumber` + `Location` to disambiguate cards — do not assume `Model` is the GUI-visible name.

**2c. iLO 6 NIC location can live in the HPE OEM Devices collection**
- Some iLO 6 systems leave `NetworkAdapters[].Location` empty/null. Fallback source:
  `Chassis.Oem.Hpe.Links.Devices` (match back to the NIC by serial number).

**3. BCM NIC SDR filenames have inverted format (version FIRST)**
- Normal: `{model}_{version}.fwpkg` — BCM/NIC: `BCM{version}_{chipmodel}.fwpkg`

**4. Some Gen12 servers report zero Storage members**
- Controllers only appear in FirmwareInventory, not in the Storage sub-tree.
- `fetch_storage_versions()` falls back to a FirmwareInventory keyword scan.

**5. Gen11+ Storage controllers are in a sub-collection, not inline**
- Path: `Storage/{id}/Controllers/` (NOT inline `StorageControllers[]` — empty on Gen11+).

**6. Gen12 (iLO 7) ComponentRepository and UpdateTaskQueue return stub Members**
- `Members[]` contains only `{"@odata.id": "..."}` — no inline data (unlike Gen11).
- `get_component_repository()` and `get_task_queue()` expand each stub via individual GETs.

**7. UpdatableBy in task queue must be `["Uefi"]` for BIOS/components — NOT `["Bmc"]`**
- `["Bmc"]` task returns `SystemResetRequired` and does not flash the ROM.
- `["Bmc", "RuntimeAgent", "Uefi"]` on iLO 7 splits into subtasks that never complete without an
  OS agent. Rule in `add_to_task_queue()`: iLO filenames → `["Bmc"]`; everything else → `["Uefi"]`.

**8. iLO 7 never marks UEFI tasks Complete after POST flash — stale Pending tasks remain**
- Do NOT treat a stale Pending task as failure — always check the actual version from
  FirmwareInventory. `_run_fw_upgrade()` auto-clears Pending/Complete tasks post-verification.

**9. iLO 6 HttpPushUri often returns empty 400 — iLO 7 works fine**
- For Gen11 servers, prefer `stage_from_uri()` (AddFromUri) over direct push when possible.

**10. Gen12+ `.json` sidecar is separate from `.fwpkg` (not embedded in ZIP)**
- Gen11: everything bundled in one signed ZIP. Gen12+: ZIP contains **only** the firmware binary;
  `{stem}.json` ships as a separate sidecar with no checksum.
- Gen11 `payload.json` uses **snake_case keys** / `{lang, x_late}`; Gen12 sidecar uses **CamelCase**
  keys / `{Lang, Value}`. Never assume the JSON is inside the fwpkg ZIP for Gen12.
- `sdr.py::_fetch_software_ids()` and `proliant spp download` already fetch both files correctly.

**11. Autocomplete delegation: set `_ARGCOMPLETE=2` before dispatching sub-CLIs**
- `register-python-argcomplete proliant` sets `_ARGCOMPLETE=1`. Top-level `cli.py` must set
  `os.environ["_ARGCOMPLETE"] = "2"` before calling a sub-CLI's main, so the sub-parser sees
  `["get","f"]` instead of `["ilo","get","f"]`.

**12. COM firmware/servers APIs mix `/compute-ops/` (deprecated) and `/compute-ops-mgmt/` prefixes**
- Firmware bundles: `/compute-ops-mgmt/v1beta2/firmware-bundles`.
- Servers + inventory: `/compute-ops-mgmt/v1/servers[...]` — **v1 only**, the v1beta2 servers path
  does not exist. `_servers_url()` in `com/inventory.py` builds v1 URLs explicitly.

**13. GLP API credential quota — `proliant com login` can silently store no GLP creds**
- HPE caps API credentials per account (~7). If the quota is full, GLP credential creation fails
  and every subsequent `compute-ops-mgmt` call 404s. `_cleanup_stale_proliant_credentials()` in
  `com/login.py` removes old proliant-created credentials before creating a new one on each login
  — keep this cleanup call in place when touching `login.py`.

**14. COM FirmwareInventory has no UpdatableBy field**
- COM `firmwareInventory` on server objects is a plain list `[{name, version, deviceContext}]` — no
  `UpdatableBy`. Must be inferred via `classify_update_method()` in `ilo/inventory.py`.

**15. `proliant com login --password` uses undocumented internal HPE GreenLake endpoints**
- Only the API-client-secret flow is documented by HPE; interactive email/password login
  reverse-engineers the GreenLake web UI's internal Okta IDX flow and can break without notice
  on an HPE-side change. Full flow + recovery notes: `~/work/work-notes/notes-proliant-cli.md` →
  "Okta IDX Login Flows".

**16. `Accept` header on IDX introspect/identify decides whether password login works at all**
- `password_login()` MUST send `Accept: application/json` (`CLASSIC_HEADERS` in `com/login.py`) on
  `idp/idx/introspect` and `idp/idx/identify`. `okta_verify_login()` keeps `IDX_HEADERS` (ion+json).
- Sending `application/ion+json` there instead routes external accounts through `redirect-idp` →
  MTLS certificate auth, and the password authenticator is never offered — login becomes
  impossible even though the rest of the request looks identical. Do not change this header
  without re-testing password login end-to-end.

**17. Inventory.ini parse errors must never raise a raw traceback**
- All three parse sites (`ilo/config.py::load_hosts()`, `oneview/config.py::list_oneview_appliances()`,
  `setup/wizard.py::_load_ini()`) delegate to `common/inventory_errors.py::format_inventory_parse_error()`
  for a friendly `ValueError` message linking to `sample-inventory.ini`. Any new parse site must use
  the same helper instead of calling `configparser` directly.

---

## COM firmware update mechanism (key facts)

COM uses **iLO Repository + UEFI Installation Queue** — NOT SPP ISO, NOT Virtual Media, NOT SUM in OS.

```
COM Cloud → POST /compute-ops-mgmt/v1/jobs  {bundle_id}
         → iLO pulls individual components from HPE CDN (NOT full 8GB ISO)
         → Components staged to iLO flash repository
         → Queued in Installation Queue
         → Server reboots → UEFI flashes during POST
```

COM job templates (durable IDs):
| Job | Template ID |
|-----|-------------|
| `ServerFirmwareUpdate` | `fd54a96c-cabc-42e3-aee3-374a2d009dba` |
| `ServerFirmwareDownload` | `0683ada8-1a89-49dd-bf04-6df715b708a6` |
| `ServerIloFirmwareUpdate` | `94caa4ef-9ff8-4805-9e97-18a09e673b66` |
| `GroupFirmwareUpdate` | `91159b5e-9eeb-11ec-a9da-00155dc0a0c0` |

Key job params: `bundle_id`, `wait_for_power_off_or_reboot`, `install_sw_drivers`.

**OS NOT required.** Components with `UpdatableBy: RuntimeAgent` only (Intel NICs, BCM OCP3) cannot be updated via COM without a running OS.

---

## UpdatableBy classification (BMC / UEFI / OS)

Source: HPE fwpkg `payload.json` flags + HPE firmware blog Part 1&2.

| payload.json | Method |
|---|---|
| `UefiFlashable: false`, `ResetRequired: false` | **BMC** — iLO flashes directly (iLO firmware) |
| `UefiFlashable: true`, `ResetRequired: true` | **UEFI** — flashed during POST reboot (BIOS, controllers) |
| `PLDMImage: true` | **UEFI** via PLDM OOB (NIC, drive, backplane) |
| `.exe`/`.rpm` only, `UpdatableBy: RuntimeAgent` | **OS** — needs iSUT/SUM in running OS |

Classification rules in `ilo/inventory.py::classify_update_method()`:
- iLO firmware → BMC (no reboot)
- System ROM/BIOS, CPLD, Power controllers → UEFI (reboot)
- BCM/Mellanox/NVIDIA in OCP slot → OS (no PLDM OOB for OCP3 NICs)
- BCM/Mellanox/NVIDIA in PCIe slot → UEFI (PLDM capable)
- Intel NICs → OS (RuntimeAgent only)

---

## HPE SDR

URL: `https://downloads.linux.hpe.com/SDR/repo/fwpp-gen{N}/{YYYY.MM.00.00}/`
`sdr.py::latest_pack_url(gen)` resolves the latest pack automatically.

---

## Upgrade flow (iLO Redfish)

```
stage_from_uri()     # POST Oem/Hpe AddFromUri — iLO downloads .fwpkg from URL
wait_for_stage()     # poll ComponentRepository until filename appears
add_to_task_queue()  # POST UpdateTaskQueue — schedule for flash
[reboot]             # iLO applies on next POST
```

Order in `_run_fw_upgrade()`: iLO (priority 0) → BIOS (priority 1) → others (priority 2).
After iLO update, wait ~90s for iLO restart before continuing.

---

## COM API base URLs

| Tier | URL | Auth |
|------|-----|------|
| COM regional | `us-west.api.greenlake.hpe.com` | Bearer (client credentials) |
| GLP global | `global.api.greenlake.hpe.com` | Bearer (GLP token) |
| ui-doorway | `aquila-user-api.common.cloud.hpe.com` | Bearer + ccs-session cookie |

Token storage: `~/.config/proliant-cli/com/token.json`

---

## HPE iLO Redfish API Reference

### iLO 7 v1.20 (Gen12)

| Domain | URL |
|---|---|
| Resource Map | https://servermanagementportal.ext.hpe.com/docs/redfishservices/ilos/ilo7/ilo7_120/ilo7_resmap120 |
| Update Service | https://servermanagementportal.ext.hpe.com/docs/redfishservices/ilos/ilo7/ilo7_120/ilo7_other_resourcedefns120#updateservice |
| Network (NIC) | https://servermanagementportal.ext.hpe.com/docs/redfishservices/ilos/ilo7/ilo7_120/ilo7_network_resourcedefns120 |
| Storage | https://servermanagementportal.ext.hpe.com/docs/redfishservices/ilos/ilo7/ilo7_120/ilo7_storage_resourcedefns120 |

### iLO 6 v1.75 (Gen11)

| Domain | URL |
|---|---|
| Resource Map | https://servermanagementportal.ext.hpe.com/docs/redfishservices/ilos/ilo6/ilo6_175/ilo6_resmap175 |
| Update Service | https://servermanagementportal.ext.hpe.com/docs/redfishservices/ilos/ilo6/ilo6_175/ilo6_other_resourcedefns175#updateservice |
| Network (NIC) | https://servermanagementportal.ext.hpe.com/docs/redfishservices/ilos/ilo6/ilo6_175/ilo6_network_resourcedefns175 |

### COM API

| Resource | URL |
|---|---|
| Developer Portal | https://developer.greenlake.hpe.com/docs/greenlake/services/compute-ops-mgmt/ |
| Firmware bundles | https://developer.greenlake.hpe.com/docs/greenlake/services/compute-ops-mgmt/public/openapi/compute-ops-mgmt-latest/firmware-bundles-v1beta2/ |
| Jobs | https://developer.greenlake.hpe.com/docs/greenlake/services/compute-ops-mgmt/jobs/ |
| Servers | https://developer.greenlake.hpe.com/docs/greenlake/services/compute-ops-mgmt/public/openapi/compute-ops-mgmt-latest/servers-v1/ |

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
