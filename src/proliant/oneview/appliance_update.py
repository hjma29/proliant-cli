"""
proliant.oneview.appliance_update
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
Appliance **software** upgrade for an HPE Synergy Composer / OneView appliance
over the OneView REST API (e.g. hopping 9.20 -> 10.0).

This is the *appliance's own software* (the ``update.bin``), NOT the SPP/SSP
server-firmware bundle. Appliance software is **upload only** — the appliance
cannot pull it from a URL — so the image must be a local/UNC file path that is
streamed to the appliance.

There are two very different "upgrades" in the Synergy/OneView world; this module
only implements the first:

  (1) **Appliance software upgrade** (this module — 9.20 -> 10.0, ``update.bin``):
      fully **self-managed**. A single ``PUT .../firmware/pending?file=`` hands the
      whole job to Composer2's two-node active/standby HA pair, which upgrades the
      standby node, does *"Prepare for active/standby node swap"* (~55%) ->
      *"Swap active/standby nodes"* (~60%, brief mgmt-plane outage) -> reboots and
      upgrades the other node -> reconverges on the new version. There is **no REST
      knob** to drive the failover manually; the caller only stages + confirms.

  (2) **SSP (Synergy Service Pack) server/infrastructure firmware** (NOT here):
      applied as a firmware **baseline** to managed hardware (compute modules,
      interconnects, frames) via a Logical Enclosure / Server Profile firmware
      update. OneView still drives the per-component flashing, but the operator
      chooses the baseline, scope, and activation timing, and it is **disruptive**
      (interconnect reboots, compute-module power cycles). The appliance must be on
      a compatible software version (1) *before* applying a newer SSP baseline.
      Our CLI only exposes this read-only (``oneview firmware ...``).

REST flow (verified against a live Synergy Composer2, cross-checked with HPE's
POSH-HPEOneView module and oneview-python SDK):

  GET    /rest/appliance/firmware/pending            -> currently staged update ({} if none)
  DELETE /rest/appliance/firmware/pending            -> clear a stuck/aborted staged update
  POST   /rest/appliance/firmware/image (multipart)  -> upload + stage an update .bin
  PUT    /rest/appliance/firmware/pending?file=<name>-> START the install
  GET    /cgi-bin/status/update-status.cgi           -> live install progress (phase/percent) — AUTHORITATIVE
  GET    /rest/appliance/nodeinfo/version            -> confirm version (post-reboot); gates "completed"
  GET    /rest/appliance/firmware/notification       -> final upgrade result

  NOTE: ``/rest/appliance/progress`` reports *firmware component* progress (e.g.
  60/60 SSP components = 100%) that is left stale from prior tasks and is NOT the
  appliance-software progress. Relying on it caused a false "upgrade complete" the
  instant install started, so ``read_progress`` is CGI-only and completion is gated
  on the version endpoint actually flipping to the target build.

The write/reboot half of this flow cannot be exercised against anything but a
live production appliance, so callers must gate real execution behind an
explicit confirmation; the default is to stage the image only.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Callable
from urllib.parse import quote

if TYPE_CHECKING:
    from proliant.oneview.client import OneViewClient


# ── REST endpoints ────────────────────────────────────────────────────────────

PENDING_URI = "/rest/appliance/firmware/image"  # POST multipart (upload)
STAGED_URI = "/rest/appliance/firmware/pending"  # GET / DELETE / PUT
PROGRESS_CGI = "/cgi-bin/status/update-status.cgi"
PROGRESS_URI = "/rest/appliance/progress"
NOTIFICATION_URI = "/rest/appliance/firmware/notification"
NODE_VERSION_URI = "/rest/appliance/nodeinfo/version"


# ── image filename parsing / discovery ────────────────────────────────────────

# Matches an appliance *update* image such as:
#   HPE_Synergy_Composer2_10.00.00_Update_Z7550-97964.bin
#   HPE_Synergy_Composer_2_9.40.00_Update_Z7550-97893.bin
#   HPE_Synergy_Composer2_7.00.00_UPDATE_Z7550-97362.bin
#   HPE_OneView_10.20.00_update_Z7550-98029.bin
# Deliberately excludes reimage/install bundles (``_INSTALL_*.zip``) and the
# small unversioned hotfix ``HPE_Synergy_Composer2_Update_*.bin`` (no version).
_IMG_RE = re.compile(
    r"""^HPE[_ ]
        (?P<family>OneView|Synergy[_ ]Composer(?:[_ ]?2)?)   # product family
        [_ ](?P<version>\d+\.\d+(?:\.\d+)?)                   # version (2 or 3 parts)
        [_ ]Update[_ ]                                        # 'Update' keyword
        .*\.bin$""",
    re.IGNORECASE | re.VERBOSE,
)


@dataclass
class ApplianceImage:
    """A parsed appliance software update image on disk."""

    path: str
    filename: str
    platform: str          # 'synergy' | 'oneview'
    family_label: str      # e.g. 'HPE Synergy Composer2'
    version: str           # normalized 'major.minor.revision'
    version_tuple: tuple[int, int, int]
    size_bytes: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "filename": self.filename,
            "platform": self.platform,
            "family_label": self.family_label,
            "version": self.version,
            "size_bytes": self.size_bytes,
            "size_gb": round(self.size_bytes / (1024 ** 3), 2) if self.size_bytes else 0.0,
        }


def _norm_version(raw: str) -> tuple[str, tuple[int, int, int]]:
    parts = raw.split(".")
    nums: list[int] = []
    for p in parts[:3]:
        m = re.match(r"\d+", p)
        nums.append(int(m.group()) if m else 0)
    while len(nums) < 3:
        nums.append(0)
    label = f"{nums[0]}.{nums[1]:02d}.{nums[2]:02d}"
    return label, (nums[0], nums[1], nums[2])


def _family_label(raw: str) -> tuple[str, str]:
    """Return (platform, display label) for a matched family token."""
    canon = re.sub(r"[_ ]+", " ", raw).strip()
    if canon.lower().startswith("oneview"):
        return "oneview", "HPE OneView"
    # Synergy Composer / Composer2 / Composer 2
    if re.search(r"composer\s*2\b|composer2\b", canon, re.IGNORECASE):
        return "synergy", "HPE Synergy Composer2"
    return "synergy", "HPE Synergy Composer"


def parse_image_filename(
    filename: str, *, path: str | None = None, size_bytes: int = 0
) -> ApplianceImage | None:
    """Parse an appliance update image filename, or ``None`` if it isn't one."""
    m = _IMG_RE.match(filename.strip())
    if not m:
        return None
    platform, label = _family_label(m.group("family"))
    version, vt = _norm_version(m.group("version"))
    return ApplianceImage(
        path=path or filename,
        filename=filename,
        platform=platform,
        family_label=label,
        version=version,
        version_tuple=vt,
        size_bytes=size_bytes,
    )


