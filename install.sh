#!/usr/bin/env bash
# Lorakeet installer for Linux, macOS and Raspberry Pi. Run it from the Lorakeet folder:
#   ./install.sh               # set up (or update) the Python environment, plus a "Lorakeet" launcher
#   ./install.sh --autostart   # ...and run Lorakeet as a system service (Linux with systemd; asks for sudo)
#   ./install.sh --no-shortcut # without the launcher
#   ./install.sh --uninstall   # remove the launcher, the service and the venv (your logged data is kept)
#
# It checks for Python 3.11+, creates the virtual environment in ./venv and installs the requirements.
# Safe to run again after updating (git pull): it only installs what changed.
# The launcher (an app-menu entry on a Linux desktop, Lorakeet.command on a Mac's Desktop) runs lorakeet.pyw:
# it starts Lorakeet if it isn't running and opens it in the browser.
# --autostart writes /etc/systemd/system/lorakeet.service for this user and folder (starts at boot, restarts
# if it stops), adds this user to the group that may open the radio's serial port, and starts it.
# A dedicated listening station (a Pi in a box or a car) is better served by deploy/setup-station.sh, which
# also adds a Wi-Fi watchdog and keeps the system log across reboots: see docs/PI-SETUP.md.
set -eu
cd "$(dirname "$0")"
here=$(pwd)
autostart=0; shortcut=1; uninstall=0
desktop_entry="${XDG_DATA_HOME:-$HOME/.local/share}/applications/lorakeet.desktop"
mac_command="$HOME/Desktop/Lorakeet.command"
for a in "$@"; do
  case "$a" in
    --autostart) autostart=1 ;;
    --no-shortcut) shortcut=0 ;;
    --uninstall) uninstall=1 ;;
    -h|--help) sed -n '2,16p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown option: $a (try --help)"; exit 2 ;;
  esac
done

if [ "$uninstall" = 1 ]; then
  echo "Uninstalling Lorakeet from $here"
  data=""
  [ -x venv/bin/python ] && data=$(venv/bin/python -c "from config import CFG; print(CFG['storage']['data_dir'])" 2>/dev/null || true)
  if [ -f /etc/systemd/system/lorakeet.service ] && grep -qxF "WorkingDirectory=$here" /etc/systemd/system/lorakeet.service; then
    sudo systemctl disable --now lorakeet && sudo rm /etc/systemd/system/lorakeet.service && sudo systemctl daemon-reload
    echo "Removed the lorakeet service."
  fi
  # only this folder's server: compare each candidate's full command line literally (pkill -f would treat the
  # path as a pattern, and match sibling folders)
  for pid in $(pgrep -f "server.py" 2>/dev/null); do
    case "$(ps -o args= -p "$pid" 2>/dev/null)" in
      *"$here/server.py"|*"$here/server.py "*) kill "$pid" 2>/dev/null && echo "Stopped Lorakeet." ;;
    esac
  done
  for f in "$desktop_entry" "$mac_command"; do
    [ -f "$f" ] && { grep -qF "\"$here/" "$f" || grep -qF "\"$here\"" "$f"; } && rm -f "$f" && echo "Removed $f"
  done
  rm -rf venv && echo "Removed the Python environment (venv)."
  [ -n "$data" ] && [ -d "$data" ] && echo "Your logged data is still in $data (delete it yourself if you want it gone)."
  echo "Done. The program files and your settings (lorakeet.toml, if any) are in $here: delete that folder to finish."
  exit 0
fi

