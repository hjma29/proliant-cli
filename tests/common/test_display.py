"""Tests for pcli.common.display."""
import json
import threading
import pytest

from pcli.common.display import (
    OutputMode,
    get_console,
    get_output_mode,
    make_table,
    print_json,
    set_output_mode,
)
from rich.console import Console
from rich.table import Table


@pytest.fixture(autouse=True)
def reset_output_mode():
    """Always restore TABLE mode after each test."""
    yield
    set_output_mode(OutputMode.TABLE)


class TestOutputMode:
    def test_default_is_table(self):
        assert get_output_mode() == OutputMode.TABLE

    def test_set_json_mode(self):
        set_output_mode(OutputMode.JSON)
        assert get_output_mode() == OutputMode.JSON

    def test_console_is_stderr_in_json_mode(self):
        set_output_mode(OutputMode.JSON)
        c = get_console()
        assert c.stderr is True

    def test_console_is_stdout_in_table_mode(self):
        set_output_mode(OutputMode.TABLE)
        c = get_console()
        assert c.stderr is False

    def test_set_mode_resets_console_singleton(self):
        c1 = get_console()
        set_output_mode(OutputMode.JSON)
        c2 = get_console()
        assert c1 is not c2

    def test_thread_isolation(self):
        """Each thread has its own output mode — no cross-thread pollution."""
        set_output_mode(OutputMode.JSON)
        assert get_output_mode() == OutputMode.JSON

        results = {}

        def thread_fn():
            # Child thread starts in TABLE mode (fresh thread-local)
            results["child_mode"] = get_output_mode()
            set_output_mode(OutputMode.JSON)
            results["child_after_set"] = get_output_mode()

        t = threading.Thread(target=thread_fn)
        t.start()
        t.join()

        # Main thread mode must be unaffected by child thread's set_output_mode
        assert get_output_mode() == OutputMode.JSON
        assert results["child_mode"] == OutputMode.TABLE  # fresh TLS in new thread
        assert results["child_after_set"] == OutputMode.JSON

    def test_thread_json_does_not_pollute_main(self):
        """Setting JSON mode in a child thread doesn't affect main thread."""
        set_output_mode(OutputMode.TABLE)

        def set_json_in_thread():
            set_output_mode(OutputMode.JSON)

        t = threading.Thread(target=set_json_in_thread)
        t.start()
        t.join()

        assert get_output_mode() == OutputMode.TABLE


class TestMakeTable:
    def test_returns_rich_table(self):
        t = make_table("Title", ("Col A", {}), ("Col B", {}))
        assert isinstance(t, Table)

    def test_correct_column_count(self):
        t = make_table("T", ("A", {}), ("B", {}), ("C", {}))
        assert len(t.columns) == 3

    def test_column_kwargs_applied(self):
        t = make_table("T", ("Name", {"no_wrap": True, "min_width": 20}))
        col = t.columns[0]
        assert col.no_wrap is True
        assert col.min_width == 20

    def test_title_set(self):
        t = make_table("My Title", ("X", {}))
        assert t.title == "My Title"

    def test_empty_title(self):
        t = make_table("", ("X", {}))
        assert t.title == ""

    def test_add_row_works(self):
        t = make_table("T", ("A", {}), ("B", {}))
        t.add_row("hello", "world")
        assert t.row_count == 1


class TestPrintJson:
    def test_outputs_valid_json(self, capsys):
        data = [{"name": "server1", "model": "DL380"}]
        print_json(data)
        out = capsys.readouterr().out
        parsed = json.loads(out)
        assert parsed[0]["name"] == "server1"

    def test_handles_non_serializable_with_str_fallback(self, capsys):
        from datetime import datetime
        data = {"ts": datetime(2025, 1, 1)}
        print_json(data)
        out = capsys.readouterr().out
        parsed = json.loads(out)
        assert "ts" in parsed

    def test_outputs_to_stdout_not_stderr(self, capsys):
        print_json({"key": "val"})
        captured = capsys.readouterr()
        assert captured.out.strip()
        assert not captured.err.strip()
