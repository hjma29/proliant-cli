"""Tests for the stable, agent-friendly iLO inventory API (proliant.ilo.api)
and the CLI surfaces built on it (inventory, --json describe/reports/bios,
power describe, uid --dry-run)."""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, patch

import pytest

from proliant.common.display import OutputMode, set_output_mode
from proliant.ilo import api

HOST = {"name": "dl325-gen12", "url": "https://10.0.0.1", "username": "u", "password": "p"}
SYSTEM = {
    "Model": "HPE ProLiant Compute DL325 Gen12",
    "SerialNumber": "SN1",
    "SKU": "P1-B21",
    "UUID": "uuid-1",
    "HostName": "",
    "BiosVersion": "A60",
    "PowerState": "On",
    "Status": {"Health": "OK"},
    "LocationIndicatorActive": False,
    "Oem": {"Hpe": {"PostState": "FinishedPost", "PowerAutoOn": "Restore"}},
}
MANAGER = {"Model": "iLO 7", "FirmwareVersion": "1.21.00", "HostName": "ilo-1", "Status": {"Health": "OK"}}
BIOS = {"Attributes": {"WorkloadProfile": "LowLatency", "AutoPowerOn": "RestoreLastState",
                       "PowerOnDelay": "NoDelay", "Slot1NicBoot1": "NetworkBoot", "Unrelated": "x"}}


@pytest.fixture(autouse=True)
def reset_output_mode():
    set_output_mode(OutputMode.TABLE)
    yield
    set_output_mode(OutputMode.TABLE)


class FakeClient:
    def __init__(self, system=None, fail_paths=()):
        self.system = dict(SYSTEM if system is None else system)
        self.fail_paths = fail_paths
        self.patches: list[tuple[str, dict]] = []

    async def get_system_uri(self):
        return "/redfish/v1/Systems/1/"

    async def get_manager_uri(self):
        return "/redfish/v1/Managers/1/"

    async def get(self, uri):
        if any(path in uri.lower() for path in self.fail_paths):
            raise RuntimeError(f"boom {uri}")
        if uri == "/redfish/v1/Systems/1/":
            return self.system
        if uri == "/redfish/v1/Managers/1/":
            return MANAGER
        if "bios" in uri.lower():
            return BIOS
        raise AssertionError(f"unexpected GET {uri}")

    async def patch(self, uri, body):
        self.patches.append((uri, body))
        return {}


def session_factory(clients: dict[str, FakeClient | Exception]):
    @asynccontextmanager
    async def session(host, **_):
        client = clients[host["name"]]
        if isinstance(client, Exception):
            raise client
        yield client
    return session


class TestResources:
    @pytest.mark.asyncio
    async def test_summary_is_plain_stable_dict(self):
        summary = await api.fetch_summary(FakeClient())
        assert summary == {
            "model": "HPE ProLiant Compute DL325 Gen12", "serial": "SN1", "product_id": "P1-B21",
            "uuid": "uuid-1", "os_hostname": None, "bios_version": "A60", "power_state": "On",
            "health": "OK", "post_state": "FinishedPost", "uid": "Off",
            "ilo": {"model": "iLO 7", "firmware": "1.21.00", "hostname": "ilo-1", "health": "OK"},
        }
        json.dumps(summary)

    @pytest.mark.asyncio
    async def test_uid_state_for_ilo6_and_ilo7(self):
        ilo6 = dict(SYSTEM, IndicatorLED="Lit")
        del ilo6["LocationIndicatorActive"]
        assert (await api.fetch_summary(FakeClient(ilo6)))["uid"] == "Lit"
        assert (await api.fetch_summary(FakeClient(dict(SYSTEM, LocationIndicatorActive=True))))["uid"] == "Lit"

    @pytest.mark.asyncio
    async def test_power_reports_auto_power_on_after_ac_loss(self):
        power = await api.fetch_power(FakeClient())
        assert power == {"power_state": "On", "post_state": "FinishedPost", "power_restore_policy": None,
                         "auto_power_on": "Restore", "power_on_delay": "NoDelay"}
        no_oem = dict(SYSTEM, Oem={}, PowerRestorePolicy="AlwaysOn")
        power = await api.fetch_power(FakeClient(no_oem))
        assert power["auto_power_on"] == "RestoreLastState"
        assert power["power_restore_policy"] == "AlwaysOn"

    @pytest.mark.asyncio
    async def test_power_survives_bios_read_failure(self):
        power = await api.fetch_power(FakeClient(fail_paths=("bios",)))
        assert power["auto_power_on"] == "Restore"
        assert power["power_on_delay"] is None

    @pytest.mark.asyncio
    async def test_bios_settings_grouped_without_unrelated_attributes(self):
        bios = await api.fetch_bios_settings(FakeClient())
        assert bios["NIC Boot"] == {"Slot1NicBoot1": "NetworkBoot"}
        flat = {k for section in bios.values() for k in section}
        assert "WorkloadProfile" in flat and "Unrelated" not in flat
        assert all(bios.values())

    def test_parse_resources(self):
        assert api.parse_resources(None) == ["summary"]
        assert api.parse_resources("all") == list(api.RESOURCES)
        assert api.parse_resources("gpu, network-ilo,gpu") == ["gpu", "network_ilo"]
        with pytest.raises(ValueError, match="bogus"):
            api.parse_resources("summary,bogus")
        assert set(api.RESOURCE_DESCRIPTIONS) == set(api.RESOURCES)


