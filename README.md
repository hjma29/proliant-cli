# HPE ProLiant CLI

**proliant** is a CLI for HPE ProLiant environments. It lets you retrieve and inspect server inventory and details across **HPE ProLiant iLO**, **Compute Ops Management (COM)**, and **Synergy OneView** — and includes built-in tools to browse **HPE Service Pack for ProLiant (SPP)** release contents directly from the terminal.

Whether you manage a handful of bare-metal servers or a large fleet across multiple management platforms, `proliant` gives you a single consistent interface — cross-platform and scriptable. Query firmware versions across hundreds of iLO nodes in seconds, browse your Compute Ops Management device inventory, or inspect servers managed by HPE Synergy OneView.

> **Disclaimer:** This is a side project — not affiliated with or supported by HPE. Code in this repository was written with GitHub Copilot. Great for exploring and gathering information; exercise the usual caution with any change operations.

## Installation

### Linux / macOS

```bash
sh -c "$(curl -fsSL https://raw.githubusercontent.com/hjma29/proliant-cli/main/install.sh)"
```

### Windows

Run the one-liner in PowerShell — it downloads and launches the GUI installer
(accept the single UAC prompt):

```powershell
Invoke-RestMethod https://raw.githubusercontent.com/hjma29/proliant-cli/main/install.ps1 | Invoke-Expression
```

