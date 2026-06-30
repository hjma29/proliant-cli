---
title: proliant-cli
description: HPE ProLiant unified CLI — one terminal for iLO, COM, and OneView
---

# proliant-cli

_HPE ProLiant unified CLI — one terminal for iLO, COM, and OneView._

**[⭐ View the project on GitHub → github.com/hjma29/proliant-cli](https://github.com/hjma29/proliant-cli)**

**proliant** is a unified, cross-platform CLI for HPE ProLiant environments. From a
single terminal you can inspect server inventory and firmware across **iLO
(Redfish)**, **Compute Ops Management (COM)**, and **Synergy OneView**, browse
**Service Pack for ProLiant (SPP)** contents, and pull **QuickSpec** documents —
no Python or GUI required.

> Disclaimer: a side project, not affiliated with or supported by HPE.

---

## See it in action

![proliant in action](demo.gif)

---

## Install

**Windows (PowerShell)**

```powershell
Invoke-RestMethod https://raw.githubusercontent.com/hjma29/proliant-cli/main/install.ps1 | Invoke-Expression
```

**Linux / macOS**

```bash
sh -c "$(curl -fsSL https://raw.githubusercontent.com/hjma29/proliant-cli/main/install.sh)"
```

Then run `proliant --help` in a new terminal.

---

## What it does

- **iLO (Redfish)** — firmware inventory and upgrades, NIC/storage/CPU/memory
  details, power and boot control, fleet reports.
- **Compute Ops Management** — device and server inventory, firmware bundles,
  GPU and health reports from the cloud API.
- **Synergy OneView** — server profiles, networks, interconnects, and an
  end-to-end fabric map with MAC tracing.
- **SPP** — browse Service Pack for ProLiant release contents.
- **QuickSpecs** — fetch HPE ProLiant QuickSpec documents in the terminal.

---

## Screenshots

The animated demo above shows a typical session. Add individual screenshots
below — they live in `docs/images/`.

<!--
  HOW TO ADD A SCREENSHOT (zero rebuild — just push):
  1. Drop a PNG into  docs/images/   (e.g. firmware-table.png)
  2. Copy one line like the example below, outside this comment block.

  ![Firmware inventory across the fleet](images/firmware-table.png)
  ![OneView fabric map with MAC tracing](images/oneview-map.png)
  ![COM GPU health report](images/com-gpu.png)
-->

---

## Videos

<!--
  HOW TO ADD A VIDEO (link a thumbnail image to the video URL):
  1. Save a thumbnail into  docs/images/  (e.g. demo-thumb.png)
  2. Copy one line like the example below, outside this comment block.

  [![Watch: firmware upgrade walkthrough](images/demo-thumb.png)](https://youtu.be/YOUR_VIDEO_ID)
-->

_Coming soon._

---

## Links

- [Source on GitHub](https://github.com/hjma29/proliant-cli)
- [Full README & command reference](https://github.com/hjma29/proliant-cli#readme)
- [Releases & downloads](https://github.com/hjma29/proliant-cli/releases)