def platform_for_appliance(model_or_family: str) -> str:
    """Map an appliance ``modelNumber``/``family`` to an image platform filter.

    'Synergy Composer2' / 'Synergy Composer' -> 'synergy'; anything else
    (VM/DL OneView appliance) -> 'oneview'.
    """
    return "synergy" if "synergy" in (model_or_family or "").lower() else "oneview"


def discover_images(
    directory: str, *, platform: str | None = None
) -> list[ApplianceImage]:
    """List appliance update images in *directory*, newest version last.

    ``platform`` (``'synergy'``/``'oneview'``) filters to matching images; when
    ``None`` all recognised update images are returned. Raises the underlying
    OS error (FileNotFoundError/NotADirectoryError/PermissionError) if the
    directory can't be read — callers surface a friendly message.
    """
    images: list[ApplianceImage] = []
    with os.scandir(directory) as it:
        for entry in it:
            if not entry.is_file():
                continue
            if not entry.name.lower().endswith(".bin"):
                continue
            try:
                size = entry.stat().st_size
            except OSError:
                size = 0
            img = parse_image_filename(entry.name, path=entry.path, size_bytes=size)
            if img is None:
                continue
            if platform and img.platform != platform:
                continue
            images.append(img)
    images.sort(key=lambda i: i.version_tuple)
    return images


# ── pending (staged) update normalization ─────────────────────────────────────

