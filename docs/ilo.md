---
title: iLO
description: Direct iLO Redfish management — firmware, inventory, power, and boot control.
---

# iLO

`proliant ilo` talks directly to a server's iLO over Redfish — no COM/OneView
appliance required. It requires an `inventory.ini` with the server's iLO
address and credentials; run [`proliant setup`](index.md#connect-your-first-server)
to create one.

## Inventory

```bash
proliant ilo servers list                        # List all configured hosts
proliant ilo servers describe <host>              # Full server details
proliant ilo firmware list                        # Firmware summary across all hosts
proliant ilo firmware list <host>                  # Firmware for a specific host
proliant ilo firmware list --fields bios,ilo,nic-fw
proliant ilo nic list                             # NIC link state + MAC address
proliant ilo storage list                         # Storage controllers + drives
proliant ilo cpu list                              # CPU models + microcode
proliant ilo memory list                           # DIMM details
proliant ilo reports memory                        # Fleet memory report
```

## Firmware upgrade

Firmware is staged and queued through iLO's own repository/task-queue
mechanism (HPE SDR packages), then applied by iLO/UEFI — no SPP ISO or
Virtual Media involved.

```bash
proliant ilo firmware upgrade <host> --dry-run    # Preview without changes
proliant ilo firmware upgrade <host>               # Upgrade from HPE SDR
proliant ilo firmware upgrade <host> --reboot      # Upgrade and reboot
```

Always try `--dry-run` first against a new host or a firmware family you
haven't upgraded before.

## Power / boot

```bash
proliant ilo power reset <host>
proliant ilo boot describe <host>
proliant ilo boot set <host> pxe
```

## Screenshots

![iLO screenshot placeholder](assets/placeholder-ilo.svg)

<!--
  HOW TO REPLACE THE PLACEHOLDER ABOVE (zero rebuild — just push):
  1. Drop a PNG into  docs/assets/  (e.g. ilo-firmware-table.png)
  2. Swap the line above for something like:

  ![Firmware inventory across the fleet](assets/ilo-firmware-table.png)
-->

## Video walkthrough

<!--
  [![Watch: iLO firmware upgrade walkthrough](assets/ilo-demo-thumb.png)](https://youtu.be/YOUR_VIDEO_ID)
-->

_Coming soon._
