"""Tests for OneView appliance software-upgrade helpers + orchestration.

The install/reboot half of the flow can only run against a live appliance, so
here it's exercised with a fake client that records calls and replays scripted
progress — covering the stage-vs-execute branching, conflict handling, and the
reboot-poll loop deterministically.
"""

from __future__ import annotations

import os

import pytest

from proliant.oneview.appliance_update import (
    NODE_VERSION_URI,
    PROGRESS_CGI,
    PROGRESS_URI,
    STAGED_URI,
    ApplianceImage,
    discover_images,
    is_progress_complete,
    is_progress_failed,
    is_reboot_phase,
    normalize_pending,
    normalize_progress,
    parse_image_filename,
    platform_for_appliance,
    run_appliance_upgrade,
)


# ── filename parsing ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("name,platform,version", [
    ("HPE_Synergy_Composer2_10.00.00_Update_Z7550-97964.bin", "synergy", "10.00.00"),
    ("HPE_Synergy_Composer2_9.20.00_Update_Z7550-97826.bin", "synergy", "9.20.00"),
    ("HPE_Synergy_Composer_2_9.40.00_Update_Z7550-97893.bin", "synergy", "9.40.00"),
    ("HPE_Synergy_Composer2_7.00.00_UPDATE_Z7550-97362.bin", "synergy", "7.00.00"),
    ("HPE_OneView_10.20.00_update_Z7550-98029.bin", "oneview", "10.20.00"),
    ("HPE_OneView_9.20.00_update_Z7550-97824.bin", "oneview", "9.20.00"),
])
def test_parse_image_filename_ok(name, platform, version):
    img = parse_image_filename(name)
    assert img is not None
    assert img.platform == platform
    assert img.version == version


@pytest.mark.parametrize("name", [
    "HPE_Synergy_Composer2_6.60.00_INSTALL_Z7550-97328.zip",   # reimage/install .zip
    "HPE_Synergy_Composer2_Update_Z7550-97855.bin",            # unversioned hotfix
    "Synergy_Service_Pack_SSP_2026.01.02_Z7550-98102.iso",      # SSP bundle, not appliance
    "random.bin",
    "notes.txt",
])
def test_parse_image_filename_rejects(name):
    assert parse_image_filename(name) is None


def test_parse_image_version_tuple_and_label():
    img = parse_image_filename("HPE_Synergy_Composer2_10.00.00_Update_Z7550-97964.bin",
                               path="/x/y.bin", size_bytes=2 * 1024 ** 3)
    assert img.version_tuple == (10, 0, 0)
    assert img.family_label == "HPE Synergy Composer2"
    assert img.as_dict()["size_gb"] == 2.0


@pytest.mark.parametrize("model,expected", [
    ("Synergy Composer2", "synergy"),
    ("Synergy Composer", "synergy"),
    ("", "oneview"),
    ("OneView VM Appliance", "oneview"),
])
def test_platform_for_appliance(model, expected):
    assert platform_for_appliance(model) == expected


# ── directory discovery ──────────────────────────────────────────────────────

def test_discover_images_filters_and_sorts(tmp_path):
    names = [
        "HPE_Synergy_Composer2_9.20.00_Update_Z7550-97826.bin",
        "HPE_Synergy_Composer2_10.00.00_Update_Z7550-97964.bin",
        "HPE_Synergy_Composer2_9.10.01_Update_Z7550-97845.bin",
        "HPE_OneView_10.20.00_update_Z7550-98029.bin",         # different platform
        "Synergy_Service_Pack_SSP_2026.01.02_Z7550-98102.iso",  # not an appliance image
        "readme.txt",
    ]
    for n in names:
        (tmp_path / n).write_bytes(b"x")

    syn = discover_images(str(tmp_path), platform="synergy")
    assert [i.version for i in syn] == ["9.10.01", "9.20.00", "10.00.00"]  # sorted ascending

    ov = discover_images(str(tmp_path), platform="oneview")
    assert [i.version for i in ov] == ["10.20.00"]

    all_imgs = discover_images(str(tmp_path))
    assert len(all_imgs) == 4  # 3 synergy + 1 oneview, SSP/txt excluded


