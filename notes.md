# pcli — Project Notes & Technical Reference

> **AI context lives in `.github/copilot-instructions.md`** — automatically loaded by Copilot each session.
> **Implementation internals, auth flows, API edge cases, lessons learned: `notes-agents.md`**
> User-facing CLI reference: `README.md`.

---

## Table of Contents

- [1. Test Servers & COM Workspace](#1-test-servers--com-workspace)
- [2. Firmware Update Methods — BMC / UEFI / OS](#2-firmware-update-methods--bmc--uefi--os)
- [3. COM Firmware Update Mechanism](#3-com-firmware-update-mechanism)
- [4. PLDM — How Gen12 Firmware Updates Work](#4-pldm--how-gen12-firmware-updates-work)
- [5. Which Firmware Can (and Cannot) Be Upgraded via iLO](#5-which-firmware-can-and-cannot-be-upgraded-via-ilo)
- [6. High-Level Upgrade Steps](#6-high-level-upgrade-steps)
- [7. iLO 6 vs iLO 7 — UpdateService Schema Differences](#7-ilo-6-vs-ilo-7--updateservice-schema-differences)
- [8. Observed Server-Specific Notes](#8-observed-server-specific-notes)
- [9. BCM957414 NIC — Stepping Chain (dl325-gen12)](#9-bcm957414-nic--stepping-chain-dl325-gen12)
- [10. COM Auth Overview](#10-com-auth-overview)
- [11. COM Device Onboarding](#11-com-device-onboarding)
- [12. Quick Reference — Redfish Endpoints](#12-quick-reference--redfish-endpoints)

---

## 1. Test Servers & COM Workspace

| Name | IP | Gen | iLO | Serial | Part# | COM |
|------|----|-----|-----|--------|-------|-----|
| dl380-gen11 | 10.16.41.17 | Gen11 | iLO 6 v1.74 | CNX242032D | N/A | ❌ blocked (ProductID=NA) |
| dl345-gen12 | 10.16.41.29 | Gen12 | iLO 7 v1.20 | TWA25345G1208 | P81949-B21 | ✅ HPECC_USWEST_1 |
| dl325-gen12 | 10.16.41.31 | Gen12 | iLO 7 v1.21 | TWA25325G1206 | P81967-B21 | ✅ HPECC_USWEST_1 |

Credentials: `Administrator / hpent123` COM token: `~/.config/hpecom/token.json` (glp_client_id / glp_client_secret for API client auth)

---

## 2. Firmware Update Methods — BMC / UEFI / OS

HPE components can be updated by three different agents:

| Method | Agent | Trigger | Reboot needed |
|--------|-------|---------|---------------|
| **BMC** | iLO flashes directly | Immediate via Redfish | ❌ No |
| **UEFI** | UEFI reads queue during POST | Next server reboot | ✅ Yes |
| **OS** | SUM/iSUT running in the OS | While OS is running | Usually yes |

`pcli ilo list firmwares` shows the `Method` column (BMC / UEFI / OS) for each component. See `notes-agents.md` for how pcli infers the method from component name patterns.

---

## 3. COM Firmware Update Mechanism

**COM does NOT download the entire SPP ISO.** This is the most common misconception.

COM uses the **iLO Repository + UEFI Installation Queue**:

```
COM Cloud → triggers iLO ServerFirmwareDownload job
          → iLO compares server FirmwareInventory vs bundle
          → iLO downloads only applicable .fwpkg files from HPE CDN
          → Components staged to iLO repository
          → UEFI flashes components during next server reboot
```

iLO downloads individual `.fwpkg` files on demand. The full SPP ISO (~6-8 GB) is never downloaded to the server.

### SUM vs iLO Native Agents

SUM is a **separate management tool** — there is no SUM inside iLO. iLO has two native firmware agents:

| Agent | What it does | When it runs |
|-------|-------------|--------------|
| **BMC (iLO)** | iLO flashes itself and power management components | Immediately — no server reboot |
| **UEFI** | UEFI reads the installation queue during POST and flashes everything else | Next server reboot |

### Components Requiring OS (cannot be updated OOB)

| Component | Reason |
|-----------|--------|
| Intel NICs | No PLDM support — needs SUM/OS agent |
| BCM OCP3 adapters on Gen11 (e.g. P10113-001) | OCP3 slot has no PLDM channel on iLO 6 — needs `bnxtnvm` in-band |
| Linux/VMware NIC drivers | OS-level software, not firmware |

For Gen12 servers (iLO 7), broader PLDM coverage means fewer OS-required components. Gen12 PCIe NICs (Broadcom, Mellanox) with PLDM support are UEFI-updatable OOB.

### SPP Bundle API — No Per-Component Data

COM `GET /compute-ops/v1beta2/firmware-bundles` returns bundle metadata only. There is **no sub-resource** listing individual components with `UpdatableBy` fields. The per-component `UpdatableBy` only exists in:
1. Each `.fwpkg`'s `payload.json` (primary source)
2. HPE firmware blog documentation

COM's own `firmwareInventory` on server objects is a plain `[{name, version, deviceContext}]` list — no `UpdatableBy` field exposed.

Bundle counts (as of May 2026): 85 total, 30 active — Gen12: 12, Gen11: 28, Gen10: 45.

---

## 4. PLDM — How Gen12 Firmware Updates Work

**PLDM = Platform Level Data Model** (DMTF DSP0267) — messaging protocol for firmware delivery over **MCTP** (sideband bus connecting iLO directly to components, no host OS needed).

```
  iLO 7 (BMC) ─── MCTP sideband ─── NIC / StorageCtrl / BIOS ROM
```

iLO 7 is the **PLDM Update Agent** — flashes NIC/storage/BIOS via PLDM without the host CPU or OS.

Gen12 changed the definition of "offline" updates:
- **Gen11 offline**: boot from SPP ISO (Linux SUM environment)
- **Gen12 offline**: iLO handles updates standalone via PLDM — no OS, no ISO boot needed

Gen12 dropped the bootable SPP ISO entirely. All updates go through iLO Redfish.

### SUM Remote vs pcli

Both SUM (from a jumpbox) and `pcli ilo upgrade` use the same iLO Redfish calls under the hood. Neither requires an OS on the target server.

| | SUM CLI + SPP ISO | pcli + SDR |
|---|---|---|
| Firmware source | Local ISO (~6-8 GB) | HPE SDR (internet, per component) |
| Air-gap friendly | ✅ Yes | ❌ Needs internet |
| Multi-server | INI input file | Not yet (planned) |
| Best for | Air-gapped fleets | Internet-connected, scripted, CI |

### iSUT and AMS (OS Agents)

These are only needed for OS-level components (Intel NICs, Windows drivers). For firmware-only updates (iLO/BIOS/NIC firmware via PLDM), **neither is required**.

| Agent | Purpose |
|---|---|
| **AMS** (Agentless Management Service) | Feeds OS inventory into iLO: hostname, OS version, NIC teams |
| **iSUT** (Intelligent System Update Tool) | Polls iLO for staged packages → downloads → installs on OS |

Both use the iLO vNIC host interface (`169.254.1.2 ↔ 169.254.1.1`). Despite the branding, "Agentless" management still requires AMS installed on the OS.

| Step | Component | Reason |
|------|-----------|--------|
| **1** | **iLO firmware** | Manages all subsequent updates. Restarts without rebooting server. |
| **2** | **System ROM (BIOS)** | May require minimum iLO version. Applied on next reboot. |
| **3** | **Everything else** | NIC, storage controllers, CPLD, power management — all via one reboot. |

`pcli ilo upgrade` enforces this order automatically: stages iLO first → waits ~90s for iLO restart → stages BIOS + others → single reboot applies all.

---

## 5. Which Firmware Can (and Cannot) Be Upgraded via iLO

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

SDR covers HPE-branded components only. NIC coverage is per chip variant — some SKUs of the same chip family may be absent from a given pack.

---

## 6. High-Level Upgrade Steps

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

## 7. iLO 6 vs iLO 7 — UpdateService Schema Differences

### OEM Actions Path (`AddFromUri` target)

| iLO version | Path |
|---|---|
| **iLO 6** (Gen10/11) | `UpdateService["Actions"]["Oem"]["Hpe"]["#HpeiLOUpdateServiceExt.AddFromUri"]` |
| **iLO 7** (Gen12) | `UpdateService["Oem"]["Hpe"]["Actions"]["#HpeiLOUpdateServiceExt.AddFromUri"]` |

`firmware.py::_oem_actions()` tries Gen12 path first, falls back to Gen11.

### ComponentRepository and UpdateTaskQueue Members

On iLO 7 (Gen12), `Members[]` contains stub objects `{"@odata.id": "..."}` — no inline data. Must expand each stub via individual GET. (`get_component_repository()` and `get_task_queue()` already do this.)

### iLO 7 — Stale Pending Tasks After UEFI Flash

iLO 7 never marks UEFI tasks Complete after POST flash. Stale Pending tasks remain forever. **Do NOT treat a stale Pending task as failure** — always verify via FirmwareInventory version. `_run_fw_upgrade()` auto-clears all Pending/Complete tasks after post-reboot verification.

### iLO 6 HttpPushUri — Often Returns Empty 400

iLO 6 `HttpPushUri` multipart upload (`/cgi-bin/uploadFile`) often fails with empty 400. Use `AddFromUri` (iLO pulls from URL) instead. iLO 7 HttpPushUri works reliably.

---

## 8. Observed Server-Specific Notes

### dl380-gen11 (10.16.41.17, CNX242032D)

- Storage: `Storage/{id}/Controllers/` sub-collection present, accessible
- Has **HPE NS204i-u Gen11 Boot Controller** (upgradeable, `1.2.14.1001`)
- Mix of HPE OEM NVMe (`MO003200KXAVU`, `HPK3`) and SK Hynix NVMe (no fw via iLO)
- NIC: **BCM57414 OCP3** (`P10113-001`) — 10/25Gb 2-port SFP28
  - `228.1.111.0` installed; SDR has `235.1.164.14` (upgrade available)
  - OCP3 NIC: **NOT in FirmwareInventory** on iLO 6 (no PLDM channel)
  - Only upgradeble via in-band `bnxtnvm` OS tool
  - `NetworkAdapters[].Location` is blank on iLO 6; GUI slot label comes from HPE OEM `Chassis/Devices` (`Location: OCP 3.0 Slot 15`)
- **COM status**: Permanently blocked — `ProductID=NA` (internal test unit, no supply chain record)
- dl380 iLO had `Gateway: 0.0.0.0` (fixed May 2026 via `ilorest load --force_network_config`)

### dl345-gen12 (10.16.41.29, TWA25345G1208)

- Storage: `Storage/{id}/Controllers/` present
- Has **HPE MR416i-o Gen11 RAID controller** (current `52.22.3-4650`)
- NICs:
  - `P10113-001` in `OCP Slot 21` reports generic `Model=BCM57414`, `SKU=10/25Gb 2-port SFP28 BCM57414 OCP3 Adapter`
  - `P26264-001` in `PCIE Slot 6` also reports generic `Model=BCM57414`, but HPE GUI labels it **Broadcom P225p**
  - Both adapters run firmware `235.1.164.14`
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

## 9. BCM957414 NIC Firmware — Stepping Chain & SUM CLI Lessons (dl325-gen12)

### The Problem

`BCM235.1.164.14_BCM957414A4142HC.fwpkg` has `MinimumActiveVersion: 226.1.107.0`. Server shipped at `214.0.194.0`. SUM refuses to deploy (exit -3, OmitHost) when hard dependency is unmet.

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

Result: Power Management `1.0.0 → 1.1.2`, UBM6 `1.00 → 1.06` updated. BCM skipped. After reboot, PLDM naturally advanced NIC: `214 → 216.0.333.11` (one step).

### Next Steps to Complete NIC Update

**Option A (Recommended):** Re-run SUM with `OmitComponent` — PLDM may advance 216→226 naturally. Requires ~2 more runs + reboots.

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

## 10. COM Auth Overview

### Two Login Modes

| Mode | Command | Auth |
|------|---------|------|
| **User (Okta)** | `pcli com login` | `aquila-user-api.common.cloud.hpe.com` (ui-doorway) |
| **API client** | `pcli com login --api-client` | Regional API `us-west.api.greenlake.hpe.com` |

`ccs-session` expires independently of `access_token` — both are required for ui-doorway calls. After `ccs_session` expires, only a full `pcli com login` restores it. See `notes-agents.md` for the full Okta IDX auth flow and token lifetime table.

---

## 11. COM Device Onboarding

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
- Compute Ops Management requires supply chain record — CNX242032D has none
- Cannot be fixed by firmware upgrade — blocked at GLP database level
- Requires HPE internal process to register in GLP supply chain

---

## 12. Quick Reference — Redfish Endpoints

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

## Storage Controllers

### Types Comparison
| Type | Interface | Speed | RAID | Device Name | Use Case |
|------|-----------|-------|------|-------------|----------|
| Smart Array / SAS HBA | SAS 12G/24G | ~2 GB/s | Hardware RAID (dedicated controller) | /dev/sda | Enterprise SAS HDDs/SSDs, large RAID arrays |
| NVMe | PCIe Gen4/5 | ~7 GB/s | Software RAID or JBOF | /dev/nvme0n1+ | High-performance SSDs, databases, VMs |
| Embedded SATA | SATA 6G | ~600 MB/s | Software RAID or none | /dev/sda | Boot drives, low-cost/non-critical storage |

### NVMe Key Facts
- Controller is **built into the drive itself** (on the SSD PCB)
- Connects directly to CPU via PCIe lanes — no separate HBA needed
- Each NVMe drive = independent disk (nvme0n1, nvme1n1, nvme2n1, ...)
- Exception: HPE Tri-Mode controllers (MR416i-p) can manage NVMe+SAS+SATA together for hardware RAID across NVMe

### Smart Array / SAS
- Separate plug-in controller card (HPE MR/SR series)
- One card controls all attached SAS/SATA drives
- Required for hardware RAID 5/6 with battery-backed cache
- HPE models: MR416i-p, MR216i-p, MR416i-o, MR932i-p (Gen12)

### Gen12 Boot Device Options (most to least common)
| Option | Device | Notes |
|--------|--------|-------|
| HPE NS204i-u (recommended) | nvme0n1 | Dedicated 2-port NVMe boot controller, 2× SSDs in hardware RAID-1. HPE standard for Gen11/12 |
| Smart Array + SATA SSD | sda | If MR controller present with SATA boot SSD |
| Embedded SATA | sda | Single point of failure, not recommended |
| USB/SD card | — | Removed from Gen11+ — too unreliable |

### NS204i-u Details
- HPE's dedicated NVMe boot controller (Slot 16 by default)
- Contains 2× NVMe SSDs mirrored (RAID-1) internally
- Always enumerates as nvme0n1 (lowest index, fixed PCIe slot)
- Data NVMe drives enumerate as nvme1n1, nvme2n1, etc.
- Frees front drive bays for data; no separate HBA needed for OS

### dl380-gen11 Lab Storage
- Boot: NS204i-u (Slot 16), 2× 480GB NVMe RAID-1 → nvme0n1 (447GB usable)
- Data: 4× SK Hynix 3.2TB NVMe (PE8030/PE8130) → nvme1n1–nvme4n1
- OS: Ubuntu, PXE boot from BCM (node002), mounts / on nvme0n1p2
- BCM disk config installs to nvme0n1 (falls through sda/hda/vda → nvme0n1)

### dl325-gen12 Lab Storage
- NVMe: Samsung PM1735 3.2TB (MO003200KWZQQ) → single data/boot drive
- Embedded SATA controller present but no drives connected
- No Smart Array / SAS controller installed

### DL325 Gen12 Drive Bay Architecture

**Chassis variants** (chosen at order time, mutually exclusive):
| Chassis | Boxes | Drive slots | Form factor |
|---------|-------|-------------|-------------|
| 8SFF CTO | Box1 only | 8 bays | 2.5" SFF SAS/SATA/NVMe |
| SFF/EDSFF CTO | Box1–5 | up to 10 SFF or 20 E3.S | Modular, mixed |
| GPU CTO | Box1–2 | 4 SFF or 8 E3.S | GPU-focused |

**8SFF CTO (dl325-gen12 lab server):**
- Box1: 8 bays, backplane = `8 SFF 24G x4NVMe/SAS UBM6 BC BP`
- Box2: optional 2-bay add-on (not installed on lab server)
- All 8 bays: Direct Attach x4 PCIe Gen5 to CPU (no controller in path)
- iLO labels them: `Box 1 Bay 1–8`

**UBM (Universal Backplane Module):**
- Each UBM chip manages 2 drive bays
- 8SFF backplane has 4 UBM chips → iLO reports 4 firmware entries for same BP
- Each UBM handles: drive presence detection, LED control, signal routing (NVMe vs SAS/SATA)
- UBM6 = 6th gen UBM, supports NVMe x4, SAS 24G, SATA 6G in same slot

**EDSFF / E3.S form factor:**
- Ruler-shaped "gumstick" drive, designed for datacenter airflow and density
- Cannot mix with SFF in same backplane — requires different cage entirely
- E3.S = one EDSFF size variant; Gen5 NVMe native
- DL325 Gen12 supports 20× E3.S drives in SFF/EDSFF CTO chassis variant

**HPE part number vs spare part number:**
- Product number (P#####-B21): ordering/option number, used in QuickSpec, for new purchases
- Spare part number (P#####-001): field replacement number, shown in Partsurfer
- Same physical drive = two different HPE part numbers

**iLO does NOT expose HPE part numbers (P#####-B21 or -001):**
- Drive `PartNumber` field = empty in Redfish API and ilorest
- CPU, NIC PartNumber fields = also empty
- Only source of B21/001 numbers: QuickSpec docs, Partsurfer (by server serial)
- `MO003200KWZQQ` (Samsung model) ≠ HPE part number; not searchable in Partsurfer

**PCIe slots on dl325-gen12:**
| Slot | Type | State |
|------|------|-------|
| PCI-E Slot 1 | Gen5 x16 | Broadcom P225p NIC |
| PCI-E Slot 2 | Gen5 x16 | Empty |
| OCP Slot A | Gen4 OCP3 | Empty |
| OCP Slot B | Gen4 OCP3 | Empty |
| NVMe Box1 Bay1 | Gen5 U.2 | PM1735 3.2TB (Gen4 drive in Gen5 slot) |
| NVMe Box1 Bay2–8 | Gen5 U.2 | Empty |

---

## HPE iLO Boot Order — Three Layers (Critical)

HPE Gen12 (iLO 7) has **three separate boot order settings**. Confusing them causes persistent boot order scrambling.

### 1. BootOrder (Redfish standard)
- **Path:** `PATCH /redfish/v1/Systems/1/` → `Boot.BootOrder`
- **Format:** UEFI NVRAM entries: `["Boot0016", "Boot000E", "Boot000D", ...]`
- **Priority:** LOWEST — gets **overwritten on every reboot** by PersistentBootConfigOrder
- **Scope:** Current boot only; survives until next reboot
- What `pcli ilo boot show` currently displays

### 2. PersistentBootConfigOrder (HPE OEM — the real source of truth)
- **Path:** `PATCH /redfish/v1/systems/1/bios/oem/hpe/boot/settings/`
- **Format:** HPE structured strings: `["NVMe.DriveBay.1.1", "NIC.Slot.1.1.Httpv4", ...]`
- **Priority:** HIGHEST — BIOS translates this to BootOrder on **every reboot**
- **Scope:** Persistent across reboots
- **This is the one you must fix to permanently change boot order**
- OS-installed UEFI entries (e.g. "redhat" Boot0016) get placed on top of this order automatically

### 3. DesiredBootDevices (HPE OEM — one-time NIC selector)
- **Path:** `PATCH /redfish/v1/systems/1/bios/oem/hpe/boot/settings/` → `DesiredBootDevices`
- **Format:** Array of 5 slots with `CorrelatableID` (PCIe path)
- **Effect:** Moves matched NIC to top of PersistentBootConfigOrder on next reboot
- **Problem:** Cannot distinguish HTTP vs PXE (same CorrelatableID) → always picks first NIC match
- **Clear it:** PATCH all 5 entries with empty `CorrelatableID: ""`

### Boot order priority chain
```
Every reboot:
  PersistentBootConfigOrder  →  translated to BootOrder (NVRAM)
  OS boot vars (Boot0016 "redhat") placed on top automatically
  DesiredBootDevices (if set) moves NIC to top before translation
```

### Fix for scrambled boot order (NIC at top after PXE boot)
```bash
# 1. Fix persistent order (NVMe first)
curl -sk -u Administrator:<pw> -X PATCH https://<ilo-ip>/redfish/v1/systems/1/bios/oem/hpe/boot/settings/ \
  -H "Content-Type: application/json" \
  -d '{"PersistentBootConfigOrder": ["NVMe.DriveBay.1.1","Generic.USB.1.1","NIC.Slot.1.1.Httpv4","NIC.Slot.1.1.IPv4","NIC.Slot.1.1.Httpv6","NIC.Slot.1.1.IPv6"]}'

# 2. Clear stale DesiredBootDevices
curl -sk -u Administrator:<pw> -X PATCH https://<ilo-ip>/redfish/v1/systems/1/bios/oem/hpe/boot/settings/ \
  -H "Content-Type: application/json" \
  -d '{"DesiredBootDevices": [{"CorrelatableID":"","Lun":"","Wwn":"","iScsiTargetName":""},{"CorrelatableID":"","Lun":"","Wwn":"","iScsiTargetName":""},{"CorrelatableID":"","Lun":"","Wwn":"","iScsiTargetName":""},{"CorrelatableID":"","Lun":"","Wwn":"","iScsiTargetName":""},{"CorrelatableID":"","Lun":"","Wwn":"","iScsiTargetName":""}]}'

# 3. Fix current active BootOrder immediately
curl -sk -u Administrator:<pw> -X PATCH https://<ilo-ip>/redfish/v1/Systems/1/ \
  -H "Content-Type: application/json" \
  -d '{"Boot": {"BootOrder": ["Boot0016","Boot000E","Boot000D","Boot000F","Boot0012","Boot0010","Boot0013"]}}'
```

### pcli ilo boot pxe — how it works (correct approach)
- Sets `BootSourceOverrideEnabled: Once` + `BootSourceOverrideTarget: UefiTarget`
- Sets `UefiTargetBootSourceOverride` to exact PXE device path (no `/Uri()` suffix)
- HTTP path has `/Uri()` suffix; PXE does not — that's how they're distinguished
- Does NOT use DesiredBootDevices (causes boot order scrambling)
- After one-time PXE boot, iLO resets override to Disabled/None automatically
