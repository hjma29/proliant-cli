# pcli — Agent & Implementation Reference

> **For sysadmin-level concepts and operational procedures: `notes.md`**
> **For user-facing CLI reference: `README.md`**
> **For AI context loaded each session: `.github/copilot-instructions.md`**
>
> This file is the deep implementation reference: how pcli works internally,
> auth flows, API edge cases, network layers, and lessons that burned us.

---

## Table of Contents

- [1. Project Layout](#1-project-layout)
- [2. COM Auth — Token Routing Root Cause](#2-com-auth--token-routing-root-cause)
- [3. QS Network Gauntlet — Akamai + Zscaler](#3-qs-network-gauntlet--akamai--zscaler)
- [4. COM API Implementation](#4-com-api-implementation)
- [5. Redfish Query Implementation (Gen11 vs Gen12)](#5-redfish-query-implementation-gen11-vs-gen12)
- [6. fwpkg / Sidecar Internals](#6-fwpkg--sidecar-internals)
- [7. pcli spp inspect — Output Section Sources](#7-pcli-spp-inspect--output-section-sources)
- [8. SPP Catalog Composition](#8-spp-catalog-composition)
- [9. classify_update_method() Rules](#9-classify_update_method-rules)
- [10. Lessons Learned](#10-lessons-learned)

---

## 1. Project Layout

```
src/pcli/
  cli.py                Top-level entry — dispatches ilo/com/qs, sets _ARGCOMPLETE=2
  ilo/
    cli.py              All pcli ilo commands: list/report/upgrade subparsers, table printers
    client.py           Async Redfish client (httpx, HTTP/2, session management)
    inventory.py        Read-only Redfish fetches; classify_update_method()
    firmware.py         Stage, queue, wait helpers for iLO firmware operations
    sdr.py              HPE SDR fetch, fwpkg parsing, find_upgrades()
    config.py           hosts-ilo.ini loader; skips sections with type=oneview or "oneview" in name
  com/
    cli.py              All pcli com commands: get devices/bundles/servers; login/logout; report
    client.py           Async HTTP COM client (httpx, HTTP/2, pagination)
    auth.py             COMSession — load/save token.json, GLP client credentials refresh
    devices.py          GLP devices API, resolve_user_ids() UUID→email
    firmware.py         FirmwareBundle dataclass, fetch_bundles()
    inventory.py        Fleet memory/GPU reports — _get_com_servers(client) using COM /servers
  qs/
    cli.py              pcli qs list/describe/diff commands
    client.py           _qs_get() with curl_cffi→httpx auto-fallback; disk cache
  oneview/
    ...
tests/
  com/                  test_auth.py, test_devices.py
  ilo/                  test_cli.py, test_firmware.py
notes.md                Sysadmin concepts and operational reference
notes-agents.md         This file — implementation internals
```

**Key design principle:** every module pair is `cli.py` (user output, argparse) + `*.py` (pure data, no print). Tests only cover the data layer.

**Autocomplete:** `cli.py` sets `_ARGCOMPLETE=2` before dispatching sub-CLIs — must be `=2` (strips two levels), not `=1`.

---

## 2. COM Auth — Token Routing Root Cause

### The 404 That Wasn't a URL Problem

`pcli com report memory` was returning HTTP 404 with:
```
"Routing details for the customer not found"
```

This was diagnosed by testing every variable:
- ✅ URL versions tested: v1beta1, v1beta2, v1beta3, v1, v2 — all 404 same way
- ✅ Regions tested: us-west, eu-central, ap-northeast — all 404
- ✅ ccs-session cookie — present and valid
- ❌ **Token type** — only this variable mattered

**Root cause: GreenLake gateway routes `compute-ops-mgmt` based on token type, not just credentials.**

```
BEFORE FIX — Okta token path (broken)
══════════════════════════════════════════════════════════════

  pcli com login
       │
       ▼
  ┌─────────────┐     Okta SSO      ┌──────────────────┐
  │    User     │──────────────────▶│   Okta IdP       │
  └─────────────┘                   └──────────────────┘
                                           │
                                    user token (JWT)
                                    ❌ NO workspace routing
                                    ❌ NO regional context
                                           │
  pcli com report memory                   ▼
       │                          ┌──────────────────────┐
       │   GET /compute-ops-mgmt/ │  GreenLake Gateway   │
       └─────────────────────────▶│  (us-west)           │
                                  │                      │
                                  │  "who is this user   │
                                  │   and which tenant   │
                                  │   do they map to?"   │
                                  │                      │
                                  │  ❌ can't route →    │
                                  └──────────────────────┘
                                           │
                                        HTTP 404
                              "Routing details for the
                               customer not found"


AFTER FIX — GLP client-credentials path (working)
══════════════════════════════════════════════════════════════

  pcli com login (stores glp_client_id + glp_client_secret)
       │
  pcli com report memory
       │
       ▼
  ┌─────────────────┐  client_credentials   ┌──────────────────┐
  │  auth.py        │──────────────────────▶│  GLP OAuth       │
  │  from_user_     │                       │  Token Service   │
  │  token()        │◀──────────────────────│                  │
  └─────────────────┘   GLP access token    └──────────────────┘
       │                ✅ workspace_id embedded
       │                ✅ tenant routing context
       │                ✅ regional endpoint hint
       │
       ▼
  ┌──────────────────────┐
  │  GreenLake Gateway   │
  │  (us-west)           │
  │                      │
  │  "GLP token →        │
  │   workspace ABC →    │
  │   route to COM       │
  │   tenant cluster"    │
  └──────────────────────┘
           │
        HTTP 200 ✅
     { servers: [...] }


WHY IT BROKE SILENTLY
══════════════════════════════════════════════════════════════

  Day 1:  Okta token expired → code fell back to GLP → ✅ worked
                                     (lucky fallback)

  Day 2:  Fresh login → brand new Okta token → no fallback triggered
          → GLP path never tried → ❌ 404 every time

  Fix:    Always use GLP when creds exist.
          Okta token only used for UI-doorway (device list).
```

### Two Auth Domains

GreenLake has **two separate auth domains** for the same workspace:

| Domain | Token | Used for |
|--------|-------|---------|
| **Okta SSO** | User JWT | UI-doorway (`aquila-user-api`) — device list, workspace list |
| **GLP client_credentials** | GLP OAuth2 | COM regional API (`us-west.api.greenlake.hpe.com`) — servers, firmware, jobs |

They look identical from the outside (both Bearer tokens) but the gateway resolves them differently.

### Token File (`~/.config/hpecom/token.json`)

```json
{
  "access_token": "...",        // Okta user token — for ui-doorway
  "refresh_token": "...",       // Okta refresh
  "ccs_session": "...",         // Cookie for ui-doorway requests
  "workspace_id": "...",
  "glp_client_id": "...",       // GLP API client credentials
  "glp_client_secret": "...",
  "glp_access_token": "...",    // Cached GLP token
  "glp_token_expires_at": 1234567890
}
```

### `from_user_token()` Logic (auth.py)

```
from_user_token() called
    │
    ├── GLP creds present?
    │     ├── YES → use glp_access_token if not expired, else fetch fresh via client_credentials
    │     │         → _user_token = False → fetch_devices uses DEVICES_URI (GLP global)
    │     └── NO  → use Okta access_token directly (fallback for dev/non-API-client setups)
    │
    └── COMSession(client, workspace_id, _user_token)
```

### Okta IDX Flow (full login)

1. `GET sso.common.cloud.hpe.com/as/authorization.oauth2` → stateToken
2. `POST auth.hpe.com/idp/idx/introspect` → stateHandle
3. `POST .../idx/identify` → `redirect-idp` (employees skip password → SAML chain)
4. Okta Verify push → `correctAnswer` number → user taps → poll → `success.href`
5. Multiple redirects → auth code → token exchange → `access_token`, `refresh_token`, `id_token`
6. `POST /authn/v1/session` with `id_token` → `ccs_session` cookie

| Token | Expires | Used for |
|-------|---------|---------|
| `access_token` | ~2h | Bearer auth on ui-doorway API calls |
| `refresh_token` | Days/weeks | Silent refresh 30 min before access_token expires |
| `id_token` | ~5 min | Only to get `ccs_session` at login — use immediately |
| `ccs_session` | Independent | Cookie for ui-doorway; dies independently of access_token |
| `glp_access_token` | ~2h | Bearer auth on COM regional + GLP global API calls |

**`ccs_session` is NOT the same as `access_token`.** After ccs-session expires, only a full `pcli com login` restores it — refresh doesn't help.

---

## 3. QS Network Gauntlet — Akamai + Zscaler

### The Two Adversaries

```
THE FULL NETWORK GAUNTLET — pcli qs on Windows
══════════════════════════════════════════════════════════════

  pcli qs list --model dl110-gen12
       │
       ▼
  ┌─────────────────────────────────────────────────────────┐
  │                  LAYER 1: ZSCALER (Corp Proxy)          │
  │                                                         │
  │  All HTTPS traffic intercepted & re-encrypted           │
  │  Zscaler injects its own TLS cert                       │
  │                                                         │
  │  curl_cffi (BoringSSL) ──❌──▶  REJECTS Zscaler cert    │
  │  "invalid library (0)"          (strict TLS pinning)    │
  │                                                         │
  │  httpx (system TLS) ──✅──▶  TRUSTS Zscaler cert        │
  │  (uses Windows cert store,      passes through          │
  │   Zscaler cert pre-installed)                           │
  └─────────────────────────────────────────────────────────┘
       │  httpx request passes through
       ▼
  ┌─────────────────────────────────────────────────────────┐
  │                  LAYER 2: AKAMAI (HPE Bot Detection)    │
  │                                                         │
  │  www.hpe.com protected by Akamai Bot Manager            │
  │                                                         │
  │  urllib / basic httpx ──❌──▶  "looks like a bot"       │
  │  (Python TLS fingerprint)       403 / challenge page    │
  │                                                         │
  │  curl_cffi chrome TLS ──✅──▶  "looks like Chrome"      │
  │  (BoringSSL + Chrome           passes bot check         │
  │   cipher suite + extensions)                            │
  └─────────────────────────────────────────────────────────┘
       │
       ▼
    hpe.com responds ✅


WHAT BEATS WHAT
══════════════════════════════════════════════════════════════

                    Zscaler        Akamai
                    (Layer 1)      (Layer 2)
                   ──────────────────────────
  urllib             ✅ passes      ❌ blocked
  httpx + HTTP/2     ✅ passes      ✅ passes*
  curl_cffi chrome   ❌ BLOCKED     ✅ passes

  * httpx + HTTP/2 + browser headers passes Akamai on the
    JSON search endpoint (less aggressive check than HTML pages)


THE SOLUTION — two-layer fallback in _qs_get()
══════════════════════════════════════════════════════════════

  _qs_get(url)
       │
       ▼
  Try curl_cffi (Chrome TLS)
       │
       ├── Zscaler present? ──❌──▶  BoringSSL TLS error (35)
       │                              │
       │                    set _curl_cffi_broken = True
       │                    rebuild _client as httpx
       │                              │
       │                    retry with httpx ──✅──▶ hpe.com
       │
       └── No Zscaler (home/VPN)? ──✅──▶  Chrome fingerprint
                                            bypasses Akamai
```

### Why This Works

- **Zscaler** performs MITM: terminates your TLS, re-encrypts with its own cert (pre-installed in Windows cert store). httpx uses the Windows cert store → trusts it. BoringSSL does not trust it → `curl: (35) OPENSSL_internal:invalid library`.
- **Akamai** uses JA3 TLS fingerprinting: Python's TLS handshake has a different cipher suite order and extensions than Chrome → blocked. curl_cffi replays Chrome's exact BoringSSL handshake → allowed.
- The `_curl_cffi_broken` flag is session-scoped — once set, all subsequent `_qs_get()` calls use httpx without retry overhead.

### Old vs New QuickSpecs Fetch Approach

```
OLD (v0.3.13 and earlier) — broken from home
──────────────────────────────────────────────
[1] urllib → GET www.hpe.com/resource-library.html     ← BLOCKED by Akamai
    Full HTML page (~200KB), 60s timeout → ❌

[2] Parse HTML → extract buried Coveo API token from JS

[3] POST coveo.com/search → JSON results

3 round-trips, fragile token extraction, ~3-5s on good day

NEW (v0.3.14+) — works everywhere
───────────────────────────────────
[1] _qs_get() → GET www.hpe.com/.../medialibrary.model.json
    ?restype=quickspecs&search=DL380+Gen12
    ~5KB JSON, server-side filtered ✅

1 round-trip, stable public API, ~0.5-1s
```

### TLS Stack Comparison

```
urllib              curl_cffi (chrome)       httpx + HTTP/2
   │                      │                       │
   ▼                      ▼                       ▼
OpenSSL           Chrome BoringSSL           OpenSSL/system
TLS 1.2/1.3           TLS 1.3                  TLS 1.3
HTTP/1.1              HTTP/2                   HTTP/2
Python JA3            Chrome JA3               Python JA3
   │                      │                       │
   ▼                      ▼                       ▼
❌ Akamai blocks   ✅ Akamai passes         ✅ passes (JSON API)
✅ Zscaler passes  ❌ Zscaler blocks        ✅ Zscaler passes
```

### QuickSpecs Page Types

pcli qs handles three HPE page structures automatically:

```
Type 1 — Collateral HTML (e.g. DL380 Gen12 a00073551enw)
    GET collateral.{docid}.html → 200, has <main> tag
    → Parse <hpe-left-rail-container> with BeautifulSoup → markitdown

Type 2 — Old collateral HTML (e.g. DL360 Gen12 a50006984enw)
    GET collateral.{docid}.html → 200, no <main> tag (PSNow wrapper)
    → Fall back to PDF download → markitdown[pdf]

Type 3 — No collateral HTML (e.g. EL140 Gen12 a50009256enw)
    GET collateral.{docid}.html → 404
    → Fall back to PDF download → markitdown[pdf]
```

Type 1: fast (~1-2s). Types 2/3: slower (~5-10s, PDF download). All results cached to `~/.cache/pcli/qs/` — subsequent calls instant.

---

## 4. COM API Implementation

### Three Base URLs

| Tier | URL | Auth |
|------|-----|------|
| **ui-doorway** | `aquila-user-api.common.cloud.hpe.com` | Bearer (Okta) + ccs-session cookie |
| **COM regional** | `us-west.api.greenlake.hpe.com` | Bearer (GLP) only |
| **GLP global** | `global.api.greenlake.hpe.com` | Bearer (GLP client_credentials) |

### Key Paths

```
# COM API (regional)
# NOTE: /compute-ops/ deprecated April 2025 → migrate to /compute-ops-mgmt/
GET  /compute-ops/v1beta2/firmware-bundles          ← bundles list (old path, still works)
GET  /compute-ops-mgmt/v1beta2/servers              ← server list with firmwareInventory
POST /compute-ops-mgmt/v1/jobs                      ← create firmware update job
GET  /compute-ops-mgmt/v1beta1/activation-keys      ← iLO CloudConnect key

# GLP global — device registration
POST https://global.api.greenlake.hpe.com/devices/v1/devices
     {"compute": [{"serialNumber": "X", "partNumber": "Y"}], "network": [], "storage": []}

# ui-doorway — devices, workspaces
GET  /ui-doorway/ui/v1/devices
POST /authn/v1/session                              ← get ccs-session from id_token
```

### Response Envelope Inconsistencies

```python
# ui-doorway /devices
{"devices": [...], "pagination": {...}}          # snake_case, "devices" key

# COM API /servers
{"items": [...], "nextPageUri": "..."}           # camelCase, "items" key

# GLP /devices
{"items": [...]}
```

`client.get_all()` checks for all envelope keys (`items`, `devices`, `members`).

**Pagination URL bug:** `get_all()` must check `if next_page.startswith("http")` before prepending `base_url` — ui-doorway sometimes returns absolute `nextPageUri`.

### COM Server IDs vs Platform Device IDs

```
COM /servers response:
  id: "P54198-B21+MXQ3490J2G"     ← {PartNumber}+{SerialNumber} format

GLP /devices response:
  id: "054acc6a-1234-..."          ← UUID assigned at registration
```

These cannot be interchanged. `/servers/{id}/inventory` uses the COM format only. `_get_com_servers(client)` in `inventory.py` uses the COM `/servers` endpoint directly.

### COM Job Template IDs (permanent — do not change)

| Job Type | Template ID |
|----------|-------------|
| `ServerFirmwareUpdate` | `fd54a96c-cabc-42e3-aee3-374a2d009dba` |
| `ServerFirmwareDownload` | `0683ada8-1a89-49dd-bf04-6df715b708a6` |
| `ServerIloFirmwareUpdate` | `94caa4ef-9ff8-4805-9e97-18a09e673b66` |
| `GroupFirmwareUpdate` | `91159b5e-9eeb-11ec-a9da-00155dc0a0c0` |

---

## 5. Redfish Query Implementation (Gen11 vs Gen12)

### Storage Controllers

On Gen11/Gen12, `storage.StorageControllers[]` is always empty — must use the sub-collection:

```python
ctrl_link = storage.get("Controllers", {}).get("@odata.id")
for c in client.get(ctrl_link).obj.get("Members", []):
    ctrl = client.get(c["@odata.id"]).obj
```

**dl325-gen12 special case:** `Storage` returns **zero Members** — NVMe/SATA controllers only appear in `FirmwareInventory`. Always fall back to FirmwareInventory keyword scan.

### FirmwareInventory

```
GET /redfish/v1/UpdateService/FirmwareInventory
→ Members[] (each needs individual GET)
→ per item: Name, Version, Updateable (bool), SoftwareId
```

`Updateable: true` means iLO can flash it via UpdateService.

**FirmwareInventory does NOT contain `UpdatableBy`** — that field is only in `payload.json` or the sidecar `.json`.

**NIC firmware on iLO 6:** NOT in FirmwareInventory — read from `NetworkAdapters` endpoint instead. On Gen12 (iLO 7), some NICs appear in FirmwareInventory as PLDM targets with `SoftwareId` = PLDM GUID.

### PLDM Target GUID Matching (BCM NICs)

BCM `.json` sidecars have `Devices.Device[].Target` = PLDM UUID with PCI IDs embedded:
```
a6b1a447-382a-5a4f-14e4-16d714e41597
                   ^^^^─ 14e4 = Broadcom PCI vendor
                        ────── 16d7 = BCM57414 PCI device ID
```
`sdr.py` matches FirmwareInventory `SoftwareId` against sidecar Target GUIDs — reliable even when `Model` is a marketing name.

### OEM Actions Path (`AddFromUri` target)

| iLO version | Path |
|---|---|
| **iLO 6** (Gen10/11) | `UpdateService["Actions"]["Oem"]["Hpe"]["#HpeiLOUpdateServiceExt.AddFromUri"]` |
| **iLO 7** (Gen12) | `UpdateService["Oem"]["Hpe"]["Actions"]["#HpeiLOUpdateServiceExt.AddFromUri"]` |

`firmware.py::_oem_actions()` tries Gen12 path first, falls back to Gen11.

### iLO 7 Specific Quirks

- `Members[]` in ComponentRepository and UpdateTaskQueue contain stub objects `{"@odata.id": "..."}` — must expand each via individual GET.
- UEFI tasks **never marked Complete** after POST flash — stale Pending tasks remain forever. Always verify via FirmwareInventory version, not task status.
- `_run_fw_upgrade()` auto-clears all Pending/Complete tasks after post-reboot verification.

### `UpdatableBy` Rules

```
["Bmc"]                        ← iLO firmware only — iLO self-flashes
["Uefi"]                       ← everything else: BIOS, NIC, storage, CPLD
["Bmc", "RuntimeAgent", "Uefi"] ← NEVER use — iLO 7 splits into two subtasks;
                                   OS_task never fires without SUM in OS
```

---

## 6. fwpkg / Sidecar Internals

### Gen11 vs Gen12 Package Structure

| Style | Count in Gen12 SPP 2026.03.00.00 | Which packages |
|---|---|---|
| **Gen12 sidecar** (binary only in ZIP, `{stem}.json` separate) | 10 | Gen12 BIOS (`A66`, `U66`–`U77`) + iLO 7 |
| **Gen11 embedded** (`payload.json` bundled inside ZIP) | 124 | NICs (BCM), drives, storage controllers, iLO 6, etc. |

```
# Gen11 style (dominant — still used in Gen12 SPP for most components):
ilo6_174.fwpkg (ZIP, signed)
  ├── ilo6_174.bin
  ├── payload.json        ← install metadata (embedded, lowercase snake_case keys)
  ├── ilo6_174.xml
  └── readme.txt

# Gen12 sidecar style (BIOS + iLO 7 only):
ilo7_1.20.00.fwpkg (ZIP, signed)   ← contains ONLY the firmware binary
  └── ilo7_1.20.00.bin

ilo7_1.20.00.json   ← sidecar, NOT part of signed ZIP (CamelCase keys)
  → description, install notes, supported models, SHA256,
    UpdatableBy, FileList, UpgradeRequirements, RevisionHistory...
```

**Why sidecar exists:** In Gen11, any metadata update (new supported model, corrected notes) required re-signing and re-releasing the entire package. In Gen12, HPE seals only the binary; the `.json` sidecar can be updated at any time without touching the signed binary.

### Key `payload.json` Fields

```json
{
  "UefiFlashable": false,    // false + ResetRequired false → BMC
  "ResetRequired": false,    // true → UEFI
  "PLDMImage": true          // true → UEFI via PLDM sideband
}
```

| Flags | Method |
|---|---|
| `UefiFlashable: false`, `ResetRequired: false` | **BMC** — iLO self-flash |
| `UefiFlashable: true` OR `ResetRequired: true` | **UEFI** — applied at POST |
| `PLDMImage: true` | **UEFI** via PLDM (NIC, storage, backplane) |
| Only `.exe`/`.rpm`, `UpdatableBy: RuntimeAgent` | **OS** — SUM/iSUT |

### Key Schema Differences

| | Gen11 embedded `payload.json` | Gen12 sidecar `.json` |
|---|---|---|
| Key style | lowercase snake_case | CamelCase |
| Description | `{lang, x_late}` pairs | `[{Language, Value}]` |
| Location | Inside ZIP | Sibling file on disk/CDN |
| SHA256 covered | Entire ZIP | Not covered (separate file) |

**`pcli spp inspect`** tries sidecar first (`{stem}.json` sibling), falls back to embedded `payload.json`.

### BCM SDR Filename Format (inverted)

```
BCM235.1.164.14_BCM957414A4142HC.fwpkg
└─ version ───┘ └─ chip model ────────┘
```

Normal fwpkg format: `{model}_{version}.fwpkg`. BCM reverses this — `sdr.py` has special-case parsing.

---

## 7. pcli spp inspect — Output Section Sources

`pcli spp inspect <file.fwpkg>` output sections, in order:

| Section | Source |
|---------|--------|
| Header (filename, size, SHA256 badge) | filesystem stat + catalog `SHA256Sum` |
| Recommended/Critical/Optional | sidecar `Package.UpgradeRequirements` |
| Description excerpt | sidecar `Package.Description[lang=en]` |
| Files inside package | sidecar `Package.Files[0].FileList[]`; fallback: ZIP listing |
| Flash Properties (device, duration, PLDM, UpdatableBy) | sidecar `Devices.Device[]` + `FirmwareImages[0]` |
| Release Notes | sidecar `Package.RevisionHistory[0].Enhancements + BugFixes` (HTML stripped) |
| Installation Notes | sidecar `Package.InstallationNotes[lang=en]` (Gen12 only) |
| Readme | `readme.txt` from ZIP (Gen11 only, first 60 lines) |

---

## 8. SPP Catalog Composition

**Local cache layout** (`spp/` directory, gitignored):

```
spp/
└── gen12/
    ├── 2025.09.01.00/
    │   └── metadata.json
    └── 2026.03.00.00/
        ├── metadata.json
        └── packages/
            ├── ilo7_1.20.00.fwpkg
            ├── ilo7_1.20.00.json     ← Gen12 sidecar
            ├── ilo6_174.fwpkg
            ├── A66_1.40_01_09_2026.fwpkg
            └── A66_1.40_01_09_2026.json
```

**Gen12 SPP `2026.03.00.00` stats:**

| Type | Count | Avg size | Total |
|------|------:|----------|------:|
| `.fwpkg` | 134 | 9.0 MB | ~1.2 GB |
| `.exe` | 44 | 10.3 MB | ~455 MB |
| `.rpm` | 94 | 2.4 MB | ~224 MB |
| `.zip` | 27 | 5.3 MB | ~144 MB |
| `.deb` | 8 | 7.0 MB | ~56 MB |
| **Total** | **307** | | **~2.1 GB** |

For firmware-only work, only the 134 `.fwpkg` + sidecar `.json` files matter (~1.2 GB). The `.rpm`/`.exe`/`.zip`/`.deb` are OS-level drivers needing SUM/OS-agent.

**SDR URL pattern:**
```
https://downloads.linux.hpe.com/SDR/repo/fwpp-gen{N}/{YYYY.MM.00.00}/
```
`sdr.py::latest_pack_url(gen)` auto-discovers the latest pack. SDR covers HPE-branded components only.

---

## 9. classify_update_method() Rules

`ilo/inventory.py::classify_update_method()` — infers update method from component name patterns (used when `payload.json` is unavailable):

| Component pattern | Method | Logic |
|---|---|---|
| `ilo`, `integrated lights-out`, `ilo management controller` | **BMC** | Always iLO self-update |
| `system rom`, `system bios`, `bios` | **UEFI** | UEFI applies at POST |
| `smart array`, `mr4`, `ns204`, `boot controller`, `storage controller` | **UEFI** | PLDM via UEFI agent |
| `power management`, `power supply`, `cpld`, `upb`, `ubm` | **UEFI** | Low-level, UEFI or BMC secondary |
| `bcm`, `broadcom`, `mellanox`, `connectx`, `nvidia` + OCP context | **OS** | OCP3 NICs: no PLDM OOB on iLO 6 |
| `bcm`, `broadcom`, `mellanox`, `connectx`, `nvidia` + PCIe context | **UEFI** | PCIe NICs: PLDM OOB capable |
| `intel ` (trailing space), `intel(r)` | **OS** | RuntimeAgent only |
| Fallback | **UEFI** | Conservative default |

**Known gotcha:** `"intel"` matches inside `"intelligent power"`. Always use `"intel "` (trailing space) or `"intel(r)"`.

---

## 10. Lessons Learned

### iLO / Redfish

1. **Gen11+: `StorageControllers[]` is always empty** — must use `Storage/{id}/Controllers/` sub-collection.
2. **NIC firmware is NOT in FirmwareInventory on iLO 6** — read from `NetworkAdapters` endpoint.
3. **BCM SDR filenames have inverted format: `BCM{version}_{chipmodel}.fwpkg`** — version first.
4. **dl325-gen12: `Storage` has zero members** — always fall back to FirmwareInventory keyword scan.
5. **Gen12 OEM actions path differs from Gen11** — `Oem.Hpe.Actions` vs `Actions.Oem.Hpe`. `_oem_actions()` handles both.
6. **`UpdatableBy: ["Bmc"]` doesn't flash BIOS** — use `["Uefi"]`. `["Bmc","RuntimeAgent","Uefi"]` splits into two subtasks on iLO 7, OS_task never fires.
7. **iLO 7 never marks UEFI tasks Complete** — stale Pending is normal after POST flash. Always verify via FirmwareInventory.
8. **iLO 6 HttpPushUri often returns empty 400** — use `AddFromUri` instead. iLO 7 is fine.
9. **BCM957414 NIC stepping chain** — can't jump from 214.x to 235.x directly. `MinimumActiveVersion: 226.1.107.0`. Natural PLDM stepping ~1 step per SUM run + reboot.
10. **BCM OCP3 NIC (P10113-001) does NOT appear in FirmwareInventory on iLO 6** — no PLDM channel. Requires in-band `bnxtnvm`.
11. **Gen12 `.json` sidecar is a separate file from `.fwpkg`** — `sdr.py` already handles this correctly.
12. **`"intel"` matches inside `"intelligent power"`** — always use `"intel "` (trailing space).
13. **Autocomplete: set `_ARGCOMPLETE=2`** — `=1` strips only one level, `=2` strips two.
14. **iLO 7 ComponentRepository/UpdateTaskQueue return stub Members** — must expand each `{"@odata.id": "..."}` via individual GET.
15. **`masterdependency.xml` must be UTF-8 without BOM** — PowerShell `Set-Content -Encoding UTF8` adds BOM and crashes SUM. Use `[System.Text.UTF8Encoding]::new($false)`.
16. **`NO_APP_ACCOUNT = YES` in SUM INI causes 30+ min inventory hang on iLO 7** — remove it.
17. **iLO 6 (Gen11) allows only one TLS connection at a time** — `asyncio.gather()` across firmware inventory members causes the second `start_tls()` to fail with `httpx.ConnectError` (empty message). iLO 7 (Gen12) handles concurrent TLS connections fine. Fix: sequential loops in `_member_resources` / `_resource_list`. Symptom: first `describe` succeeds (reuses warm TLS), second fails (needs new handshake).

### COM / Compute Ops Management

17. **COM downloads individual `.fwpkg` files, NOT the full SPP ISO** — only applicable components are pulled from HPE CDN.
18. **No SUM inside iLO** — two native agents: BMC (immediate) and UEFI (POST reboot).
19. **FirmwareInventory on COM server objects has no `UpdatableBy` field** — must infer from name patterns.
20. **COM bundle API has no per-component sub-resource** — component-level data only in `payload.json`.
21. **`ccs-session` expires independently of `access_token`** — both required for ui-doorway; only full re-login restores ccs-session.
22. **`id_token` expires in ~5 min** — use immediately at login to set up workspace session.
23. **GLP token vs Okta token** — `us-west.api.greenlake.hpe.com` (COM) requires GLP OAuth2 token; Okta user token causes 404 "Routing details for customer not found".
24. **COM server IDs are `{PartNumber}+{SerialNumber}`** — platform device IDs are UUIDs; they cannot be interchanged.
25. **`ActivationKey` not `workspace_id` for iLO CloudConnect** — `EnableCloudConnect` needs the activation key from `/activation-keys`.
26. **`ProductID=NA` test units cannot be onboarded** — no supply chain record in GLP database.
27. **COM `/compute-ops/` prefix deprecated April 2025** — migrate to `/compute-ops-mgmt/` paths.
28. **Pagination URL bug** — `get_all()` must check `if next_page.startswith("http")` before prepending `base_url`.

### QS / Network

29. **curl_cffi Chrome TLS passes Akamai; httpx passes Zscaler** — they are mutually exclusive on corp network. `_qs_get()` auto-falls-back from curl_cffi to httpx on TLS error.
30. **`_curl_cffi_broken` is session-scoped** — set once on first TLS error; subsequent calls skip curl_cffi without retry overhead.
31. **HPE JSON search API (`medialibrary.model.json`) has lighter Akamai protection** — httpx + HTTP/2 passes; the full HTML resource library page requires Chrome TLS fingerprint.
32. **Lab WSL bypassed Akamai** — HPE corporate egress IPs are likely whitelisted by Akamai entirely; home/external IPs are not.

### PyInstaller Build Workflow

33. **Always use `.venv-build`** — `.venv` is dev; `.venv-build` has PyInstaller + all deps compiled for bundling.
34. **After version bump** — run `uv pip install -e .` against `.venv-build` before rebuild; old `pcli-X.Y.Z.dist-info` directories in `_internal/` must be manually removed after robocopy sync.
35. **Deploy:** `robocopy dist\pcli C:\Users\mahongj\Downloads\proliant-cli-windows /E /NFL /NDL /NJH /NJS`