def test_discover_images_missing_dir(tmp_path):
    with pytest.raises(OSError):
        discover_images(str(tmp_path / "nope"))


# ── pending normalization ────────────────────────────────────────────────────

def test_normalize_pending_empty_is_none():
    assert normalize_pending({}) is None
    assert normalize_pending(None) is None


def test_normalize_pending_populated():
    p = normalize_pending({
        "fileName": "HPE_Synergy_Composer2_10.00.00_Update_Z7550-97964.bin",
        "version": "10.00.00",
        "estimatedUpgradeTime": 45,
        "rebootRequired": True,
    })
    assert p["file_name"].endswith("97964.bin")
    assert p["version"] == "10.00.00"
    assert p["estimated_upgrade_minutes"] == 45
    assert p["reboot_required"] is True


# ── progress normalization ───────────────────────────────────────────────────

def test_normalize_progress_cgi_shape():
    prog = normalize_progress({
        "percentageCompletion": "62%", "taskStep": "TS_INSTALL", "status": "Installing",
    })
    assert prog["percent"] == 62.0
    assert prog["task_step"] == "TS_INSTALL"
    assert not is_progress_complete(prog)
    assert not is_progress_failed(prog)


def test_normalize_progress_rest_shape_and_complete():
    prog = normalize_progress({"percentComplete": 100.0})
    assert prog["percent"] == 100.0
    assert is_progress_complete(prog)


@pytest.mark.parametrize("payload", [
    {"taskStep": "TS_COMPLETED", "status": "Completed"},
    {"status": "success"},
    {"percentComplete": 100},
])
def test_is_progress_complete_true(payload):
    assert is_progress_complete(normalize_progress(payload))


@pytest.mark.parametrize("payload", [
    {"taskStep": "TS_FAILED"},
    {"status": "Error"},
])
def test_is_progress_failed_true(payload):
    assert is_progress_failed(normalize_progress(payload))


def test_is_reboot_phase():
    assert is_reboot_phase(normalize_progress({"taskStep": "TS_REBOOT"}))
    assert not is_reboot_phase(normalize_progress({"taskStep": "TS_INSTALL"}))


# ── fake client + orchestration ──────────────────────────────────────────────

class FakeClient:
    """Async-context OneViewClient stand-in that records calls."""

    def __init__(self, *, pending=None, progress_script=None, version=None,
                 version_script=None):
        self.pending = pending           # raw GET /pending payload (or None/{})
        self.progress_script = list(progress_script or [])
        self.version = version or {}
        # Optional per-read version payloads (popped on each version GET) to
        # simulate the software_version flipping only after the reboot.
        self.version_script = list(version_script) if version_script is not None else None
        self.calls: list[tuple] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, uri, params=None):
        self.calls.append(("get", uri))
        if uri == STAGED_URI:
            return self.pending or {}
        if uri == PROGRESS_CGI:
            return self.progress_script.pop(0) if self.progress_script else {}
        if uri == PROGRESS_URI:
            return {}
        if uri == NODE_VERSION_URI:
            if self.version_script:
                self.version = self.version_script.pop(0)
            return self.version
        return {}

    async def delete(self, uri):
        self.calls.append(("delete", uri))
        self.pending = None
        return {}

    async def put(self, uri, body=None):
        self.calls.append(("put", uri))
        return {}

    async def upload_file(self, uri, path, filename=None, on_progress=None):
        self.calls.append(("upload", filename or os.path.basename(path)))
        if on_progress is not None:
            on_progress(512, 1024)
            on_progress(1024, 1024)
        self.pending = {
            "fileName": filename or os.path.basename(path),
            "version": "10.00.00",
            "estimatedUpgradeTime": 45,
            "rebootRequired": True,
        }
        return self.pending

    def methods(self):
        return [m for m, _ in self.calls]