if [ ! -x venv/bin/python ]; then
  py=""
  # 3.11 to 3.14: the meshtastic library doesn't support 3.15 yet
  for c in python3.14 python3.13 python3.12 python3.11 python3 python; do
    command -v "$c" >/dev/null 2>&1 || continue
    if "$c" -c 'import sys; sys.exit(0 if (3, 11) <= sys.version_info[:2] <= (3, 14) else 1)' 2>/dev/null; then py=$c; break; fi
  done
  if [ -z "$py" ]; then
    echo "Python 3.11 to 3.14 is needed (found: $(python3 --version 2>&1 || echo none))."
    echo "On Debian, Ubuntu or Raspberry Pi OS: sudo apt install python3 python3-venv"
    exit 1
  fi
  echo "Creating the virtual environment with $($py --version)..."
  if ! "$py" -m venv venv; then
    rm -rf venv
    echo "Couldn't create the virtual environment. On Debian, Ubuntu or Raspberry Pi OS: sudo apt install python3-venv"
    exit 1
  fi
fi
echo "Installing the requirements..."
venv/bin/python -m pip install --disable-pip-version-check --prefer-binary -q -r requirements.txt
version=$(venv/bin/python server.py --version)
echo "$version is installed."

if [ "$shortcut" = 1 ]; then
  if [ "$(uname)" = Darwin ]; then
    printf '#!/bin/sh\n# Opens Lorakeet (starts it if it is not running). Written by install.sh.\ncd "%s" && exec venv/bin/python lorakeet.pyw\n' "$here" > "$mac_command"
    chmod +x "$mac_command"
    echo "Added Lorakeet.command to your Desktop: double-click it to open Lorakeet."
  elif [ -n "${DISPLAY:-}${WAYLAND_DISPLAY:-}" ]; then
    mkdir -p "$(dirname "$desktop_entry")"
    cat > "$desktop_entry" <<EOF
[Desktop Entry]
Type=Application
Name=Lorakeet
Comment=Meshtastic logger and dashboard (starts it if it isn't running)
Exec="$here/venv/bin/python" "$here/lorakeet.pyw"
Path=$here
Icon=$here/static/icon-512.png
Terminal=false
Categories=Network;HamRadio;
EOF
    echo "Added Lorakeet to your applications menu."
  fi
fi

if [ "$(uname)" = Linux ] && ! id -nG | grep -qwE 'dialout|uucp'; then
  echo "  For a USB radio, your user needs serial-port access: sudo usermod -aG dialout \$USER (uucp on Arch),"
  echo "  then log out and back in. (--autostart does this for the service.)"
fi
if [ "$autostart" = 1 ]; then
  if ! command -v systemctl >/dev/null 2>&1; then
    echo "--autostart needs systemd (Linux). On macOS, run: venv/bin/python server.py"
    exit 1
  fi
  user=$(id -un)
  # the radio's serial port belongs to "dialout" on Debian-family systems ("uucp" on Arch)
  group=dialout; getent group dialout >/dev/null || group=uucp
  echo "Setting up the lorakeet service for $user (sudo)..."
  sudo tee /etc/systemd/system/lorakeet.service >/dev/null <<EOF
# Written by install.sh. Follow the log with: journalctl -u lorakeet -f
[Unit]
Description=Lorakeet Meshtastic logger
After=network-online.target
Wants=network-online.target
StartLimitIntervalSec=300
StartLimitBurst=10

[Service]
User=$user
SupplementaryGroups=$group
WorkingDirectory=$here
ExecStart="$here/venv/bin/python" -u server.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF
  id -nG "$user" | tr ' ' '\n' | grep -qx "$group" || sudo usermod -aG "$group" "$user"
  sudo systemctl daemon-reload
  sudo systemctl enable --now lorakeet
  echo "Lorakeet runs as a service now and starts at boot (journalctl -u lorakeet -f to follow it)."
fi

echo
echo "Next:"
[ "$autostart" = 1 ] || echo "  Run it:   venv/bin/python lorakeet.pyw   (starts it and opens the browser; or ./install.sh --autostart to keep it running)"
echo "  Open      http://127.0.0.1:5190   (the first time, a setup page asks a few questions)"
echo "  Optional: venv/bin/python server.py --set-password, so other devices on your network can log in"
echo "  Uninstall: ./install.sh --uninstall"
