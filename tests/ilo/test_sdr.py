"""Tests for SDR firmware package parsing and upgrade matching."""

from __future__ import annotations

from proliant.ilo import sdr


def test_parse_fwpkg_preserves_bcm_nic_pattern(monkeypatch):
    monkeypatch.setattr(sdr, "_fetch_software_ids", lambda _url: ["swid-1"])

    comp = sdr._parse_fwpkg(
        "BCM235.1.164.14_BCM957414A4142HC.fwpkg",
        "https://example.invalid/BCM235.1.164.14_BCM957414A4142HC.fwpkg",
    )

    assert comp is not None
    assert comp.prefix == "BCM957414A4142HC"
    assert comp.chip_model == "BCM957414A4142HC"
    assert comp.version_str == "235.1.164.14"
    assert comp.version == (235, 1, 164, 14)
    assert comp.software_ids == ["swid-1"]


def test_parse_fwpkg_handles_hpe_storage_packages(monkeypatch):
    monkeypatch.setattr(sdr, "_fetch_software_ids", lambda _url: ["target-guid"])

    comp = sdr._parse_fwpkg(
        "HPE_MR416i-p_Gen11_52.36.3-6584_A.fwpkg",
        "https://example.invalid/HPE_MR416i-p_Gen11_52.36.3-6584_A.fwpkg",
    )

    assert comp is not None
    assert comp.prefix == "HPE_MR416i-p"
    assert comp.version_str == "52.36.3-6584"
    assert comp.version == (52, 36, 3)
    assert comp.software_ids == ["target-guid"]


def test_find_upgrades_matches_storage_controller_without_false_positive():
    pack_components = [
        sdr.FwComponent(
            filename="HPE_MR416i-p_Gen11_52.36.3-6584_A.fwpkg",
            url="https://example.invalid/HPE_MR416i-p_Gen11_52.36.3-6584_A.fwpkg",
            prefix="HPE_MR416i-p",
            version_str="52.36.3-6584",
            version=(52, 36, 3),
        )
    ]

    candidates = sdr.find_upgrades(
        [{"Name": "HPE MR416i-p Gen11 Controller", "Version": "52.36.3-6584"}],
        pack_components,
    )

    assert len(candidates) == 1
    assert candidates[0].sdr is not None
    assert candidates[0].sdr.prefix == "HPE_MR416i-p"
    assert candidates[0].needs_update is False


def _ilo7(version, min_active=()):
    return sdr.FwComponent(
        filename=f"ilo7_{version}.fwpkg",
        url=f"https://example.invalid/ilo7_{version}.fwpkg",
        prefix="ilo7",
        version_str=version,
        version=tuple(int(n) for n in version.split(".")),
        min_active_version=min_active,
    )


def test_find_upgrades_ilo_picks_newest_package_the_installed_ilo_can_flash():
    pack = [_ilo7("1.24.00"), _ilo7("1.25.02", min_active=(1, 24, 0))]

    old = sdr.find_upgrades([{"Name": "iLO 7", "Version": "1.21.00 Apr 07 2026"}], pack)
    assert old[0].sdr.version_str == "1.24.00"

    current = sdr.find_upgrades([{"Name": "iLO 7", "Version": "1.24.00 Aug 01 2026"}], pack)
    assert current[0].sdr.version_str == "1.25.02"
    assert current[0].needs_update is True


def test_parse_fwpkg_reads_ilo_minimum_active_version(monkeypatch):
    monkeypatch.setattr(sdr, "_get", lambda _url: '{"MinimumActiveVersion": "1.24.00"}')

    comp = sdr._parse_fwpkg("ilo7_1.25.02.fwpkg", "https://example.invalid/ilo7_1.25.02.fwpkg")

    assert comp.version == (1, 25, 2)
    assert comp.min_active_version == (1, 24, 0)
