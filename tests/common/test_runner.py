"""Tests for pcli.common.runner — run_sync and run_parallel."""
import asyncio
import contextlib
import pytest

from pcli.common.runner import run_sync, run_parallel


class TestRunSync:
    def test_runs_coroutine_and_returns_value(self):
        async def _coro():
            return 42
        assert run_sync(_coro()) == 42

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
    def _make_session_factory(self, fail_hosts=None):
        """Create a mock session_factory that returns a dummy client."""
        fail_set = set(fail_hosts or [])

        @contextlib.asynccontextmanager
        async def _factory(host):
            if host["name"] in fail_set:
                raise ConnectionError(f"Cannot reach {host['name']}")
            yield host  # client = host dict itself

        return _factory

    def test_returns_tuples_for_all_hosts(self):
        hosts = [{"name": "h1"}, {"name": "h2"}, {"name": "h3"}]
        factory = self._make_session_factory()

        async def _fetch(client):
            return [client["name"]]

        results = run_sync(run_parallel(hosts, _fetch, session_factory=factory))
        assert len(results) == 3

    def test_empty_hosts_returns_empty(self):
        factory = self._make_session_factory()

        async def _fetch(client):
            return []

        results = run_sync(run_parallel([], _fetch, session_factory=factory))
        assert results == []

    def test_failed_host_returns_error_tuple(self):
        hosts = [{"name": "good"}, {"name": "bad"}]
        factory = self._make_session_factory(fail_hosts=["bad"])

        async def _fetch(client):
            return [client["name"]]

        results = run_sync(run_parallel(hosts, _fetch, session_factory=factory))
        # All hosts should appear in results — failed ones have error set
        host_names = {r[0] for r in results}
        assert "good" in host_names
        assert "bad" in host_names
        bad_result = next(r for r in results if r[0] == "bad")
        assert bad_result[1] is not None  # error message present
        assert bad_result[2] == []        # empty results

    def test_successful_host_has_no_error(self):
        hosts = [{"name": "srv1"}]
        factory = self._make_session_factory()

        async def _fetch(client):
            return ["item"]

        results = run_sync(run_parallel(hosts, _fetch, session_factory=factory))
        assert results[0][1] is None  # no error
        assert results[0][2] == ["item"]
