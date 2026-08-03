"""Tests for proliant.common.memory_health DIMMStatus classification."""

from __future__ import annotations

from proliant.common.memory_health import (
    ATTENTION_DIMM_STATUSES,
    GOOD_DIMM_STATUSES,
    dimm_status_label,
    is_attention_status,
)


class TestIsAttentionStatus:
    def test_good_statuses_are_not_attention(self):
        for status in GOOD_DIMM_STATUSES:
            assert is_attention_status(status) is False

    def test_attention_statuses_flagged(self):
        for status in ATTENTION_DIMM_STATUSES:
            assert is_attention_status(status) is True

    def test_empty_status_is_not_attention(self):
        assert is_attention_status("") is False

    def test_unknown_value_not_in_either_set_is_not_attention(self):
        # Defensive: an enum value we haven't classified shouldn't false-alarm.
        assert is_attention_status("SomeFutureEnumValue") is False


class TestDimmStatusLabel:
    def test_good_in_use_renders_ok(self):
        assert dimm_status_label("GoodInUse") == "[dim]OK[/dim]"

    def test_empty_status_renders_ok(self):
        assert dimm_status_label("") == "[dim]OK[/dim]"

    def test_degraded_renders_bold_red(self):
        assert dimm_status_label("Degraded") == "[bold red]Degraded[/bold red]"

    def test_configuration_error_renders_bold_red(self):
        assert dimm_status_label("ConfigurationError") == "[bold red]ConfigurationError[/bold red]"

    def test_unclassified_status_passed_through_plain(self):
        assert dimm_status_label("SomeFutureEnumValue") == "SomeFutureEnumValue"
