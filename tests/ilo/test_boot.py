from __future__ import annotations

import pytest

from proliant.ilo.boot import fetch_boot_order, set_one_time_pxe


class FakeILOClient:
    def __init__(self, responses: dict[str, dict]):
        self.responses = responses
        self.patches: list[tuple[str, dict]] = []

    async def get_system_uri(self) -> str:
        return "/redfish/v1/Systems/1"

    async def get(self, uri: str) -> dict:
        return self.responses[uri]

    async def patch(self, uri: str, body: dict) -> dict:
        self.patches.append((uri, body))
        return {}


def _base_responses() -> dict[str, dict]:
    return {
        "/redfish/v1/Systems/1": {
            "Bios": {"@odata.id": "/redfish/v1/systems/1/bios/"},
            "Boot": {
                "BootOptions": {"@odata.id": "/redfish/v1/Systems/1/BootOptions"},
                "BootOrder": ["Boot0016", "Boot000D", "Boot000F", "Boot0011"],
                "BootSourceOverrideEnabled": "Disabled",
                "BootSourceOverrideMode": "UEFI",
                "BootSourceOverrideTarget": "None",
                "BootSourceOverrideTarget@Redfish.AllowableValues": ["None", "Pxe", "UefiTarget"],
                "UefiTargetBootSourceOverride": "None",
                "UefiTargetBootSourceOverride@Redfish.AllowableValues": [
                    "PciRoot(0x2)/Pci(0x1,0x1)/Pci(0x0,0x0)/MAC(BC97E1E296C0,0x1)/IPv4(0.0.0.0)",
                    "PciRoot(0x2)/Pci(0x1,0x1)/Pci(0x0,0x1)/MAC(BC97E1E296C1,0x1)/IPv4(0.0.0.0)",
                ],
            }
        },
        "/redfish/v1/systems/1/bios/": {
            "Oem": {
                "Hpe": {
                    "Links": {
                        "Boot": {"@odata.id": "/redfish/v1/systems/1/bios/oem/hpe/boot/"}
                    }
                }
            }
        },
        "/redfish/v1/systems/1/bios/oem/hpe/boot/": {
            "@Redfish.Settings": {
                "SettingsObject": {
                    "@odata.id": "/redfish/v1/systems/1/bios/oem/hpe/boot/settings/"
                }
            },
            "BootSources": [
                {
                    "BootOptionNumber": "0016",
                    "BootString": "redhat",
                    "CorrelatableID": "HD(1,GPT,aaaa,0x800,0x12C000)/\\EFI\\redhat\\shimx64.efi",
                    "StructuredBootString": "NVMe.DriveBay.1.1",
                    "UEFIDevicePath": "HD(1,GPT,aaaa,0x800,0x12C000)/\\EFI\\redhat\\shimx64.efi",
                },
                {
                    "BootOptionNumber": "000D",
                    "BootString": "Generic USB Boot",
                    "CorrelatableID": "UsbClass(0xFFFF,0xFFFF,0xFF,0xFF,0xFF)",
                    "StructuredBootString": "Generic.USB.1.1",
                    "UEFIDevicePath": "UsbClass(0xFFFF,0xFFFF,0xFF,0xFF,0xFF)",
                },
                {
                    "BootOptionNumber": "000F",
                    "BootString": "Slot 1 Port 1 : Broadcom Adapter (PXE IPv4)",
                    "CorrelatableID": "PciRoot(0x2)/Pci(0x1,0x1)/Pci(0x0,0x0)",
                    "StructuredBootString": "NIC.Slot.1.1.IPv4",
                    "UEFIDevicePath": "PciRoot(0x2)/Pci(0x1,0x1)/Pci(0x0,0x0)/MAC(BC97E1E296C0,0x1)/IPv4(0.0.0.0)",
                },
                {
                    "BootOptionNumber": "0011",
                    "BootString": "Slot 1 Port 2 : Broadcom Adapter (PXE IPv4)",
                    "CorrelatableID": "PciRoot(0x2)/Pci(0x1,0x1)/Pci(0x0,0x1)",
                    "StructuredBootString": "NIC.Slot.1.2.IPv4",
                    "UEFIDevicePath": "PciRoot(0x2)/Pci(0x1,0x1)/Pci(0x0,0x1)/MAC(BC97E1E296C1,0x1)/IPv4(0.0.0.0)",
                },
            ],
            "PersistentBootConfigOrder": [
                "NVMe.DriveBay.1.1",
                "Generic.USB.1.1",
                "NIC.Slot.1.1.IPv4",
                "NIC.Slot.1.2.IPv4",
            ],
        },
        "/redfish/v1/systems/1/bios/oem/hpe/boot/settings/": {
            "@odata.id": "/redfish/v1/systems/1/bios/oem/hpe/boot/settings/",
            "DesiredBootDevices": [
                {"CorrelatableID": "", "Lun": "", "Wwn": "", "iScsiTargetName": ""},
                {"CorrelatableID": "", "Lun": "", "Wwn": "", "iScsiTargetName": ""},
            ],
        },
        "/redfish/v1/Systems/1/BootOptions": {
            "Members": [
                {"@odata.id": "/redfish/v1/Systems/1/BootOptions/1"},
                {"@odata.id": "/redfish/v1/Systems/1/BootOptions/2"},
                {"@odata.id": "/redfish/v1/Systems/1/BootOptions/3"},
                {"@odata.id": "/redfish/v1/Systems/1/BootOptions/4"},
            ]
        },
        "/redfish/v1/Systems/1/BootOptions/1": {
            "DisplayName": "redhat",
            "BootOptionReference": "Boot0016",
            "UefiDevicePath": "HD(1,GPT,aaaa,0x800,0x12C000)/\\EFI\\redhat\\shimx64.efi",
        },
        "/redfish/v1/Systems/1/BootOptions/2": {
            "DisplayName": "Generic USB Boot",
            "BootOptionReference": "Boot000D",
            "UefiDevicePath": "UsbClass(0xFFFF,0xFFFF,0xFF,0xFF,0xFF)",
        },
        "/redfish/v1/Systems/1/BootOptions/3": {
            "DisplayName": "Slot 1 Port 1 : Broadcom Adapter (PXE IPv4)",
            "BootOptionReference": "Boot000F",
            "UefiDevicePath": "PciRoot(0x2)/Pci(0x1,0x1)/Pci(0x0,0x0)/MAC(BC97E1E296C0,0x1)/IPv4(0.0.0.0)",
        },
        "/redfish/v1/Systems/1/BootOptions/4": {
            "DisplayName": "Slot 1 Port 2 : Broadcom Adapter (PXE IPv4)",
            "BootOptionReference": "Boot0011",
            "UefiDevicePath": "PciRoot(0x2)/Pci(0x1,0x1)/Pci(0x0,0x1)/MAC(BC97E1E296C1,0x1)/IPv4(0.0.0.0)",
        },
    }


