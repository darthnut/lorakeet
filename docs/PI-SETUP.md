# Setting up a listening station (Raspberry Pi)

A **station** is a radio plus a small computer that logs what it hears. One computer is the **hub**: it keeps the
combined database and serves the dashboard. Every other station logs on its own and sends what it logged to the
hub whenever it can reach it, catching up after any outage, so nothing is lost. This guide sets up a Raspberry Pi
station; any computer running Lorakeet can be one.

You don't need a station to use Lorakeet: one radio on your own computer is a complete setup. Stations are for
hearing more of the mesh (a Pi in the attic, at a friend's house, in a car) and comparing what each place hears.

Set everything up at home first and run it beside your hub's radio for a day before taking it anywhere.

**Hardware that works:** a Raspberry Pi 3 or 4 with a proper power supply (5 V / 2.5-3 A), a microSD card (16 GB
is plenty: a station keeps its own copy, a few MB a day), and a Meshtastic radio on USB (tested: Heltec V4,
Heltec Mesh Node T1, SenseCAP T1000-E). For an outdoor station: a weatherproof box, an SMA pigtail and a
915/868 MHz antenna mounted outside or near a window.

## 1. The hub, once

On the computer that will collect everything (usually the one you already run Lorakeet on):

1. Open **Stations** in the top bar → **Make this the hub**, choosing where stations will connect from:
   - **This computer's local network**: stations at home, on the same Wi-Fi or router.
   - **Tailscale**: stations somewhere else. [Tailscale](https://tailscale.com) is a free private network between
     your own devices; install it on the hub and on each station, signed in to the same account. Nothing is opened
     to the internet.

   Restart Lorakeet when the page asks (with a button, when it runs in the background or as a service). The page then lists the addresses stations
   will use.
2. **Windows firewall:** if a station can't connect, allow Python (or TCP port 5190) on **Private** networks in
   Windows Security → Firewall & network protection → Allow an app through firewall. For Tailscale, check that its
   network counts as private (`Get-NetConnectionProfile` in PowerShell).
3. **Add a station** → give it a name ("Garage Pi") → **Make a pairing code**. The code works for one station, is
   shown only once (make another if you lose it), and can be revoked any time. Keep it for step 3.

## 2. The radio

With an antenna attached (never power a LoRa radio without one):

1. Check it's the right band for your region (915 MHz in the US, 868 MHz in Europe).
2. Flash the **same firmware version as your other stations** with the
   [Meshtastic web flasher](https://flasher.meshtastic.org), so the debug-log format matches what Lorakeet parses
   (see the supported versions in the README).
3. **Its settings come after step 3**, on the station's own **Radio** page (see the end of step 3): region, role
   **Client, mute** (listens without relaying), per-reception detail on, and a clear name. It explains each one
   and backs up the radio's settings before changing anything.

   Or with the Meshtastic CLI, before plugging it into the Pi: save the config first
   (`meshtastic --export-config > backup.yaml`; it holds keys, so keep it private), then set the region and preset,
   `device.role CLIENT_MUTE` and `security.debug_log_api_enabled true`. Wait for the reboot after each write.

## 3. The Pi

1. **Raspberry Pi Imager** → Raspberry Pi OS **Lite (64-bit)**. In its settings: a hostname (e.g.
   `lorakeet-station`), user `lorakeet`, SSH on, and your Wi-Fi.
2. Boot it, then from your computer: `ssh lorakeet@lorakeet-station.local`
   ```sh
   sudo apt update && sudo apt full-upgrade -y
   sudo apt install -y git python3-venv
   git clone https://github.com/darthnut/lorakeet.git ~/lorakeet
   ```
3. **Passwordless sudo**, if needed: the setup script needs it, and checks first (Raspberry Pi Imager's user doesn't
   have it). If the script stops and asks for it, run this once yourself (it asks for your password), then run the
   script again:
   ```sh
   echo "$USER ALL=(ALL) NOPASSWD: ALL" | sudo tee /etc/sudoers.d/010-$USER-nopasswd && sudo chmod 440 /etc/sudoers.d/010-$USER-nopasswd && sudo visudo -cf /etc/sudoers.d/010-$USER-nopasswd
   ```
4. **Run the setup script** and paste the pairing code when it asks:
   ```sh
   cd ~/lorakeet && bash deploy/setup-station.sh
   ```
   It installs what Lorakeet needs, gives your user access to the USB radio, writes a `lorakeet.toml`, asks whether
   to install Tailscale (only needed if the hub is at another place), **tests the pairing code against the hub and
   saves it**, keeps the system log across reboots, adds a Wi-Fi watchdog, and starts Lorakeet as a service that
   runs at boot. Safe to re-run. Skipped the code, or it couldn't reach the hub? Pair later with
   ```sh
   venv/bin/python server.py --join -        # paste the code; it's tested before it's saved
   sudo systemctl restart lorakeet
   ```
   (`--force` saves a code even when the hub can't be reached right now.)
5. **Name it and place its antenna.** On the hub's Stations page: **Rename** and **Set location** next to the
   station (the antenna's position puts it on the maps and helps place radios that never share theirs). Or on the
   Pi, in `lorakeet.toml`:
   ```toml
   [station]
   name = "Garage"
   location = [47.60620, -122.33210]   # the ANTENNA's latitude, longitude
   ```
   then `sudo systemctl restart lorakeet`.

**The station's own Radio page.** The Radio and setup pages only answer on the station's own computer, which has
no screen. Reach them through SSH from your computer:
```sh
ssh -L 5199:127.0.0.1:5190 lorakeet@lorakeet-station.local
```
then open http://127.0.0.1:5199/radio.html in your browser (with Tailscale, `lorakeet-station` without `.local`
works from anywhere), and work through it (settings, channels, and the check
that it's logging). Close the SSH window when you're done.

## 4. Check it at home

- **Hub, Stations page:** the station appears within a minute, with its last contact, Lorakeet version, backlog
  (rows logged but not sent yet) and location. Analytics → Listening stations compares what each one hears, and
  every page's station picker shows one station or all of them combined.
- **On the Pi:** `journalctl -u lorakeet -f` shows `connected: !xxxxxxxx` and `sync: collector`;
  `curl -s localhost:5190/api/sync` shows `lastOk` recent and a backlog of 0.
- **Outage test:** unplug the Pi's network for ten minutes. The backlog grows, then drains when it's back; after
  30 minutes of silence the hub raises "Station … isn't syncing" (Settings: `stationSilenceMin`).
- Stations side by side should hear mostly the same packets: a good check that the data lines up.

## 5. Move it

1. Add the new site's Wi-Fi: `sudo nmcli --ask dev wifi connect "<SSID>"` (prompts for the password).
2. Mount the antenna as high as practical; keep a sealed box out of direct sun.
3. Power it up; it starts by itself. If the hub is somewhere else, both need Tailscale (step 1). Watch the hub's
   Stations page until its last contact is recent.

## Managing stations

On the hub's Stations page, next to each station:

- **Rename / Set location**: the hub's own setting, which wins over what the station reports.
- **Revoke**: stops it sending. Its code no longer works; make a new code to pair it again.
- **Forget**: removes it from the list and the maps. Everything it logged stays in the database.

A station whose radio is updated to **firmware 2.8** may get a new node number; if it stops syncing afterwards,
revoke it and pair it again with a new code.

## Updating it later

From the hub (a Linux or macOS shell, or Git Bash on Windows, with SSH access to the station):
`bash deploy/update-station.sh lorakeet@<station>`. It stages the code, refuses to deploy anything that doesn't
parse, installs new requirements, and keeps `lorakeet.toml`, the data and the venv. Database changes are applied
when it starts. Or on the Pi: `git pull && ./install.sh && sudo systemctl restart lorakeet`.

## Lessons from the first stations

- **Power:** a computer's USB port can't run a Pi 3 (under-voltage under any load). With a proper supply,
  `vcgencmd get_throttled` stays `0x0` with the radio attached. The hub's Analytics → Listening stations shows it.
- **A SenseCAP T1000-E shows up as `239a:8029`** (Adafruit's nRF52 USB id) on `/dev/ttyACM0`; its magnetic cable
  carries data.
- **Old firmware with a current Python library may not report its configuration** (every section empty), and
  writing settings on top of that can reset them. The Radio page refuses to change anything then. Update the
  firmware first. nRF52 radios update without a button press: `meshtastic --enter-dfu`, then copy the `.uf2` file
  onto the USB drive that appears.
- **A Pi has no battery-backed clock.** Booted without network, it stamps rows with the last shutdown time until
  it syncs. A mobile station (`[station] mobile = true`) sets its clock from the radio's GPS when NTP isn't
  available.

## A station in a car

- **Settings:** `[station] mobile = true` (location then comes from the radio's GPS) and, for test drives,
  `drive_pings = true` (a tiny message every kilometre on the private channel named in `drive_ping_channel`,
  "Lorakeet" by default, which you can make on the Radio page; it transmits). On the radio, a short
  `position.gps_update_interval` (15-30 s rather than the default 120 s) keeps positions current at speed; the
  Radio page suggests it for a mobile station. Put it back afterwards if the radio runs on its own battery.
- **Internet on the road:** a phone hotspot, with Tailscale (the hub is elsewhere). A Pi 3 only does 2.4 GHz
  Wi-Fi, and a phone that is itself on a 5 GHz Wi-Fi network shares its hotspot on 5 GHz only: turn the phone's
  Wi-Fi off near the car. Without any internet nothing is lost; the station uploads when it's back on a known
  network.
- **The Wi-Fi watchdog** (installed by the setup script) rejoins saved networks by itself if the Wi-Fi wedges,
  which a Pi 3 did twice in one day after hours of searching for a hotspot that wasn't there.
  `journalctl -t lorakeet-wifi` shows what it did; the system log is kept across reboots.
- **Checking on it from the road:** with `[remote] enabled = true` and your handheld radio in `allow`, send the
  station's radio a direct message `lk status` (network, upload backlog, power, GPS) or `lk gps`.
- **Placement:** the radio on the dashboard or roof. Inside a cab it hears much less, and the GPS wanders.
- **Coverage:** the Visualizations page's Coverage view maps what the car heard along its route.

## Appendix: setting up sync by hand (the older way)

Before pairing codes, a hub and its stations shared one secret token, typed into both `lorakeet.toml` files. It
still works, and the Stations page lists such stations as "shared token" (pair them again to give each its own
revocable code).

- Hub: `[sync] mode = "hub"`, `token = "<16+ characters>"` (make one with
  `python -c "import secrets; print(secrets.token_urlsafe(24))"`), and `allow` = the networks stations connect from
  (default: Tailscale's). Per-station secrets: `station_tokens`, see `lorakeet.example.toml`.
- Station: `[sync] mode = "collector"`, `hub_url = "http://<hub's address>:5190"`, the same `token`.
