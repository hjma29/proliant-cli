# HPE ProLiant CLI

<!--docs-sync:start-->
[![GitHub Downloads](https://img.shields.io/github/downloads/hjma29/proliant-cli/total?label=downloads)](https://github.com/hjma29/proliant-cli/releases)
[![GitHub Release](https://img.shields.io/github/v/tag/hjma29/proliant-cli?color=blue&label=release)](https://github.com/hjma29/proliant-cli/releases/latest)

**ProLiant CLI** is a terminal CLI tool for HPE ProLiant server environments. It lets you retrieve and inspect server inventory and details across **HPE ProLiant iLO**, **Compute Ops Management (COM)**, and **Synergy OneView** — and includes built-in tools to browse **HPE Service Pack for ProLiant (SPP)** release contents directly from the terminal.

Whether you manage a handful of bare-metal servers or a large fleet across multiple management platforms, ProLiant CLI gives you a single consistent interface. Query firmware versions across hundreds of iLO nodes in seconds, browse your Compute Ops Management device inventory, or inspect servers managed by HPE Synergy OneView.



## Screenshots

![proliant demo](https://hjma29.github.io/proliant-cli/assets/demo.gif)


> **Note:** An independent project (not an official HPE tool), built with GitHub Copilot. Read-only commands are safe to explore — use caution with real hardware state change.

## Installation



### Windows

Download `setup.exe`:

[![Download for Windows](https://img.shields.io/badge/Download-Windows-0078D6?style=for-the-badge&logo=windows)](https://github.com/hjma29/proliant-cli/releases/latest/download/proliant-cli-windows-setup.exe)


Or run the one-liner installation script in Windows Terminal or PowerShell — it downloads and launches the GUI installer.

```powershell
Invoke-RestMethod https://raw.githubusercontent.com/hjma29/proliant-cli/main/install.ps1 | Invoke-Expression
```


### Linux / macOS

```bash
sh -c "$(curl -fsSL https://raw.githubusercontent.com/hjma29/proliant-cli/main/install.sh)"
```


## Video Walkthrough for Windows `setup.exe` Wizard Installation

![Video walkthrough](docs/assets/walkthrough.gif)



## Usage

```
proliant ilo <resource> <action>      # Direct iLO Redfish management
proliant com <resource> <action>      # HPE Compute Ops Management
proliant oneview <resource> <action>  # HPE OneView management
proliant spp <action>                 # HPE Service Pack for ProLiant (SPP)
```



### Connect your first server

Run `proliant setup` to manage your local inventory file — a guided menu to view, add, edit, or delete iLO servers (and, optionally, OneView appliances).

```bash
proliant setup
```

### iLO


```bash
proliant ilo servers list                        
proliant ilo servers describe <server name>      
[snip]
```

### COM

```bash
proliant com login                               
proliant com whoami                              
proliant com servers list                        
proliant com servers describe <server name>
proliant com reports gpu                        
proliant com reports memory
[snip]
```

### OneView

```bash

proliant oneview networks list
proliant oneview networks describe <name>
proliant oneview uplinksets list
proliant oneview uplinksets describe <name>
proliant oneview server-profiles list
proliant oneview server-profiles describe <name>
proliant oneview mac list --address <mac>
proliant oneview mac list --network-name <name>
proliant oneview mac describe <mac>
proliant oneview interconnects list
proliant oneview interconnects describe <name>          # ports, utilization, firmware baseline (matches GUI detail page)
proliant oneview appliances list                        # list configured appliances (* = active)
proliant oneview appliances describe [name]             # appliance General page (HA nodes, memory, uptime, firmware)
proliant oneview firmware bundles                        # registered SPP/SSP baselines
proliant oneview firmware compliance                     # per-server compliance vs newer baselines
proliant oneview release                                 # HPE Synergy Software Releases matrix (Composer <-> recommended/supported SSP)
proliant oneview activity                                # recent tasks + alerts, newest first (mirrors the GUI Activity page)
proliant oneview activity --resource <name> --limit 30   # filter the feed to one resource (e.g. LE01, Enclosure-01)
proliant oneview activity --state Error                  # only failed operations (or --tasks-only / --alerts-only)
proliant oneview activity --tree --resource LE01         # expand one operation's subtask tree (per-interconnect phase/percent)
proliant oneview activity --watch --resource LE01        # live-follow a running operation until it finishes (the GUI Activity view)
proliant oneview update enclosure                        # no NAME -- interactive step-by-step wizard (numbered menus; 'b' back, 'c' cancel)
proliant oneview update enclosure <LE-name>              # plan an SSP rollout to one logical enclosure (shared infra only)
proliant oneview update enclosure <LE-name> --baseline <ssp> --scope shared-infra-and-profiles
#   --scope shared-infra            updates frame link modules + interconnects only (default)
#   --scope shared-infra-and-profiles   also updates every server profile in this enclosure's compute modules
proliant oneview update enclosure <LE-name> --execute    # apply it (reboots interconnects, and compute if selected)
#   The plan shows an OneView<->SSP compatibility note (per HPE's Synergy Software Releases matrix).
proliant oneview update enclosure <LE-name> --execute --activation-mode parallel
#   --activation-mode orchestrated (default)  one redundant side at a time, non-disruptive -- requires real redundancy
#   --activation-mode parallel                flashes every interconnect at once regardless of redundancy (disruptive;
#                                              the only way to force firmware onto a genuinely non-redundant fabric)
proliant oneview update appliance readiness              # pre-upgrade readiness report
proliant oneview update appliance run --from-dir <dir>   # pick + stage an appliance software update
proliant oneview update appliance run --image <file> --execute   # stage + install (reboots the appliance)
proliant oneview update appliance pending                # show the currently staged update
proliant oneview update appliance cancel --yes           # remove a stuck staged update
proliant oneview update appliance cleanup                # preview unused firmware baselines to free disk
```


### SPP

```bash
proliant spp list                                
proliant spp inspect <version>                   
proliant spp diff <version1> <version2>          
```


## Self-update

```bash
proliant version                                 # Show installed version; offers to upgrade if a newer release exists
```


<!--docs-sync:end-->



## Full documentation

[![View full docs](https://img.shields.io/badge/View%20full%20docs-hjma29.github.io%2Fproliant--cli-1f6feb?style=for-the-badge&logo=materialformkdocs&logoColor=white)](https://hjma29.github.io/proliant-cli/)
