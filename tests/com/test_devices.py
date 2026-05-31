"""Tests for hpecom.devices — device listing."""
import pytest
import respx
import httpx

from pcli.com.devices import fetch_devices, Device, DEVICES_URI

FAKE_DEVICES_RESPONSE = {
    "count": 2,
    "devices": [
        {
            "resource_id": "aaa-111",
            "serial_number": "TWA25325G1206",
            "part_number": "P81967-B21",
            "device_type": "COMPUTE",
            "name": "dl325-gen12",
            "device_model": "HPE ProLiant DL325 Gen12",
            "subscription_tier": "CONNECTED",
            "subscription_key": "SVC-12345",
            "tags": [{"name": "env", "value": "lab"}],
        },
        {
            "resource_id": "bbb-222",
            "serial_number": "TWA25345G1208",
            "part_number": "P81949-B21",
            "device_type": "COMPUTE",
            "name": "dl345-gen12",
            "device_model": "HPE ProLiant DL345 Gen12",
            "subscription_tier": "MANAGED",
            "subscription_key": None,
            "tags": [],
        },
    ],
}

FAKE_DEVICES_PAGE1 = {
    "count": 3,
    "devices": [FAKE_DEVICES_RESPONSE["devices"][0]],
    "nextPageUri": "/ui-doorway/ui/v1/devices?offset=1",
}
FAKE_DEVICES_PAGE2 = {
    "count": 3,
    "devices": [FAKE_DEVICES_RESPONSE["devices"][1]],
}


class TestDeviceModel:
    def test_from_api_basic(self):
        raw = FAKE_DEVICES_RESPONSE["devices"][0]
        d = Device.from_api(raw)
        assert d.serial_number == "TWA25325G1206"
        assert d.product_id == "P81967-B21"
        assert d.device_type == "COMPUTE"
        assert d.tags == {"env": "lab"}

    def test_from_api_no_subscription(self):
        raw = FAKE_DEVICES_RESPONSE["devices"][1]
        d = Device.from_api(raw)
        assert d.subscription_key is None


class TestFetchDevices:
    @pytest.mark.asyncio
    async def test_fetch_all_devices(self, session):
        devices_url = session.gl_url("/devices")

        with respx.mock(assert_all_called=False) as mock:
            mock.get(devices_url).mock(
                return_value=httpx.Response(200, json=FAKE_DEVICES_RESPONSE)
            )
            result = await fetch_devices(session)

        assert len(result) == 2
        assert result[0].serial_number == "TWA25325G1206"
        assert result[1].display_name == "dl345-gen12"

    @pytest.mark.asyncio
    async def test_fetch_devices_pagination(self, session):
        """fetch_devices follows nextPageUri across multiple pages."""
        devices_url = session.gl_url("/devices")
        page2_url = devices_url + "?offset=1"

        resp1 = httpx.Response(200, json={
            "count": 2,
            "devices": [FAKE_DEVICES_RESPONSE["devices"][0]],
            "nextPageUri": page2_url,
        })
        resp2 = httpx.Response(200, json={
            "count": 2,
            "devices": [FAKE_DEVICES_RESPONSE["devices"][1]],
        })

        with respx.mock(assert_all_called=False, assert_all_mocked=False) as mock:
            mock.get(devices_url).mock(side_effect=[resp1, resp2])
            result = await fetch_devices(session)

        assert len(result) == 2
        assert result[0].serial_number == "TWA25325G1206"
        assert result[1].serial_number == "TWA25345G1208"

    @pytest.mark.asyncio
    async def test_fetch_devices_type_filter(self, session):
        """Device type filter is passed as query param."""
        devices_url = session.gl_url("/devices")

        with respx.mock(assert_all_called=False) as mock:
            route = mock.get(devices_url).mock(
                return_value=httpx.Response(200, json=FAKE_DEVICES_RESPONSE)
            )
            await fetch_devices(session, device_type="COMPUTE")

        # Verify deviceType param was sent
        assert "deviceType=COMPUTE" in str(route.calls[0].request.url)

    @pytest.mark.asyncio
    async def test_fetch_devices_empty(self, session):
        devices_url = session.gl_url("/devices")

        with respx.mock(assert_all_called=False) as mock:
            mock.get(devices_url).mock(
                return_value=httpx.Response(200, json={"count": 0, "items": []})
            )
            result = await fetch_devices(session)

        assert result == []


# GLP global API (camelCase) format — used when Okta session expired
FAKE_GLP_DEVICES_RESPONSE = {
    "total": 1,
    "items": [
        {
            "id": "aaa-111",
            "serialNumber": "TWA25325G1206",
            "partNumber": "P81967-B21",
            "deviceType": "COMPUTE",
            "deviceName": "dl325-gen12",
            "model": "HPE ProLiant DL325 Gen12",
            "subscription": [{"key": "SVC-12345"}],
            "tags": None,
        }
    ],
}


class TestFetchDevicesGLPFallback:
    """Tests for GLP global API fallback (client-credentials mode)."""

    @pytest.mark.asyncio
    async def test_glp_fallback_used_for_client_creds(self, client_creds_session):
        """Client-credentials session uses GLP global API, not ui-doorway."""
        with respx.mock(assert_all_called=False) as mock:
            mock.get(DEVICES_URI).mock(
                return_value=httpx.Response(200, json=FAKE_GLP_DEVICES_RESPONSE)
            )
            result = await fetch_devices(client_creds_session)

        assert len(result) == 1
        assert result[0].serial_number == "TWA25325G1206"
        assert result[0].subscription_key == "SVC-12345"
        assert result[0].display_name == "dl325-gen12"

    @pytest.mark.asyncio
    async def test_glp_from_api_camel_case(self):
        """Device.from_api() handles GLP global API camelCase fields."""
        raw = FAKE_GLP_DEVICES_RESPONSE["items"][0]
        d = Device.from_api(raw)
        assert d.serial_number == "TWA25325G1206"
        assert d.product_id == "P81967-B21"
        assert d.device_type == "COMPUTE"
        assert d.display_name == "dl325-gen12"
        assert d.subscription_key == "SVC-12345"
        assert d.tags == {}
