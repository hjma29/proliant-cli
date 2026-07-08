"""Tests for `proliant setting telemetry` -- status display and toggle."""
from __future__ import annotations

import proliant.common as common_mod
from proliant.setting import cli as setting_cli


def _patch_config_dir(monkeypatch, tmp_path):
    monkeypatch.setattr(common_mod, "config_dir", lambda: tmp_path)


class TestTelemetryEffectiveState:
    def test_default_is_off_when_nothing_configured(self, monkeypatch, tmp_path):
        monkeypatch.delenv("PROLIANT_TELEMETRY", raising=False)
        assert setting_cli._telemetry_effective_state(tmp_path) == "off"
        assert "never configured" in setting_cli._telemetry_reason(tmp_path)

    def test_enabled_marker_wins(self, monkeypatch, tmp_path):
        monkeypatch.delenv("PROLIANT_TELEMETRY", raising=False)
        (tmp_path / "telemetry-enabled").touch()
        assert setting_cli._telemetry_effective_state(tmp_path) == "on"
        assert setting_cli._telemetry_reason(tmp_path) == "explicitly enabled"

    def test_disabled_marker_wins_even_with_enabled_marker(self, monkeypatch, tmp_path):
        monkeypatch.delenv("PROLIANT_TELEMETRY", raising=False)
        (tmp_path / "telemetry-enabled").touch()
        (tmp_path / "telemetry-disabled").touch()
        assert setting_cli._telemetry_effective_state(tmp_path) == "off"
        assert setting_cli._telemetry_reason(tmp_path) == "explicitly disabled"

    def test_disabled_marker_wins_over_env_var(self, monkeypatch, tmp_path):
        monkeypatch.setenv("PROLIANT_TELEMETRY", "1")
        (tmp_path / "telemetry-disabled").touch()
        assert setting_cli._telemetry_effective_state(tmp_path) == "off"

    def test_env_var_enables_when_no_markers(self, monkeypatch, tmp_path):
        monkeypatch.setenv("PROLIANT_TELEMETRY", "1")
        assert setting_cli._telemetry_effective_state(tmp_path) == "on"
        assert "PROLIANT_TELEMETRY" in setting_cli._telemetry_reason(tmp_path)


class TestCmdTelemetryDirectSet:
    """Passing 'on'/'off' directly must set the state without any prompt."""

    def test_on_creates_enabled_marker_no_prompt(self, monkeypatch, tmp_path, capsys):
        _patch_config_dir(monkeypatch, tmp_path)
        monkeypatch.setattr("builtins.input", lambda *a, **kw: (_ for _ in ()).throw(
            AssertionError("input() should not be called for a direct on/off")))
        setting_cli._cmd_telemetry("on")
        assert (tmp_path / "telemetry-enabled").exists()
        assert not (tmp_path / "telemetry-disabled").exists()
        out = capsys.readouterr().out.lower()
        assert "enabled" in out
        assert "proliant" not in out  # should describe the problem, not brand the product

    def test_off_creates_disabled_marker_no_prompt(self, monkeypatch, tmp_path, capsys):
        _patch_config_dir(monkeypatch, tmp_path)
        (tmp_path / "telemetry-enabled").touch()
        monkeypatch.setattr("builtins.input", lambda *a, **kw: (_ for _ in ()).throw(
            AssertionError("input() should not be called for a direct on/off")))
        setting_cli._cmd_telemetry("off")
        assert (tmp_path / "telemetry-disabled").exists()
        assert not (tmp_path / "telemetry-enabled").exists()
        assert "disabled" in capsys.readouterr().out.lower()


