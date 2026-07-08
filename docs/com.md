---
title: COM
description: HPE Compute Ops Management — cloud device/server inventory and firmware bundles.
---

# COM (Compute Ops Management)

`proliant com` talks to the HPE Compute Ops Management (COM) cloud API. Unlike
`ilo` and `oneview`, it doesn't use a local inventory file — you authenticate
once (with Okta or your HPE GreenLake email/password) and the CLI stores a
token for subsequent calls.

## Login

```bash
proliant com login                                # Interactive login (Okta Verify push)
proliant com login --email you@hpe.com            # Pre-fill email, skip the prompt
proliant com logout
```

Login method is auto-detected from the email domain: `@hpe.com` accounts try
Okta Verify push first (falling back to a masked password prompt if the
account has no Okta Verify authenticator enrolled), and external accounts
(e.g. gmail.com) go straight to a masked password prompt.

## Inventory & reports

```bash
proliant com devices list                         # All devices in workspace
proliant com servers list                         # Servers with firmware info
proliant com servers describe <name>
proliant com bundles list                         # Available SPP bundles
proliant com bundles list --gen 12 --type base
proliant com reports gpu                          # GPU inventory report
proliant com reports memory
```

## Workspaces

If your account has access to multiple GreenLake workspaces, switch between
them without logging out:

```bash
proliant com workspaces list
proliant com workspaces use MyWorkspace           # Switch active workspace
```

## Screenshots

![COM screenshot placeholder](assets/placeholder-com.svg)

<!--
  HOW TO REPLACE THE PLACEHOLDER ABOVE (zero rebuild — just push):
  1. Drop a PNG into  docs/assets/  (e.g. com-gpu-report.png)
  2. Swap the line above for something like:

  ![COM GPU health report](assets/com-gpu-report.png)
-->

## Video walkthrough

![COM video walkthrough](assets/com-walkthrough.gif)
