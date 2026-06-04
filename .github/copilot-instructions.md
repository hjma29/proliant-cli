# pcli — Copilot Instructions

> **This is the single AI context file for this repo** — update it whenever a new bug, gotcha, or schema
> difference is discovered.
> Full human-readable reference: `notes.md` (update that for detailed findings with API/Redfish details).
> User-facing CLI reference: `README.md` (update when commands or flags change).

---

## What this project is

`pcli` — Unified Python CLI for HPE ProLiant server management combining:
- **`pcli ilo`** — Direct iLO Redfish management (firmware inventory, upgrade via HPE SDR)
- **`pcli com`** — HPE Compute Ops Management (COM) cloud API

Replaces two separate tools: `hpeilo` (iLO Redfish) and `hpecom` (COM API).

---

## Repo layout

```
src/pcli/
  cli.py                Top-level entry point — dispatches to ilo/com sub-CLIs
                        Sets _ARGCOMPLETE=2 before delegating for correct tab completion
  ilo/
    cli.py              All pcli ilo commands: get/upgrade subparsers, table printers
    client.py           Async Redfish client (httpx, HTTP/2, session management)
    inventory.py        All read-only Redfish fetches; classify_update_method() BMC/UEFI/OS
    firmware.py         Stage, queue, wait helpers for iLO firmware operations
    sdr.py              HPE SDR fetch, fwpkg parsing, find_upgrades() version matching
    config.py           hosts.yml discovery: env → ~/.config/pcli/ilo/ → ./
  com/
    cli.py              All pcli com commands: get devices/bundles/servers; login/logout
    client.py           Async HTTP COM client (httpx, HTTP/2, pagination)
    auth.py             COMSession — load/save token.json, client credentials refresh
    devices.py          GLP devices API, resolve_user_ids() UUID→email
    firmware.py         FirmwareBundle dataclass, fetch_bundles() from COM API
tests/                  pytest — run with: pytest tests/ -q  (40 tests, must pass before commit)
notes.md                Full findings, gotchas, Redfish + COM API reference
```

---

## Test servers

| Name | IP | Gen | iLO | Serial | COM |
|------|----|----|-----|--------|-----|
| dl380-gen11 | 10.16.41.17 | Gen11 | iLO 6 v1.74 | CNX242032D | ❌ blocked (ProductID=NA) |
| dl345-gen12 | 10.16.41.29 | Gen12 | iLO 7 v1.20 | TWA25345G1208 | ✅ HPECC_USWEST_1 |
| dl325-gen12 | 10.16.41.31 | Gen12 | iLO 7 v1.21 | TWA25325G1206 | ✅ HPECC_USWEST_1 |

Credentials: `Administrator / hpent123`
COM token: `~/.config/hpecom/token.json` (glp_client_id / glp_client_secret for API client auth)

---

## CLI commands (current)

```bash
# iLO commands
pcli ilo get firmwares [--host NAME] [--fields model,bios,ilo,nic-fw,storage-fw]
pcli ilo get update-method [--host NAME]    # BMC/UEFI/OS classification per component
pcli ilo get ilo|network|nic|storage|cpu|memory|full|com|serial|disk-map [--host NAME] [--raw]
pcli ilo upgrade --host NAME [--dry-run] [--reboot] [--component all|ilo|bios|nic|storage]
pcli ilo upgrade components|queue|stage|flash|clear --host NAME
pcli ilo init

# COM commands
pcli com login [--api-client]
pcli com logout
pcli com get devices [--fields NAME,...] [--sort FIELD] [--all]
pcli com get bundles [--gen 10|11|12] [--type base|patch|hotfix] [--all] [--raw]
pcli com get servers  (planned)
```

---

## Critical gotchas — will cause bugs if ignored

**1. Gen12 (iLO 7) OEM actions path differs from Gen11 (iLO 6)**
- Gen11: `svc["Actions"]["Oem"]["Hpe"]`
- Gen12: `svc["Oem"]["Hpe"]["Actions"]`
- Fixed in `ilo/firmware.py::_oem_actions()` — tries Gen12 path first, falls back to Gen11.

**2. NIC firmware is NOT in FirmwareInventory — must use NetworkAdapters**
- Path: `GET /redfish/v1/Chassis/1/NetworkAdapters/{id}` → `Controllers[0].FirmwarePackageVersion`
- `inventory.py::fetch_nic_firmware_inventory()` returns FirmwareInventory-style dicts.

**2b. Gen12 NIC labels can differ between Redfish fields for the same Broadcom family**
- `NetworkAdapters[].Model` may be a generic silicon name like `BCM57414`, while the GUI shows an HPE marketing name such as `Broadcom P225p`.
- For user-facing inventory, prefer the most descriptive label available (`PartNumber` mapping or `SKU`) and always include slot location.
- Example observed on `dl345-gen12`: OCP card reports `BCM57414` + `P10113-001`; PCIe card reports `BCM57414` + `P26264-001` (GUI labels it `P225p`).

**3. BCM NIC SDR filenames have inverted format (version FIRST)**
- Normal: `{model}_{version}.fwpkg` — BCM/NIC: `BCM{version}_{chipmodel}.fwpkg`
- Example: `BCM235.1.164.14_BCM957414A4142HC.fwpkg`

