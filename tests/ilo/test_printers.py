from __future__ import annotations

from proliant.ilo.printers import print_fleet_table


def test_print_fleet_table_sanitizes_control_characters(capsys) -> None:
    results = [
        (
            "dl345-gen12",
            None,
            [
                ("Model", "HPE ProLiant Compute DL345 Gen12"),
                ("iLO", "iLO 7 1.21.00 Apr 07 2026"),
                ("BIOS", "A66 v1.40 (01/09/2026)\r"),
                ("NIC-FW", "235.1.164.14"),
                ("Storage-FW", "52.22.3-4650"),
            ],
        )
    ]

    print_fleet_table(results)

    out = capsys.readouterr().out
    assert "\r" not in out
    assert "dl345-gen12" in out
    assert "235.1.164.14" in out
    assert "52.22.3-4650" in out
