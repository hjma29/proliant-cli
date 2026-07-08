---
title: Getting Started
description: Install proliant and connect it to your iLO, COM, or OneView environment.
---

# Getting Started

ProLiant CLI `proliant` is a cross-platform CLI for HPE ProLiant server environments. From a single
terminal you can inspect server inventory and firmware across **iLO
(Redfish)**, **Compute Ops Management (COM)**, and **Synergy OneView**, and
browse **Service Pack for ProLiant (SPP)** contents.

!!! warning "Disclaimer"
    This is a side project, not affiliated with or supported by HPE. Code in
    this repository was written with GitHub Copilot. Great for exploring and
    gathering information; exercise the usual caution with any change
    operations.

## See it in action

![proliant in action](assets/demo.gif)

## Install

=== "Windows"

    Run the one-liner in PowerShell — it downloads and launches the GUI
    installer:

    ```powershell
    Invoke-RestMethod https://raw.githubusercontent.com/hjma29/proliant-cli/main/install.ps1 | Invoke-Expression
    ```

    Or download `proliant-cli-windows-setup.exe` from the
    [latest release](https://github.com/hjma29/proliant-cli/releases/latest)
    and run it directly. It installs to `C:\Program Files\proliant-cli`, adds
    that folder to your PATH, and creates an Add/Remove Programs entry.

=== "Linux / macOS"

    ```bash
    sh -c "$(curl -fsSL https://raw.githubusercontent.com/hjma29/proliant-cli/main/install.sh)"
    ```


```bash
proliant --help
```

## Connect your first server

Run `proliant setup` to manage your local inventory file — a guided menu to view,
add, edit, or delete iLO servers (and, optionally, OneView appliance).

```bash
proliant setup
```

COM doesn't use a local inventory file — it authenticates against the cloud
API directly with `proliant com login`. See the [COM](com.md) page for
details.

## Where to go next

- **[iLO](ilo.md)** — direct Redfish management: firmware inventory and
  upgrades, NIC/storage/CPU/memory details, power and boot control.
- **[COM](com.md)** — Compute Ops Management device/server inventory,
  firmware bundles, GPU and health reports from the cloud API.
- **[OneView](oneview.md)** — server profiles, networks, interconnects, and
  an end-to-end fabric map with MAC tracing.
- **[Additional Setup](additional-setup.md)** — SPP browsing, shell
  completion, telemetry opt-out, and self-update.

## Video walkthrough

![Video walkthrough](assets/walkthrough.gif)

---

- [Source on GitHub](https://github.com/hjma29/proliant-cli)
- [Full README & command reference](https://github.com/hjma29/proliant-cli#readme)
- [Releases & downloads](https://github.com/hjma29/proliant-cli/releases)
