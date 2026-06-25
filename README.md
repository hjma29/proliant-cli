# proliant — HPE ProLiant unified CLI

> This is a hobby project — not affiliated with or supported by HPE. Great for exploring and gathering information; exercise the usual caution with any change operations.

**proliant** is a unified CLI for HPE ProLiant environments. It lets you retrieve and inspect server inventory and details across **HPE ProLiant iLO**, **Compute Ops Management (COM)**, and **OneView** — and includes built-in tools to browse **HPE Service Pack for ProLiant (SPP)** release contents and fetch **HPE QuickSpec** documents directly from the terminal.

Whether you manage a handful of bare-metal servers or a large fleet across multiple management platforms, `proliant` gives you a single consistent interface — cross-platform, no Python required. Query firmware versions across hundreds of iLO nodes in seconds, browse your Compute Ops Management device inventory, inspect servers managed by HPE OneView, or pull up the latest QuickSpec for any ProLiant model — all without opening a browser or logging into a GUI.

![proliant demo](docs/demo.gif)

## Installation

### Linux / macOS

```bash
sh -c "$(curl -fsSL https://raw.githubusercontent.com/hjma29/proliant-cli/main/install.sh)"
```

### Windows

```powershell
irm https://raw.githubusercontent.com/hjma29/proliant-cli/main/install.ps1 | iex
```

## Usage

```
proliant ilo <resource> <action>      # Direct iLO Redfish management
proliant com <resource> <action>      # HPE Compute Ops Management
proliant oneview <resource> <action>  # HPE OneView management
proliant spp <action>                 # HPE Service Pack for ProLiant (SPP)
proliant qs <action>                  # HPE ProLiant QuickSpecs reader
```

Use `--help` at any level (`proliant ilo --help`, `proliant ilo firmware --help`) for full options.

### iLO

Talks directly to iLO via Redfish. Requires a `hosts-ilo.ini` in the current directory — run `proliant ilo init` to create one.

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
proliant com login                               # Login (Okta or --api-client)
proliant com logout
proliant com devices list                        # All devices in workspace
proliant com servers list                        # Servers with firmware info
proliant com servers describe <name>
proliant com bundles list                        # Available SPP bundles
proliant com bundles list --gen 12 --type base
proliant com workspaces list
proliant com reports gpu                         # GPU inventory report
proliant com reports memory
```

### OneView

```bash
proliant oneview servers list
proliant oneview servers describe <name>
proliant oneview firmware list
proliant oneview networks list
proliant oneview server-profiles list
proliant oneview reports memory
```

### SPP (Service Pack for ProLiant)

```bash
proliant spp list                                # List available SPP releases
proliant spp inspect <version>                   # Inspect SPP contents
proliant spp diff <version1> <version2>          # Compare two SPP releases
```

### QuickSpecs

```bash
proliant qs list --model dl380gen11              # Find QuickSpec revisions
proliant qs describe <doc-id>                    # Read as formatted markdown
proliant qs diff <doc-id1> <doc-id2>             # Compare two versions
```

## Self-update

```bash
proliant update                                  # Update to the latest release
```
