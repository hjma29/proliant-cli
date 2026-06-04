"""Tests for pcli.common.runner — run_sync and run_parallel."""
import asyncio
import pytest

from pcli.common.runner import run_sync, run_parallel


class TestRunSync:
    def test_runs_coroutine_and_returns_value(self):
        async def _coro():
            return 42

        result = run_sync(_coro())
        assert result == 42

    def test_propagates_exception(self):
        async def _fail():
            raise ValueError("boom")

        with pytest.raises(ValueError, match="boom"):
            run_sync(_fail())

    def test_runs_async_sleep(self):
        async def _sleepy():
            await asyncio.sleep(0)
            return "done"

        assert run_sync(_sleepy()) == "done"


class TestRunParallel:
    def test_returns_results_for_all_hosts(self):
        hosts = ["host1", "host2", "host3"]

        async def _fetch(host):
            return {"host": host, "value": len(host)}

        results = run_parallel(hosts, _fetch, max_workers=3)
        assert len(results) == 3
        host_names = {r["host"] for r in results}
        assert host_names == set(hosts)

    def test_empty_hosts_returns_empty(self):
        async def _fetch(host):
            return host

        results = run_parallel([], _fetch)
        assert results == []

    def test_failed_hosts_excluded_from_results(self):
        """run_parallel should not crash if a single host fails."""
        hosts = ["good1", "bad", "good2"]

        async def _fetch(host):
            if host == "bad":
                raise RuntimeError("unreachable")
            return {"host": host}

        # run_parallel should return only successful results
        results = run_parallel(hosts, _fetch, max_workers=3)
        result_hosts = {r["host"] for r in results}
        assert "good1" in result_hosts
        assert "good2" in result_hosts

    def test_single_host(self):
        async def _fetch(host):
            return host.upper()

        results = run_parallel(["myhost"], _fetch)
        assert results == ["MYHOST"]