async def _noop_sleep(_seconds):
    return None


def _image(name="HPE_Synergy_Composer2_10.00.00_Update_Z7550-97964.bin"):
    img = parse_image_filename(name, path=f"/imgs/{name}", size_bytes=2 * 1024 ** 3)
    assert img is not None
    return img


@pytest.mark.asyncio
async def test_run_stage_only_uploads_and_stops():
    fake = FakeClient(pending=None)
    result = await run_appliance_upgrade(lambda: fake, _image(), execute=False)
    assert result["status"] == "staged"
    assert "upload" in fake.methods()
    assert "put" not in fake.methods()  # never started an install


@pytest.mark.asyncio
async def test_run_already_staged_skips_upload():
    name = "HPE_Synergy_Composer2_10.00.00_Update_Z7550-97964.bin"
    fake = FakeClient(pending={"fileName": name, "version": "10.00.00"})
    result = await run_appliance_upgrade(lambda: fake, _image(name), execute=False)
    assert result["status"] == "staged"
    assert "upload" not in fake.methods()


@pytest.mark.asyncio
async def test_run_conflict_without_clear():
    fake = FakeClient(pending={"fileName": "HPE_Synergy_Composer2_9.30.00_Update_X.bin"})
    result = await run_appliance_upgrade(lambda: fake, _image(), execute=False)
    assert result["status"] == "conflict"
    assert "upload" not in fake.methods()
    assert "delete" not in fake.methods()


@pytest.mark.asyncio
async def test_run_conflict_cleared_then_uploads():
    fake = FakeClient(pending={"fileName": "HPE_Synergy_Composer2_9.30.00_Update_X.bin"})
    result = await run_appliance_upgrade(
        lambda: fake, _image(), execute=False, clear_existing=True
    )
    assert result["status"] == "staged"
    assert "delete" in fake.methods()
    assert "upload" in fake.methods()


@pytest.mark.asyncio
async def test_run_execute_aborted_by_confirm():
    fake = FakeClient(pending=None)
    result = await run_appliance_upgrade(
        lambda: fake, _image(), execute=True, confirm=lambda staged: False
    )
    assert result["status"] == "aborted"
    assert "put" not in fake.methods()


@pytest.mark.asyncio
async def test_run_execute_completes_after_reboot():
    fake = FakeClient(
        pending=None,
        progress_script=[
            {"percentageCompletion": "40%", "taskStep": "TS_INSTALL", "status": "Installing"},
            {"percentageCompletion": "100%", "taskStep": "TS_COMPLETED", "status": "Completed"},
        ],
        version={"softwareVersion": "10.00.00-1234", "modelNumber": "Synergy Composer2"},
    )
    result = await run_appliance_upgrade(
        lambda: fake, _image(), execute=True, confirm=lambda staged: True,
        sleeper=_noop_sleep, poll_interval_s=1, reboot_timeout_s=100,
    )
    assert result["status"] == "completed"
    assert result["version"]["software_version"] == "10.00.00-1234"
    assert "put" in fake.methods()  # install was started


@pytest.mark.asyncio
async def test_run_execute_reports_failure():
    fake = FakeClient(
        pending=None,
        progress_script=[{"taskStep": "TS_FAILED", "status": "Error"}],
    )
    result = await run_appliance_upgrade(
        lambda: fake, _image(), execute=True, confirm=lambda staged: True,
        sleeper=_noop_sleep, poll_interval_s=1, reboot_timeout_s=100,
    )
    assert result["status"] == "failed"


