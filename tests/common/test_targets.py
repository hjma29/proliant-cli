"""Tests for pcli.common.targets — resolve_hosts() and _read_names_from()."""
import io
import sys
import pytest

from pcli.common.targets import resolve_hosts, _read_names_from


def _make_loader(all_hosts: list[dict]):
    """Create a simple loader that returns matching hosts by name."""
    host_map = {h["name"].lower(): h for h in all_hosts}
    def loader(name=None):
        if name is None:
            return all_hosts
        return [host_map[name.lower()]] if name.lower() in host_map else []
    return loader


ALL_HOSTS = [
    {"name": "server1", "url": "https://ilo1", "username": "u", "password": "p"},
    {"name": "server2", "url": "https://ilo2", "username": "u", "password": "p"},
    {"name": "server3", "url": "https://ilo3", "username": "u", "password": "p"},
]


class TestReadNamesFrom:
    def test_reads_file(self, tmp_path):
        f = tmp_path / "hosts.txt"
        f.write_text("hostA\nhostB\nhostC\n")
        result = _read_names_from(str(f))
        assert result == ["hostA", "hostB", "hostC"]

    def test_skips_blank_lines(self, tmp_path):
        f = tmp_path / "hosts.txt"
        f.write_text("hostA\n\nhostB\n\n")
        result = _read_names_from(str(f))
        assert "" not in result
        assert len(result) == 2

    def test_skips_comment_lines(self, tmp_path):
        f = tmp_path / "hosts.txt"
        f.write_text("hostA\n# this is a comment\nhostB\n")
        result = _read_names_from(str(f))
        assert all(not h.startswith("#") for h in result)
        assert len(result) == 2

    def test_reads_stdin_dash(self, monkeypatch):
        monkeypatch.setattr("sys.stdin", io.StringIO("host1\nhost2\n"))
        monkeypatch.setattr("sys.stdin.isatty", lambda: False)
        result = _read_names_from("-")
        assert result == ["host1", "host2"]

    def test_stdin_skips_blank_lines(self, monkeypatch):
        monkeypatch.setattr("sys.stdin", io.StringIO("host1\n\nhost2\n"))
        monkeypatch.setattr("sys.stdin.isatty", lambda: False)
        result = _read_names_from("-")
        assert "" not in result
        assert len(result) == 2


class TestResolveHostsSingle:
    def test_single_host_returns_matching_dict(self):
        loader = _make_loader(ALL_HOSTS)
        result = resolve_hosts("server1", hosts_from=None, loader=loader)
        assert len(result) == 1
        assert result[0]["name"] == "server1"

    def test_no_host_arg_returns_all(self):
        loader = _make_loader(ALL_HOSTS)
        result = resolve_hosts(None, hosts_from=None, loader=loader)
        assert len(result) == 3


class TestResolveHostsCommaSeparated:
    def test_two_hosts(self):
        loader = _make_loader(ALL_HOSTS)
        result = resolve_hosts("server1,server2", hosts_from=None, loader=loader)
        names = {h["name"] for h in result}
        assert names == {"server1", "server2"}

    def test_three_hosts_with_spaces(self):
        loader = _make_loader(ALL_HOSTS)
        result = resolve_hosts("server1, server2, server3", hosts_from=None, loader=loader)
        assert len(result) == 3

    def test_unknown_host_skipped_with_warning(self, capsys):
        loader = _make_loader(ALL_HOSTS)
        result = resolve_hosts("server1,unknown99", hosts_from=None, loader=loader)
        assert len(result) == 1
        assert result[0]["name"] == "server1"
        err = capsys.readouterr().err
        assert "unknown99" in err


class TestResolveHostsFromFile:
    def test_reads_hosts_from_file(self, tmp_path):
        f = tmp_path / "hosts.txt"
        f.write_text("server1\nserver2\n")
        loader = _make_loader(ALL_HOSTS)
        result = resolve_hosts(None, hosts_from=str(f), loader=loader)
        names = {h["name"] for h in result}
        assert names == {"server1", "server2"}

    def test_exits_on_empty_file(self, tmp_path):
        f = tmp_path / "empty.txt"
        f.write_text("\n\n")
        loader = _make_loader(ALL_HOSTS)
        with pytest.raises(SystemExit):
            resolve_hosts(None, hosts_from=str(f), loader=loader)