def normalize_pending(data: dict[str, Any] | None) -> dict[str, Any] | None:
    """Normalize a ``/rest/appliance/firmware/pending`` payload, or ``None``.

    Returns ``None`` when nothing is staged (the appliance returns ``{}``).
    """
    if not data:
        return None
    file_name = (
        data.get("fileName")
        or data.get("uploadfilename")
        or data.get("baseName")
        or ""
    )
    est = data.get("estimatedUpgradeTime")
    return {
        "file_name": file_name,
        "version": data.get("version") or "",
        "estimated_upgrade_minutes": est if isinstance(est, (int, float)) else None,
        "reboot_required": bool(data.get("rebootRequired", True)),
        "raw": data,
    }


# ── install progress normalization ────────────────────────────────────────────

def _parse_percent(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    m = re.search(r"\d+(?:\.\d+)?", str(value))
    return float(m.group()) if m else None


def normalize_progress(data: dict[str, Any] | None) -> dict[str, Any]:
    """Normalize the live install-status payload (cgi or /rest/appliance/progress).

    The unauthenticated update-status CGI (``PROGRESS_CGI``) is reachable even
    during the mgmt-plane outage of the active/standby node swap and is the
    same source the appliance's own GUI ("Updating..." screen) uses — it
    additionally reports *when* the current step started and how long it's
    expected to take, e.g.::

        {"percentageCompletion": "60%", "step": "Swap active/standby nodes",
         "stepStartTime": "2026-07-19T03:14:39.002Z", "stepExpectedMins": "15",
         "stepExpectation": "(takes about 15 minutes)", ...}
    """
    data = data or {}
    percent = _parse_percent(
        data.get("percentageCompletion")
        if data.get("percentageCompletion") is not None
        else data.get("percentComplete")
    )
    return {
        "percent": percent,
        "task_step": data.get("taskStep") or "",
        "status": data.get("status") or "",
        "step": data.get("step") or "",
        "step_start_time": data.get("stepStartTime") or "",
        "step_expected_mins": _parse_percent(data.get("stepExpectedMins")),
        "step_expectation": data.get("stepExpectation") or "",
    }


_FAILURE_RE = re.compile(r"FAIL(?!OVER)|ERROR")


def is_progress_failed(progress: dict[str, Any]) -> bool:
    """True when the appliance's own status genuinely reports a failure.

    A plain ``"FAIL" in status`` substring check misfires on ``TS_PRE_FAILOVER``
    / ``TS_FAILOVER`` — the normal, expected task steps during the
    active/standby node swap (see module docstring). Those are excluded so a
    real install is never mistaken for "failed" mid-swap; genuine failures
    (``TS_FAILED``, status ``"Error"``, etc.) still match.
    """
    ts = (progress.get("task_step") or "").upper()
    status = (progress.get("status") or "").upper()
    return bool(_FAILURE_RE.search(ts) or _FAILURE_RE.search(status))


def is_progress_complete(progress: dict[str, Any]) -> bool:
    ts = (progress.get("task_step") or "").upper()
    status = (progress.get("status") or "").lower()
    if ts in {"TS_COMPLETED", "TS_COMPLETE"}:
        return True
    if status in {"completed", "complete", "success", "succeeded"}:
        return True
    pct = progress.get("percent")
    return pct is not None and pct >= 100


def is_reboot_phase(progress: dict[str, Any]) -> bool:
    ts = (progress.get("task_step") or "").upper()
    step = (progress.get("step") or "").upper()
    return "REBOOT" in ts or "REBOOT" in step


# ── I/O wrappers (thin; each maps 1:1 to a REST call) ──────────────────────────

async def read_pending(client: "OneViewClient") -> dict[str, Any] | None:
    """Return the currently staged appliance update, or ``None`` if none."""
    data = await client.get(STAGED_URI)
    return normalize_pending(data)


async def clear_pending(client: "OneViewClient") -> None:
    """Remove a stuck/aborted staged update (DELETE pending)."""
    await client.delete(STAGED_URI)


async def upload_image(
    client: "OneViewClient",
    image_path: str,
    *,
    filename: str | None = None,
    on_progress: Callable[[int, int], None] | None = None,
) -> dict[str, Any] | None:
    """Stream an update image to the appliance and return the staged summary.

    *on_progress* (if given) is forwarded to the client so a byte-level upload
    progress bar can be shown for the multi-GB image.
    """
    fn = filename or os.path.basename(image_path)
    resp = await client.upload_file(PENDING_URI, image_path, filename=fn, on_progress=on_progress)
    # The POST response is itself the pending object; fall back to a re-read if
    # the appliance returned an empty/opaque body.
    pending = normalize_pending(resp)
    if pending is None:
        pending = await read_pending(client)
    return pending


async def start_install(client: "OneViewClient", file_name: str) -> dict[str, Any]:
    """START the staged install (PUT pending?file=<name>)."""
    uri = f"{STAGED_URI}?file={quote(file_name)}"
    return await client.put(uri, {})


async def read_progress(client: "OneViewClient") -> dict[str, Any]:
    """Read live appliance-update progress from the update-status CGI.

    Only the appliance's own ``update-status.cgi`` is authoritative for a
    *software* upgrade — it reports the real phase (``step``/``status``) and
    ``percentageCompletion``. The generic ``/rest/appliance/progress`` endpoint
    reports firmware *component* progress and can read back a stale ``100%``
    left over from a prior task; trusting it made the monitor declare an upgrade
    "complete" the instant it started, so it is deliberately not used here.
    """
    try:
        data = await client.get(PROGRESS_CGI)
    except Exception:  # noqa: BLE001 — the CGI is briefly unavailable as the update spins up
        data = {}
    return normalize_progress(data)


async def read_version(client: "OneViewClient") -> dict[str, Any]:
    """Read the appliance software version (post-reboot confirmation)."""
    data = await client.get(NODE_VERSION_URI)
    return {
        "software_version": data.get("softwareVersion", ""),
        "model": data.get("modelNumber", ""),
        "family": data.get("family", ""),
    }


# ── orchestration ─────────────────────────────────────────────────────────────
#
# ``run_appliance_upgrade`` is written against a ``client_factory`` (a zero-arg
# callable returning an ``async with``-able OneViewClient) and an injectable
# ``sleeper`` so the whole flow — including the reboot/reconnect polling loop —
# is unit-testable with a fake client. The install/reboot half can only ever be
# exercised for real against a live appliance, so callers keep it behind an
# explicit confirmation; ``execute=False`` (the default) stops after staging.

def _same_image(staged_file: str, image_filename: str) -> bool:
    if not staged_file or not image_filename:
        return False
    return os.path.basename(staged_file).lower() == os.path.basename(image_filename).lower()


def _version_tuple(raw: str) -> tuple[int, int, int]:
    """Numeric (major, minor, revision) of a version/build string.

    Tolerates a trailing build suffix, e.g. ``'10.00.00-0507518'`` -> ``(10, 0, 0)``.
    """
    return _norm_version(raw or "0")[1]


def _progress_active(prog: dict[str, Any]) -> bool:
    """True when the payload shows the install is genuinely underway.

    Used so a stale/initial ``100%`` (before the install has begun) can't be
    mistaken for completion — we only accept a "complete" signal once we've
    actually observed the update running or the appliance rebooting.
    """
    pct = prog.get("percent")
    if isinstance(pct, (int, float)) and 0.0 < pct < 100.0:
        return True
    blobs = (
        (prog.get("status") or "").upper(),
        (prog.get("task_step") or "").upper(),
        (prog.get("step") or "").upper(),
    )
    keys = ("INSTALL", "UPDATE", "REBOOT", "PROGRESS", "SWAP", "PREPARE", "STAGE")
    return any(k in b for b in blobs for k in keys)


async def _poll_install(
    client_factory: Callable[[], Any],
    emit: Callable[[str, dict[str, Any]], None],
    sleeper: Callable[[float], Any],
    poll_interval_s: float,
    timeout_s: float,
    target_version: str | None = None,
) -> dict[str, Any]:
    """Poll install progress, tolerating the reboot outage, until done/failed.

    Completion is confirmed by the appliance actually reaching *target_version*
    (its node-version endpoint flipping to the new build) rather than by a
    progress percentage alone, which can read a stale ``100%`` before the
    install has really begun. When *target_version* is unknown, an explicit
    "complete" progress signal is accepted, but only once the install has been
    observed running.
    """
    elapsed = 0.0
    last: dict[str, Any] = {}
    started = False
    target_vt = _version_tuple(target_version) if target_version else None

    while elapsed <= timeout_s:
        prog: dict[str, Any] = {}
        version: dict[str, Any] = {}
        reachable = True
        try:
            async with client_factory() as client:
                prog = await read_progress(client)
                try:
                    version = await read_version(client)
                except Exception:  # noqa: BLE001 — version read is best-effort mid-update
                    version = {}
        except Exception as exc:  # noqa: BLE001 — appliance unreachable (rebooting)
            reachable = False
            started = True  # an unreachable appliance means the install is underway
            emit("rebooting", {"detail": str(exc)})

        if reachable:
            last = prog or last
            emit("progress", prog)
            if is_progress_failed(prog):
                return {"status": "failed", "progress": prog, "version": version}
            if _progress_active(prog):
                started = True

            sv = (version or {}).get("software_version") or ""
            if target_vt is not None:
                # Authoritative success: the box actually came up on the target.
                if sv and _version_tuple(sv) >= target_vt:
                    return {"status": "completed", "progress": prog, "version": version}
            elif started and is_progress_complete(prog):
                return {"status": "completed", "progress": prog, "version": version}

        await sleeper(poll_interval_s)
        elapsed += poll_interval_s

    return {"status": "timeout", "progress": last}


async def run_appliance_upgrade(
    client_factory: Callable[[], Any],
    image: "ApplianceImage",
    *,
    execute: bool = False,
    confirm: Callable[[dict[str, Any]], bool] | None = None,
    on_event: Callable[[str, dict[str, Any]], None] | None = None,
    clear_existing: bool = False,
    sleeper: Callable[[float], Any] | None = None,
    poll_interval_s: float = 20.0,
    reboot_timeout_s: float = 40 * 60,
) -> dict[str, Any]:
    """Stage (and optionally install) an appliance software update.

    Returns a result dict whose ``status`` is one of:
      ``staged``     image uploaded and staged; not installed (execute=False)
      ``conflict``   a *different* update is already staged and clear_existing=False
      ``aborted``    execute=True but the confirm callback returned False
      ``completed``  install finished and the appliance came back
      ``failed``     the appliance reported an install failure
      ``timeout``    the appliance didn't return within reboot_timeout_s
    """
    import asyncio

    sleeper = sleeper or asyncio.sleep
    emit = on_event or (lambda kind, data: None)

    # ── phase 1: stage ────────────────────────────────────────────────────
    async with client_factory() as client:
        existing = await read_pending(client)
        if existing and _same_image(existing.get("file_name", ""), image.filename):
            emit("already-staged", existing)
            staged = existing
        else:
            if existing:
                if not clear_existing:
                    return {"status": "conflict", "pending": existing}
                emit("clearing", existing)
                await clear_pending(client)
            emit("uploading", {"filename": image.filename, "size_bytes": image.size_bytes})

            def _upload_cb(sent: int, total: int) -> None:
                emit("upload-progress", {"completed": sent, "total": total})

            staged = await upload_image(
                client, image.path, filename=image.filename, on_progress=_upload_cb
            )
            staged = await read_pending(client) or staged
        emit("staged", staged or {})

    if not staged:
        return {"status": "failed", "detail": "upload did not produce a staged update"}

    if not execute:
        return {"status": "staged", "pending": staged}

    if confirm is not None and not confirm(staged):
        return {"status": "aborted", "pending": staged}

    # ── phase 2: start install ────────────────────────────────────────────
    async with client_factory() as client:
        emit("installing", staged)
        await start_install(client, staged["file_name"])

    # ── phase 3: monitor to completion/reboot ─────────────────────────────
    target_version = (
        (staged.get("version") if isinstance(staged, dict) else "") or image.version
    )
    result = await _poll_install(
        client_factory, emit, sleeper, poll_interval_s, reboot_timeout_s,
        target_version=target_version,
    )
    return {"pending": staged, **result}
