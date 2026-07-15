# Changelog

All notable changes are documented here. Binaries for Windows, Linux (x86), Linux (ARM64), and macOS are attached to each release.

---

## v1.0.37 — 2026-07-10

### New Features
- `proliant oneview activity`: shows OneView's **Activity** feed — the named operations it ran (`/rest/tasks`: firmware updates, refreshes, background inventory collection) merged with health/condition alerts (`/rest/alerts`), newest first, mirroring the OneView GUI's Activity page (Name, Resource, Date, Duration, State, Owner). This is where a firmware update's real per-phase progress and, crucially, its actual failure reason show up. By default the feed lists only top-level operations (matching the GUI, which hides each operation's subtask tree behind an expander) and shows a live running-duration for in-progress tasks. Drill into an operation's nested subtask tree — the exact hierarchy the GUI shows when you expand a row, with each subtask's own state/percent and live phase text (e.g. "Stage firmware 80% completed", "Initiating loading of images on the interconnect / reboot") — with `--tree [NAME]`, or **live-follow** a running operation until it finishes with `--watch` (refresh interval `--interval SECONDS`). This is the CLI equivalent of watching the GUI Activity page during a firmware rollout, so a multi-minute "Logical enclosure firmware update" no longer looks stuck at the parent task's flat 0% while its interconnects actually flash underneath. Filter the feed with `--resource NAME` (substring, e.g. `LE01`), `--state STATE` (e.g. `Error`), `--limit N`, `--all-tasks` (include subtasks), or restrict to `--tasks-only`/`--alerts-only`; `--json` for scripting. Inline resource references embedded in task/alert text are shown by name instead of raw `{"name":…,"uri":…}` JSON.
- `proliant oneview update enclosure --concurrency N` (`--scope shared-infra-and-profiles` only): submits up to `N` server-profile firmware updates at once instead of always one at a time. Default is `1` (unchanged, fully sequential) — researched against HPE's own tooling first: no official OneView REST API, PowerShell library, Python SDK, or Ansible collection updates server-profile firmware in bulk either, they all loop one profile at a time, so sequential remains the safe default. Profiles are submitted in waves of up to `N`; each wave's requests are polled to completion before the next wave starts. A failed/blocked/unverified profile does **not** stop later waves — each profile targets an independent physical server, so one server's firmware trouble has no bearing on whether the rest of the batch can still update safely (see the Bug Fixes entry below for why this matters even at the default `N=1`). The confirmation panel now also warns how many compute modules will power-cycle *simultaneously* when `N > 1`, and the live progress display shows one bar per in-flight profile instead of a single reused bar.
- `proliant oneview server-profiles reapply <NAME>`: the CLI equivalent of the OneView GUI's server-profile "Reapply configuration" action — fetches the profile's current, already-stored configuration and PUTs it straight back unmodified, making OneView reconcile whatever is actually out of sync on the live hardware (network/storage settings, BIOS, boot order, firmware consistency). This is what clears alerts like *"Server hardware has been inserted into the enclosure bay — Resolution: Reapply the server profile"* (e.g. after a hardware re-insertion or an eFuse power-cycle) without changing any profile setting. Prompts with a type-to-confirm safety gate before touching live hardware (skip with `--yes`); shows a live progress bar for the underlying task, same as `update enclosure`.
- `proliant oneview update enclosure --scope profiles-only`: updates just the targeted logical enclosure's server-profile compute firmware and skips the logical-enclosure/interconnect step entirely — no GUI equivalent (the GUI's "Update firmware" dialog always bundles shared infra in). Added after a live rollout got stuck with shared infra reporting "unverified" (OneView said the interconnect update completed, but it hadn't actually installed), which blocked all 6 pending server-profile firmware updates behind it indefinitely under the existing shared-infra-first ordering. A server-profile firmware PUT is its own independent OneView operation — not HPE-documented as depending on the LE rollout finishing — so `profiles-only` lets compute firmware proceed on its own schedule while the shared-infra issue is investigated separately.
- `proliant oneview server-profiles update <NAME>`: rolls out an SSP firmware baseline to one named server profile's compute module directly, without requiring (or touching) its logical enclosure at all — narrower than `update enclosure --scope profiles-only`, which still updates every profile under a given LE. Useful for bringing a single server current (e.g. right after an `efuse`/`reapply`) without waiting on or affecting its enclosure-mates. Same plan/`--execute` split, baseline/install-type/force flags, and confirm-before-apply safety gate as `update enclosure`.

### Bug Fixes
- `proliant oneview update enclosure --scope profiles-only` / `--scope shared-infra-and-profiles`: fixed a batch of server-profile firmware updates silently stopping after the *first* profile that failed/blocked/unverified, never even attempting the rest of the targeted profiles — confirmed live: a 6-profile `profiles-only` batch stopped dead after the first server's drive firmware hung and failed, leaving 5 completely unrelated servers untouched with no indication they'd been skipped. Each server profile targets an independent physical server, so one server's firmware trouble has no bearing on whether the others can still update safely — every targeted profile is now always attempted, and the result summary reports each failed/blocked/unverified profile individually (not just whichever one happened to be last) alongside how many succeeded normally. This applies at any `--concurrency`, including the default of `1`.
- `proliant oneview update enclosure --execute`: a failed firmware update now shows OneView's own actionable error reason and remediation inline instead of an unhelpful "Check the OneView UI / 'proliant oneview reports'" (a command that has nothing to do with firmware). For example, forcing an update through a non-redundant fabric with `--activation-mode parallel` while the enclosure's compute modules are still powered on now reports OneView's real error — *"…servers are currently powered on. Firmware update cannot be initiated until the listed servers within the logical enclosure are powered off."* plus its "Power off the listed servers and retry" recommended action — pulled from the task's own `taskErrors`, and points you at the new `proliant oneview activity` for the full task detail.
- `proliant oneview interconnects describe`: fixed a misleading "Firmware baseline" line that showed the logical enclosure's *assigned/target* SSP as if it were already installed, even when the update had actually failed or was blocked (confirmed live: a blocked SY-2025.10.01 rollout kept showing as "Firmware baseline" long after the interconnect had never left its real SY-2023.05.01 baseline). Always shows the same three plain fields as the GUI's own "General" page — "Firmware baseline", "Firmware version from baseline", and "Installed firmware version" — with no separate "target"/"not yet applied" concept invented on top, now sourced from the Logical Interconnect's own actually-installed SPP tracking instead of the logical enclosure's request-only pointer; any mismatch between the baseline's version and what's installed is simply visible by comparing those two lines, exactly like the GUI.
- `proliant oneview update enclosure --execute`: fixed garbled/overlapping terminal output that could appear around the validation-warning "Proceed anyway despite this warning?" prompt and between targets. The live progress bar was being torn down and rebuilt from scratch for every target and every validation-warning retry; it's now a single persistent bar reused (reset in place) across the whole run and only actually stopped for the interactive prompt, matching Rich's supported pattern and avoiding the terminal corruption some terminals showed under the old repeated stop/start churn. Each finished target now also prints a permanent "✓ NAME updated." line so you have a written record even though the bar itself gets reused for the next target.
- `proliant oneview update enclosure --execute`: fixed a false "SSP apply complete. Updated N target(s)." reported for a logical-enclosure firmware update that OneView actually blocked and never applied. OneView reports this case as a `Warning` task at 100% ("firmware update was successful with warning") when a non-redundant fabric would be disrupted by the update — the CLI treated `Warning` as success unconditionally. It now re-checks whether the update actually landed before declaring success; the first pass of this fix compared the enclosure's own target-baseline pointer (which OneView sets immediately on request and never rolls back, so it still read "changed" even when blocked — the same root cause as the plan-phase bug below), so it's now cross-checked against each Logical Interconnect's actual installed SPP instead, same as the plan check. When they disagree, the CLI now shows OneView's real warning/resolution text and prompts to proceed anyway (see below) instead of silently reporting success.
- `proliant oneview update enclosure`: split "proceed through the non-disruptive-fabric validation warning" from "force-reinstall firmware" into two independent knobs (they were previously conflated, so confirming the warning always set `forceInstallFirmware` too). Clearing only the validation guard (`validateIfLIFirmwareUpdateIsNonDisruptive`) is the guard-only "proceed"; `--force` independently forces a reinstall *and* bypasses the guard up front. This is what makes the on-the-spot A/B choice below possible — proceed (guard-only) vs. force (disruptive) are now genuinely different requests, matching how the GUI treats accepting a redundant-fabric warning versus forcing through a non-redundant one.
- `proliant oneview update enclosure --execute`: when a firmware update is blocked by OneView's non-redundant-fabric validation, the CLI now reliably shows the real reason and resolution steps instead of a blank message. OneView nests the actual `VALIDATION_FAILED_FOR_LOGICAL_INTERCONNECT` error two levels deep in the task tree (*Logical enclosure firmware update* → per-interconnect *Update firmware* → the validation subtask); the CLI previously looked only at the top task and its direct children, so the "…does not have redundant connectivity configured for one or more uplink sets: pvlan-uplinkset…" text was dropped. It now walks the whole task subtree to surface OneView's own warning + recommended actions, matching the GUI's warning modal.
- `proliant oneview update enclosure` (plan and `--execute`): fixed the plan wrongly reporting a logical enclosure as "current / up to date" when a previous rollout to that same baseline had actually been blocked or never finished. OneView sets the enclosure's own target-baseline pointer as soon as an update is *requested*, and never rolls it back if the update fails — so comparing only that pointer against the requested baseline could say "up to date" even while every interconnect underneath was still running the old firmware. The plan now cross-checks each enclosure's Logical Interconnects' actual installed SPP (the same source the GUI's own "Installed:" state uses) before trusting a "no change needed" verdict, and reports "target baseline set but not yet installed" when they disagree.
- `proliant oneview update enclosure --execute`: fixed the validation-warning "Proceed anyway despite this warning? [y/N]" prompt silently swallowing its own "[y/N]" hint — Rich's terminal library parses `[...]` as markup, and `[y/N]` isn't a valid style, so it vanished, leaving no visible indication of what to type. Typing "OK" (the literal wording our own prompt tells you to click) wasn't recognized either and silently declined instead of proceeding. Now renders the hint correctly and also accepts "ok"/"okay" alongside "y"/"yes". Found and fixed the same swallowed-hint bug in `proliant oneview update appliance run`'s "Select image [1-N]" prompt and `proliant spp download`'s "Proceed with download? [Y/n]" prompt.
- `proliant oneview update enclosure --execute`: the live progress bar polled for task status every 20s, so a target that resolves in under ~20s (e.g. immediately blocked by a validation guard) showed no visible progress at all until it suddenly appeared "100% complete" — reduced polling to every 5s for more responsive, granular feedback. The bar also now shows an explicit "Checking whether the update actually applied…" message (in yellow) the moment a task lands in a `Warning` state, instead of leaving a `100% Warning` frame on screen looking like a confident, finished success while the CLI goes and determines whether that warning meant "done, minor note" or "blocked, nothing changed".
- `proliant oneview update enclosure --execute`: a plain "Completed" task (not just the already-handled "Warning" case) is no longer trusted unconditionally either — live-tested with `--activation-mode parallel`, a rollout reported "Completed" after only ~7 seconds while the real interconnect stage+activate cycle underneath kept running for several more minutes. The CLI cross-checks the actual installed baseline the same way it does for "Warning", now repolling every 5s for up to 5 minutes (not just one brief grace re-check) to give OneView's asynchronous LI-firmware activation time to actually finish. If it still disagrees once that window elapses, this surfaces as an honest "SSP apply reported complete but could not be verified" instead of a blind "SSP apply complete." — unlike a validation-guard block, this isn't auto-retried with `--force` (there's no known reason that would help), it just tells you to double-check via `oneview interconnects describe` or the OneView Activity log before trusting the result.
- `proliant oneview update enclosure --scope shared-infra-and-profiles`: fixed the scope silently being downgraded to shared-infra-only on the actual OneView request even though the plan preview correctly listed every server profile as a target — the logical-enclosure firmware PATCH always sent `"firmwareUpdateOn": "SharedInfrastructureOnly"` regardless of `--scope`, so server profiles were never actually included in the apply. `--scope shared-infra-and-profiles` now reaches OneView as `SharedInfrastructureAndServerProfiles` as intended.
- `proliant oneview update enclosure --execute`: fixed the live progress bar showing a contradictory "100%  Running" for several minutes during a server-profile compute update (confirmed live: a task genuinely still ~9 minutes from finishing showed "100%" the whole time). The bar overlays the deepest currently-active subtask's own phase text/percent onto the display (e.g. "Update frame link module firmware  30%" instead of the root task's own flat 0%) — but once that active subtask's own children had all already finished (e.g. "Power on" and "Generate install set" both long Completed), a "pick whichever was touched most recently" fallback grabbed one of those *finished* children and showed its 100% as if it were live progress. It now only descends into a level's still-*Running* child; if none exists, it keeps showing the deepest node that's actually in flight instead of a stale, completed one. Also fixed the progress text occasionally showing a raw `{"name":"Enclosure-01, bay 6","uri":"/rest/server-hardware/…"}` JSON blob inline (OneView embeds these for the GUI to render as links) instead of just the plain resource name.
- `proliant oneview update enclosure --execute` / `server-profiles update` / `server-profiles reapply`: fixed the live progress bar showing a stuck "0%" for the entire duration of a server-profile compute firmware update, even though the GUI's Activity page showed real, moving step-by-step progress (confirmed live: an "Apply profile" task's own `percentComplete` sat flat at 0 for the whole ~12 minute run). OneView tracks this task type's actual progress in a separate `computedPercentComplete` field (step-weighted, the same value the GUI's own bar reads) alongside `completedSteps`/`totalSteps`, which the CLI never looked at. It now prefers `computedPercentComplete` when present and shows a "step N/M" counter alongside the bar, falling back to the old plain field for task types that don't populate it.
- All commands: fixed a `UnicodeEncodeError` crash when output is piped or redirected on Windows (a non-TTY). Rich falls back to the legacy Windows console renderer, which encodes with the OS code page (cp1252 on most systems) where UI glyphs used throughout the CLI — `↔`, `✓`, `⚠`, `•`, box-drawing, progress bars — have no mapping and raised mid-render (e.g. `proliant oneview update enclosure ... | tee log.txt` crashed right after printing the plan panel). stdout/stderr are now reconfigured to UTF-8 at startup so redirected output is robust regardless of the console code page.
- Tab completion: fixed roughly 40 flags across `ilo`/`com`/`oneview`/`spp` (e.g. `update enclosure --concurrency`, `mac list --address`, `ilo network set static --ip`) never appearing in `--<TAB>` flag-name completion at all — confirmed live: `--concurrency` was completely absent from `proliant oneview update enclosure LE01 --<TAB>` even though `--help` listed it correctly. These flags all suppress file-path completion for their *value* via a shared `suppress_file_completion()` helper, which used argcomplete's own `SuppressCompleter` — argcomplete treats that as "hide this option from completion entirely," not just "don't complete a value for it." The helper now returns a plain empty-list completer instead, which still avoids suggesting workspace files for the value but no longer hides the flag itself.

