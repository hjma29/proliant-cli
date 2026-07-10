"""
proliant.oneview.appliance_update
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
Appliance **software** upgrade for an HPE Synergy Composer / OneView appliance
over the OneView REST API (e.g. hopping 9.20 -> 10.0).

This is the *appliance's own software* (the ``update.bin``), NOT the SPP/SSP
server-firmware bundle. Appliance software is **upload only** — the appliance
cannot pull it from a URL — so the image must be a local/UNC file path that is
streamed to the appliance.

REST flow (verified against a live Synergy Composer2, cross-checked with HPE's
POSH-HPEOneView module and oneview-python SDK):

  GET    /rest/appliance/firmware/pending            -> currently staged update ({} if none)
  DELETE /rest/appliance/firmware/pending            -> clear a stuck/aborted staged update
  POST   /rest/appliance/firmware/image (multipart)  -> upload + stage an update .bin
  PUT    /rest/appliance/firmware/pending?file=<name>-> START the install
  GET    /cgi-bin/status/update-status.cgi           -> live install progress (phase/percent)
  GET    /rest/appliance/progress                    -> numeric percentComplete (fallback)
  GET    /rest/appliance/nodeinfo/version            -> confirm version (post-reboot)
  GET    /rest/appliance/firmware/notification       -> final upgrade result

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
    """Normalize the live install-status payload (cgi or /rest/appliance/progress)."""
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
        "step": data.get("step") or data.get("stepExpectation") or "",
    }


def is_progress_failed(progress: dict[str, Any]) -> bool:
    ts = (progress.get("task_step") or "").upper()
    status = (progress.get("status") or "").upper()
    return "FAIL" in ts or "FAIL" in status or "ERROR" in status


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
    client: "OneViewClient", image_path: str, *, filename: str | None = None
) -> dict[str, Any] | None:
    """Stream an update image to the appliance and return the staged summary."""
    fn = filename or os.path.basename(image_path)
    resp = await client.upload_file(PENDING_URI, image_path, filename=fn)
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
    """Read live install progress, preferring the cgi phase view."""
    try:
        data = await client.get(PROGRESS_CGI)
        prog = normalize_progress(data)
        if prog.get("task_step") or prog.get("status"):
            return prog
    except Exception:  # noqa: BLE001 — fall back to the REST progress endpoint
        pass
    data = await client.get(PROGRESS_URI)
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


async def _poll_install(
    client_factory: Callable[[], Any],
    emit: Callable[[str, dict[str, Any]], None],
    sleeper: Callable[[float], Any],
    poll_interval_s: float,
    timeout_s: float,
) -> dict[str, Any]:
    """Poll install progress, tolerating the reboot outage, until done/failed."""
    elapsed = 0.0
    last: dict[str, Any] = {}
    while elapsed <= timeout_s:
        try:
            async with client_factory() as client:
                prog = await read_progress(client)
            last = prog
            emit("progress", prog)
            if is_progress_failed(prog):
                return {"status": "failed", "progress": prog}
            if is_progress_complete(prog):
                version: dict[str, Any] = {}
                try:
                    async with client_factory() as client:
                        version = await read_version(client)
                except Exception:  # noqa: BLE001 — version read is best-effort
                    version = {}
                return {"status": "completed", "progress": prog, "version": version}
        except Exception as exc:  # noqa: BLE001 — appliance unreachable (rebooting)
            emit("rebooting", {"detail": str(exc)})
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
            staged = await upload_image(client, image.path, filename=image.filename)
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
    result = await _poll_install(
        client_factory, emit, sleeper, poll_interval_s, reboot_timeout_s
    )
    return {"pending": staged, **result}