class TestCmdTelemetryStatusPrompt:
    """Calling with no state shows status (default + current) and confirms before toggling."""

    def test_shows_default_and_current_off(self, monkeypatch, tmp_path, capsys):
        _patch_config_dir(monkeypatch, tmp_path)
        monkeypatch.delenv("PROLIANT_TELEMETRY", raising=False)
        monkeypatch.setattr("builtins.input", lambda *a, **kw: "n")
        setting_cli._cmd_telemetry(None)
        out = capsys.readouterr().out
        assert "Default:" in out
        assert "off" in out
        assert "Current:" in out
        assert "never configured" in out

    def test_prompt_always_says_enable_never_disable(self, monkeypatch, tmp_path, capsys):
        """The question wording must stay 'Enable telemetry?' regardless of
        current state -- never flip to 'Disable telemetry?' (confusing)."""
        _patch_config_dir(monkeypatch, tmp_path)
        monkeypatch.delenv("PROLIANT_TELEMETRY", raising=False)
        captured_prompt = {}

        def _fake_input(prompt=""):
            captured_prompt["text"] = prompt
            return "n"

        monkeypatch.setattr("builtins.input", _fake_input)
        setting_cli._cmd_telemetry(None)
        assert captured_prompt["text"].startswith("Enable telemetry?")
        assert "disable" not in captured_prompt["text"].lower()

        # Same check when currently on.
        (tmp_path / "telemetry-enabled").touch()
        monkeypatch.setattr("builtins.input", _fake_input)
        setting_cli._cmd_telemetry(None)
        assert captured_prompt["text"].startswith("Enable telemetry?")
        assert "disable" not in captured_prompt["text"].lower()

    def test_prompt_default_shown_as_yN_when_currently_off(self, monkeypatch, tmp_path):
        _patch_config_dir(monkeypatch, tmp_path)
        monkeypatch.delenv("PROLIANT_TELEMETRY", raising=False)
        captured_prompt = {}
        monkeypatch.setattr("builtins.input", lambda prompt="": captured_prompt.setdefault("text", prompt) or "n")
        setting_cli._cmd_telemetry(None)
        assert "[y/N]" in captured_prompt["text"]

    def test_prompt_default_shown_as_Yn_when_currently_on(self, monkeypatch, tmp_path):
        _patch_config_dir(monkeypatch, tmp_path)
        monkeypatch.delenv("PROLIANT_TELEMETRY", raising=False)
        (tmp_path / "telemetry-enabled").touch()
        captured_prompt = {}
        monkeypatch.setattr("builtins.input", lambda prompt="": captured_prompt.setdefault("text", prompt) or "n")
        setting_cli._cmd_telemetry(None)
        assert "[Y/n]" in captured_prompt["text"]

    def test_answering_no_makes_no_changes_when_currently_off(self, monkeypatch, tmp_path, capsys):
        _patch_config_dir(monkeypatch, tmp_path)
        monkeypatch.delenv("PROLIANT_TELEMETRY", raising=False)
        monkeypatch.setattr("builtins.input", lambda *a, **kw: "n")
        setting_cli._cmd_telemetry(None)
        assert not (tmp_path / "telemetry-enabled").exists()
        assert not (tmp_path / "telemetry-disabled").exists()
        assert "no changes made" in capsys.readouterr().out.lower()

    def test_answering_yes_enables_when_currently_off(self, monkeypatch, tmp_path, capsys):
        _patch_config_dir(monkeypatch, tmp_path)
        monkeypatch.delenv("PROLIANT_TELEMETRY", raising=False)
        monkeypatch.setattr("builtins.input", lambda *a, **kw: "y")
        setting_cli._cmd_telemetry(None)
        assert (tmp_path / "telemetry-enabled").exists()
        out = capsys.readouterr().out
        assert "telemetry enabled" in out.lower()

    def test_answering_no_disables_when_currently_on(self, monkeypatch, tmp_path, capsys):
        _patch_config_dir(monkeypatch, tmp_path)
        monkeypatch.delenv("PROLIANT_TELEMETRY", raising=False)
        (tmp_path / "telemetry-enabled").touch()
        monkeypatch.setattr("builtins.input", lambda *a, **kw: "n")
        setting_cli._cmd_telemetry(None)
        assert (tmp_path / "telemetry-disabled").exists()
        assert not (tmp_path / "telemetry-enabled").exists()
        out = capsys.readouterr().out
        assert "telemetry disabled" in out.lower()

    def test_answering_yes_makes_no_changes_when_currently_on(self, monkeypatch, tmp_path, capsys):
        _patch_config_dir(monkeypatch, tmp_path)
        monkeypatch.delenv("PROLIANT_TELEMETRY", raising=False)
        (tmp_path / "telemetry-enabled").touch()
        monkeypatch.setattr("builtins.input", lambda *a, **kw: "y")
        setting_cli._cmd_telemetry(None)
        assert (tmp_path / "telemetry-enabled").exists()
        assert "no changes made" in capsys.readouterr().out.lower()

    def test_empty_answer_keeps_current_state(self, monkeypatch, tmp_path, capsys):
        _patch_config_dir(monkeypatch, tmp_path)
        monkeypatch.delenv("PROLIANT_TELEMETRY", raising=False)
        (tmp_path / "telemetry-enabled").touch()
        monkeypatch.setattr("builtins.input", lambda *a, **kw: "")
        setting_cli._cmd_telemetry(None)
        assert (tmp_path / "telemetry-enabled").exists()
        assert "no changes made" in capsys.readouterr().out.lower()

    def test_shows_current_on_with_reason_when_enabled(self, monkeypatch, tmp_path, capsys):
        _patch_config_dir(monkeypatch, tmp_path)
        monkeypatch.delenv("PROLIANT_TELEMETRY", raising=False)
        (tmp_path / "telemetry-enabled").touch()
        monkeypatch.setattr("builtins.input", lambda *a, **kw: "n")
        setting_cli._cmd_telemetry(None)
        out = capsys.readouterr().out
        assert "explicitly enabled" in out

    def test_keyboard_interrupt_during_prompt_makes_no_changes(self, monkeypatch, tmp_path, capsys):
        _patch_config_dir(monkeypatch, tmp_path)
        monkeypatch.delenv("PROLIANT_TELEMETRY", raising=False)

        def _raise(*a, **kw):
            raise KeyboardInterrupt

        monkeypatch.setattr("builtins.input", _raise)
        setting_cli._cmd_telemetry(None)  # must not raise
        assert not (tmp_path / "telemetry-enabled").exists()
        assert not (tmp_path / "telemetry-disabled").exists()


class TestTelemetryArgparse:
    def test_state_argument_is_optional(self):
        parser = setting_cli._build_parser()
        args = parser.parse_args(["telemetry"])
        assert args.cmd == "telemetry"
        assert args.state is None

    def test_state_on_off_still_accepted(self):
        parser = setting_cli._build_parser()
        assert parser.parse_args(["telemetry", "on"]).state == "on"
        assert parser.parse_args(["telemetry", "off"]).state == "off"
