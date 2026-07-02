# Code Signing Policy

This page describes how `proliant` release binaries are (and will be) signed. It
exists mainly to satisfy [SignPath Foundation](https://signpath.org/)'s
requirement that applicants publish a code signing policy before a free
Authenticode certificate for open-source projects is granted.

## Current status

**Not yet signed.** Releases built by [`build-exe.yml`](.github/workflows/build-exe.yml)
are currently distributed unsigned via `install.ps1` / `install.sh` and the
GitHub Releases page. This document tracks the plan and one-time setup to
change that.

## What will be signed

| Artifact | Plan |
| :--- | :--- |
| `proliant-cli-windows.exe` (or its future GUI installer, e.g. `proliant-setup.exe`) | Authenticode-signed via SignPath on every tagged release build. |
| `proliant-cli-linux-x86` / `proliant-cli-linux-arm64` | Unsigned for now. May add `cosign` keyless signing (Sigstore, via GitHub OIDC) later — no cost, no external approval needed. |
| `proliant-cli-macos` | Unsigned for now. Apple notarization requires a paid ($99/yr) Apple Developer Program membership; not planned unless macOS distribution grows. |

## Verifying a Windows signature (once enabled)

Signed binaries will show **"SignPath Foundation"** as the publisher in the
Windows UAC/SmartScreen prompt and in *File → Properties → Digital
Signatures*. Verify from PowerShell:

```powershell
Get-AuthenticodeSignature .\proliant-cli-windows.exe | Format-List
```

`Status` should read `Valid`; `SignerCertificate.Subject` should contain
`CN=SignPath Foundation`. Note that a new certificate/binary identity accrues
SmartScreen reputation gradually — expect brief warnings on the first
downloads of each newly signed version.

## Team roles

- **Committers and reviewers** — members with push access to
  [`hjma29/proliant-cli`](https://github.com/hjma29/proliant-cli). Currently
  a single maintainer: `@hjma29`.
- **Approvers** — the designated release approver who clicks *Approve* in the
  SignPath dashboard for each `release-signing` request. Currently: `@hjma29`.

All maintainers use multi-factor authentication on both GitHub and
SignPath.io.

## Privacy

`proliant` is a CLI tool for managing HPE ProLiant servers (iLO Redfish and
HPE Compute Ops Management). It does not collect or transmit telemetry beyond:
an anonymous install-count ping (OS only, no personal data) in `install.ps1`
/ `install.sh`, and opt-in crash reporting via Sentry. No user credentials,
inventory data, or server data are ever sent to any third party by this
project.

## Build provenance

Every release binary is built from a tagged commit (`vX.Y.Z`) by the
[`Build and Release`](.github/workflows/build-exe.yml) GitHub Actions
workflow, which runs on GitHub-hosted runners directly from this repository's
source — no self-hosted or third-party build infrastructure is involved. Once
SignPath is wired in, origin verification on the `release-signing` policy
will confirm that a signed binary was produced by this exact workflow from
the cited tag.

---

## Setup checklist (one-time, tracked here for whoever wires this up)

### Prerequisites

- [x] **OSI-approved license at repo root.** `proliant` is MIT-licensed — see [`LICENSE`](LICENSE).
- [x] **MFA enabled** on the maintainer's GitHub account.
- [x] **Project already released and documented** — see `CHANGELOG.md` and `README.md`; releases exist back to v1.0.x.
- [x] **Automated CI build** — [`build-exe.yml`](.github/workflows/build-exe.yml) already builds Windows/Linux/macOS binaries via Nuitka on every tag push.

### Apply

1. Submit the application at <https://signpath.org/apply>.
2. Wait for review (manual, days to weeks). SignPath Foundation may decline
   without detailed justification per their terms — be ready to follow up
   politely if there's no response after a couple of weeks.

### Configure SignPath.io (after approval)

1. Create a project:
   - **Slug:** `proliant-cli` (must match the project slug used in the
     signing step added to `build-exe.yml`).
   - **Repository URL:** `https://github.com/hjma29/proliant-cli`
   - **Trusted Build System:** GitHub.com
2. Upload an artifact configuration describing the `.exe` to sign.
3. Add two signing policies:
   - `test-signing` — for validating the integration against a throwaway build.
   - `release-signing` — origin verification enabled, restricted to tag
     builds (`refs/tags/v*`).
4. Designate `@hjma29` (or another maintainer) as Approver on `release-signing`.

### Wire to GitHub

In repo settings (*Settings → Secrets and variables → Actions*):

- **Secret** `SIGNPATH_API_TOKEN` — an interactive API token from a SignPath
  user with the `Submitter` role on both signing policies above.
- **Variable** `SIGNPATH_ORGANIZATION_ID` — the SignPath org ID (visible
  top-right in the SignPath.io UI).

Add a signing step to `build-exe.yml` (Windows leg only) that runs after the
Nuitka build and before the GitHub Release upload, guarded so the workflow
still produces an unsigned binary if these secrets aren't set (keeps CI green
for forks/PRs that can't access repo secrets).

### Validate

1. Trigger `test-signing` on a branch build first to confirm the round-trip
   works end-to-end.
2. On the next tagged release, watch the Actions log: the signing step should
   block pending approval in the SignPath dashboard, then complete once
   approved.
3. Confirm the published GitHub Release asset shows **"SignPath Foundation"**
   as the publisher in Windows file properties.
4. Update the **Current status** and **Team roles** sections above once this
   is live — remove the "Not yet signed" note.

### Rollback

Delete the `SIGNPATH_API_TOKEN` secret and `SIGNPATH_ORGANIZATION_ID`
variable from repo settings. The workflow reverts to producing unsigned
output with no code change required.
