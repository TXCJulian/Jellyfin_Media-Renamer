#!/bin/sh
set -e

PUID=${PUID:-1000}
PGID=${PGID:-1000}
UMASK=${UMASK:-022}

umask "$UMASK"

# Adjust appuser UID/GID if needed
if [ "$(id -u appuser)" != "$PUID" ] || [ "$(id -g appuser)" != "$PGID" ]; then
    groupmod -o -g "$PGID" appgroup 2>/dev/null || true
    usermod -o -u "$PUID" -g "$PGID" appuser 2>/dev/null || true
fi

# Create and own the data directories for appuser.
#
# These are also created in the Dockerfile, but mounting a volume at /data
# MASKS the image's copies -- the mount is usually a fresh filesystem owned by
# root, so every subdirectory disappears and appuser (who cannot write to a
# root-owned /data) can no longer recreate them. That surfaces as
# "Downloader is not initialised" and as a PermissionError from the encoder
# store, depending on which feature is reached first.
#
# We still run as root here (privileges are dropped at the exec below), so we
# can provision them regardless of how the host directory is owned.
mkdir -p /data/cutter-jobs /data/downloader /data/encoder/originals 2>/dev/null || true
chown appuser:appgroup /data/cutter-jobs /data/downloader /data/encoder /data/encoder/originals 2>/dev/null || true
chown -R appuser:appgroup /var/lib/media-renamer 2>/dev/null || true

# Grant GPU access: match host render/video group GIDs for /dev/dri
if [ -d /dev/dri ]; then
    for dev in /dev/dri/renderD* /dev/dri/card*; do
        [ -e "$dev" ] || continue
        dev_gid=$(stat -c '%g' "$dev")
        if ! id -G appuser | tr ' ' '\n' | grep -q "^${dev_gid}$"; then
            # Create or reuse a group with the device's GID, then add appuser
            grp_name=$(getent group "$dev_gid" | cut -d: -f1 || true)
            if [ -z "$grp_name" ]; then
                grp_name="devdri${dev_gid}"
                groupadd -g "$dev_gid" "$grp_name" 2>/dev/null || true
            fi
            usermod -a -G "$grp_name" appuser 2>/dev/null || true
        fi
    done
fi

exec setpriv --reuid="$PUID" --regid="$PGID" --init-groups "$@"
