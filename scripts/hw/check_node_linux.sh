#!/bin/sh
# 0 PASS, 1 FAIL, 2 CANNOT TEST. No hardware is assumed beyond the host.
set -u

if [ "${1:-}" = '--help' ]; then
    printf '%s\n' 'Usage: check_node_linux.sh [CORE_URL [CORE_CERT_PIN]]' \
        'Set WAVR_BIN to an installed wavr binary. The optional remote status check may also need WAVR_LOCAL_TOKEN in the environment.'
    exit 0
fi
if [ "$#" -gt 2 ]; then
    printf '%s\n' 'CANNOT TEST: too many arguments' 'Usage: check_node_linux.sh [CORE_URL [CORE_CERT_PIN]]'
    exit 2
fi

wavr_bin=${WAVR_BIN:-wavr}
if ! command -v "$wavr_bin" >/dev/null 2>&1; then
    printf '%s\n' 'CANNOT TEST: wavr binary is unavailable' "binary=$wavr_bin"
    exit 2
fi
if ! version=$("$wavr_bin" version 2>/dev/null); then
    printf '%s\n' 'FAIL: wavr version failed' 'The binary did not run on this host.'
    exit 1
fi
if ! capabilities=$("$wavr_bin" capabilities --json 2>/dev/null); then
    printf '%s\n' 'FAIL: wavr capabilities failed' "version=$version"
    exit 1
fi
arch=$(printf '%s\n' "$capabilities" | sed -n 's/^[[:space:]]*"arch": "\([^"]*\)".*/\1/p' | sed -n '1p')
cpus=$(printf '%s\n' "$capabilities" | sed -n 's/^[[:space:]]*"cpu_count": \([0-9][0-9]*\).*/\1/p' | sed -n '1p')
case "$cpus" in
    ''|0|*[!0-9]*) valid=no ;;
    *) valid=yes ;;
esac
if [ -z "$arch" ] || [ "$valid" != yes ]; then
    printf '%s\n' 'FAIL: capability manifest lacks a valid arch or CPU count' "version=$version" "arch=${arch:-missing} cpu_count=${cpus:-missing}"
    exit 1
fi

if [ "$#" -ge 1 ]; then
    if [ "$#" -eq 2 ]; then
        "$wavr_bin" status --url "$1" --pin "$2" -q >/dev/null 2>&1
    else
        "$wavr_bin" status --url "$1" -q >/dev/null 2>&1
    fi
    status_code=$?
    if [ "$status_code" -ne 0 ]; then
        printf '%s\n' "FAIL: Core status returned $status_code" "version=$version" "arch=$arch cpu_count=$cpus" 'Check Core reachability, certificate pin, authentication and attention state.'
        exit 1
    fi
    printf '%s\n' 'PASS: native Node host and Core status' "version=$version" "arch=$arch cpu_count=$cpus" 'Core status=0'
else
    printf '%s\n' 'PASS: native Node host' "version=$version" "arch=$arch cpu_count=$cpus" 'Core status=not requested'
fi
