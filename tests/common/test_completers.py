"""Tests for proliant.common.completers.cached_names — the disk-cache helper
that keeps tab completion fast for completers backed by live network calls
(OneView/iLO/COM logins, SPP SDR fetches)."""
from __future__ import annotations

import json

import pytest

from proliant.common import completers as completers_module
from proliant.common.completers import cached_names


@pytest.fixture(autouse=True)
def _isolated_cache_dir(tmp_path, monkeypatch):
    """Point the completion cache at a throwaway directory for every test."""
    monkeypatch.setattr(completers_module, "cache_dir", lambda: tmp_path)
    return tmp_path


def test_cached_names_calls_fetch_fn_on_first_use():
    calls = []

    def fetch():
        calls.append(1)
        return ["alpha", "beta"]

    result = cached_names("key1", fetch)

    assert result == ["alpha", "beta"]
    assert len(calls) == 1


def test_cached_names_reuses_cache_within_ttl():
    calls = []

    def fetch():
        calls.append(1)
        return ["alpha", "beta"]

    cached_names("key2", fetch, ttl=20.0)
    result = cached_names("key2", fetch, ttl=20.0)

    assert result == ["alpha", "beta"]
    assert len(calls) == 1  # second call served from cache, no re-fetch


def test_cached_names_refetches_after_ttl_expires():
    calls = []

    def fetch():
        calls.append(1)
        return [f"name-{len(calls)}"]

    cached_names("key3", fetch, ttl=0.0)
    result = cached_names("key3", fetch, ttl=0.0)

    assert result == ["name-2"]
    assert len(calls) == 2  # ttl=0 means every call is a miss


def test_cached_names_writes_full_unfiltered_list(tmp_path):
    def fetch():
        return ["server-a", "server-b", "server-c"]

    cached_names("key4", fetch)

    cache_file = tmp_path / "completions" / "key4.json"
    assert cache_file.exists()
    data = json.loads(cache_file.read_text(encoding="utf-8"))
    assert data["names"] == ["server-a", "server-b", "server-c"]
    assert "ts" in data


def test_cached_names_survives_corrupt_cache_file(tmp_path):
    cache_file = tmp_path / "completions" / "key5.json"
    cache_file.parent.mkdir(parents=True, exist_ok=True)
    cache_file.write_text("not valid json{{{", encoding="utf-8")

    result = cached_names("key5", lambda: ["fresh"])

    assert result == ["fresh"]


def test_cached_names_survives_unwritable_cache_dir(monkeypatch, tmp_path):
    unwritable = tmp_path / "readonly-parent" / "cache"

    def _boom_mkdir(*args, **kwargs):
        raise OSError("permission denied")

    monkeypatch.setattr(completers_module, "cache_dir", lambda: unwritable)
    monkeypatch.setattr(
        type(unwritable), "mkdir", _boom_mkdir, raising=False
    )

    result = cached_names("key6", lambda: ["still-works"])

    assert result == ["still-works"]


def test_cached_names_different_keys_are_independent():
    cached_names("key7a", lambda: ["a"])
    cached_names("key7b", lambda: ["b"])

    assert cached_names("key7a", lambda: ["should-not-be-called"]) == ["a"]
    assert cached_names("key7b", lambda: ["should-not-be-called"]) == ["b"]
