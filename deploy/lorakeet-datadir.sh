#!/bin/bash
# Where a station's Lorakeet keeps its data: the USB drive (label LKDATA, mounted at /mnt/lkdata) when it's there
# and writable, otherwise a folder on the SD card. Lorakeet's own data folder, ~/.local/share/lorakeet, is a link to
# whichever is in use, so Lorakeet needs no setting for it (and fleetwatch reports where the link points).
#
#   lorakeet-datadir.sh start   before Lorakeet starts (lorakeet.service drop-in, as root): set the link
#   lorakeet-datadir.sh watch   every minute (lorakeet-datadir.timer): switch when the drive fails or comes back
#
# Each database keeps its own sync bookmark and a new one numbers its rows from the time it was made (store.py's
# rowid floor), so the hub never mixes rows from the two up. One rule keeps rows from being stranded: Lorakeet only
# moves back from the card to the drive once everything it logged on the card has reached the hub.
# Setup: deploy/data-drive-setup.sh.
set -u
USER_=lorakeet
SHARE=/home/$USER_/.local/share
MNT=/mnt/lkdata
DRIVE=$MNT/lorakeet
CARD=$SHARE/lorakeet-card
LINK=$SHARE/lorakeet
STATE=/run/lorakeet-datadir
BACK_AFTER_S=600   # the drive has to look healthy this long before Lorakeet moves back to it
log() { logger -t lorakeet-datadir -p "$1" "$2"; }

drive_ok() {
  # pulled out (or died off the bus): clear the dead mount, so the drive can mount again when it's plugged back in
  if mountpoint -q "$MNT" && [ ! -e /dev/disk/by-label/LKDATA ]; then umount -l "$MNT" 2>/dev/null; fi
  # a drive plugged back in after boot isn't mounted by anything else
  if ! mountpoint -q "$MNT" && [ -e /dev/disk/by-label/LKDATA ]; then mount "$MNT" 2>/dev/null; fi
  mountpoint -q "$MNT" || return 1
  install -d -o "$USER_" -g "$USER_" "$DRIVE" 2>/dev/null || return 1
  local t="$DRIVE/.write-test"
  { echo ok > "$t" && sync "$t" && rm -f "$t"; } 2>/dev/null
}

point_to() {
  install -d -o "$USER_" -g "$USER_" "$1"
  ln -sfn "$1" "$LINK.new" && mv -T "$LINK.new" "$LINK"
  # readable by everyone (the link sits in Lorakeet's private home folder): fleetwatch reports it
  mkdir -p "$STATE" && echo "$1" > "$STATE/current"
}

# the small files worth having on both sides: the install id, and the radio settings backups (private keys:
# this user only)
mirror_small() {
  [ -f "$1/install_id" ] && [ ! -f "$2/install_id" ] && cp -p "$1/install_id" "$2/"
  if [ -d "$1/radio-backups" ]; then
    install -d -m 700 -o "$USER_" -g "$USER_" "$2/radio-backups"
    cp -a -n "$1/radio-backups/." "$2/radio-backups/" 2>/dev/null
  fi
  return 0
}

card_synced() {  # nothing logged on the card still waiting to reach the hub
  local b
  b=$(curl -s -m 5 http://127.0.0.1:5190/api/sync | python3 -c '
import json, sys
b = json.load(sys.stdin).get("backlog")
print(sum(b.values()) if isinstance(b, dict) else (b if isinstance(b, int) else -1))' 2>/dev/null)
  [ "${b:-}" = 0 ]
}

have=$(readlink "$LINK" 2>/dev/null || true)
if [ -e "$LINK" ] && [ ! -L "$LINK" ]; then
  log err "$LINK is a real folder, not a link: run deploy/data-drive-setup.sh first"
  exit 0  # never stop Lorakeet from starting over this
fi

install -d -o "$USER_" -g "$USER_" "$CARD"  # Lorakeet's, ready before anything is copied into it
case "${1:-start}" in
  start)
    if drive_ok; then
      mirror_small "$DRIVE" "$CARD" 2>/dev/null
      if [ "$have" = "$CARD" ]; then
        point_to "$CARD"
        log notice "drive is back; staying on the SD card until what's logged there has reached the hub"
      else
        point_to "$DRIVE"
      fi
    else
      [ -d "$DRIVE" ] && mirror_small "$DRIVE" "$CARD" 2>/dev/null
      [ "$have" != "$CARD" ] && log warning "USB drive missing or not writable: Lorakeet is logging to the SD card"
      point_to "$CARD"
    fi
    ;;
  watch)
    mkdir -p "$STATE"
    if [ "$have" = "$DRIVE" ]; then
      if ! drive_ok; then
        log warning "USB drive failed or went away: switching Lorakeet to the SD card"
        point_to "$CARD"
        systemctl restart lorakeet.service
      fi
    elif [ "$have" = "$CARD" ]; then
      if drive_ok; then
        [ -f "$STATE/ok-since" ] || date +%s > "$STATE/ok-since"
        if [ $(( $(date +%s) - $(cat "$STATE/ok-since") )) -ge $BACK_AFTER_S ] && card_synced; then
          log notice "USB drive healthy and the SD card's rows are all at the hub: moving Lorakeet back to the drive"
          mirror_small "$CARD" "$DRIVE"
          point_to "$DRIVE"
          rm -f "$STATE/ok-since"
          systemctl restart lorakeet.service
        fi
      else
        rm -f "$STATE/ok-since"
      fi
    fi
    ;;
esac
exit 0
