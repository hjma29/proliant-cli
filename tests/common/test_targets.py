"""Tests for pcli.common.targets — resolve_hosts()."""
import io
import os
import pytest

from pcli.common.targets import resolve_hosts


class TestResolveHostsSingle:
    def test_single_hostname(self):
        result = resolve_hosts("ilo.server1.example.com", hosts_from=None)
        assert result == ["ilo.server1.example.com"]

    def test_single_ip(self):
        result = resolve_hosts("192.168.1.100", hosts_from=None)
        assert result == ["192.168.1.100"]


class TestResolveHostsCommaSeparated:
    def test_two_hosts(self):
        result = resolve_hosts("host1,host2", hosts_from=None)
        assert result == ["host1", "host2"]

    def test_three_hosts_with_spaces(self):
        result = resolve_hosts("a, b, c", hosts_from=None)
        assert result == ["a", "b", "c"]

    def test_trailing_comma_ignored(self):
        result = resolve_hosts("host1,host2,", hosts_from=None)
        assert "host1" in result
        assert "host2" in result
        assert "" not in result


class TestResolveHostsFromFile:
    def test_reads_file(self, tmp_path):
        f = tmp_path / "hosts.txt"
        f.write_text("hostA\nhostB\nhostC\n")
        result = resolve_hosts(None, hosts_from=str(f))
        assert result == ["hostA", "hostB", "hostC"]

    def test_skips_blank_lines(self, tmp_path):
        f = tmp_path / "hosts.txt"
        f.write_text("hostA\n\nhostB\n\n")
        result = resolve_hosts(None, hosts_from=str(f))
        assert "" not in result
        assert len(result) == 2

    def test_skips_comment_lines(self, tmp_path):
        f = tmp_path / "hosts.txt"
        f.write_text("hostA\n# this is a comment\nhostB\n")
        result = resolve_hosts(None, hosts_from=str(f))
        assert all(not h.startswith("#") for h in result)


class TestResolveHostsFromStdin:
    def test_reads_stdin_dash(self, monkeypatch):
        monkeypatch.setattr("sys.stdin", io.StringIO("host1\nhost2\n"))
        result = resolve_hosts(None, hosts_from="-")
        assert result == ["host1", "host2"]

    def test_stdin_skips_blank_lines(self, monkeypatch):
        monkeypatch.setattr("sys.stdin", io.StringIO("host1\n\nhost2\n"))
        result = resolve_hosts(None, hosts_from="-")
        assert "" not in result
        assert len(result) == 2
