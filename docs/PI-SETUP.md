# Setting up a listening station (Raspberry Pi collector)

A **station** is a radio plus a small computer that logs what it hears. One machine is the **hub** (it keeps
the combined database and serves the dashboard); every other station is a **collector** that logs locally
and syncs to the hub whenever it can reach it. This guide sets up a Raspberry Pi collector.

> **Shortcut:** after flashing the card (step 3.1) and copying the code to `~/lorakeet`, run
> `bash deploy/setup-station.sh` on the Pi. It does steps 3.2-3.6 (packages, venv, serial access, a
> `lorakeet.toml` from `deploy/station.example.toml`, Tailscale, the service), is safe to re-run, and
> stops for the two human steps (passwordless sudo, approving Tailscale). To push new code to a running
> station later, from the hub: `bash deploy/update-station.sh lorakeet@<station>`. It stages the code,
> refuses to deploy anything that doesn't parse, and keeps `lorakeet.toml` and the venv.

Set everything up at home first and run it beside your hub's radio for a day before taking it anywhere.

**Hardware that works:** a Raspberry Pi 3 or 4 with a proper power supply (5 V / 2.5-3 A), a microSD card
(16 GB is plenty), and a Meshtastic radio on USB (tested: Heltec V4, Heltec Mesh Node T1, SenseCAP
T1000-E). For an outdoor station: a weatherproof box, an SMA pigtail and a 915/868 MHz antenna mounted
outside or near a window.

## 1. The hub, once

1. **Install Tailscale** (tailscale.com) on the hub and sign in. It gives your stations a private network
   to reach the hub from anywhere, without opening ports to the internet. Note the hub's Tailscale name
   (e.g. `home-pc`).
2. **Generate a token:** `python -c "import secrets; print(secrets.token_urlsafe(24))"`
3. **Add it to the hub's `lorakeet.toml`** (git-ignored; keep it out of version control):
   ```toml
   [sync]
   mode = "hub"
   token = "<the token>"
   ```
   Restart the server. It now accepts collectors on `/api/ingest`, only with the token, only from
   Tailscale addresses. For one secret per station, see `sync.station_tokens` in `lorakeet.example.toml`.
4. **Firewall:** allow inbound TCP 5190 on the Tailscale network (on Windows, check which network profile
   the Tailscale adapter has with `Get-NetConnectionProfile`).

## 2. The radio

With an antenna attached (never power a LoRa radio without one):

1. Check it's the right band for your region (915 MHz in the US, 868 MHz in Europe).
2. Flash the **same firmware version as your other stations**, so the debug-log format matches what
   Lorakeet parses (see the supported versions in the README).
3. Configure it: your region and preset, role **CLIENT_MUTE** (listen-only: it won't rebroadcast),
   `security.debug_log_api_enabled = true` (per-reception detail comes from the debug log), and a clear
   name. Wait for the reboot after each config write, and **save the full config before changing
   anything** (`meshtastic --export-config > backup.yaml`; it contains keys, so keep it private).
4. Note its node id (`!xxxxxxxx`): that's the station id the hub will show.

## 3. The Pi

1. **Raspberry Pi Imager** → Raspberry Pi OS **Lite (64-bit)**. In its settings: a hostname
   (e.g. `lorakeet-station`), user `lorakeet`, SSH on, and your Wi-Fi. Python 3.11 or newer.
2. Boot, then: `ssh lorakeet@lorakeet-station.local`
   ```sh
   sudo apt update && sudo apt full-upgrade -y
   sudo apt install -y git python3-venv
   ```
3. **Tailscale:** `curl -fsSL https://tailscale.com/install.sh | sh`, then `sudo tailscale up --ssh` and
   approve it in the Tailscale admin page. From then on it's reachable as `ssh lorakeet@lorakeet-station`
   from anywhere.
4. **The code:** clone the repository to `/home/lorakeet/lorakeet`, then
   ```sh
   cd ~/lorakeet && python3 -m venv venv && venv/bin/pip install -r requirements.txt
   ```
5. **Its `lorakeet.toml`** (start from `deploy/station.example.toml`):
   ```toml
   [sync]
   mode = "collector"
   hub_url = "http://home-pc:5190"     # the hub's Tailscale name
   token = "<the same token>"

   [station]
   location = [47.60620, -122.33210]   # the antenna's position, used on the maps

   [http]
   lan = "off"        # "view" for a read-only dashboard on the local Wi-Fi
   ```
   Data goes to `~/.local/share/lorakeet`. The radio is auto-detected on USB.
6. **Run it as a service:**
   ```sh
   sudo cp deploy/lorakeet.service /etc/systemd/system/
   sudo systemctl daemon-reload && sudo systemctl enable --now lorakeet
   journalctl -u lorakeet -f
   ```
   Look for `connected: !xxxxxxxx` and `sync: collector`.

## 4. Check it at home

- Hub: `http://127.0.0.1:5190/api/sync` lists the new station with a recent `last` and growing `rows`;
  `/api/stations` lists every station.
- Collector: `curl localhost:5190/api/sync` on the Pi shows `lastOk` recent and an empty `backlog`.
- Unplug the Pi's network for ten minutes: the hub raises "Station … isn't syncing" after 30 minutes
  (setting `stationSilenceMin`), the backlog grows, then drains when it's back.
- Stations side by side should hear mostly the same packets: a good check that the data lines up.

## 5. Move it

1. Add the new site's Wi-Fi: `sudo nmcli --ask dev wifi connect "<SSID>"` (prompts for the password).
2. Mount the antenna as high as practical; keep a sealed box out of direct sun.
3. Power it up; it starts by itself. Watch `/api/sync` on the hub until the first batches arrive.

## Updating it later

From the hub: `bash deploy/update-station.sh lorakeet@<station>`. Database migrations run on start.

## Lessons from the first stations

- **Power:** a computer's USB port can't run a Pi 3 (under-voltage under any load). With a proper supply,
  `vcgencmd get_throttled` stays `0x0` with the radio attached. The dashboard's station table shows it.
- **Raspberry Pi Imager users don't get passwordless sudo.** The setup script needs it; one way, run by
  you: `/etc/sudoers.d/010-lorakeet-nopasswd` containing `lorakeet ALL=(ALL) NOPASSWD: ALL`, checked with
  `sudo visudo -cf` before saving.
- **A SenseCAP T1000-E shows up as `239a:8029`** (Adafruit's nRF52 USB id) on `/dev/ttyACM0`; its magnetic
  cable carries data.
- **Old firmware with a current Python library may not report its configuration** (every section empty),
  and writing settings on top of that can reset them. Update the firmware first and always read the full
  config back before writing. nRF52 radios update without a button press: `meshtastic --enter-dfu`, then
  copy the `.uf2` file onto the USB drive that appears.
- **A Pi has no battery-backed clock.** Booted without network, it stamps rows with the last shutdown time
  until it syncs. A mobile station (`[station] mobile = true`) sets its clock from the radio's GPS when NTP
  isn't available.
