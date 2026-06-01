# pcli — Project Notes & Technical Reference

> **AI context lives in `.github/copilot-instructions.md`** — automatically loaded by Copilot each session.
> This file is the human-readable deep reference: Redfish quirks, COM API lessons, upgrade flows.
> User-facing CLI reference: `README.md`.

---

## Table of Contents

- [1. Test Servers & COM Workspace](#1-test-servers--com-workspace)
- [2. Project Layout](#2-project-layout)
- [3. Firmware Update Methods — BMC / UEFI / OS](#3-firmware-update-methods--bmc--uefi--os)
  - [How the Classification Works](#how-the-classification-works)
  - [payload.json Inside fwpkg — The Source of Truth](#payloadjson-inside-fwpkg--the-source-of-truth)
  - [pcli ilo get update-method](#pcli-ilo-get-update-method)
- [4. COM Firmware Update Mechanism](#4-com-firmware-update-mechanism)
  - [COM Does NOT Download the Entire SPP ISO](#com-does-not-download-the-entire-spp-iso)
  - [No SUM Inside iLO — Two Native Agents](#no-sum-inside-ilo--two-native-agents)
  - [COM Job Templates](#com-job-templates)
  - [Components Requiring OS (RuntimeAgent)](#components-requiring-os-runtimeagent)
  - [SPP Bundle API — No Per-Component Data](#spp-bundle-api--no-per-component-data)
- [5. Querying Components — Gen11 vs Gen12 Redfish Differences](#5-querying-components--gen11-vs-gen12-redfish-differences)
  - [Storage Controllers](#storage-controllers)
  - [Full Firmware Inventory (FirmwareInventory)](#full-firmware-inventory-firmwareinventory)
  - [iLO Version](#ilo-version)
  - [BIOS / System ROM](#bios--system-rom)
  - [NICs / Network Adapters](#nics--network-adapters)
  - [CPU Microcode](#cpu-microcode)
  - [Memory / DIMMs](#memory--dimms)
- [6. PLDM — How Gen12 Firmware Updates Work](#6-pldm--how-gen12-firmware-updates-work)
  - [SUM Remote / iLO-Connected Mode](#sum-remote--ilo-connected-mode)
  - [SUM CLI + SPP ISO on Jumpbox](#sum-cli--spp-iso-on-jumpbox)
- [7. Recommended Firmware Upgrade Order](#7-recommended-firmware-upgrade-order)
- [8. Which Firmware Can (and Cannot) Be Upgraded via iLO](#8-which-firmware-can-and-cannot-be-upgraded-via-ilo)
  - [HPE SDR — Software Delivery Repository](#hpe-sdr--software-delivery-repository)
- [9. High-Level Upgrade Steps](#9-high-level-upgrade-steps)
- [10. iLO 6 vs iLO 7 — UpdateService Schema Differences](#10-ilo-6-vs-ilo-7--updateservice-schema-differences)
- [11. Observed Server-Specific Notes](#11-observed-server-specific-notes)
- [12. BCM957414 NIC — Stepping Chain & SUM CLI Lessons (dl325-gen12)](#12-bcm957414-nic-firmware--stepping-chain--sum-cli-lessons-dl325-gen12)
- [13. COM Auth Architecture](#13-com-auth-architecture)
- [14. COM API Endpoints](#14-com-api-endpoints)
- [15. COM Device Onboarding](#15-com-device-onboarding)
- [16. Quick Reference — Redfish Endpoints](#16-quick-reference--redfish-endpoints)
- [17. Lessons Learned (All)](#17-lessons-learned-all)

---

## 1. Test Servers & COM Workspace

| Name | IP | Gen | iLO | Serial | Part# | COM |
|------|----|-----|-----|--------|-------|-----|
| dl380-gen11 | 10.16.41.17 | Gen11 | iLO 6 v1.74 | CNX242032D | N/A | ❌ blocked (ProductID=NA) |
| dl345-gen12 | 10.16.41.29 | Gen12 | iLO 7 v1.20 | TWA25345G1208 | P81949-B21 | ✅ HPECC_USWEST_1 |
| dl325-gen12 | 10.16.41.31 | Gen12 | iLO 7 v1.21 | TWA25325G1206 | P81967-B21 | ✅ HPECC_USWEST_1 |

Credentials: `Administrator / hpent123`
COM token: `~/.config/hpecom/token.json` (glp_client_id / glp_client_secret for API client auth)

---

## 2. Project Layout

```
src/pcli/
  cli.py                Top-level entry — dispatches ilo/com, sets _ARGCOMPLETE=2
  ilo/
    cli.py              All pcli ilo commands: get/upgrade subparsers, table printers
    client.py           Async Redfish client (httpx, HTTP/2, session management)
    inventory.py        Read-only Redfish fetches; classify_update_method()
    firmware.py         Stage, queue, wait helpers for iLO firmware operations
    sdr.py              HPE SDR fetch, fwpkg parsing, find_upgrades()
    config.py           hosts.yml: env → ~/.config/pcli/ilo/ → ./
  com/
    cli.py              All pcli com commands: get devices/bundles/servers; login/logout
    client.py           Async HTTP COM client (httpx, HTTP/2, pagination)
    auth.py             COMSession — load/save token.json, client credentials refresh
    devices.py          GLP devices API, resolve_user_ids() UUID→email
    firmware.py         FirmwareBundle dataclass, fetch_bundles()
tests/                  pytest — all 40 tests must pass before commit
notes.md                This file
```

---

## 3. Firmware Update Methods — BMC / UEFI / OS

HPE components can be updated by three different agents:

| Method | Agent | Trigger | Reboot needed |
|--------|-------|---------|---------------|
| **BMC** | iLO flashes directly | Immediate via Redfish | ❌ No |
| **UEFI** | UEFI reads queue during POST | Next server reboot | ✅ Yes |
| **OS** | SUM/iSUT running in the OS | While OS is running | Usually yes |

### How the Classification Works

The authoritative data is in `payload.json` inside each `.fwpkg` file (see below).
For iLO Redfish FirmwareInventory — which lacks this field — `pcli` infers the method
from component name patterns.

Rules in `ilo/inventory.py::classify_update_method()`:

| Component pattern | Method | Logic |
|---|---|---|
| `ilo`, `integrated lights-out`, `ilo management controller` | **BMC** | Always iLO self-update |
| `system rom`, `system bios`, `bios` | **UEFI** | UEFI applies at POST |
| `smart array`, `mr4`, `ns204`, `boot controller`, `storage controller` | **UEFI** | PLDM via UEFI agent |
| `power management`, `power supply`, `cpld`, `upb`, `ubm` | **UEFI** | Low-level, UEFI or BMC secondary |
| `bcm`, `broadcom`, `mellanox`, `connectx`, `nvidia` + OCP context | **OS** | OCP3 NICs: no PLDM OOB |
| `bcm`, `broadcom`, `mellanox`, `connectx`, `nvidia` + PCIe context | **UEFI** | PCIe NICs: PLDM OOB capable |
| `intel `, `intel(r)` (trailing space, NOT `intelligent`) | **OS** | RuntimeAgent only |
| Fallback | **UEFI** | Conservative default |

**Known gotcha:** The string `"intel"` matches inside `"intelligent power"`.
Always use `"intel "` (trailing space) and `"intel(r)"` for Intel NIC patterns.

### payload.json Inside fwpkg — The Source of Truth

Every `.fwpkg` ZIP contains a `payload.json`. Key flags:

```json
{
  "UefiFlashable": false,
  "ResetRequired": false,
  "PLDMImage": true
}
```

| Flags | Method |
|---|---|
| `UefiFlashable: false`, `ResetRequired: false` | **BMC** — iLO self-flashes (iLO firmware) |
| `UefiFlashable: true` OR `ResetRequired: true` | **UEFI** — applied during POST |
| `PLDMImage: true` | **UEFI** via PLDM sideband (NIC, storage, backplane) |
| Only `.exe`/`.rpm` installers, `UpdatableBy: RuntimeAgent` | **OS** — SUM/iSUT in OS |

Confirmed examples:
- `ilo7_xxx.fwpkg`: `UefiFlashable: false, ResetRequired: false` → **BMC**
- `A66_xxx.fwpkg` (BIOS): `UefiFlashable: true, ResetRequired: true` → **UEFI**
- `BCM235.1.164.14_BCM957414A4142HC.fwpkg`: `PLDMImage: true` → **UEFI** (PLDM)

**Gen12+ note:** The `.json` sidecar is shipped as a **separate file** alongside the `.fwpkg`
(not embedded inside the ZIP). HPE's reason: keeps signature integrity.
`sdr.py::_fetch_software_ids()` already fetches the sibling `.json` URL — correct behavior.

### pcli ilo get update-method

```bash
pcli ilo get update-method [--host NAME] [--raw]
```

Output: Rich table with columns `Component | Version | Method | Reboot | Context`.
`Method` column color-coded: BMC=green, UEFI=yellow, OS=red.

---

## 4. COM Firmware Update Mechanism

### COM Does NOT Download the Entire SPP ISO

This is the most common misconception. COM uses **iLO Repository + UEFI Installation Queue**:

```
COM Cloud  ─► POST /compute-ops-mgmt/v1/jobs  {bundle_id, server_id}
           ─► iLO ServerFirmwareDownload job:
                  Analyze server FirmwareInventory vs bundle contents
                  Pull only applicable .fwpkg components from HPE CDN
                  (NOT the full 8+ GB SPP ISO)
           ─► Components staged to iLO flash repository
           ─► POST UpdateTaskQueue per component
           ─► COM (or admin) triggers server reboot
           ─► UEFI reads Installation Queue during POST → flashes components
```

iLO downloads individual `.fwpkg` files on demand from HPE CDN. The full SPP ISO is never
downloaded to the server.

### No SUM Inside iLO — Two Native Agents

| Agent | What it does | When it runs |
|-------|-------------|--------------|
| **BMC (iLO)** | iLO flashes itself and power management | Immediately — no reboot |
| **UEFI** | UEFI reads queue during POST and flashes | Next reboot |

SUM is a **separate tool** that runs on a management host. There is no SUM inside iLO.

### COM Job Templates

Durable template IDs (permanent — do not change):

| Job Type | Template ID |
|----------|-------------|
| `ServerFirmwareUpdate` | `fd54a96c-cabc-42e3-aee3-374a2d009dba` |
| `ServerFirmwareDownload` | `0683ada8-1a89-49dd-bf04-6df715b708a6` |
| `ServerIloFirmwareUpdate` | `94caa4ef-9ff8-4805-9e97-18a09e673b66` |
| `GroupFirmwareUpdate` | `91159b5e-9eeb-11ec-a9da-00155dc0a0c0` |

Key job parameters:
- `bundle_id` — firmware bundle UUID from `/firmware-bundles`
- `wait_for_power_off_or_reboot: false` — COM triggers reboot automatically
- `install_sw_drivers: false` — downloads drivers to iLO repo but does NOT install to OS

### Components Requiring OS (RuntimeAgent)

These components **cannot** be updated via COM OOB (or via pure iLO Redfish):

| Component | Reason |
|-----------|--------|
| Intel NICs | `UpdatableBy: RuntimeAgent` — no PLDM support |
| BCM OCP3 adapters (e.g. P10113-001) | OCP3 slot lacks PLDM channel on iLO 6; `bnxtnvm` in-band required |
| Linux/VMware NIC drivers (`.rpm`/`.zip` SCs) | OS-level software, not firmware |

For Gen12 servers (iLO 7), broader PLDM coverage means fewer OS-required components.
Gen12 PCIe NICs (Broadcom, Mellanox) with PLDM support are UEFI-updatable OOB.

### SPP Bundle API — No Per-Component Data

COM `GET /compute-ops/v1beta2/firmware-bundles` returns bundle metadata only.
There is **no sub-resource** listing individual components with `UpdatableBy` fields.
The per-component `UpdatableBy` only exists in:
1. Each `.fwpkg`'s `payload.json` (primary source)
2. HPE firmware blog documentation

COM's own `firmwareInventory` on server objects is a plain `[{name, version, deviceContext}]`
list — no `UpdatableBy` field exposed.

Bundle counts (as of May 2026): 85 total, 30 active — Gen12: 12, Gen11: 28, Gen10: 45.

---

## 5. Querying Components — Gen11 vs Gen12 Redfish Differences

### Storage Controllers

| Generation | Where controllers live | How to access |
|---|---|---|
| Gen10 and older | Inline `StorageControllers[]` inside `Storage/{id}` | Read directly |
| **Gen11 and Gen12** | Sub-collection at `Storage/{id}/Controllers/` | Fetch sub-collection |

On Gen11/Gen12, `storage.StorageControllers[]` is always empty — must use the sub-collection:

```python
ctrl_link = storage.get("Controllers", {}).get("@odata.id")
for c in client.get(ctrl_link).obj.get("Members", []):
    ctrl = client.get(c["@odata.id"]).obj
```

**dl325-gen12 special case:** `Storage` returns **zero Members** — NVMe/SATA controllers
only appear in `FirmwareInventory`. Always fall back to FirmwareInventory keyword scan.

### Full Firmware Inventory (`FirmwareInventory`)

```
GET /redfish/v1/UpdateService/FirmwareInventory
→ Members[] (each needs individual GET)
→ per item: Name, Version, Updateable (bool), SoftwareId
```

`Updateable: true` means iLO can flash it via UpdateService. Filter by this before suggesting upgrades.

**FirmwareInventory does NOT contain `UpdatableBy`** — that field is only in `payload.json`.

### iLO Version

```
GET /redfish/v1/Managers/1/  →  FirmwareVersion
```

### BIOS / System ROM

```
GET /redfish/v1/Systems/1/Bios/  →  Attributes.SystemRomVersion   (preferred)
GET /redfish/v1/Systems/1/       →  BiosVersion                    (fallback)
```

### NICs / Network Adapters

```
GET /redfish/v1/Chassis/1/NetworkAdapters/
→ Members[] → each adapter:
    Controllers[0].FirmwarePackageVersion   ← firmware version
    Model                                    ← chip/marketing name
    SKU                                      ← HPE part name
```

NIC firmware is **NOT** in FirmwareInventory on Gen11 (only via NetworkAdapters).
On Gen12, some NICs appear in FirmwareInventory as PLDM targets with `SoftwareId` = PLDM GUID.

#### PLDM Target GUID matching

BCM `.json` sidecars have `Devices.Device[].Target` = PLDM UUID embedding PCI IDs:
```
a6b1a447-382a-5a4f-14e4-16d714e41597
                   ^^^^─ 14e4 = Broadcom PCI vendor
                        ────── 16d7 = BCM57414 PCI device ID
```
When a NIC appears in FirmwareInventory, its `SoftwareId` = this GUID.
`sdr.py` matches against `.json` sidecar Target GUIDs — reliable even when `Model` is a marketing name.

#### BCM SDR filename format — version FIRST (inverted)

```
BCM235.1.164.14_BCM957414A4142HC.fwpkg
└─ version ───┘ └─ chip model ────────┘
```
Normal fwpkg format: `{model}_{version}.fwpkg`. BCM reverses this.

### CPU Microcode

```
GET /redfish/v1/Systems/1/Processors/{id}
→ ProcessorId.MicrocodeInfo   (e.g. "0xA10F11")
```

Microcode is **read-only** — updated only as part of the BIOS package.

### Memory / DIMMs

```
GET /redfish/v1/Systems/1/Memory/{id}
→ FirmwareRevision, CapacityMiB, PartNumber, Manufacturer
```

---

## 6. PLDM — How Gen12 Firmware Updates Work

**PLDM = Platform Level Data Model** (DMTF DSP0267) — messaging protocol for firmware
delivery over **MCTP** (sideband bus connecting iLO directly to components, no host OS needed).

```
  iLO 7 (BMC) ─── MCTP sideband ─── NIC / StorageCtrl / BIOS ROM
```

iLO 7 is the **PLDM Update Agent** — flashes NIC/storage/BIOS via PLDM without the host CPU.

Gen12 changed the definition of "offline" updates:
- **Gen11 offline**: boot from SPP ISO (Linux SUM environment)
- **Gen12 offline**: iLO handles updates standalone via PLDM — no OS, no ISO boot needed

Gen12 dropped the bootable SPP ISO entirely. All updates go through iLO Redfish.

`pcli ilo upgrade` uses `AddFromUri` + `UpdateTaskQueue` — the exact same Redfish calls
that SUM uses internally when connected to the iLO out-of-band management IP.

### SUM Remote / iLO-Connected Mode

SUM running on a jumpbox communicates with iLO over HTTPS:
```
Jumpbox: SUM ──HTTPS──► iLO mgmt port
  GET /FirmwareInventory      ← discover installed versions
  POST AddFromUri             ← stage .fwpkg (iLO downloads from HPE SDR)
  POST UpdateTaskQueue        ← queue for flash
  POST ComputerSystem.Reset   ← reboot to apply
```

No OS or agent needed on the target — pure out-of-band Redfish.

**vNIC vs OOB distinction:**
- SUM running ON the server itself → uses `hpilo` kernel driver (vNIC, in-band)
- SUM on jumpbox pointing at iLO IP → pure HTTPS Redfish, no driver needed

### SUM CLI + SPP ISO on Jumpbox

```bash
# Mount SPP ISO
sudo mount -o loop SPP_2026.03.0_spp.iso /mnt/spp

# Run against single server
/mnt/spp/smartupdate --s \
  --target 10.16.41.31 --user Administrator --password hpent123 \
  --baseline /mnt/spp --reboot

# Run fleet via input file (INI format, NOT XML)
/mnt/spp/smartupdate --inputfile update.in --s
```

**Input file format (INI-style):**
```ini
SILENT = YES
REBOOTALLOWED = YES
REBOOTDELAY = 30
SOURCEPATH = /mnt/spp
FORCEALL = YES
ONFAILEDDEPENDENCY = OmitComponent

[TARGETS]
HOST = 10.16.41.31
UID = Administrator
PWD = hpent123
[END]
```

**Note:** "Non-bootable SPP ISO on Gen12" means you can't *boot* from it — using it as a
**firmware source on a jumpbox** while SUM connects to iLO remotely is fully supported.

| | SUM CLI + SPP ISO | pcli + SDR |
|---|---|---|
| Firmware source | Local ISO (~6-8 GB) | HPE SDR (internet, per component) |
| Air-gap friendly | ✅ Yes | ❌ Needs internet |
| Multi-server | INI input file | Not yet implemented |
| Best for | Air-gapped fleets | Internet-connected, scripted, CI |

---

## 7. Recommended Firmware Upgrade Order

| Step | Component | Reason |
|------|-----------|--------|
| **1** | **iLO firmware** | Manages all subsequent updates. Restarts without rebooting server. |
| **2** | **System ROM (BIOS)** | May require minimum iLO version. Applied on next reboot. |
| **3** | **Everything else** | NIC, storage controllers, CPLD, power management — all via one reboot. |

`pcli ilo upgrade` enforces this order automatically: stages iLO first → waits ~90s for
iLO restart → stages BIOS + others → single reboot applies all.

---

## 8. Which Firmware Can (and Cannot) Be Upgraded via iLO

### What CAN be upgraded via iLO

| Component | Notes |
|---|---|
| **iLO firmware** | Restarts itself, server stays running |
| **System ROM / BIOS** | Applied on next server reboot |
| **HPE-branded NICs** (BCM, Mellanox PCIe) | Applied on reboot; read version from NetworkAdapters |
| **Storage controllers** (Smart Array, MR416i-o, NS204i-u) | Applied on reboot |
| **Power Management Controller** | HPE-specific component |
| **CPLD** | Applied on reboot |
| **UBM / Backplane PIC** | Applied on reboot |

### What CANNOT be upgraded via iLO

| Component | Reason |
|---|---|
| **Third-party NVMe SSDs** (Samsung, SK Hynix) | Not in HPE SDR — use vendor tools |
| **HPE OEM NVMe SSD** | Sometimes `Updateable: false` even if HPE has a package |
| **CPU microcode** | Delivered inside BIOS package only |
| **DIMM firmware** | Updated by BIOS on POST |
| **GPU / accelerator** | Vendor tools (NVIDIA SMI, etc.) |
| **Intel NICs** | `RuntimeAgent` only — need SUM in OS |
| **BCM OCP3 NICs (iLO 6)** | No PLDM channel — `bnxtnvm` in-band required |

### HPE SDR — Software Delivery Repository

```
https://downloads.linux.hpe.com/SDR/repo/fwpp-gen{N}/{YYYY.MM.00.00}/
```

Examples:
```
https://downloads.linux.hpe.com/SDR/repo/fwpp-gen12/2026.03.00.00/
https://downloads.linux.hpe.com/SDR/repo/fwpp-gen11/2026.03.00.00/
```

`sdr.py::latest_pack_url(gen)` auto-discovers the latest pack.

SDR covers HPE-branded components only. NIC coverage is per chip variant — some SKUs
of the same chip family may be absent from a given pack.

---

## 9. High-Level Upgrade Steps

### iLO Firmware

```
1. POST AddFromUri  →  iLO downloads .fwpkg from URL (~30-120s)
2. Poll ComponentRepository until filename appears
3. POST UpdateTaskQueue  { "UpdatableBy": ["Bmc"], "TPMOverride": true }
   → Applied immediately (no reboot). Task: Pending → Running → Complete
4. Wait ~90s for iLO restart. Poll GET /redfish/v1/ until responsive.
5. Verify: GET /Managers/1/ → FirmwareVersion
```

### BIOS / System ROM

```
1. POST AddFromUri  →  stage large .fwpkg (60-300s)
2. Poll ComponentRepository
3. POST UpdateTaskQueue  { "UpdatableBy": ["Uefi"], "TPMOverride": true }
   → Task stays "Pending" until reboot
4. POST ComputerSystem.Reset {"ResetType": "GracefulRestart"}
5. BIOS flash during POST (~5-10 min). Verify version after boot.
```

### Other Components (NIC, Storage, CPLD)

Same flow as BIOS. Always use `"UpdatableBy": ["Uefi"]`.

**Critical `UpdatableBy` rules:**
- `["Bmc"]` for iLO firmware only — on anything else, returns `SystemResetRequired` but does NOT flash
- `["Uefi"]` for everything else (BIOS, NIC, storage, CPLD)
- **Never use** `["Bmc", "RuntimeAgent", "Uefi"]` — iLO 7 splits into two subtasks; the OS_task never fires without SUM in OS

---

## 10. iLO 6 vs iLO 7 — UpdateService Schema Differences

### OEM Actions Path (`AddFromUri` target)

| iLO version | Path |
|---|---|
| **iLO 6** (Gen10/11) | `UpdateService["Actions"]["Oem"]["Hpe"]["#HpeiLOUpdateServiceExt.AddFromUri"]` |
| **iLO 7** (Gen12) | `UpdateService["Oem"]["Hpe"]["Actions"]["#HpeiLOUpdateServiceExt.AddFromUri"]` |

`firmware.py::_oem_actions()` tries Gen12 path first, falls back to Gen11.

### ComponentRepository and UpdateTaskQueue Members

On iLO 7 (Gen12), `Members[]` contains stub objects `{"@odata.id": "..."}` — no inline data.
Must expand each stub via individual GET. (`get_component_repository()` and `get_task_queue()` already do this.)

### iLO 7 — Stale Pending Tasks After UEFI Flash

iLO 7 never marks UEFI tasks Complete after POST flash. Stale Pending tasks remain forever.
**Do NOT treat a stale Pending task as failure** — always verify via FirmwareInventory version.
`_run_fw_upgrade()` auto-clears all Pending/Complete tasks after post-reboot verification.

### iLO 6 HttpPushUri — Often Returns Empty 400

iLO 6 `HttpPushUri` multipart upload (`/cgi-bin/uploadFile`) often fails with empty 400.
Use `AddFromUri` (iLO pulls from URL) instead. iLO 7 HttpPushUri works reliably.

---

## 11. Observed Server-Specific Notes

### dl380-gen11 (10.16.41.17, CNX242032D)

- Storage: `Storage/{id}/Controllers/` sub-collection present, accessible
- Has **HPE NS204i-u Gen11 Boot Controller** (upgradeable, `1.2.14.1001`)
- Mix of HPE OEM NVMe (`MO003200KXAVU`, `HPK3`) and SK Hynix NVMe (no fw via iLO)
- NIC: **BCM57414 OCP3** (`P10113-001`) — 10/25Gb 2-port SFP28
  - `228.1.111.0` installed; SDR has `235.1.164.14` (upgrade available)
  - OCP3 NIC: **NOT in FirmwareInventory** on iLO 6 (no PLDM channel)
  - Only upgradeble via in-band `bnxtnvm` OS tool
- **COM status**: Permanently blocked — `ProductID=NA` (internal test unit, no supply chain record)
- dl380 iLO had `Gateway: 0.0.0.0` (fixed May 2026 via `ilorest load --force_network_config`)

### dl345-gen12 (10.16.41.29, TWA25345G1208)

- Storage: `Storage/{id}/Controllers/` present
- Has **HPE MR416i-o Gen11 RAID controller** (current `52.22.3-4650`)
- NVMe drives: SK Hynix (no fw via iLO)
- ✅ In COM workspace HPECC_USWEST_1

### dl325-gen12 (10.16.41.31, TWA25325G1206)

- Storage: **zero Members** in Storage tree — use FirmwareInventory fallback scan
- NIC: **Broadcom P225p** (BCM957414 family, PCI ID `14E4:16D7`)
  - SDR package: `BCM235.1.164.14_BCM957414A4142HC.fwpkg`
  - Factory shipped: `214.0.194.0` → PLDM stepped to `216.0.333.11` (as of 2026-05-29)
  - Stepping chain remaining: `216 → 226.1.107.0 → 235.1.164.14`
- ✅ In COM workspace HPECC_USWEST_1
- **Current firmware (2026-05-29):**

  | Component | Version | Status |
  |---|---|---|
  | iLO 7 | 1.21.00 | ✅ current |
  | BIOS (System ROM A66) | 1.40 (01/09/2026) | ✅ current |
  | Power Management Controller | 1.1.2 | ✅ updated |
  | UBM6 Backplane PIC | 1.06 | ✅ updated |
  | BCM P225p NIC | **216.0.333.11** | ⚠️ needs 2 more stepping runs |
  | NVMe Drive | HP07 | — |

---

## 12. BCM957414 NIC Firmware — Stepping Chain & SUM CLI Lessons (dl325-gen12)

### The Problem

`BCM235.1.164.14_BCM957414A4142HC.fwpkg` has `MinimumActiveVersion: 226.1.107.0`.
Server shipped at `214.0.194.0`. SUM refuses to deploy (exit -3, OmitHost) when
hard dependency is unmet.

### Strategies Tried

| Strategy | Result |
|---|---|
| Direct Redfish upload | HTTP 200, silently ignored (iLO enforces MinimumActiveVersion at PLDM) |
| `ONFAILEDDEPENDENCY = FORCE` | Does NOT override hard error dependencies |
| `masterdependency.xml` patch | Worked but SUM inventory hung (`NO_APP_ACCOUNT = YES` was set) |
| **`ONFAILEDDEPENDENCY = OmitComponent`** | ✅ Skips BCM, deploys all others |

### What Worked — SUM OmitComponent Run

```ini
SILENT = YES
ROMONLY = YES
SOURCEPATH = C:\...\packages
ONFAILEDDEPENDENCY = OmitComponent
REBOOTALLOWED = YES
REBOOTDELAY = 30

[Node]
TARGET = 10.16.41.31
TARGETTYPE = ILO
USERNAME = Administrator
PASSWORD = hpent123
```

Result: Power Management `1.0.0 → 1.1.2`, UBM6 `1.00 → 1.06` updated. BCM skipped.
After reboot, PLDM naturally advanced NIC: `214 → 216.0.333.11` (one step).

### Next Steps to Complete NIC Update

**Option A (Recommended):** Re-run SUM with `OmitComponent` — PLDM may advance 216→226 naturally.
Requires ~2 more runs + reboots.

**Option B:** Patch `masterdependency.xml` — change `226.1.107000 → 1.0.0` in BCM_NXE blocks.
- **Must write as UTF-8 without BOM** — PowerShell's `Set-Content -Encoding UTF8` adds BOM and crashes SUM:
  ```powershell
  $utf8NoBom = [System.Text.UTF8Encoding]::new($false)
  [System.IO.File]::WriteAllText($path, $content, $utf8NoBom)
  ```
- Remove `NO_APP_ACCOUNT = YES` from INI (causes 30+ min inventory hang)

**Option C:** Redfish `AddFromUri` via local HTTP server (bypasses SUM 30-min inventory).

### Key Lessons

1. `ONFAILEDDEPENDENCY = OmitComponent` is essential for partial deploys — `FORCE` does not bypass hard errors
2. `NO_APP_ACCOUNT = YES` causes SUM inventory to hang on iLO 7 — remove it
3. iLO 7 PLDM inventory takes 25-30 min — normal, not a hang
4. PLDM stepping happens as side-effect of SUM inventory + reboot — NIC advances one step per cycle
5. `masterdependency.xml` must be UTF-8 without BOM
6. SDR intermediate BCM packages don't exist before 2026.01 — PLDM natural stepping is the only path from 214.x
7. SUM logs: `C:\cpqsystem\sum\log\10.16.41.31\sum_log.txt`

---

## 13. COM Auth Architecture

### Two Login Modes

| Mode | Command | Auth |
|------|---------|------|
| **User (Okta)** | `pcli com login` | `aquila-user-api.common.cloud.hpe.com` (ui-doorway) |
| **API client** | `pcli com login --api-client` | Regional API `us-west.api.greenlake.hpe.com` |

### Okta IDX Flow (HPE accounts)

1. `GET sso.common.cloud.hpe.com/as/authorization.oauth2` → stateToken
2. `POST auth.hpe.com/idp/idx/introspect` → stateHandle
3. `POST .../idx/identify` → `redirect-idp` (employees skip password → SAML chain)
4. Okta Verify push → `correctAnswer` number → user taps → poll → `success.href`
5. Multiple redirects → auth code → token exchange → `access_token`, `refresh_token`, `id_token`

### Token Details

| Token | Expires | Used for |
|-------|---------|---------|
| `access_token` | ~2h | Bearer auth on all API calls |
| `refresh_token` | Days/weeks | Silent refresh 30 min before access_token expires |
| `id_token` | ~5 min | Only to get `ccs_session` at login — ephemeral |
| `ccs_session` | Independent | Cookie for ui-doorway requests; dies independently of access_token |

**`ccs_session` is NOT the same as access_token.** After ccs-session expires, only a full
`pcli com login` restores it — refresh doesn't help.

---

## 14. COM API Endpoints

### Three Base URLs

| Tier | URL | Auth |
|------|-----|------|
| **ui-doorway** | `aquila-user-api.common.cloud.hpe.com` | Bearer + ccs-session cookie |
| **COM regional** | `us-west.api.greenlake.hpe.com` | Bearer only |
| **GLP global** | `global.api.greenlake.hpe.com` | GLP Bearer (client credentials) |

### Key Paths

```
# COM API (regional) — NOTE: /compute-ops/ deprecated April 2025, migrate to /compute-ops-mgmt/
GET  /compute-ops/v1beta2/firmware-bundles        ← bundles list (old path, still works)
GET  /compute-ops-mgmt/v1beta2/servers            ← server list with firmwareInventory
POST /compute-ops-mgmt/v1/jobs                    ← create firmware update job
GET  /compute-ops-mgmt/v1beta1/activation-keys    ← iLO CloudConnect key

# GLP global — device registration
POST https://global.api.greenlake.hpe.com/devices/v1/devices
     {"compute": [{"serialNumber": "X", "partNumber": "Y"}], "network": [], "storage": []}

# ui-doorway — devices, workspaces
GET  /ui-doorway/ui/v1/devices
POST /authn/v1/session                            ← get ccs-session from id_token
```

### Response Envelope Inconsistencies

```
ui-doorway /devices   → {"devices": [...], "pagination": {...}}    # snake_case
COM API /servers      → {"items": [...], "nextPageUri": "..."}      # camelCase
GLP /devices          → {"items": [...]}
```

`client.get_all()` handles all envelope keys.

---

## 15. COM Device Onboarding

### Working Path (Gen12 servers)

```bash
pcli com add device --serial-number TWA25345G1208 --part-number P81949-B21
# HTTP 202 → device appears in workspace in ~30s
```

### iLO CloudConnect (EnableCloudConnect)

```
POST /redfish/v1/Managers/1/Actions/Oem/Hpe/HpeiLO.EnableCloudConnect
Body: {"ActivationKey": "<key>"}    ← use activation key, NOT workspace_id
```

Get key: `GET https://us-west.api.greenlake.hpe.com/compute-ops-mgmt/v1beta1/activation-keys`

### dl380-gen11 (CNX242032D) — Permanently Blocked

- **Root cause:** `SKU=NA`, `PartNumber=N/A`, `ProductID=NA` — internal HPE test unit
- GreenLake requires supply chain record — CNX242032D has none
- Cannot be fixed by firmware upgrade — blocked at GLP database level
- Requires HPE internal process to register in GLP supply chain

---

## 16. Quick Reference — Redfish Endpoints

```
# Service root
GET /redfish/v1/

# System info, model, BIOS version, power state
GET /redfish/v1/Systems/1/

# iLO manager info + firmware version
GET /redfish/v1/Managers/1/

# BIOS
GET /redfish/v1/Systems/1/Bios/

# Full firmware inventory
GET /redfish/v1/UpdateService/FirmwareInventory/
GET /redfish/v1/UpdateService/FirmwareInventory/{id}

# UpdateService + staging
GET  /redfish/v1/UpdateService/
POST /redfish/v1/UpdateService/  (Actions.Oem.Hpe.AddFromUri or Oem.Hpe.Actions)

# Component repository (staged .fwpkg files)
GET /redfish/v1/UpdateService/ComponentRepository/

# Update task queue
GET  /redfish/v1/UpdateService/UpdateTaskQueue/
POST /redfish/v1/UpdateService/UpdateTaskQueue/   ← schedule component update
DELETE /redfish/v1/UpdateService/UpdateTaskQueue/{id}

# Storage
GET /redfish/v1/Systems/1/Storage/
GET /redfish/v1/Systems/1/Storage/{id}/Controllers/     ← Gen11+ sub-collection
GET /redfish/v1/Systems/1/Storage/{id}/Drives/{n}

# NICs
GET /redfish/v1/Chassis/1/NetworkAdapters/
GET /redfish/v1/Chassis/1/NetworkAdapters/{id}/

# CPUs
GET /redfish/v1/Systems/1/Processors/{id}

# Memory
GET /redfish/v1/Systems/1/Memory/{id}

# Power control
POST /redfish/v1/Systems/1/Actions/ComputerSystem.Reset/
     {"ResetType": "GracefulRestart"}   ← graceful reboot
     {"ResetType": "ForceOff"}          ← hard power off
     {"ResetType": "On"}                ← power on
     {"ResetType": "ForceRestart"}      ← hard reboot
```

---

## 17. Lessons Learned (All)

### iLO / Redfish

1. **Gen11+: `StorageControllers[]` is always empty** — must use `Storage/{id}/Controllers/` sub-collection.
2. **NIC firmware is NOT in FirmwareInventory on iLO 6** — read from `NetworkAdapters` endpoint.
3. **BCM SDR filenames have inverted format: `BCM{version}_{chipmodel}.fwpkg`** — version first.
4. **dl325-gen12: `Storage` has zero members** — always fall back to FirmwareInventory keyword scan.
5. **Gen12 OEM actions path is different from Gen11** — `Oem.Hpe.Actions` vs `Actions.Oem.Hpe`. `_oem_actions()` handles both.
6. **`UpdatableBy` in task queue must be `["Uefi"]` for non-iLO components** — `["Bmc"]` doesn't actually flash BIOS. `["Bmc","RuntimeAgent","Uefi"]` splits into two subtasks on iLO 7 and OS_task never fires.
7. **iLO 7 never marks UEFI tasks Complete** — stale Pending is normal after POST flash. Always verify via FirmwareInventory.
8. **iLO 6 HttpPushUri often returns empty 400** — use `AddFromUri` instead. iLO 7 is fine.
9. **BCM957414 NIC stepping chain: can't jump from 214.x to 235.x directly** — `MinimumActiveVersion: 226.1.107.0`. Natural PLDM stepping ~1 step per SUM run + reboot.
10. **BCM OCP3 NIC (P10113-001) does NOT appear in FirmwareInventory on iLO 6** — no PLDM channel. Requires in-band `bnxtnvm`.
11. **Gen12+ `.json` sidecar is a SEPARATE file** from `.fwpkg` — confirmed HPE policy. `sdr.py` already handles this correctly.
12. **`"intel"` matches inside `"intelligent power"`** — always use `"intel "` (trailing space) or `"intel(r)"` in pattern matching.
13. **Autocomplete: set `_ARGCOMPLETE=2` before dispatching sub-CLIs** — `=1` strips only one level, `=2` strips two.
14. **iLO 7 ComponentRepository/UpdateTaskQueue return stub Members** — must expand each `{"@odata.id": "..."}` via individual GET.

### COM / GreenLake

15. **COM downloads individual `.fwpkg` files, NOT the full SPP ISO** — only applicable components are pulled.
16. **No SUM inside iLO** — two native agents: BMC (immediate) and UEFI (POST reboot).
17. **FirmwareInventory on COM server objects has no `UpdatableBy` field** — must infer from name patterns.
18. **COM bundle API has no per-component sub-resource** — component-level data only in `payload.json`.
19. **`ccs-session` expires independently of `access_token`** — both required for ui-doorway; only full re-login restores ccs-session.
20. **`id_token` expires in ~5 min** — use immediately at login to set up workspace session.
21. **GLP token vs user token** — `global.api.greenlake.hpe.com` requires GLP OAuth2 token (client_id/secret), not Okta user token.
22. **`ActivationKey` not `workspace_id` for iLO 6 CloudConnect** — `EnableCloudConnect` needs activation key.
23. **`ProductID=NA` test units cannot be onboarded** — no supply chain record in GLP database.
24. **iLO 6 static network config cannot be PATCHed** — `Gateway` field is `PropertyNotWritableOrUnknown`. Must use `ilorest load --force_network_config`.
25. **`NO_APP_ACCOUNT = YES` in SUM INI causes 30+ min inventory hang on iLO 7** — remove it.
26. **SUM `ONFAILEDDEPENDENCY = FORCE` does NOT bypass hard error dependencies** — use `OmitComponent`.
27. **COM `/compute-ops/` prefix deprecated April 2025** — migrate to `/compute-ops-mgmt/` paths.
28. **Pagination URL bug**: `get_all()` must check `if next_page.startswith("http")` before prepending base_url — ui-doorway sometimes returns absolute `nextPageUri`.
