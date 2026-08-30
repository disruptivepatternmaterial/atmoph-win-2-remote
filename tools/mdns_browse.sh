#!/usr/bin/env bash
# Enumerate mDNS service instances through the operating system's resolver.
#
# tools/atmoph_netscan.py speaks mDNS directly, which is preferable, but a
# sandboxed or multicast-restricted process cannot send to 224.0.0.251. macOS
# runs mDNSResponder out of process, so dns-sd still works where raw multicast
# does not. Use this to find candidate hosts, then point atmoph_netscan.py at
# the addresses it reports.
#
# Usage: tools/mdns_browse.sh [seconds] [service_type ...]

set -uo pipefail

DURATION="${1:-6}"
shift || true

if [ "$#" -gt 0 ]; then
    TYPES=("$@")
else
    # Types an Android-based appliance such as an Atmoph Window plausibly
    # advertises, plus the generic web and debug services worth a look.
    TYPES=(
        _adb._tcp
        _fully._tcp
        _http._tcp
        _http-alt._tcp
        _api._tcp
        _device-info._tcp
        _workstation._tcp
        _airplay._tcp
        _googlecast._tcp
        _androidtvremote2._tcp
        _home-assistant._tcp
    )
fi

command -v dns-sd >/dev/null 2>&1 || {
    echo "dns-sd not found; this script is macOS only" >&2
    exit 1
}

run_for() {
    # Run a dns-sd query for DURATION seconds and print whatever it collected.
    local seconds="$1"
    shift
    dns-sd "$@" >"$tmp" 2>&1 &
    local pid=$!
    sleep "$seconds"
    kill "$pid" 2>/dev/null
    wait "$pid" 2>/dev/null
    cat "$tmp"
}

tmp="$(mktemp -t mdnsbrowse)"
trap 'rm -f "$tmp"' EXIT

for type in "${TYPES[@]}"; do
    echo "=== ${type} ==="
    # Columns: timestamp A/R flags if domain type instance...
    instances="$(run_for "$DURATION" -B "$type" |
        awk '$2 == "Add" { $1=""; $2=""; $3=""; $4=""; $5=""; $6=""; sub(/^ +/, ""); print }' |
        sort -u)"
    if [ -z "$instances" ]; then
        echo "  (none)"
        continue
    fi
    while IFS= read -r instance; do
        [ -z "$instance" ] && continue
        echo "  instance: ${instance}"
        run_for 3 -L "$instance" "$type" |
            grep -E 'can be reached at|^ *[A-Za-z0-9_-]+=' |
            sed 's/^/    /'
    done <<<"$instances"
done
