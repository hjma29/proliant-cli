"""
proliant.common.memory_health
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
Shared classification of the HPE ``Oem.Hpe.DIMMStatus`` Redfish field.

HPE's iLO Redfish implementation does not expose the standard
``MemoryMetrics`` resource (confirmed via a live 404 probe against a Gen12
iLO), so no correctable/uncorrectable ECC error *counts* are available
through Redfish or OneView on this hardware. ``DIMMStatus`` is the closest
available per-DIMM health signal, so this module classifies it into
"healthy" vs. "needs attention" for display purposes.

Enum values are from HPE's public ``HpeMemoryExt`` OEM schema
(v2_5_0/v2_5_1): null, Unknown, Other, NotPresent, PresentUnused, GoodInUse,
AddedButUnused, UpgradedButUnused, ExpectedButMissing, DoesNotMatch,
NotSupported, ConfigurationError, Degraded, PresentSpare,
GoodPartiallyInUse, MapOutConfiguration, MapOutError.
"""

from __future__ import annotations

# Statuses meaning the DIMM is present and not reporting any problem.
GOOD_DIMM_STATUSES = {
    "GoodInUse",
    "GoodPartiallyInUse",
    "PresentUnused",
    "PresentSpare",
    "AddedButUnused",
    "UpgradedButUnused",
}

# Statuses that indicate an actual problem worth flagging to the operator.
ATTENTION_DIMM_STATUSES = {
    "Degraded",
    "ConfigurationError",
    "DoesNotMatch",
    "ExpectedButMissing",
    "MapOutConfiguration",
    "MapOutError",
    "NotSupported",
    "Other",
}


def is_attention_status(status: str) -> bool:
    """True if a populated DIMM's status indicates a real problem."""
    return bool(status) and status in ATTENTION_DIMM_STATUSES


def dimm_status_label(status: str) -> str:
    """Rich-markup label for a single populated DIMM's status."""
    if not status or status in GOOD_DIMM_STATUSES:
        return "[dim]OK[/dim]"
    if is_attention_status(status):
        return f"[bold red]{status}[/bold red]"
    return status
