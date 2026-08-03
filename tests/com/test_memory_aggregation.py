"""Tests for aggregate_by_part_number() memory-report grouping (used by
ilo/com/oneview 'reports memory' commands)."""

from __future__ import annotations

from proliant.com.inventory import aggregate_by_part_number


def _dimm(server, hpe_pn, vendor_pn, vendor="SK Hynix", capacity_gb=16, type_="RDIMM", speed_mts=2933):
    return {
        "server": server,
        "hpe_pn": hpe_pn,
        "vendor_pn": vendor_pn,
        "vendor": vendor,
        "capacity_gb": capacity_gb,
        "type": type_,
        "speed_mts": speed_mts,
    }


def test_aggregate_groups_matching_hpe_and_vendor_part_numbers_together():
    dimms = [
        _dimm("host1", "P03051-091", "HMA82GR7CJR4N-WM"),
        _dimm("host2", "P03051-091", "HMA82GR7CJR4N-WM"),
    ]
    rows = aggregate_by_part_number(dimms)
    assert len(rows) == 1
    assert rows[0]["count"] == 2
    assert rows[0]["vendor_pn"] == "HMA82GR7CJR4N-WM"
    assert rows[0]["servers"] == {"host1", "host2"}


def test_aggregate_keeps_different_vendor_part_numbers_separate_when_hpe_pn_unknown():
    # Two physically different DIMMs that both lack an HPE-branded part
    # number (e.g. broken inventory collection) must not be merged into one
    # row just because they share the literal "Unknown" HPE part number.
    dimms = [
        _dimm("host1", "Unknown", "HMA81GR7AFR8N-VK", capacity_gb=8, speed_mts=2666),
        _dimm("host2", "Unknown", "HMA82GR7CJR4N-WM", capacity_gb=16, speed_mts=2933),
    ]
    rows = aggregate_by_part_number(dimms)
    assert len(rows) == 2
    by_vendor_pn = {r["vendor_pn"]: r for r in rows}
    assert by_vendor_pn["HMA81GR7AFR8N-VK"]["capacity_gb"] == 8
    assert by_vendor_pn["HMA82GR7CJR4N-WM"]["capacity_gb"] == 16


def test_aggregate_handles_missing_vendor_pn_gracefully():
    dimms = [{
        "server": "host1",
        "hpe_pn": "P03051-091",
        "vendor": "SK Hynix",
        "capacity_gb": 16,
        "type": "RDIMM",
        "speed_mts": 2933,
    }]
    rows = aggregate_by_part_number(dimms)
    assert rows[0]["vendor_pn"] == ""