def _add_ambiguous_http_source(responses: dict[str, dict]) -> dict[str, dict]:
    responses["/redfish/v1/systems/1/bios/oem/hpe/boot/"]["BootSources"].insert(
        2,
        {
            "BootOptionNumber": "000E",
            "BootString": "Slot 1 Port 1 : Broadcom Adapter (HTTP(S) IPv4)",
            "CorrelatableID": "PciRoot(0x2)/Pci(0x1,0x1)/Pci(0x0,0x0)",
            "StructuredBootString": "NIC.Slot.1.1.Httpv4",
            "UEFIDevicePath": "PciRoot(0x2)/Pci(0x1,0x1)/Pci(0x0,0x0)/MAC(BC97E1E296C0,0x1)/IPv4(0.0.0.0)/Uri()",
        },
    )
    return responses


@pytest.mark.asyncio
async def test_fetch_boot_order_classifies_entries_and_pxe_targets():
    client = FakeILOClient(_base_responses())

    result = await fetch_boot_order(client)

    assert result["mode"] == "UEFI"
    assert result["override_enabled"] == "Disabled"
    assert [entry["reference"] for entry in result["order"]] == ["Boot0016", "Boot000D", "Boot000F", "Boot0011"]
    assert [entry["kind"] for entry in result["order"]] == ["UEFI OS", "USB", "PXE IPv4", "PXE IPv4"]
    assert result["pxe_ipv4"][0]["mac"] == "bc97e1e296c0"
    assert result["pxe_ipv4"][0]["port_hint"] == "Slot 1 Port 1"
    assert result["pxe_ipv4"][0]["correlatable_id"] == "PciRoot(0x2)/Pci(0x1,0x1)/Pci(0x0,0x0)"


@pytest.mark.asyncio
async def test_set_one_time_pxe_without_port_uses_uefi_target_override():
    client = FakeILOClient(_base_responses())

    result = await set_one_time_pxe(client)

    assert result["status"] == "accepted"
    assert result["selected"]["reference"] == "Boot000F"
    assert client.patches == [
        (
            "/redfish/v1/Systems/1",
            {
                "Boot": {
                    "BootSourceOverrideEnabled": "Once",
                    "BootSourceOverrideTarget": "UefiTarget",
                    "UefiTargetBootSourceOverride": "PciRoot(0x2)/Pci(0x1,0x1)/Pci(0x0,0x0)/MAC(BC97E1E296C0,0x1)/IPv4(0.0.0.0)",
                }
            },
        )
    ]


@pytest.mark.asyncio
async def test_set_one_time_pxe_specific_port_uses_matching_ipv4_target():
    client = FakeILOClient(_base_responses())

    result = await set_one_time_pxe(client, port="BC97:E1:E2:96:C1", dry_run=True)

    assert result["status"] == "dry-run"
    assert result["selected"]["reference"] == "Boot0011"
    assert result["payload"] == {
        "Boot": {
            "BootSourceOverrideEnabled": "Once",
            "BootSourceOverrideTarget": "UefiTarget",
            "UefiTargetBootSourceOverride": "PciRoot(0x2)/Pci(0x1,0x1)/Pci(0x0,0x1)/MAC(BC97E1E296C1,0x1)/IPv4(0.0.0.0)",
        }
    }
    assert client.patches == []


@pytest.mark.asyncio
async def test_set_one_time_pxe_specific_port_requires_match():
    client = FakeILOClient(_base_responses())

    with pytest.raises(RuntimeError, match="No PXE IPv4 boot option matched"):
        await set_one_time_pxe(client, port="does-not-exist")


@pytest.mark.asyncio
async def test_set_one_time_pxe_shared_correlatable_id_still_uses_exact_uefi_target():
    client = FakeILOClient(_add_ambiguous_http_source(_base_responses()))

    result = await set_one_time_pxe(client, port="Slot 1 Port 1", dry_run=True)

    assert result["selected"]["reference"] == "Boot000F"
    assert result["payload"]["Boot"]["UefiTargetBootSourceOverride"].endswith("/IPv4(0.0.0.0)")
    assert "/Uri()" not in result["payload"]["Boot"]["UefiTargetBootSourceOverride"]
    assert client.patches == []
