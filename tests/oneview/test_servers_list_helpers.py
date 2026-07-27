"""Tests for `proliant oneview servers list` helper functions: sorting
servers by enclosure/bay number and compacting firmware version dates."""

from __future__ import annotations

from proliant.oneview.cli import _format_fw_version, _server_sort_key


def test_server_sort_key_orders_by_enclosure_then_bay_number():
    names = [
        "Enclosure-01, bay 2",
        "Enclosure-01, bay 10",
        "Enclosure-01, bay 1",
        "Enclosure-02, bay 1",
    ]
    assert sorted(names, key=_server_sort_key) == [
        "Enclosure-01, bay 1",
        "Enclosure-01, bay 2",
        "Enclosure-01, bay 10",
        "Enclosure-02, bay 1",
    ]


def test_server_sort_key_sorts_unmatched_names_last():
    names = ["Some Custom Name", "Enclosure-01, bay 1"]
    assert sorted(names, key=_server_sort_key) == [
        "Enclosure-01, bay 1",
        "Some Custom Name",
    ]


def test_format_fw_version_compacts_verbose_release_date():
    assert _format_fw_version("3.17 Dec 02 2025") == "3.17 (2025.12.02)"


def test_format_fw_version_returns_original_when_unparseable():
    assert _format_fw_version("3.17") == "3.17"
    assert _format_fw_version("") == ""