@pytest.mark.asyncio
async def test_run_execute_times_out():
    fake = FakeClient(pending=None, progress_script=[])  # always empty -> never completes
    result = await run_appliance_upgrade(
        lambda: fake, _image(), execute=True, confirm=lambda staged: True,
        sleeper=_noop_sleep, poll_interval_s=1, reboot_timeout_s=2,
    )
    assert result["status"] == "timeout"


# ── version-gated completion + no false-complete (regression) ─────────────────
#
# The original bug: a stale ``100%`` (left over from a prior firmware task) made
# the poller declare the upgrade "complete" the instant install started. The fix
# gates completion on the appliance version endpoint actually flipping to the
# target build, so a would-be-complete progress payload can't short-circuit it.

_OLD_VER = {"softwareVersion": "9.20.00-0500184", "modelNumber": "Synergy Composer2"}
_NEW_VER = {"softwareVersion": "10.00.00-0507518", "modelNumber": "Synergy Composer2"}


@pytest.mark.asyncio
async def test_stale_100_percent_does_not_false_complete():
    """A CGI payload already reading 100%/Completed must NOT be accepted as done
    while the appliance is still on the old build — completion is gated on the
    version endpoint flipping to the target."""
    fake = FakeClient(
        pending=None,
        progress_script=[
            {"percentageCompletion": "100%", "taskStep": "TS_COMPLETED", "status": "Completed"},
            {"percentageCompletion": "100%", "taskStep": "TS_COMPLETED", "status": "Completed"},
            {"percentageCompletion": "100%", "taskStep": "TS_COMPLETED", "status": "Completed"},
        ],
        version_script=[_OLD_VER, _OLD_VER, _NEW_VER],
    )
    result = await run_appliance_upgrade(
        lambda: fake, _image(), execute=True, confirm=lambda staged: True,
        sleeper=_noop_sleep, poll_interval_s=1, reboot_timeout_s=100,
    )
    assert result["status"] == "completed"
    # Proves it waited for the real version flip instead of trusting the stale 100%.
    assert result["version"]["software_version"] == "10.00.00-0507518"
    version_reads = [c for c in fake.calls if c == ("get", NODE_VERSION_URI)]
    assert len(version_reads) >= 3


@pytest.mark.asyncio
async def test_completion_gated_on_version_not_progress():
    """Progress never reports 'complete' (stuck mid node-swap), but the appliance
    comes back on the target version — the run still completes, proving success is
    version-gated rather than percentage-gated."""
    fake = FakeClient(
        pending=None,
        progress_script=[
            {"percentageCompletion": "55%", "step": "Prepare for active/standby node swap",
             "status": "Updating"},
            {"percentageCompletion": "60%", "step": "Swap active/standby nodes",
             "status": "Updating"},
        ],
        version_script=[_OLD_VER, _OLD_VER, _NEW_VER],
    )
    result = await run_appliance_upgrade(
        lambda: fake, _image(), execute=True, confirm=lambda staged: True,
        sleeper=_noop_sleep, poll_interval_s=1, reboot_timeout_s=100,
    )
    assert result["status"] == "completed"
    assert result["version"]["software_version"] == "10.00.00-0507518"
    assert "put" in fake.methods()  # install was actually started


@pytest.mark.asyncio
async def test_upload_progress_callback_emits_events():
    """The byte-level upload callback surfaces as 'upload-progress' events so the
    CLI can render a progress bar for the multi-GB image upload."""
    fake = FakeClient(pending=None)
    events: list[tuple[str, dict]] = []
    result = await run_appliance_upgrade(
        lambda: fake, _image(), execute=False,
        on_event=lambda kind, data: events.append((kind, data)),
    )
    assert result["status"] == "staged"
    kinds = [k for k, _ in events]
    assert "uploading" in kinds
    assert "staged" in kinds
    prog_events = [d for k, d in events if k == "upload-progress"]
    assert prog_events, "expected at least one upload-progress event"
    assert prog_events[-1]["total"] == 1024
    assert prog_events[-1]["completed"] == 1024
