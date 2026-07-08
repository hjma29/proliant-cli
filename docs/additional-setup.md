---
title: Additional Setup
description: SPP browsing, shell completion, telemetry, and self-update.
---

# Additional Setup

Extra features and configuration that apply across `ilo`, `com`, and
`oneview`.

## SPP (Service Pack for ProLiant)

Browse Service Pack for ProLiant release contents without downloading the
full ISO:

```bash
proliant spp list                                # List available SPP releases
proliant spp inspect <version>                   # Inspect SPP contents
proliant spp diff <version1> <version2>          # Compare two SPP releases
```

## Local inventory file

`proliant setup` is the guided way to manage your local inventory file — it
can view, add, edit, or delete entries (both iLO hosts and an optional
`[oneview]` appliance section), test connectivity live, and open the file
directly in your `$EDITOR`/`$VISUAL` if you'd rather hand-edit it. Every
save keeps rotating backups, so an accidental edit or deletion is always
recoverable.

```bash
proliant setup
```

`proliant` looks for the local inventory file in this order: an explicit env
override, then `~/.config/proliant/ilo/`, then the current directory. It's
only used by `ilo` and `oneview` — `com` authenticates against the cloud API
directly.

## Shell completion

Both installers (`install.ps1` / `install.sh`) wire up dynamic tab completion
automatically — PowerShell, bash, and zsh are all supported out of the box.
If completion ever stops working after a shell/profile change, open a new
terminal; if it's still missing, re-run the installer to regenerate the
completion block.

## Self-update

```bash
proliant version    # Show installed version; offers to upgrade if a newer release exists
```

## Telemetry

`proliant` sends anonymous, best-effort usage counters (no personal data) to
help understand adoption. Set the following to opt out:

```bash
export PROLIANT_NO_TELEMETRY=1
```

## Screenshots

![Additional Setup screenshot placeholder](assets/placeholder-setup.svg)

<!--
  HOW TO REPLACE THE PLACEHOLDER ABOVE (zero rebuild — just push):
  1. Drop a PNG into  docs/assets/  (e.g. setup-menu.png)
  2. Swap the line above for something like:

  ![proliant setup guided menu](assets/setup-menu.png)
-->

## Video walkthrough

<!--
  [![Watch: local inventory file setup walkthrough](assets/setup-demo-thumb.png)](https://youtu.be/YOUR_VIDEO_ID)
-->

_Coming soon._
