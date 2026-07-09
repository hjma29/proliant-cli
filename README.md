# HPE ProLiant CLI

<!--docs-sync:start-->
**ProLiant CLI** is a terminal CLI tool for HPE ProLiant server environments. It lets you retrieve and inspect server inventory and details across **HPE ProLiant iLO**, **Compute Ops Management (COM)**, and **Synergy OneView** — and includes built-in tools to browse **HPE Service Pack for ProLiant (SPP)** release contents directly from the terminal.

Whether you manage a handful of bare-metal servers or a large fleet across multiple management platforms, ProLiant CLI gives you a single consistent interface. Query firmware versions across hundreds of iLO nodes in seconds, browse your Compute Ops Management device inventory, or inspect servers managed by HPE Synergy OneView.



## Screenshots

![proliant demo](https://hjma29.github.io/proliant-cli/assets/demo.gif)


> **Disclaimer:** This is an independent project — not affiliated with, endorsed by, or supported by HPE. The code in this repository was written with GitHub Copilot. Provided as-is with no warranty; you're responsible for any impact to your hardware. Safe for read-only exploration — use normal caution with any change operation.

## Installation



### Windows

Run the one-liner in PowerShell — it downloads and launches the GUI installer
(accept the single UAC prompt):

```powershell
Invoke-RestMethod https://raw.githubusercontent.com/hjma29/proliant-cli/main/install.ps1 | Invoke-Expression
```

Or grab `proliant-cli-windows-setup.exe` directly:

[![Download for Windows](https://img.shields.io/badge/Download-Windows-0078D6?style=for-the-badge&logo=windows)](https://github.com/hjma29/proliant-cli/releases/latest/download/proliant-cli-windows-setup.exe)

### Linux / macOS

```bash
sh -c "$(curl -fsSL https://raw.githubusercontent.com/hjma29/proliant-cli/main/install.sh)"
```


## Video walkthrough for Windows .exe Download Wizard Installation

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
[snip]
```


### SPP (Service Pack for ProLiant)

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



More information can be found at 
[hjma29.github.io/proliant-cli](https://hjma29.github.io/proliant-cli/)
