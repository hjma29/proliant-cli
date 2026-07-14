"""
proliant.oneview.profile_reapply
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
Reapply a server profile's already-stored configuration to its assigned
server hardware -- the CLI equivalent of the OneView GUI's server-profile
"Reapply configuration" action. No profile field is changed: the current
profile body is fetched and PUT straight back unmodified, which makes
OneView re-run its apply-profile state machine and reconcile whatever is
actually out of sync on the live hardware (network/storage settings, BIOS,
boot order, firmware consistency, etc.). This is what clears alerts such as
"Server hardware has been inserted into the enclosure bay -- Resolution:
Reapply the server profile."

Reachable via ``proliant oneview server-profiles reapply <NAME>``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Callable

from proliant.oneview.ssp_update import is_task_failed, normalize_task, poll_task

if TYPE_CHECKING:
    from proliant.oneview.client import OneViewClient


async def run_profile_reapply(
    client_factory: Callable[[], Any],
    *,
    name: str,
    confirm: Callable[[dict], bool] | None = None,
    on_event: Callable[[str, dict], None] | None = None,
    sleeper: Callable[[float], Any] | None = None,
    poll_interval_s: float = 5.0,
    task_timeout_s: float = 90 * 60,
) -> dict[str, Any]:
    """Look up *name*, optionally confirm, then reapply it.

    Returns a result dict whose ``status`` is one of:

      ``not-found``  no server profile matches *name* -- ``known`` lists what does
      ``aborted``    ``confirm`` returned False -- nothing was modified
      ``applied``    the reapply task completed
      ``failed``     the reapply task reported failure
      ``timeout``    the reapply task didn't reach a terminal state in time
    """
    import asyncio

    sleeper = sleeper or asyncio.sleep
    emit = on_event or (lambda kind, data: None)

    async with client_factory() as client:
        profiles = await client.get_all("/rest/server-profiles")
        matched = [p for p in profiles if (p.get("name", "") or "").lower() == name.lower()]
        if not matched:
            known = ", ".join(p.get("name", "") for p in profiles)
            return {"status": "not-found", "query": name, "known": known}
        profile = matched[0]
        info = {
            "name": profile.get("name", ""),
            "uri": profile.get("uri", ""),
            "status": profile.get("status", ""),
            "state": profile.get("state", ""),
        }
        emit("plan", info)

        if confirm is not None and not confirm(info):
            return {"status": "aborted", "profile": info}

        emit("applying", {"kind": "server-profile", "name": info["name"]})
        full = await client.get(info["uri"])
        resp = await client.put(info["uri"], full)
        task = normalize_task(resp)
        if "/rest/tasks/" in task["uri"]:
            # Surface an immediate first tick so the bar shows the task the
            # moment it is accepted, rather than waiting a full poll interval.
            emit("task-progress", task)
            task = await poll_task(
                client, task["uri"], emit=emit, sleeper=sleeper,
                interval_s=poll_interval_s, timeout_s=task_timeout_s,
            )
        else:
            # Synchronous response (no monitoring task) -- already final.
            task = {**task, "state": task["state"] or "Completed"}

        entry = {**task, "kind": "server-profile", "name": info["name"]}
        emit("applied", entry)

        if task.get("timed_out"):
            status = "timeout"
        elif is_task_failed(task):
            status = "failed"
        else:
            status = "applied"
        return {"status": status, "profile": info, "results": [entry]}