### Enhancements
- `proliant oneview update enclosure` (no `NAME`): launches an interactive, menu-driven wizard instead of requiring every flag up front — walks through logical enclosure, baseline, scope, install type (when applicable), activation mode, force, and a final review/execute question, one numbered choice at a time. Type `b` at any step to go back and change a previous answer, or `c`/`q` to cancel without changing anything. Passing `NAME` (with or without other flags) still works exactly as before for scripting/automation; the wizard requires an interactive terminal and is unavailable in `--json` mode.
- `proliant oneview release`: shows HPE's published Synergy Software Releases compatibility matrix — for every Composer (HPE OneView) version, which SSP is *recommended* and which are *additionally supported*. Marks the row matching the currently-connected appliance when reachable; works offline otherwise. The same source data already backs the compatibility note shown by `update enclosure`'s plan.
- `proliant oneview update enclosure --activation-mode {orchestrated,parallel}`: exposes OneView's own `-InterconnectActivationMode` choice. `orchestrated` (default) flashes one side of each redundant Logical Interconnect pair at a time so the fabric stays up; if an uplink set isn't redundant OneView raises its non-disruptive validation *warning*, which you can proceed through (exactly as with the GUI's "Review the warnings… click OK to proceed") to apply the update with a brief interruption limited to the affected uplinks. `parallel` flashes every interconnect at once regardless of redundancy — a full network outage during the update, and OneView additionally requires the affected compute modules to be powered off first — and is clearly flagged as disruptive in the confirmation panel. When a target stays blocked, the CLI surfaces OneView's own reason and "Resolution:" steps (e.g. restore uplink-set redundancy) rather than prescribing a specific hardware action.
- `proliant oneview update enclosure --execute`: when OneView blocks a target update on its non-disruptive-fabric validation guard, the CLI now shows OneView's own warning **and its "Resolution:" remediation steps** inline — the exact same text as the OneView GUI's "Review the warnings. If the conditions are acceptable, then click OK to proceed." modal, with embedded resource references (logical interconnects, server profiles) shown by name instead of raw JSON — **plus a read-only "which uplink set / which leg" diagnostic** the GUI makes you hunt for: for each flagged uplink set it lists every live uplink leg across the interconnect pair (location, port, link state, negotiated speed) and a plain-language note on *why* it isn't redundant (a down leg, a single-sided set, or a speed mismatch) and what to fix (verified live: pinpoints `pvlan-uplinkset` on `LE01-LIG-VC100` with its Bay 3 leg up at 10G and its Bay 6 leg down). It then presents an on-the-spot **A/B choice**: **A) abort and fix the fabric redundancy first** (recommended, no disruption), or **B) force the update through now** — the disruptive path that actually gets a genuinely non-redundant fabric to update (clears the guard *and* force-reinstalls), briefly interrupting the affected uplinks and any server profiles riding them, matching accepting the GUI's warning. Previously the prompt only offered a plain "proceed anyway" that cleared the guard without forcing — which, confirmed live, an Orchestrated per-interconnect redundancy block simply re-rejected, so it never actually got through. `--yes` proceeds non-disruptively (guard-only, may still stay blocked on a truly non-redundant fabric); `--force` takes path B non-interactively; `--json` never prompts.
- Replaced `proliant oneview firmware apply` and `proliant oneview upgrade ...` with a unified `proliant oneview update` command family, matching OneView's own "Update firmware" dialog instead of a confusing set of flags:
  - `proliant oneview update enclosure <NAME>` — roll out an SSP baseline to one named logical enclosure. `--baseline` picks the SSP (defaults to newest, so the same rollout can be repeated against different baselines for consistency testing); `--scope shared-infra` (default) updates only frame link modules + interconnects, `--scope shared-infra-and-profiles` also rolls the baseline out to every server profile OneView shows under that enclosure's compute modules — the CLI now discovers those profiles itself instead of requiring `--server-profile`/`--all-profiles`. The old `--all-enclosures`/`--logical-enclosure`/`--server-profile`/`--all-profiles` flags are gone; a logical enclosure name is now a required argument, mirroring the GUI's per-enclosure dialog.
  - `proliant oneview update appliance {readiness,run,pending,cancel,cleanup}` — same appliance-software-upgrade commands as the old `proliant oneview upgrade`, just renamed under `update` to sit next to `update enclosure`.

---

## v1.0.36 — 2026-07-10

### New Features
- `proliant oneview upgrade run`: upload and stage an appliance software update image (`.bin`), then optionally install it. Pick an image interactively from a directory/share with `--from-dir`, or point at one with `--image`. Staging is the default; the reboot-inducing install is gated behind `--execute` plus a typed confirmation, and blocked when the readiness verdict is FAIL (override with `--force`).
- `proliant oneview upgrade pending`: show the currently staged appliance update.
- `proliant oneview upgrade cancel`: remove a stuck or aborted staged update.
- `proliant oneview appliances describe [NAME]`: show an appliance's General page — the active/standby Composer HA pair and their connection state, model, memory, per-node start time and uptime, firmware version/date, and the Composable Infrastructure Appliances inventory. Defaults to the active appliance; `--json` for scripting.
- `proliant oneview firmware apply`: roll out an SSP (Synergy Service Pack) firmware baseline that OneView orchestrates — shared infrastructure (logical enclosure: interconnects + frame link modules) and/or compute (server profiles), applied in that order. Pick a baseline with `--baseline` (defaults to the newest registered SSP) and a scope with `--logical-enclosure`/`--all-enclosures` and/or `--server-profile`/`--all-profiles`. The plan shows a source-backed OneView↔SSP compatibility note — whether the chosen SSP is *recommended*, *supported*, or *not listed* for the running OneView version (per HPE's Synergy Software Releases matrix), and warns before an unsupported apply. Default is a non-destructive plan; the hardware-rebooting apply is gated behind `--execute` plus a typed confirmation. The confirmation is scope-aware — an infrastructure-only apply states that no server profiles are included and compute modules will not be power-cycled. During `--execute` a live per-target progress bar tracks the underlying OneView task to completion, showing the current stage (e.g. "Update logical interconnect") and percent instead of returning the instant it starts.
- `proliant oneview interconnects describe <name>`: single-interconnect detail page matching OneView's GUI — General (logical interconnect, firmware baseline vs. installed version, management interface, stacking, IP addresses), Hardware (product, location, MAC, WWN, serial/part numbers, health), Interconnect Link Ports, Uplink Ports (type, speed, uplink set, connector, connected-to — showing the remote switch's actual hostname from LLDP, not just its chassis MAC like the GUI does), Downlink Ports (server hardware, adapter port, server profile — resolved from live port-neighbor wiring, not just profile assignment), live Utilization (CPU/memory/power/temperature), and Remote Support state.

### Bug Fixes
- `proliant oneview upgrade run --execute`: fixed a false "Upgrade complete" that printed immediately when an install started. Progress is now read only from the appliance's own update-status feed, and completion is confirmed by the appliance actually rebooting onto the target build — not by a stale `100%` left over from a prior firmware task.

### Enhancements
- `proliant oneview upgrade run`: live progress display during long operations — a byte-level progress bar (size, speed, ETA) while the multi-GB image uploads, and a phase/percent bar while the appliance installs and reboots (e.g. "Swap active/standby nodes").
- `proliant setup`: adding a OneView appliance now prompts for a friendly alias (like the iLO flow), defaulting to the auto-generated `oneview`/`oneview-2`/… name so pressing Enter keeps the previous behavior.
- `proliant ilo`: removed the `--raw` flag (it had drifted inconsistent — a genuine unprocessed Redfish dump for some resources, but a no-op alias of the normal output for `servers`, `serial`, and `license`). `--json` is now the one consistent automation flag across `ilo`/`com`/`oneview`.

---

## v1.0.35 — 2026-07-09

### Enhancements
- `proliant oneview` and `proliant ilo`: tab completion and `--help` now list each command only once. Removed the duplicate singular/plural and short-form aliases (e.g. `server`, `repo`, `repositories`, `mem`, `check`, `appliance`) that cluttered the completion menu — use the canonical command names shown in `--help`.

---

## v1.0.34 — 2026-07-09

### New Features
- `proliant oneview mac describe` now supports `--vlan` and `--network-name` filters so MAC traces can be narrowed to specific VLANs or network-name matches.

---

## v1.0.33 — 2026-07-08

### Enhancements
- GitHub Pages docs: added a dedicated Installation page with the full terminal walkthrough (moved out of Getting Started so that page stays focused), fixed a broken internal link to the inventory setup section, and refreshed the demo GIF/screenshots on both README and the docs site.

---

## v1.0.32 — 2026-07-08

### New Features
- `proliant setting telemetry` (no arguments): shows a status panel — the default, your current setting and why — then asks `Enable telemetry? [Y/n]`/`[y/N]` before changing anything. One command now replaces having to guess or check config files; `proliant setting telemetry on`/`off` still work directly with no prompt for scripting.

### Bug Fixes
- Error telemetry (Sentry): fixed a leak where a crash report could include your machine's hostname and Windows/macOS/Linux username (as part of file paths in the stack trace). Both are now stripped before anything is sent, and local variable values are no longer captured at all.

### Enhancements
- `proliant -h`: rewritten to be shorter and more useful — replaced generic examples with real, verified commands, removed outdated tab-completion notes, and collapsed namespaces/commands into a single list.

---

## v1.0.31 — 2026-07-08

### Bug Fixes
- `proliant com login`: removed the confusing `--password`/`-p` flag entirely — it was a boolean switch that took no value (the password is always entered at a masked prompt), so `proliant com login --email you@example.com --password abc` misleadingly printed the *parent* `proliant com` parser's "unrecognized arguments" error instead of a login-specific one. Login method is now fully auto-detected: `@hpe.com` accounts try Okta Verify push first and fall back automatically to a masked password prompt if the account has no Okta Verify authenticator enrolled (previously this just retried the same broken push flow 3 times and failed with `Okta Verify not available. Authenticators: ['Password']`); external accounts (e.g. gmail.com) go straight to the password prompt, same as before.
- Windows installer (`install.ps1`): the "Getting started" hint after install suggested `proliant ilo init` — a leftover from before the `proliant setup` wizard existed. Now correctly points to `proliant setup` for configuring iLO/OneView inventory.

---

## v1.0.30 — 2026-07-08

### New Features
- `proliant com whoami`: shows who you're currently logged in as — email + login method (Okta Verify push, username/password, or API client) and active workspace/region — without needing to inspect `token.json` by hand.

### Bug Fixes
- `proliant com login --password`: an account with no password authenticator enrolled yet in HPE GreenLake (Okta's IDX flow asks to *enroll* one instead of challenging an existing password) used to retry 3 times and then fail with raw internal jargon — `Unexpected remediations after identify: ['enroll-authenticator', ...]. Authenticators: [...]`. Now fails immediately (no pointless retries) with a clear message explaining the account needs a password set up via the GreenLake console first.

### Enhancements
- `proliant com login`: removed the login-specific `--region` flag — it duplicated the top-level `--region` flag and encouraged picking a region before the workspace/account was even known. Region/workspace selection now happens after login via `proliant com regions use` / `proliant com workspaces use`, same as before this flag existed.

---

## v1.0.29 — 2026-07-08

### Bug Fixes
- `proliant ilo`/`proliant com`: the yellow "did you mean" / valid-choices highlighting on an invalid-argument error was written as raw ANSI escape codes straight to stderr. On some Windows consoles (older/legacy `conhost` sessions — e.g. the classic blue "Windows PowerShell" console, as opposed to Windows Terminal) these escape codes don't render as color at all, showing plain text instead, even though every other colored message in the app (drawn via Rich) displayed correctly in the same window. Error output now goes through Rich as well, so it uses the same terminal-aware color path (including Rich's legacy-Windows-console fallback) as the rest of the CLI, and no longer leaks raw escape bytes when stderr is piped/redirected.

---

## v1.0.28 — 2026-07-08

### Bug Fixes
- `proliant com` (`devices list`, `servers list`, `workspaces list`, `regions list`, `bundles list`, `workspace use`, `region use`, `devices add`, `servers describe`, `reports gpu`, `reports memory`): a 403 from Compute Ops Management (account has no role assigned in the workspace) used to print httpx's raw `Client error '403 Forbidden' for url ...` message with a dangling `developer.mozilla.org` link. Now shows the same plain-language explanation GreenLake's own web UI shows — "It looks like you do not have a role assigned for Compute Ops Management ... Contact your HPE GreenLake administrator" — with no raw URL. `servers describe`, `reports gpu`, and `reports memory` previously had no error handling at all and could crash with a raw Python traceback on any API error; they now fail cleanly like every other `com` command.
- `proliant setup`: the "Setup complete!" hint suggested running `proliant ilo list firmwares`, which isn't valid syntax (`proliant ilo` commands are resource-then-action, e.g. `proliant ilo firmware list`) and would error with "invalid choice: 'list'". Now just points back to `proliant setup` for adding/editing/re-testing entries.

---

## v1.0.27 — 2026-07-08

### Bug Fixes
- Windows installer: the "Launch a new terminal" checkbox on the Finished page opened a shell rooted in `C:\Windows\System32\WindowsPowerShell\v1.0\` (or wherever `wt.exe`/`powershell.exe` itself lives) instead of the user's home directory — Inno Setup defaults a `[Run]` entry's working directory to the launched exe's own folder when none is specified. Now explicitly starts in `%USERPROFILE%`.

---

## v1.0.26 — 2026-07-07

### Enhancements
- `proliant ilo servers list`: now shows the server's friendly inventory.ini alias as the first column (previously not shown at all), added a Power column, and the OS Name/iLO Name columns now size to their actual content instead of stretching with excess blank padding to fill the terminal.
- `proliant setup`: adding a OneView appliance no longer prompts for an "OneView section name" — it's an inventory.ini implementation detail the user never needs to type. The first appliance is named `oneview`, additional ones auto-number as `oneview-2`, `oneview-3`, etc. Still renameable later via the wizard's "Edit an entry" flow if you want something more descriptive.
- `proliant com --json` (`servers list`, `devices list`, `workspaces list`, `regions list`, `bundles list`): now emits clean, self-describing field names (e.g. `"Health"`, `"Name"`, `"Serial"`, `"CPU"`) matching what the table shows, instead of dumping the unprocessed COM API response verbatim. Removed the redundant `--raw` flag from `com` (it produced byte-identical output to `--json` today) — `--json` is now the one consistent automation flag across `ilo`/`com`/`oneview`.
- `proliant com servers describe` tab completion now only suggests server Name — it previously suggested the name, serial number, *and* iLO hostname for every server, tripling the completion list for no benefit since Name is already the friendly identifier shown first in `servers list`. Serial number/iLO hostname still work fine if typed manually; they just aren't suggested unless a server has no name at all.

### Bug Fixes
- `proliant setup` (and any `proliant ilo`/`proliant oneview` command) no longer crashes with a raw `configparser.DuplicateOptionError` traceback when `inventory.ini` has a syntax error (e.g. a duplicate key, or a line accidentally left outside any section). `proliant setup` now explains what's wrong in plain language, offers to open the file in your editor right away, and retries once you're done — a working example is now included as `sample-inventory.ini` at the repo root and linked from the error message.
- Fixed a Windows-only crash (`OSError: [Errno 22] Invalid argument`) that could happen printing a sufficiently large table on a "legacy" console with no virtual-terminal support — Rich's fallback write path has no output-size guard. Startup now force-enables VT/ANSI processing on the console so Rich always takes its safe, chunked write path.
- Windows installer: the "Launch a new terminal" option on the Finished page could open a terminal where `proliant` wasn't recognized yet, even though PATH was updated correctly — that terminal was spawned by the installer itself, which was still holding its own pre-install copy of PATH in memory. The installer now refreshes its own environment immediately after updating PATH, so the terminal it launches works right away (a separately, manually opened terminal always worked, since Windows Explorer already refreshes PATH for anything it spawns).
- `proliant ilo` (any command): fixed a fresh-install bug where every iLO login silently used an empty username and failed with "check authentication?" — even though `proliant setup`'s own live connection test had just reported "Reachable" for the exact same entries. `proliant setup` never writes an inventory.ini `[defaults]` section, and skips writing `username=` on an entry when it matches its own assumed default ("Administrator") to keep the file terse; the real `proliant ilo` commands' fallback for a missing username didn't match that assumption (`""` instead of `"Administrator"`), so any fleet using the common default iLO username authenticated with nothing. Only affected fresh/first-time setups where every entry uses "Administrator" and no `[defaults]` section was ever added by hand.
- `proliant setup`'s "Open inventory.ini in editor" option gave up with "No editor found" on headless Linux servers/VMs that have no `$EDITOR` set and no `xdg-open` (no desktop session) — the common case for a fresh minimal Ubuntu install. It now falls back to whichever terminal editor is actually installed (`nano`, then `vim`, then `vi`) before giving up and telling the user to edit the file by hand.

---

## v1.0.24 — 2026-07-10

### New Features
- `proliant oneview appliances list` / `proliant oneview appliances use <name>`: `inventory.ini` can hold more than one OneView appliance (each its own section with `type = oneview`) — these commands list them (marking which is active) and let you switch which one every other `proliant oneview` command targets, persisted across sessions. With only one appliance configured (the common case), behavior is unchanged — no extra steps needed.

---

## v1.0.23 — 2026-07-10

### Bug Fixes
- `proliant com servers list` / `com devices list` undercounted the fleet (e.g. 35/40 shown vs. 44 in the GreenLake GUI). Both commands previously read GreenLake's device-claim inventory (`/devices`), which omits servers synced in automatically via a linked OneView appliance bridge. They now read COM's own server inventory (`/compute-ops-mgmt/v1/servers`), which is what the GUI's Servers page and Overview widget actually use — counts now match exactly.

### Enhancements
- `com servers list` and `com devices list` column layout now mirrors the GreenLake GUI's Servers page: Health, Name, State, Serial, Group, Power, Baseline, Model by default, with many more available via `--fields` (Generation, Product ID, Manufacturer, UUID, CPU, Operating System, Connection Type, Appliance, OneView Name/State, iLO Hostname/IP/Version/License, Auto iLO FW Update, Maintenance Mode, Subscription Tier). `devices list` additionally shows a Type column and merges in GreenLake-claimed storage/network devices alongside COM's real compute inventory. Note: "iLO Security" is not exposed by any COM API today and is intentionally omitted.
- `com servers describe`: enriched with UUID, CPU, Maintenance Mode, Connection Type (Direct/OneView managed), Appliance name, OneView Name/State, and iLO License — matching the fields shown on the GUI's server detail page.

---

## v1.0.22 — 2026-07-09

### Enhancements
- **Breaking:** `proliant update` and the `-V`/`--version` flag have been replaced by a single `proliant version` command — it prints the installed version and, if a newer release is available on GitHub, offers to install it (`-y`/`--yes` to skip the confirmation prompt).
- **Breaking:** `proliant setting list inventory` has been removed — `proliant setup` already covers viewing/adding/editing/deleting inventory.ini entries. `proliant setting list cli-tree` is now simply `proliant setting cli-tree`.
- Namespaces are now listed/dispatched in `ilo` -> `com` -> `oneview` -> `spp` -> `setting` order everywhere (top-level help, tab completion, `setting cli-tree`).
- Removed "unified" marketing language from the CLI help text and README — just "HPE ProLiant CLI".

---

## v1.0.21 — 2026-07-08

### Enhancements
- `proliant setup`: the server name prompt is now labeled "Server alias (friendly label)" with a hint explaining it's a short label you choose (used with `--host`; it need not match the iLO or OS hostname).
- `proliant setup`: added an "Open inventory.ini in editor" menu option that opens the config in your `$EDITOR`/`$VISUAL` (falling back to the OS default handler), then offers to reload and re-test connections.
- `proliant setup`: now keeps automatic rotating backups of `inventory.ini` (the last 3 versions, as `inventory.ini.bak1`–`.bak3`) before saving any change, so an accidental edit or deletion can be recovered.
- `proliant update`: now sends an anonymous, best-effort update ping (counts updates by OS, matching the one-liner install counter) so self-updates are tracked too. No personal data is sent; set `PROLIANT_NO_TELEMETRY=1` to opt out.

---

## v1.0.20 — 2026-07-08

### New Features
- `proliant com regions list`: list the Compute Ops Management regions provisioned for the active workspace (e.g. `us-west`, `eu-central`) — mirrors the region switcher in the GreenLake console, with the active region marked. Add `--all` to also show unprovisioned/available regions.
- `proliant com regions use <region>` (alias `proliant com region use <region>`): switch the active COM region for the current workspace. The choice is remembered per-workspace, so switching workspaces later restores whichever region you last used there.
- `proliant com login`: fresh logins now auto-detect which COM region(s) are actually provisioned for the workspace instead of always assuming `us-west`. If more than one region is provisioned and you haven't picked one before, it prefers `us-west` when available and prints a hint showing how to switch (`proliant com regions use <region>`).

### Bug Fixes
- The global `--region` flag was silently ignored for the normal login-session (user-token/GLP) path used by `com devices/servers/bundles/reports` — it only worked for the rare explicit client-credentials flow. The flag now correctly overrides the active region for that single command.
- `proliant com workspaces list`: the Region column always showed the currently active session's region for every workspace row, even ones you weren't logged into — implying they all shared one region. It's now labeled "COM Region" and shows each workspace's own last-known/remembered region (or `—` if unknown).

---

## v1.0.19 — 2026-07-07

### New Features
- `proliant oneview upgrade readiness`: read-only pre-upgrade check. Reports the appliance version, the supported Synergy Composer upgrade path (recommended next hop + full milestone chain to the latest release), and a PASS/WARN/FAIL assessment of disk space, memory/CPU, active alerts, backup freshness, logical interconnect consistency, and interconnect redundancy.
- `proliant oneview upgrade cleanup`: reclaim appliance disk by removing unused firmware baselines (SPP/SSP) not assigned to any logical enclosure, logical interconnect, or server profile. Newer unused baselines are kept as upgrade targets. Dry-run preview by default; `--yes` performs the deletion. Repository-only — never touches running enclosures or interconnects.
- `proliant oneview firmware bundles`: list all registered SPP/SSP firmware bundles (name, version, type, release date, size, repository), sorted oldest -> newest.
- `proliant oneview firmware repository`: list firmware repositories (Internal + external Firmware Bundles sources) with total/available space and bundle count per repository — mirrors the GUI's Firmware > Repositories tab.
- `proliant oneview firmware compliance`: real per-server firmware compliance against each registered bundle newer than what's currently assigned anywhere (the same "candidate" bundles `upgrade cleanup` retains as upgrade targets), using OneView's own `POST /rest/server-hardware/firmware-compliance` compliance-check engine — one row per (server, candidate bundle) with a real count of components needing an update, mirroring the GUI's Firmware > Firmware Compliance tab layout (the GUI's internal-only Update Category/Estimated Update Time columns aren't exposed via the REST API).
- **Breaking:** `proliant oneview firmware list` has moved to `proliant oneview servers firmware list` (same `--server` flag) — the top-level `firmware` command is now appliance/repository-level (`bundles`/`repository`/`compliance`, see above) to match the OneView GUI's Firmware section, rather than per-server component inventory.

### Enhancements
- `proliant oneview upgrade cleanup`: prunable and external-repository baseline tables are now sorted oldest -> newest by release date, instead of the API's arbitrary member order, making it easier to scan chronologically.
- `proliant com workspaces use <name>`: switching the active workspace is now discoverable directly under `proliant com workspaces -h` (previously only existed as the separate, easy-to-miss `proliant com workspace use`, which still works as a backward-compatible alias).
- `proliant com login`: the interactive multi-workspace picker no longer uses a hard-to-see arrow-key cursor — it now shows a numbered, multi-column list (like `ls -C`) and you just type the number (or part of the name) and press Enter.
- `proliant com devices list`: now renders with the same server-focused columns as `proliant com servers list` (Serial, OS Name, iLO Name, Model, Type, Location) instead of a different, less detailed layout — storage/network devices show grayed-out dashes for the compute-only columns. `servers list` is now strictly compute-only (its `--type` flag was removed since servers are always `COMPUTE`); `devices list` keeps `--type` and includes storage/network too.
- `proliant com devices list --model` / `proliant com servers list --model`: now tab-completes actual model names seen in your workspace (e.g. `dl380-gen11`) instead of just suppressing file-path completion.

### Bug Fixes
- `proliant oneview upgrade cleanup`/`readiness`: firmware baselines that only exist in an external repository (e.g. an SPP repository added under Firmware Bundles > External Repositories) are no longer counted as reclaimable or attempted for deletion. OneView always rejects deleting these (HTTP 400 "exists only in the external repository...") and their reported size isn't appliance disk at all, so `cleanup` used to promise disk it could never free and spam a failed-deletion line per baseline. They're now listed separately as informational "not deletable via OneView" entries.
- `proliant setup` (edit iLO/OneView entry): a failed connection test no longer shows the raw internal error (e.g. `POST /redfish/v1/SessionService/Sessions failed — HTTP 401: check username/password`) — auth failures now show a clean `Auth failed: check username/password` or `Auth failed: account lacks permission for this operation` message. The entry's name (e.g. `dl380-gen11`) is now also editable during edit, prompted first as "Server Name" / "OneView section", with uniqueness validated against other entries.
- `proliant com get devices` / `proliant com servers list`: columns (OS Name, iLO Name, Model, Location, etc.) no longer stretch to fill the full terminal width with excess blank padding — the table now sizes to its content instead, so long/truncated names stay compact and readable regardless of terminal width.
- `proliant com servers list`: OS Name and iLO Name were always hard-truncated to 18 characters even when the terminal had plenty of room, cutting off most hostnames (e.g. `ILO2M240400JR.mgm…`). These columns now show the full name whenever space allows (up to 36 chars), only falling back to an ellipsis when the terminal is genuinely too narrow.
- `proliant com login` / `proliant com workspaces list` / `proliant com workspace use`: self-service workspaces a user creates themselves in the GreenLake console (e.g. via "Create workspace") never appeared at login or in `workspaces list`/`workspace use` — only workspaces the user was invited into by someone else showed up. Root cause: `/authn/v1/session` (used at login) only returns invited-org accounts; self-service workspaces live in a separate `list-accounts` API that was never queried. Login, `workspaces list`, and `workspace use` now merge both sources, so all of a user's workspaces are offered/shown/switchable.
- `proliant com workspace use`: switching the active workspace updated the workspace shown by `workspaces list`, but `com devices list` (and every other `compute-ops-mgmt` API call) kept silently returning the *previous* workspace's data — the GLP API credential used for those calls is workspace-scoped and was never regenerated on switch. `workspace use` now regenerates the GLP credential for the newly selected workspace so all COM API commands correctly follow the switch.
- `proliant com workspaces use <name>`: switching could fail with a raw `Workspace switch failed (401): {"Status":"Unauthorized Request. Session not found.","errorCode":"HPE_GL_V1_SESSION_NOT_FOUND"}` even right after a successful login, because the `ccs-session` cookie HPE issues can expire well before the OAuth access token does (observed: access token still valid for over an hour). The switch now force-refreshes the token (which re-establishes a fresh `ccs-session`) and retries once on a 401 before giving up, and any still-unrecoverable failure now shows a clean "your login session has expired — run `proliant com login` again" instead of the raw JSON error body.

---

## v1.0.18 — 2026-07-06

### New Features
- `proliant setup`: new guided menu for managing your iLO servers and OneView appliance in `inventory.ini` — view, add, edit, or delete entries, with each connection live-tested before it's saved. Merges into any existing config instead of overwriting it. `proliant ilo init` still works as a shortcut to the same wizard.

### Enhancements
- `proliant setup`: the entries table now has a live "Status" column (Reachable / Timeout / Unreachable / Auth failed) instead of guessing from config alone. All entries are tested in parallel when the wizard starts (so total wait time doesn't scale with the number of servers), and re-tested automatically right after you add, edit, or delete an entry.
- Windows installer: the "Finished" page now has a checked-by-default "Launch a new terminal" option, so you can jump straight into using `proliant` instead of having to go find/open a shell yourself. Prefers Windows Terminal, falling back to PowerShell if Windows Terminal isn't installed.

### Bug Fixes
- Windows installer: the post-install confirmation message is now a simple "installed successfully" + install location — dropped the extra getting-started/tab-completion text, which isn't needed now that completion is set up automatically.
- `proliant qs` (QuickSpecs browser) is temporarily disabled — rendering wasn't reliable enough across HPE's HTML and PDF QuickSpec formats. The command now prints a clean "currently unavailable" message instead of a broken table, and no longer appears in `--help`, tab completion, or the README/docs. The underlying module isn't deleted (may return once rendering is more reliable) but its dependencies are no longer bundled into the release binaries, shrinking their size.

---

## v1.0.17 — 2026-07-05

### Bug Fixes
- Fixed tab completion not working after a fresh install, even in a brand-new PowerShell window. The GUI installer (`proliant-cli-windows-setup.exe`) never wrote anything into `$PROFILE` itself — completion was only ever set up as a side effect of running a `proliant` command for the first time, so a user who installed and went straight to `proliant i<Tab>` without running any command first got nothing. The installer now triggers that one-time setup itself right after install, so tab completion is already working the first time you open a terminal.

---

## v1.0.16 — 2026-07-05

### New Features
- `proliant update` (Windows): before installing, now shows the target version, install directory, and how to uninstall later, and asks for confirmation. Use `-y`/`--yes` to skip the prompt for scripted/unattended use.

### Bug Fixes
- Fixed a rare crash (`ValueError: I/O operation on closed file`) that could happen if the CLI's internal startup routine ran more than once in the same process — hardened the Windows UTF-8 output setup to reconfigure the existing stream instead of creating a duplicate one.
- `proliant ilo`/`proliant oneview`: commands no longer appear to hang with no feedback when given a wrong or unreachable host IP. A "Connecting to..." hint now shows while logging in (it disappears once a real response comes back), the initial connection now fails within ~8 seconds instead of up to 60, and a connect-timeout error is now reported cleanly instead of leaking as an unhandled traceback. Also fixed a bug where iLO requests issued after login had no timeout at all, so a server that stopped responding mid-session could hang indefinitely.
- Fresh installs on a clean machine now tell you what to do next: the installer (both the interactive GUI and `install.ps1`) shows an install location and a "getting started" checklist (`proliant --help`, `proliant ilo init`) instead of just disappearing, and the one-time "tab completion enabled" message now also mentions running `. $PROFILE` to load it in the *current* window instead of only suggesting a new one.

---

## v1.0.15 — 2026-07-03

### Enhancements
- Tab completion is significantly faster:
  - Top-level completion (e.g. `proliant i<TAB>` → `ilo`) now answers instantly from PowerShell itself instead of launching a new `proliant` process every keystroke — cut from ~700-850ms to well under 50ms.
  - Completions that look up live data (OneView/iLO/COM object names, SPP versions) are now cached for a few seconds, so repeatedly pressing `<TAB>` while typing the same command doesn't re-fetch from the network or device each time.

### Bug Fixes
- PowerShell profile setup: fixed a bug where re-running `proliant update` could leave a duplicate copy of the "show completion menu" tweak in your PowerShell profile. Existing profiles are automatically cleaned up the next time completion is refreshed.
- `proliant update` (Windows): tab-completion improvements now reach existing installs automatically after an update, instead of only applying to brand-new installs.

---

## v1.0.14 — 2026-07-02

### Bug Fixes
- `proliant --help`: no longer lists `install-completion` as a runnable command — it never existed as a subcommand, so running it failed with "unknown namespace". Tab completion is set up by the installer instead.
- `proliant com` (devices, bundles, etc.): a revoked or rotated auto-managed GLP API credential from a previous `proliant com login` now shows "Session expired ... run 'proliant com login'" instead of a raw HPE JSON error.
- `proliant update` (Windows): the installer now shows a confirmation dialog with the installed version and location when finishing a silent update, instead of just disappearing with no feedback.

---

## v1.0.13 — 2026-07-02

### Bug Fixes
- `proliant com login --password`: the password prompt now masks input — every character you type **or paste** shows a matching `*`. Previously nothing appeared as you typed, making the prompt look frozen.
- `proliant com workspaces`: listing workspaces now works right after an OAuth/email login instead of failing with "requires a user OAuth token session".

### Enhancements
- `proliant com login`: when your account has only one workspace, the CLI now logs in directly and tells you which workspace it selected instead of showing a single-item picker.

---

## v1.0.12 — 2026-07-02

### Enhancements
- Windows now installs via a signed GUI installer (`proliant-cli-windows-setup.exe`) into `C:\Program Files\proliant-cli`, with an Add/Remove Programs entry and machine PATH setup. This replaces the single self-extracting `.exe`, which some endpoint security tools (Defender, CrowdStrike Falcon) flagged.
- `proliant update` on Windows now downloads and runs the installer instead of swapping the running binary in place.

### Bug Fixes
- `proliant oneview`: missing config now shows a clean "run init" message instead of a raw Python traceback, and reads inventory from the same `~/.config/proliant-cli` location as the other commands.

---

## v1.0.11 — 2026-07-01

### New Features
- `proliant oneview enclosures describe`: show GUI-like enclosure bay layout and hardware detail tables.
- `proliant oneview server-profiles describe`: show detailed profile, firmware, connection, boot, BIOS, and address settings.
- `proliant oneview mac describe`: trace a MAC with a diagram focused on the learned endpoint or uplink.

### Bug Fixes
- PowerShell completion now handles namespace delegation, trailing spaces, and values containing spaces or commas more reliably.
- Sentry telemetry now drops expected user/environment errors such as authentication failures, timeouts, missing config, and invalid input.
- OneView requests now report connection and timeout failures as clean CLI errors.

### Enhancements
- OneView MAC list output hides server profile columns when entries are not related to a server profile.
- OneView `--json` can be used before or after subcommand arguments.
- OneView output uses cleaner status coloring, compact server names, and richer network, enclosure, and profile details.

---

## v1.0.9 — 2026-06-26

### New Features
- `proliant setting telemetry on|off`: enable or disable Sentry error telemetry via marker files.
- `proliant setting uninstall`: remove all proliant-cli config and cache directories.
- `proliant ilo init` now creates `inventory.ini` in `~/.config/proliant-cli/` instead of the current directory.

### Enhancements
- Renamed `proliant config` subcommand to `proliant setting`.
- Standardised config and cache directories to `~/.config/proliant-cli/` and `~/.cache/proliant-cli/` across all platforms.
- SPP and QuickSpecs caches now stored under `~/.cache/proliant-cli/spp/` and `~/.cache/proliant-cli/qs/`.
- Telemetry now controlled by marker files (`telemetry-enabled`/`telemetry-disabled`) in addition to `PROLIANT_TELEMETRY` env var.

---

## v1.0.8 — 2026-06-26

### Bug Fixes
- `proliant com login --password`: fixed login failure for external HPE Accounts (non-`@hpe.com`).

---

## v1.0.7 — 2026-06-25

### Bug Fixes
- `proliant com login --password`: fixed login failure on accounts that use the HPE GreenLake SSO flow.

---

## v1.0.6

### New Features
- Initial public release of unified `proliant` CLI combining iLO Redfish and COM cloud management.
- `proliant ilo`: firmware inventory, update-method classification, network/storage/NIC/CPU/memory inspection, firmware upgrade.
- `proliant com`: device listing, firmware bundles, login/logout.

---
