"""Tests for proliant.com.printers table rendering."""
from __future__ import annotations

from rich.console import Console

from proliant.com import printers as prn
from proliant.com.devices import Device


def _make_device(serial: str, os_name: str | None, ilo_name: str) -> Device:
    raw = {"name": ilo_name, "category": "COMPUTE"}
    if os_name:
        raw["secondary_name"] = os_name
    return Device(
        id=serial,
        serial_number=serial,
        product_id="P07785-B21",
        device_type="COMPUTE",
        display_name=serial,
        model="ProLiant DL380A Gen11",
        service_name="Compute Ops Mgmt",
        subscription_key=None,
        tags={},
        raw=raw,
    )


def test_servers_table_columns_do_not_stretch_on_wide_terminal(monkeypatch, capsys):
    """Regression test: on a wide terminal, OS Name/iLO Name/Model/Location must not
    be padded out with excess blank space (previously caused by Table(expand=True)
    combined with per-column max_width, which Rich does not respect)."""
    wide_console = Console(width=200, force_terminal=False, no_color=True)
    monkeypatch.setattr(prn, "get_console", lambda: wide_console)

    devices = [
        _make_device("2M231700K4", None, "ILO2M231700K4.vmhost.local"),
        _make_device("2M240400JK", "DL380-20", "ILO2M240400JK.aitest.local"),
    ]

    prn.print_devices_table(devices, default_fields=prn._SERVER_DEFAULT_FIELDS)

    out = capsys.readouterr().out
    lines = [ln for ln in out.splitlines() if ln.strip()]
    header_line = next(ln for ln in lines if "Serial" in ln and "OS Name" in ln)

    # The whole table (all columns) should be far narrower than the console width --
    # i.e. it must not expand to fill all 200 columns.
    assert len(header_line) < 100


def test_devices_table_columns_do_not_stretch_on_wide_terminal(monkeypatch, capsys):
    wide_console = Console(width=200, force_terminal=False, no_color=True)
    monkeypatch.setattr(prn, "get_console", lambda: wide_console)

    devices = [_make_device("2M231700K4", "DL380-20", "ILO2M231700K4.vmhost.local")]

    prn.print_devices_table(devices)

    out = capsys.readouterr().out
    lines = [ln for ln in out.splitlines() if ln.strip()]
    header_line = next(ln for ln in lines if "Device" in ln and "Model" in ln)

    assert len(header_line) < 120


def test_os_name_and_ilo_name_not_truncated_when_room_available():
    """Regression test: OS Name / iLO Name formatters used to hard-truncate to 18
    chars regardless of available space. They should now return the full name and
    let the (much wider) column max_width / Rich's own ellipsis handle overflow
    only when truly needed."""
    long_os = "ai-ent-n1.hol.enable.hpe.com"
    long_ilo = "ilo-azure-n1.hol.enable.hpe.com"
    d = _make_device("3M1D0P14MJ", long_os, long_ilo)

    os_cell = prn._fmt_os_name(d)
    ilo_cell = prn._fmt_ilo_name(d)

    assert long_os in os_cell
    assert "…" not in os_cell
    assert ilo_cell == long_ilo


def test_servers_table_shows_full_hostnames_on_reasonably_wide_terminal(monkeypatch, capsys):
    console = Console(width=120, force_terminal=False, no_color=True)
    monkeypatch.setattr(prn, "get_console", lambda: console)

    long_os = "ai-ent-n1.hol.enable.hpe.com"
    long_ilo = "ilo-azure-n1.hol.enable.hpe.com"
    devices = [_make_device("3M1D0P14MJ", long_os, long_ilo)]

    prn.print_devices_table(devices, default_fields=prn._SERVER_DEFAULT_FIELDS)

    out = capsys.readouterr().out
    assert long_os in out
    assert long_ilo in out
