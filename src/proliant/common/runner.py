"""
proliant.common.runner
~~~~~~~~~~~~~~~~~~
Shared async parallel execution helpers.

Provides:
  - run_parallel(): query multiple hosts concurrently with a semaphore
  - run_sync(): execute an async coroutine from synchronous code
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

import httpx

T = TypeVar("T")

# Default concurrency limit for parallel host queries
DEFAULT_MAX_WORKERS = 10


async def run_parallel(
    hosts: list[dict],
    fetch_fn: Callable[[Any], Awaitable[list[Any]]],
    *,
    session_factory: Callable[[dict], Any] | None = None,
    max_workers: int = DEFAULT_MAX_WORKERS,
) -> list[tuple[str, str | None, list[Any]]]:
    """Run fetch_fn against multiple hosts concurrently.

    Args:
        hosts: List of host dicts (must have "name" key)
        fetch_fn: Async function that takes a client and returns a list of results
        session_factory: Async context manager factory: host_dict → client.
                        If None, uses proliant.ilo.client.ilo_session.
        max_workers: Max concurrent connections

    Returns:
        List of (host_name, error_or_None, results) tuples
    """
    if session_factory is None:
        from proliant.ilo.client import ilo_session
        session_factory = ilo_session

    semaphore = asyncio.Semaphore(max_workers)

    async def _query_one(host: dict) -> tuple[str, str | None, list[Any]]:
        async with semaphore:
            try:
                async with session_factory(host) as client:
                    results = await fetch_fn(client)
                return host["name"], None, results
            except httpx.ConnectError as exc:
                return host["name"], f"Unreachable: {exc}", []
            except httpx.TimeoutException as exc:
                return host["name"], f"Timeout: {exc}", []
            except Exception as exc:  # noqa: BLE001
                return host["name"], f"Error: {exc}", []

    return list(await asyncio.gather(*(_query_one(h) for h in hosts)))


def run_sync(coro) -> Any:
    """Run an async coroutine from synchronous code (CLI entry points)."""
    return asyncio.run(coro)
