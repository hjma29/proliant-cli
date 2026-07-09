---
title: OneView
description: HPE Synergy OneView — server profiles, networking, and firmware compliance.
---

# OneView

`proliant oneview` manages servers through an HPE Synergy OneView appliance.
It requires a local inventory file with a `[oneview]` (or `type = oneview`)
section — run `proliant setup` to add one.




```bash
proliant oneview servers list
proliant oneview server-profiles list
proliant oneview server-profiles describe <name>
proliant oneview networks list
proliant oneview networks describe <name>
proliant oneview uplinksets list
proliant oneview uplinksets describe <name>
proliant oneview mac list --address <mac>
proliant oneview mac list --network-name <name>
proliant oneview mac describe <mac>
```


## Screenshots

![proliant oneview servers list](assets/oneview-screenshot.svg)

<!--
  ADD MORE REAL-USAGE SCREENSHOTS HERE (zero rebuild — just push):
  1. Drop a PNG into  docs/assets/  (e.g. oneview-fabric-map.png)
  2. Add another image line below, e.g.:

  ![OneView fabric map with MAC tracing](assets/oneview-fabric-map.png)
-->

