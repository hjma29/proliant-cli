#!/usr/bin/env bash
# Smoke test — validates all read-only CLI commands against live lab servers.
# Run manually before tagging a release:
#
#   bash tests/smoke.sh
#
# Requires: lab network access + hosts-ilo.ini + COM credentials configured.
# Not run in CI (lab servers are not reachable from GitHub Actions runners).

set -euo pipefail

PASS=0
FAIL=0
ERRORS=()

run() {
    local label="$1"
    shift
    printf "  %-50s" "$label"
    if output=$("$@" 2>&1); then
        echo "✓"
        PASS=$((PASS + 1))
    else
        echo "✗"
        ERRORS+=("FAILED: $label\n  cmd: $*\n  out: $(echo "$output" | head -3)")
        FAIL=$((FAIL + 1))
    fi
}

echo ""
echo "proliant smoke tests"
echo "════════════════════════════════════════════════════════"

echo ""
echo "▶ Core"
run "proliant --version"            proliant --version
run "proliant --help"               proliant --help
run "install-completion (idempotent)" proliant install-completion

echo ""
echo "▶ iLO — fleet"
run "ilo servers list"              proliant ilo servers list
run "ilo firmware list"             proliant ilo firmware list
run "ilo nic list"                  proliant ilo nic list
run "ilo nic-host list"             proliant ilo nic-host list
run "ilo nic-ilo list"              proliant ilo nic-ilo list
run "ilo storage list"              proliant ilo storage list
run "ilo cpu list"                  proliant ilo cpu list
run "ilo memory list"               proliant ilo memory list
run "ilo com list"                  proliant ilo com list
run "ilo full list"                 proliant ilo full list
run "ilo serial list"               proliant ilo serial list
run "ilo license list"              proliant ilo license list
run "ilo update-method list"        proliant ilo update-method list

echo ""
echo "▶ iLO — per-server (dl325-gen12)"
run "ilo boot describe"             proliant ilo boot describe dl325-gen12
run "ilo bios describe"             proliant ilo bios describe dl325-gen12
run "ilo power (dry)"               proliant ilo power --help

echo ""
echo "▶ iLO — reports"
run "ilo reports memory list"       proliant ilo reports memory list
run "ilo reports cpu list"          proliant ilo reports cpu list

echo ""
echo "▶ COM"
if { proliant com list devices 2>&1 || true; } | grep -q "Not logged in"; then
    echo "  com list devices                                  ⚠ skipped (not logged in)"
    echo "  com list bundles                                  ⚠ skipped (not logged in)"
else
    run "com list devices"              proliant com list devices
    run "com list bundles"              proliant com list bundles
fi

echo ""
echo "════════════════════════════════════════════════════════"
echo "  Passed: $PASS   Failed: $FAIL"

if [ ${#ERRORS[@]} -gt 0 ]; then
    echo ""
    echo "Failures:"
    for err in "${ERRORS[@]}"; do
        echo -e "$err"
    done
    exit 1
fi

echo ""
echo "All smoke tests passed ✓"
