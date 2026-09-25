#!/bin/sh
# Install a previously downloaded native binary and its systemd unit.
set -eu

if [ "$#" -ne 1 ] || [ ! -f "$1" ]; then
    printf '%s\n' 'Usage: install-node.sh /path/to/downloaded/wavr' >&2
    exit 2
fi
if [ "$(id -u)" -ne 0 ]; then
    printf '%s\n' 'Run as root to install into /usr/local/bin and /etc/systemd/system.' >&2
    exit 2
fi
if ! command -v systemctl >/dev/null 2>&1; then
    printf '%s\n' 'systemd is required for this installer.' >&2
    exit 2
fi
if ! command -v useradd >/dev/null 2>&1 || ! command -v groupadd >/dev/null 2>&1 || ! command -v getent >/dev/null 2>&1; then
    printf '%s\n' 'useradd, groupadd and getent are required to create the service identity.' >&2
    exit 2
fi

source_binary=$1
unit_dir=$(CDPATH= cd -P "$(dirname "$0")" && pwd)
if ! getent group wavr-node >/dev/null; then
    groupadd --system wavr-node
fi
if ! id wavr-node >/dev/null 2>&1; then
    useradd --system --no-create-home --gid wavr-node --shell /bin/false wavr-node
fi
install -d -m 0700 -o wavr-node -g wavr-node /var/lib/wavr-node
install -m 0755 "$source_binary" /usr/local/bin/wavr
install -m 0644 "$unit_dir/wavr-node.service" /etc/systemd/system/wavr-node.service
systemctl daemon-reload
printf '%s\n' \
    'Installed /usr/local/bin/wavr and wavr-node.service.' \
    '1. Check /usr/local/bin/wavr version and capabilities.' \
    '2. Adjust ExecStart sensor port and DeviceAllow in a systemd override if needed.' \
    '3. Enroll as wavr-node with --state /var/lib/wavr-node/node.json (see docs/deploy/node-lite.md).' \
    '4. Run: systemctl enable --now wavr-node.service' \
    '5. Read: systemctl status wavr-node.service and journalctl -u wavr-node.service'
