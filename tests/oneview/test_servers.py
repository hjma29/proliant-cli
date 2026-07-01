"""Tests for OneView server hardware normalization."""

from __future__ import annotations

from proliant.oneview.servers import parse_server


def test_parse_server_uses_mp_host_info_ip_and_compacts_synergy_model():
    result = parse_server({
        "name": "Enclosure-01, bay 1",
        "model": "Synergy 480 Gen10",
        "serialNumber": "MXQ1240F2Q",
        "mpModel": "iLO5",
        "mpFirmwareVersion": "2.81 Mar 07 2023",
        "mpHostInfo": {
            "mpIpAddresses": [
                {"address": "fe80::1602:ecff:fe44:bd50", "type": "LinkLocal"},
                {"address": "10.16.41.9", "type": "DHCP"},
            ],
        },
        "powerState": "On",
        "state": "ProfileApplied",
        "serverProfileUri": "/rest/server-profiles/profile1",
        "uri": "/rest/server-hardware/server1",
        "position": 1,
    })

    assert result["model"] == "480 Gen10"
    assert result["ilo_ip"] == "10.16.41.9"
