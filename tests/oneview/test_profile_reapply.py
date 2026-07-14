"""Tests for proliant.oneview.profile_reapply — server-profile reapply engine."""

from __future__ import annotations

import pytest

from proliant.oneview.profile_reapply import run_profile_reapply


PROFILE = {"name": "aci-vc-LAG-host1", "uri": "/rest/server-profiles/p-1",
           "status": "Critical", "state": "Normal"}


class FakeClient:
    """Records GET/PUT calls and replays a single task's poll sequence."""

    def __init__(self, profiles=None, task_states=None):
        self.profiles = profiles if profiles is not None else [PROFILE]
        # Each entry is one GET response for the task URI, consumed in order;
        # the last entry repeats once exhausted.
        self.task_states = task_states if task_states is not None else [
            {"uri": "/rest/tasks/t-1", "taskState": "Completed", "percentComplete": 100},
        ]
        self._task_calls = 0
        self.put_calls: list[tuple[str, dict]] = []
        self.get_calls: list[str] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get_all(self, uri):
        assert uri == "/rest/server-profiles"
        return self.profiles

    async def get(self, uri):
        self.get_calls.append(uri)
        if uri == PROFILE["uri"]:
            return {**PROFILE, "firmware": {}, "connections": []}
        if uri.startswith("/rest/tasks/"):
            idx = min(self._task_calls, len(self.task_states) - 1)
            self._task_calls += 1
            return self.task_states[idx]
        return {}

    async def put(self, uri, body):
        self.put_calls.append((uri, body))
        return {"uri": "/rest/tasks/t-1", "taskState": "Running", "percentComplete": 0}


def _factory(client):
    return lambda: client


async def _instant_sleep(_seconds):
    return None


@pytest.mark.asyncio
async def test_reapply_not_found_lists_known_profiles():
    client = FakeClient(profiles=[{"name": "other-profile"}])

    result = await run_profile_reapply(_factory(client), name="missing")

    assert result["status"] == "not-found"
    assert result["known"] == "other-profile"


@pytest.mark.asyncio
async def test_reapply_matches_name_case_insensitively():
    client = FakeClient()

    result = await run_profile_reapply(_factory(client), name="ACI-VC-LAG-HOST1")

    assert result["status"] == "applied"
    assert client.put_calls[0][0] == PROFILE["uri"]


@pytest.mark.asyncio
async def test_reapply_puts_the_full_profile_body_back_unmodified():
    """The GUI's 'Reapply configuration' changes nothing about the profile
    itself -- the PUT body must be exactly what GET returned."""
    client = FakeClient()

    await run_profile_reapply(_factory(client), name=PROFILE["name"])

    uri, body = client.put_calls[0]
    assert uri == PROFILE["uri"]
    assert body == {**PROFILE, "firmware": {}, "connections": []}


@pytest.mark.asyncio
async def test_reapply_aborted_when_confirm_returns_false():
    client = FakeClient()

    result = await run_profile_reapply(
        _factory(client), name=PROFILE["name"], confirm=lambda info: False,
    )

    assert result["status"] == "aborted"
    assert client.put_calls == []  # never actually applied


@pytest.mark.asyncio
async def test_reapply_confirm_receives_matched_profile_info():
    client = FakeClient()
    seen = {}

    def confirm(info):
        seen.update(info)
        return True

    await run_profile_reapply(_factory(client), name=PROFILE["name"], confirm=confirm)

    assert seen["name"] == PROFILE["name"]
    assert seen["uri"] == PROFILE["uri"]


@pytest.mark.asyncio
async def test_reapply_applied_on_task_completion():
    client = FakeClient()

    result = await run_profile_reapply(_factory(client), name=PROFILE["name"])

    assert result["status"] == "applied"
    assert result["results"][0]["kind"] == "server-profile"
    assert result["results"][0]["name"] == PROFILE["name"]


@pytest.mark.asyncio
async def test_reapply_failed_when_task_reports_error():
    client = FakeClient(task_states=[
        {"uri": "/rest/tasks/t-1", "taskState": "Error", "percentComplete": 100},
    ])

    result = await run_profile_reapply(_factory(client), name=PROFILE["name"])

    assert result["status"] == "failed"


@pytest.mark.asyncio
async def test_reapply_timeout_when_task_never_completes():
    client = FakeClient(task_states=[
        {"uri": "/rest/tasks/t-1", "taskState": "Running", "percentComplete": 50},
    ])

    result = await run_profile_reapply(
        _factory(client), name=PROFILE["name"],
        sleeper=_instant_sleep, poll_interval_s=1, task_timeout_s=0,
    )

    assert result["status"] == "timeout"


@pytest.mark.asyncio
async def test_reapply_emits_applying_task_progress_and_applied_events():
    client = FakeClient()
    events: list[tuple[str, dict]] = []

    await run_profile_reapply(
        _factory(client), name=PROFILE["name"],
        on_event=lambda kind, payload: events.append((kind, payload)),
    )

    kinds = [k for k, _ in events]
    assert "plan" in kinds
    assert "applying" in kinds
    assert "task-progress" in kinds
    assert "applied" in kinds