Or download `proliant-cli-windows-setup.exe` from the
[latest release](https://github.com/hjma29/proliant-cli/releases/latest) and
run it directly. It installs to `C:\Program Files\proliant-cli`, adds that
folder to your PATH, and creates an Add/Remove Programs entry.

## Screenshots

![proliant demo](docs/assets/demo.gif)

![proliant demo](docs/assets/demo.svg)

Full docs with per-page screenshots and videos:
[hjma29.github.io/proliant-cli](https://hjma29.github.io/proliant-cli/)

## Usage

```
proliant ilo <resource> <action>      # Direct iLO Redfish management
proliant com <resource> <action>      # HPE Compute Ops Management
proliant oneview <resource> <action>  # HPE OneView management
proliant spp <action>                 # HPE Service Pack for ProLiant (SPP)
```

Use `--help` at any level (`proliant ilo --help`, `proliant ilo firmware --help`) for full options.

### Getting started

Run `proliant setup` to manage your local inventory file — a guided menu to view,
add, edit, or delete iLO servers (and, optionally, a OneView appliance).
The entries table shows a live Status column (Reachable / Timeout /
Unreachable / Auth failed), checked in parallel on start and refreshed after
any change. Safe to run any time to add, change, or remove entries.

```bash
proliant setup
```

### iLO

Talks directly to iLO via Redfish. Requires a local inventory file — run `proliant setup` to create one.

```bash
# Inventory
proliant ilo servers list                        # List all configured hosts
proliant ilo servers describe <host>             # Full server details
proliant ilo firmware list                       # Firmware summary across all hosts
proliant ilo firmware list <host>                # Firmware for a specific host
proliant ilo firmware list --fields bios,ilo,nic-fw
proliant ilo nic list                            # NIC link state + MAC address
proliant ilo storage list                        # Storage controllers + drives
proliant ilo cpu list                            # CPU models + microcode
proliant ilo memory list                         # DIMM details
proliant ilo reports memory                      # Fleet memory report

# Firmware upgrade
proliant ilo firmware upgrade <host> --dry-run   # Preview without changes
proliant ilo firmware upgrade <host>             # Upgrade from HPE SDR
proliant ilo firmware upgrade <host> --reboot    # Upgrade and reboot

# Power / boot
proliant ilo power reset <host>
proliant ilo boot describe <host>
proliant ilo boot set <host> pxe
```

### COM

```bash
proliant com login                               # Login (Okta or email/password)
proliant com logout
proliant com devices list                        # All devices in workspace
proliant com servers list                        # Servers with firmware info
proliant com servers describe <name>
proliant com bundles list                        # Available SPP bundles
proliant com bundles list --gen 12 --type base
proliant com workspaces list
proliant com workspaces use MyWorkspace           # Switch active workspace
proliant com reports gpu                         # GPU inventory report
proliant com reports memory
```

### OneView

Requires a local inventory file with a `[oneview]` (or `type = oneview`) section — run `proliant setup` to add one.

```bash
proliant oneview servers list
proliant oneview servers firmware list
proliant oneview servers firmware list --server "Enclosure-01, bay 1"
proliant oneview firmware bundles
proliant oneview firmware repository
proliant oneview firmware compliance
proliant oneview networks list
proliant oneview networks describe <name>
proliant oneview networksets list
proliant oneview networksets describe <name>
proliant oneview uplinksets list
proliant oneview uplinksets describe <name>
proliant oneview server-profiles list
proliant oneview server-profiles describe <name>
proliant oneview enclosures list
proliant oneview enclosures describe <name>
proliant oneview mac list --address <mac>
proliant oneview mac list --network-name <name>
proliant oneview mac describe <mac>
proliant oneview reports memory
proliant oneview upgrade readiness              # pre-upgrade readiness report
proliant oneview upgrade cleanup                # preview unused firmware baselines
proliant oneview upgrade cleanup --yes          # delete unused baselines (free disk)
proliant oneview appliances list                # multiple appliances configured? * = active
proliant oneview appliances use datacenter-b    # switch which appliance commands target
```

#### Upgrade readiness & disk cleanup

`proliant oneview upgrade readiness` is a **read-only** pre-upgrade check. It reports
the appliance software version, the supported Synergy Composer upgrade path (with the
recommended next hop and full milestone chain to the latest release), and a PASS/WARN/FAIL
assessment of appliance disk space, memory/CPU, active alerts, backup freshness, logical
interconnect consistency, and interconnect redundancy. It never modifies anything.

`proliant oneview upgrade cleanup` frees appliance disk by removing **unused** firmware
baselines (SPP/SSP) — those not assigned to any logical enclosure, logical interconnect,
or server profile, and older than your currently-assigned baseline. Newer unused baselines
are retained as upgrade targets. It defaults to a dry-run preview; pass `--yes` to delete.
This only removes files from the appliance repository and never touches running enclosures
or interconnects (OneView also blocks deletion of any in-use baseline server-side).
Unused baselines that only exist in an **external** firmware repository (added under
Firmware Bundles > External Repositories) are listed separately as informational — OneView
never allows deleting these via the API, and their reported size isn't appliance disk, so
they're excluded from the reclaimable total.

`proliant oneview firmware compliance` checks every firmware-managed server profile
against each registered baseline that's newer than what's currently assigned anywhere
(the same "candidate" bundles `upgrade cleanup` retains as upgrade targets), using
OneView's real per-component compliance check. Each row shows whether an update is
required and how many components need it. The GUI's Update Category
(Recommended/Optional) and Estimated Update Time columns are computed by an internal
component-diff engine and aren't exposed via the REST API, so they aren't shown here.

### SPP (Service Pack for ProLiant)

```bash
proliant spp list                                # List available SPP releases
proliant spp inspect <version>                   # Inspect SPP contents
proliant spp diff <version1> <version2>          # Compare two SPP releases
```

## Automation / scripting

Every `ilo`/`com`/`oneview` sub-CLI accepts a `--json` flag for piping to `jq`
(Linux/macOS) or `ConvertFrom-Json` (PowerShell) instead of printing a Rich
table. It's a flag on the sub-CLI itself, so it goes right after
`ilo`/`com`/`oneview`, before the resource/action:

```bash
proliant ilo --json firmware list | jq -r '.[] | select(.BIOS < "2.90") | .Server'
proliant com --json servers list | jq -r '.[] | select(.Health != "OK") | .Name'
```

```powershell
proliant ilo --json firmware list | ConvertFrom-Json | Where-Object BIOS -lt "2.90" | Select-Object Server
proliant com --json servers list | ConvertFrom-Json | Where-Object Health -ne "OK"
```

Plain-text output can also be filtered directly without `--json`:

```bash
proliant com servers list | grep hsthyperv
```
```powershell
proliant com servers list | Select-String "hsthyperv"
```

`proliant ilo` additionally supports `--raw` on most `list`/`describe` commands,
which dumps the unprocessed Redfish API response (bypassing proliant's own
field parsing) — useful for inspecting fields the table/`--json` don't surface.

Chain commands together with `--hosts-from -` to read target hosts from stdin:

```bash
proliant ilo firmware list --json | jq -r '.[] | select(.BIOS < "2.90") | .Server' \
  | xargs -n1 proliant ilo firmware upgrade
```

## Self-update

```bash
proliant version                                 # Show installed version; offers to upgrade if a newer release exists
```