class TestCollect:
    @pytest.mark.asyncio
    async def test_failures_are_isolated_per_server_and_resource(self):
        clients = {
            "ok": FakeClient(),
            "partial": FakeClient(fail_paths=("managers",)),
            "down": ConnectionError("unreachable"),
        }
        hosts = [{"name": name} for name in clients]
        with patch("proliant.ilo.api.ilo_session", session_factory(clients)):
            result = await api.collect(hosts, ["summary", "power"])
        assert result["schema_version"] == api.SCHEMA_VERSION
        assert result["resources"] == ["summary", "power"]
        servers = result["servers"]
        assert servers["ok"]["summary"]["serial"] == "SN1"
        assert "error" in servers["partial"]["summary"]
        assert servers["partial"]["power"]["power_state"] == "On"
        assert servers["down"] == {"error": "ConnectionError: unreachable"}
        json.dumps(result)

    @pytest.mark.asyncio
    async def test_unknown_resource_rejected_before_any_request(self):
        with patch("proliant.ilo.api.ilo_session") as session:
            with pytest.raises(ValueError):
                await api.collect([HOST], ["nope"])
        session.assert_not_called()


class TestCli:
    def run(self, argv, collect_result=None):
        from proliant.ilo import cli
        collect = AsyncMock(return_value=collect_result or {"schema_version": 1, "servers": {}})
        with patch("proliant.ilo.cli._load_hosts_or_exit", return_value=[HOST]), \
             patch("proliant.ilo.api.collect", collect):
            cli.main(argv)
        return collect

    def test_inventory_always_json_with_requested_resources(self, capsys):
        collect = self.run(["inventory", "dl325-gen12", "--resources", "summary,gpu"],
                           {"schema_version": 1, "servers": {"dl325-gen12": {}}})
        assert json.loads(capsys.readouterr().out)["schema_version"] == 1
        assert collect.call_args.args[1] == ["summary", "gpu"]

    def test_inventory_rejects_unknown_resource(self, capsys):
        with pytest.raises(SystemExit) as exc:
            self.run(["inventory", "--resources", "bogus"])
        assert exc.value.code == 2
        assert "bogus" in capsys.readouterr().err

    @pytest.mark.parametrize("argv, resources", [
        (["--json", "servers", "describe", "dl325-gen12"], list(api.RESOURCES)),
        (["--json", "storage", "describe", "dl325-gen12"], ["storage"]),
        (["--json", "network", "describe", "dl325-gen12"], ["network"]),
        (["--json", "network-ilo", "describe", "dl325-gen12"], ["network_ilo"]),
        (["--json", "reports", "gpu"], ["gpu"]),
        (["--json", "reports", "cpu"], ["cpu"]),
        (["--json", "reports", "memory"], ["memory"]),
    ])
    def test_json_variants_use_stable_api(self, capsys, argv, resources):
        collect = self.run(argv)
        assert collect.call_args.args[1] == resources
        json.loads(capsys.readouterr().out)

    def test_power_describe_json(self, capsys):
        from proliant.ilo import cli
        with patch("proliant.ilo.cli._load_hosts_or_exit", return_value=[HOST]), \
             patch("proliant.ilo.cli.ilo_session", session_factory({"dl325-gen12": FakeClient()})):
            cli.main(["--json", "power", "describe", "dl325-gen12"])
        out = json.loads(capsys.readouterr().out)
        assert out["host"] == "dl325-gen12" and out["auto_power_on"] == "Restore"

    def test_uid_dry_run_does_not_patch(self, capsys):
        from proliant.ilo import cli
        client = FakeClient()
        with patch("proliant.ilo.cli._load_hosts_or_exit", return_value=[HOST]), \
             patch("proliant.ilo.cli.ilo_session", session_factory({"dl325-gen12": client})):
            cli.main(["--json", "uid", "on", "dl325-gen12", "--dry-run"])
        out = json.loads(capsys.readouterr().out)
        assert out["status"] == "dry-run" and out["payload"] == {"LocationIndicatorActive": True}
        assert client.patches == []

    def test_uid_sends_patch_without_dry_run(self, capsys):
        from proliant.ilo import cli
        client = FakeClient()
        with patch("proliant.ilo.cli._load_hosts_or_exit", return_value=[HOST]), \
             patch("proliant.ilo.cli.ilo_session", session_factory({"dl325-gen12": client})):
            cli.main(["uid", "off", "dl325-gen12"])
        assert client.patches == [("/redfish/v1/Systems/1/", {"LocationIndicatorActive": False})]
        assert "UID light turned off" in capsys.readouterr().out

    def test_bios_describe_json(self, capsys):
        from proliant.ilo import cli
        with patch("proliant.ilo.cli._load_hosts_or_exit", return_value=[HOST]), \
             patch("proliant.ilo.cli.ilo_session", session_factory({"dl325-gen12": FakeClient()})):
            cli.main(["--json", "bios", "describe", "dl325-gen12"])
        out = json.loads(capsys.readouterr().out)
        assert out["attributes"]["WorkloadProfile"] == "LowLatency" and out["pending"] is False
