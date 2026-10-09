#!/usr/bin/env bash
# Turn a fresh Raspberry Pi OS Lite (64-bit) install into a listening station. Run ON the Pi, as the station
# user, from the code folder:   cd ~/lorakeet && bash deploy/setup-station.sh
# Safe to re-run: every step checks what's already done. See docs/PI-SETUP.md for the whole procedure.
# Two steps stay with a person: passwordless sudo (needs the user's password once, below) and approving
# the Tailscale sign-in link this prints.
set -euo pipefail
cd "$(dirname "$0")/.."
here="$PWD"

if ! sudo -n true 2>/dev/null; then
  echo "This needs passwordless sudo for $USER. Run this once (it asks for your password), then re-run:"
  echo "  echo '$USER ALL=(ALL) NOPASSWD: ALL' | sudo tee /etc/sudoers.d/010-$USER-nopasswd && sudo chmod 440 /etc/sudoers.d/010-$USER-nopasswd && sudo visudo -cf /etc/sudoers.d/010-$USER-nopasswd"
  exit 1
fi
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' || { echo "Python 3.11+ needed"; exit 1; }

echo "== packages"
dpkg -s git python3-venv >/dev/null 2>&1 || { sudo apt-get update -q && sudo apt-get install -y -q git python3-venv; }

echo "== Python environment"
[ -x venv/bin/python ] || python3 -m venv venv
venv/bin/pip install -q --upgrade pip
venv/bin/pip install -q -r requirements.txt

echo "== serial access"
id -nG | grep -qw dialout || sudo usermod -aG dialout "$USER"

echo "== config"
if [ ! -f lorakeet.toml ]; then
  cp deploy/station.example.toml lorakeet.toml && chmod 600 lorakeet.toml
  echo "   created lorakeet.toml: fill in [station] and [sync] (hub_url, token) before the station can sync"
fi

echo "== Tailscale (remote access + the link home)"
command -v tailscale >/dev/null || { curl -fsSL https://tailscale.com/install.sh -o /tmp/ts-install.sh && sudo sh /tmp/ts-install.sh; }
if ! tailscale ip -4 >/dev/null 2>&1; then
  echo "   open the link below to approve this station in your Tailscale account:"
  # no --ssh: the Pi's normal SSH server (your keys, one host key) answers over Tailscale too. Tailscale's own
  # SSH server presents a different host key and its default policy asks for browser re-approval, which
  # breaks unattended updates (update-station.sh).
  sudo tailscale up --hostname "$(hostname)"
fi

echo "== logs kept across reboots, and the Wi-Fi watchdog"
# the system log survives reboots (capped), so a station that went offline can say why afterwards
sudo mkdir -p /var/log/journal /etc/systemd/journald.conf.d
printf '[Journal]\nStorage=persistent\nSystemMaxUse=200M\n' | sudo tee /etc/systemd/journald.conf.d/lorakeet.conf >/dev/null
sudo systemd-tmpfiles --create --prefix /var/log/journal   # the folder needs journald's ownership
sudo systemctl restart systemd-journald
sudo journalctl --flush                                     # and what's in memory so far goes to disk
# a Pi's Wi-Fi can wedge after hours of failed scans and never rejoin until power-cycled: deploy/wifi-watchdog.sh
chmod +x deploy/wifi-watchdog.sh
sed "s#/home/lorakeet/lorakeet#$here#g" deploy/lorakeet-wifi-watchdog.service | sudo tee /etc/systemd/system/lorakeet-wifi-watchdog.service >/dev/null
sudo cp deploy/lorakeet-wifi-watchdog.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now lorakeet-wifi-watchdog.timer >/dev/null

echo "== service"
sed "s#/home/lorakeet/lorakeet#$here#g; s#^User=lorakeet#User=$USER#" deploy/lorakeet.service | sudo tee /etc/systemd/system/lorakeet.service >/dev/null
sudo systemctl daemon-reload
sudo systemctl enable --now lorakeet >/dev/null
sleep 20
echo "   service: $(systemctl is-active lorakeet)"
echo "   power: $(vcgencmd get_throttled 2>/dev/null || echo n/a)   (0x0 = good; anything else: a better power supply)"
journalctl -u lorakeet --no-pager -n 30 | grep -E "connected:|sync:|failed" | tail -3 || true
echo "Done. Check the hub's Analytics page: the station should appear under Listening stations."
