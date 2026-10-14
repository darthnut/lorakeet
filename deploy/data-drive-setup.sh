#!/bin/bash
# One-time setup of a USB drive for a station's Lorakeet data, so the SD card takes almost no writes.
#
#   sudo bash data-drive-setup.sh /dev/sda --erase
#
# ERASES the drive: formats it ext4 with the label LKDATA, mounts it at /mnt/lkdata at every boot (nofail: a
# missing or dead drive never stops the Pi from booting), moves Lorakeet's data folder onto it, and installs
# lorakeet-datadir (falls back to a folder on the SD card if the drive is missing or fails, and back again once the
# card's rows have reached the hub). The old data folder stays on the card, renamed, as a safety copy.
# Run from a folder holding lorakeet-datadir.sh, .conf, .service and .timer (deploy/ in the Lorakeet checkout).
set -euo pipefail
trap 'echo "data-drive-setup.sh failed at line $LINENO" >&2' ERR
dev=${1:?usage: sudo bash data-drive-setup.sh /dev/sdX --erase}
[ "${2:-}" = "--erase" ] || { echo "this erases $dev: add --erase to go ahead" >&2; exit 2; }
[ "$(id -u)" = 0 ] || { echo "run with sudo" >&2; exit 2; }
here=$(cd "$(dirname "$0")" && pwd)
USER_=lorakeet
SHARE=/home/$USER_/.local/share
LINK=$SHARE/lorakeet
MNT=/mnt/lkdata

# -- is this really a USB drive with nothing of ours on it?
[ "$(lsblk -dno TYPE "$dev")" = disk ] || { echo "$dev isn't a whole disk" >&2; exit 2; }
[ "$(lsblk -dno TRAN "$dev")" = usb ] || { echo "$dev isn't a USB drive" >&2; exit 2; }
root_disk=$(lsblk -no PKNAME "$(findmnt -no SOURCE /)")
[ "$(basename "$dev")" != "$root_disk" ] || { echo "$dev holds the running system" >&2; exit 2; }
if lsblk -no MOUNTPOINTS "$dev" | grep -q .; then echo "$dev is mounted: unmount it first" >&2; exit 2; fi
echo "erasing $dev: $(lsblk -dno SIZE,VENDOR,MODEL,SERIAL "$dev" | tr -s ' ')"

# -- format and mount
wipefs -aq "$dev"
parted -s "$dev" mklabel gpt mkpart lkdata ext4 1MiB 100%
udevadm settle
part=$(lsblk -lnpo NAME,TYPE "$dev" | awk '$2 == "part" {print $1; exit}')
mkfs.ext4 -q -F -L LKDATA -m 1 "$part"
uuid=$(blkid -s UUID -o value "$part")
mkdir -p "$MNT"
sed -i "\\| $MNT |d" /etc/fstab
echo "UUID=$uuid $MNT ext4 defaults,noatime,nofail,x-systemd.device-timeout=10s 0 2" >> /etc/fstab
systemctl daemon-reload
mount "$MNT"

# -- the fallback and the watch
install -m 755 "$here/lorakeet-datadir.sh" /usr/local/sbin/lorakeet-datadir
install -d /etc/systemd/system/lorakeet.service.d
install -m 644 "$here/lorakeet-datadir.conf" /etc/systemd/system/lorakeet.service.d/datadir.conf
install -m 644 "$here/lorakeet-datadir.service" /etc/systemd/system/lorakeet-datadir.service
install -m 644 "$here/lorakeet-datadir.timer" /etc/systemd/system/lorakeet-datadir.timer
systemctl daemon-reload

# -- move the data (Lorakeet stopped, so the databases are consistent; their -wal files go with them)
systemctl stop lorakeet.service
if [ -d "$LINK" ] && [ ! -L "$LINK" ]; then
  install -d -o "$USER_" -g "$USER_" "$MNT/lorakeet"
  cp -a "$LINK/." "$MNT/lorakeet/"
  for db in "$MNT"/lorakeet/*.db; do
    [ -e "$db" ] || continue
    r=$(sudo -u "$USER_" python3 -c 'import sqlite3, sys; print(sqlite3.connect(sys.argv[1]).execute("PRAGMA quick_check").fetchone()[0])' "$db")
    [ "$r" = ok ] || { echo "copy of $db failed its check ($r): data left where it was" >&2; systemctl start lorakeet.service; exit 1; }
  done
  keep="$SHARE/lorakeet-before-drive-$(date +%Y%m%d-%H%M)"
  mv "$LINK" "$keep"
  echo "old data folder kept on the card as $keep"
fi
/usr/local/sbin/lorakeet-datadir start
systemctl enable --now lorakeet-datadir.timer >/dev/null
systemctl start lorakeet.service
sleep 8
echo "data: $(readlink -f "$LINK")"
echo "lorakeet: $(systemctl is-active lorakeet.service)"
df -h "$MNT" | tail -1