**4. Gen12 (dl325-gen12) Storage has zero members**
- Controllers only appear in FirmwareInventory, not in the Storage sub-tree.
- `fetch_storage_versions()` falls back to FirmwareInventory keyword scan.

**5. Gen11+ Storage controllers are in a sub-collection, not inline**
- Path: `Storage/{id}/Controllers/` (NOT inline `StorageControllers[]` — empty on Gen11+).

**6. Gen12 (iLO 7) ComponentRepository and UpdateTaskQueue return stub Members**
- `Members[]` contains only `{"@odata.id": "..."}` — no inline data (unlike Gen11).
- `get_component_repository()` and `get_task_queue()` expand each stub via individual GETs.

**7. UpdatableBy in task queue must be `["Uefi"]` for BIOS/components — NOT `["Bmc"]`**
- `["Bmc"]` task returns `SystemResetRequired` — BIOS ROM is NOT flashed.
- `["Uefi"]` task: UEFI applies the flash during next POST — this actually works.
- Passing `["Bmc", "RuntimeAgent", "Uefi"]` on iLO 7 splits into two subtasks; the OS_task never fires without SUM agent in OS.
- Rule in `add_to_task_queue()`: iLO filenames → `["Bmc"]`; everything else → `["Uefi"]`.

**8. iLO 7 never marks UEFI tasks Complete after POST flash — stale Pending tasks remain**
- After UEFI flashes a component during POST, the iLO 7 task stays "Pending" forever.
- Do NOT treat stale Pending task as failure — always check actual version from FirmwareInventory.
- `_run_fw_upgrade()` auto-clears all Pending/Complete tasks after post-reboot verification.

**9. iLO 6 HttpPushUri often returns empty 400 — iLO 7 works fine**
- For Gen11 servers, use `stage_from_uri()` (AddFromUri) instead of direct push when possible.
- iLO 7 HttpPushUri is reliable.

**10. BCM957414 NIC stepping chain — cannot jump from 214.x to 235.x directly**
- Factory firmware 214.0.194.0 → requires stepping through 226.1.107.0 before reaching 235.1.164.14.
- `ONFAILEDDEPENDENCY = OmitComponent` in SUM INI handles this gracefully.
- PLDM advances ~1 step per run+reboot cycle.

**11. BCM57414 OCP3 NIC (P10113-001) does NOT support PLDM OOB on iLO 6**
- `dl380-gen11` BCM57414 OCP3 returns "No matching target found" — not in FirmwareInventory.
- Requires in-band OS tools (`bnxtnvm`) for NIC firmware update.
- PCIe variant of same chip supports PLDM OOB.

**12. Gen12+ `.json` sidecar is separate from `.fwpkg` (not embedded in ZIP)**
- Gen11: everything bundled in one signed ZIP (`payload.json`, `.xml`, `readme.txt` + binary).
- Gen12+: ZIP contains **only** the firmware binary. `{stem}.json` ships as a separate sidecar.
- **Reason:** The `.fwpkg` is signed as a whole ZIP blob. Separating the metadata lets HPE update
  supported-model lists, install notes, and release notes without re-signing the firmware binary.
- SHA256 in SPP catalog covers only the `.fwpkg` — sidecar JSON has no checksum (fetch best-effort).
- Gen11 `payload.json` uses **snake_case keys** and `{lang, x_late}` value entries.
  Gen12 sidecar uses **CamelCase keys** and `{Lang, Value}` entries.
- `sdr.py::_fetch_software_ids()` fetches sibling `.json` URL — already correct.
- `pcli spp download` fetches both `{stem}.fwpkg` and `{stem}.json` for every package.
- Never assume JSON is inside the fwpkg ZIP for Gen12.

**13. Autocomplete delegation: set `_ARGCOMPLETE=2` before dispatching sub-CLIs**
- `register-python-argcomplete pcli` sets `_ARGCOMPLETE=1`.
- Top-level `cli.py` must set `os.environ["_ARGCOMPLETE"] = "2"` before calling ilo/com main.
- With `=1`: argcomplete strips "pcli" only → sub-CLI parser gets `["ilo","get","f"]` (WRONG).
- With `=2`: argcomplete strips "pcli ilo" → sub-CLI parser gets `["get","f"]` (CORRECT).

**14. COM firmware bundles API uses old `/compute-ops/` prefix**
- Current working path: `/compute-ops/v1beta2/firmware-bundles`
- `/compute-ops` deprecated April 2025 → should migrate to `/compute-ops-mgmt`
- COM servers API: `/compute-ops-mgmt/v1beta2/servers`

**15. COM FirmwareInventory has no UpdatableBy field**
- COM `firmwareInventory` field on server objects is a plain list `[{name, version, deviceContext}]`.
- No `UpdatableBy` exposed — must be inferred from component name + context patterns.
- `classify_update_method()` in `ilo/inventory.py` contains the classification rules.

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

Token storage: `~/.config/hpecom/token.json`

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
- Always run `pytest tests/ -q` before committing (40 tests, all must pass)
- Use `--dry-run` when testing upgrade paths against live servers
- httpx timeout: connect=10s, read=60s
