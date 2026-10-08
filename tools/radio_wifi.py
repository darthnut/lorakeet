"""Put a Wi-Fi capable Meshtastic radio (ESP32 boards) on your network, then report its address, so Lorakeet
can connect to it with [radio] host. Asks for the network name and password: the password isn't echoed,
stored in your shell history, or printed.

    python tools/radio_wifi.py --port COM7

Stop Lorakeet first (one program at a time can use the USB port). Note: on ESP32 radios, turning Wi-Fi on
turns Bluetooth off, so the phone app connects over Wi-Fi instead.
"""
import argparse
import getpass
import re
import time

from pubsub import pub
from meshtastic.serial_interface import SerialInterface

ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
ap.add_argument("--port", required=True, help="the radio's serial port, e.g. COM7 or /dev/ttyACM0")
a = ap.parse_args()

ssid = input("Wi-Fi network name: ").strip()
psk = getpass.getpass("Wi-Fi password (not shown): ")
if not ssid:
    raise SystemExit("no network name given")

i = SerialInterface(a.port)
try:
    net = i.localNode.localConfig.network
    net.wifi_enabled, net.wifi_ssid, net.wifi_psk = True, ssid, psk
    i.localNode.writeConfig("network")
    print("saved; the radio restarts and joins the network…")
    time.sleep(2)
finally:
    i.close()
psk = None

# Watch the radio's log for its new address (it logs it once Wi-Fi connects).
found = []
ipre = re.compile(r"\b(\d{1,3}(?:\.\d{1,3}){3})\b")
def on_line(line, interface):  # noqa: ARG001
    if re.search(r"ip|wifi|connected", line, re.I):
        for ip in ipre.findall(line):
            if not ip.startswith(("0.", "255.")) and ip not in found:
                found.append(ip)
pub.subscribe(on_line, "meshtastic.log.line")
time.sleep(20)
for attempt in range(3):
    try:
        i = SerialInterface(a.port)
        break
    except Exception:  # noqa: BLE001 - still rebooting
        time.sleep(10)
else:
    raise SystemExit("couldn't reconnect to the radio to read its address; check your router's device list")
try:
    t0 = time.time()
    while time.time() - t0 < 60 and not found:
        time.sleep(1)
finally:
    i.close()
if found:
    print(f"the radio's address: {found[-1]}   (Lorakeet: [radio] host = \"{found[-1]}\")")
else:
    print("saved, but the radio didn't report an address within a minute. Check the network name and password,\n"
          "or look for a device named Meshtastic_xxxx in your router's device list.")
