"""Tests for proliant.com.printers table rendering."""
from __future__ import annotations

from rich.console import Console

from proliant.com import printers as prn
from proliant.com.servers import Server


def _make_server(serial: str, os_name: str | None, ilo_hostname: str, name: str | None = None) -> Server:
    return Server(
        id=serial,
        name=name or serial,
        serial_number=serial,
        model="ProLiant DL380A Gen11",
        product_id="P07785-B21",
        manufacturer="HPE",
        generation="GEN_11",
        uuid="—",
        health="OK",
        power_state="ON",
        state_label="Connected",
        connected=True,
        connection_type="Direct",
        maintenance_mode=False,
        auto_ilo_fw_update=False,
        subscription_tier="—",
        subscription_state="—",
        baseline="—",
        group="—",
        appliance_name="—",
        oneview_name="—",
        oneview_state="—",
        ilo_hostname=ilo_hostname,
        ilo_ip="—",
        ilo_version="—",
        ilo_license="—",
        operating_system=os_name or "—",
        cpu="—",
        raw={},
    )


def test_servers_table_columns_do_not_stretch_on_wide_terminal(monkeypatch, capsys):
    """Regression test: on a wide terminal, Name/Serial/Model/Group must not
    be padded out with excess blank space (previously caused by Table(expand=True)
    combined with per-column max_width, which Rich does not respect)."""
    wide_console = Console(width=200, force_terminal=False, no_color=True)
    monkeypatch.setattr(prn, "get_console", lambda: wide_console)

    servers = [
        _make_server("2M231700K4", None, "ILO2M231700K4.vmhost.local"),
        _make_server("2M240400JK", "Microsoft Windows Server", "ILO2M240400JK.aitest.local", name="DL380-20"),
    ]

    prn.print_devices_table(servers, default_fields=prn._SERVER_DEFAULT_FIELDS)

    out = capsys.readouterr().out
    lines = [ln for ln in out.splitlines() if ln.strip()]
    header_line = next(ln for ln in lines if "Serial" in ln and "Name" in ln)

    # The whole table (all columns) should be far narrower than the console width --
    # i.e. it must not expand to fill all 200 columns.
    assert len(header_line) < 130


def test_devices_table_columns_do_not_stretch_on_wide_terminal(monkeypatch, capsys):
    """'devices list' renders with the same server-focused columns as
    'servers list' (only the underlying device scope differs)."""
    wide_console = Console(width=200, force_terminal=False, no_color=True)
    monkeypatch.setattr(prn, "get_console", lambda: wide_console)

    servers = [_make_server("2M231700K4", "DL380-20", "ILO2M231700K4.vmhost.local", name="DL380-20")]

    prn.print_devices_table(servers)

    out = capsys.readouterr().out
    lines = [ln for ln in out.splitlines() if ln.strip()]
    header_line = next(ln for ln in lines if "Serial" in ln and "Model" in ln)

    assert len(header_line) < 120


def test_os_name_and_ilo_name_not_truncated_when_room_available():
    """Regression test: Operating System / iLO Hostname columns used to hard-
    truncate to 18 chars regardless of available space via dedicated formatter
    functions. Those formatters are gone -- the raw Server fields are used
    directly and only Rich's own column max_width/ellipsis handling applies."""
    long_os = "ai-ent-n1.hol.enable.hpe.com"
    long_ilo = "ilo-azure-n1.hol.enable.hpe.com"
    s = _make_server("3M1D0P14MJ", long_os, long_ilo)

    os_cell = prn._SERVER_FIELDS["os"][3](s)
    ilo_cell = prn._SERVER_FIELDS["ilo-hostname"][3](s)

    assert os_cell == long_os
    assert ilo_cell == long_ilo


def test_servers_table_shows_full_hostnames_on_reasonably_wide_terminal(monkeypatch, capsys):
    console = Console(width=140, force_terminal=False, no_color=True)
    monkeypatch.setattr(prn, "get_console", lambda: console)

    long_ilo = "ilo-azure-n1.hol.enable.hpe.com"
    servers = [_make_server("3M1D0P14MJ", "VMware ESXi", long_ilo, name="ai-ent-n1.hol.enable.hpe.com")]

    prn.print_devices_table(
        servers,
        fields="name,serial,ilo-hostname",
        default_fields=prn._SERVER_DEFAULT_FIELDS,
    )

    out = capsys.readouterr().out
    assert "ai-ent-n1.hol.enable.hpe.com" in out
    assert long_ilo in out
