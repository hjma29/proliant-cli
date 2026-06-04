"""Tests for pcli.common.http — BaseAsyncClient helpers."""
import pytest
import httpx

from pcli.common.http import BaseAsyncClient


class ConcreteClient(BaseAsyncClient):
    """Minimal concrete subclass for testing protected methods."""
    pass


class TestSafeJson:
    def test_returns_dict_for_valid_json(self):
        client = ConcreteClient.__new__(ConcreteClient)
        response = httpx.Response(200, json={"key": "value"})
        result = client._safe_json(response)
        assert result == {"key": "value"}

    def test_returns_empty_dict_for_empty_body(self):
        client = ConcreteClient.__new__(ConcreteClient)
        response = httpx.Response(204, content=b"")
        result = client._safe_json(response)
        assert result == {}

    def test_returns_raw_key_for_non_json_body(self):
        client = ConcreteClient.__new__(ConcreteClient)
        response = httpx.Response(200, content=b"not json", headers={"content-type": "text/plain"})
        result = client._safe_json(response)
        assert "raw" in result
        assert "not json" in result["raw"]


class TestErrorDetail:
    def test_extracts_message_from_json_body(self):
        client = ConcreteClient.__new__(ConcreteClient)
        response = httpx.Response(400, json={"message": "bad request"})
        detail = client._error_detail(response)
        assert "bad request" in detail

    def test_falls_back_to_text_for_non_json(self):
        client = ConcreteClient.__new__(ConcreteClient)
        response = httpx.Response(500, content=b"internal error", headers={"content-type": "text/plain"})
        detail = client._error_detail(response)
        assert "internal error" in detail

    def test_returns_string(self):
        client = ConcreteClient.__new__(ConcreteClient)
        response = httpx.Response(403, json={"error": "forbidden"})
        assert isinstance(client._error_detail(response), str)


class TestRaiseForStatus:
    def test_does_not_raise_for_2xx(self):
        client = ConcreteClient.__new__(ConcreteClient)
        request = httpx.Request("GET", "http://test")
        response = httpx.Response(200, json={"ok": True}, request=request)
        client._raise_for_status(response)  # should not raise

    def test_raises_runtime_error_for_4xx(self):
        client = ConcreteClient.__new__(ConcreteClient)
        request = httpx.Request("GET", "http://test")
        response = httpx.Response(404, json={"message": "not found"}, request=request)
        with pytest.raises(RuntimeError):
            client._raise_for_status(response)

    def test_raises_runtime_error_for_5xx(self):
        client = ConcreteClient.__new__(ConcreteClient)
        request = httpx.Request("GET", "http://test")
        response = httpx.Response(500, content=b"error", request=request)
        with pytest.raises(RuntimeError):
            client._raise_for_status(response)

    def test_error_message_includes_url(self):
        client = ConcreteClient.__new__(ConcreteClient)
        request = httpx.Request("GET", "http://myserver/redfish/v1")
        response = httpx.Response(401, json={"message": "unauthorized"}, request=request)
        with pytest.raises(RuntimeError, match="myserver"):
            client._raise_for_status(response)
