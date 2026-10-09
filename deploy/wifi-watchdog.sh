#!/usr/bin/env bash
# Lorakeet station Wi-Fi watchdog: run every minute by lorakeet-wifi-watchdog.timer (as root).
#
# NetworkManager normally switches between saved networks by itself (home Wi-Fi, a phone's hotspot). But a
# Raspberry Pi's Wi-Fi can wedge after a long spell of failed scans (a phone hotspot that went away for hours):
# it then never joins anything again until power-cycled, though the network is right there. This escalates,
# step by step, only while there is no network at all:
#   offline 2+ min:  rescan, and bring up any saved Wi-Fi network that's in range (highest priority first)
#   offline 10+ min: switch the Wi-Fi radio off and on (at most every 10 min)
#   offline 20+ min: reload the Wi-Fi driver (brcmfmac on a Pi; at most every 20 min): what a power cycle does
# It never reboots: the logger keeps logging throughout. Every action is logged (journalctl -t lorakeet-wifi).
# Out of range of every saved network (a dead zone), the steps just repeat harmlessly.
set -u
STATE=/run/lorakeet-wifi-watchdog
mkdir -p "$STATE"
log() { logger -t lorakeet-wifi "$*"; }
now=$(date +%s)

if nmcli -t -f TYPE,STATE device | grep -qE '^(wifi|ethernet):connected$'; then
  [ -f "$STATE/since" ] && log "back online after $(( now - $(cat "$STATE/since") )) s"
  rm -f "$STATE/since"
  exit 0
fi
[ -f "$STATE/since" ] || { echo "$now" > "$STATE/since"; log "no network"; }
down=$(( now - $(cat "$STATE/since") ))
[ "$down" -lt 120 ] && exit 0
ago() { [ -f "$STATE/$1" ] && echo $(( now - $(cat "$STATE/$1") )) || echo 999999; }

# 1. any saved Wi-Fi network in range: bring it up directly instead of waiting for autoconnect
nmcli device wifi rescan >/dev/null 2>&1; sleep 6
visible=$(nmcli -t -f SSID device wifi list 2>/dev/null)
nmcli -t -f NAME,TYPE connection show | while IFS=: read -r name type; do
  [ "$type" = "802-11-wireless" ] || continue
  echo "$(nmcli -g connection.autoconnect-priority connection show "$name") $name"
done | sort -rn | cut -d' ' -f2- | while IFS= read -r name; do
  ssid=$(nmcli -g 802-11-wireless.ssid connection show "$name")
  if printf '%s\n' "$visible" | grep -qxF "$ssid"; then
    log "offline ${down}s: '$ssid' is in range, connecting"
    nmcli connection up "$name" >/dev/null 2>&1 && { log "joined '$ssid'"; exit 0; }
    log "couldn't join '$ssid'"
  fi
done
nmcli -t -f TYPE,STATE device | grep -qE '^wifi:connected$' && exit 0

# 2. the radio, off and on
if [ "$down" -ge 600 ] && [ "$(ago radio)" -ge 600 ]; then
  log "offline ${down}s, no saved network joined: switching the Wi-Fi radio off and on"
  echo "$now" > "$STATE/radio"
  nmcli radio wifi off; sleep 3; nmcli radio wifi on
  exit 0
fi

# 3. the driver (the software equivalent of a power cycle for the Wi-Fi chip)
if [ "$down" -ge 1200 ] && [ "$(ago driver)" -ge 1200 ] && lsmod | grep -q '^brcmfmac'; then
  log "offline ${down}s: reloading the Wi-Fi driver (brcmfmac)"
  echo "$now" > "$STATE/driver"
  modprobe -r brcmfmac && sleep 2 && modprobe brcmfmac
fi
