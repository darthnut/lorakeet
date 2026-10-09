# Third-party notices

Lorakeet is licensed under the GNU General Public License v3.0 (see `LICENSE`). It uses the following
components, which keep their own licenses.

## Python packages (installed with `pip install -r requirements.txt`)

| Component | License | Source |
|---|---|---|
| Meshtastic Python library (`meshtastic`) and its dependencies (protobuf, pyserial, pypubsub, bleak, …) | GPL-3.0 (meshtastic); see each package | https://github.com/meshtastic/python |
| cryptography (AES for packet anatomy) | Apache-2.0 or BSD-3-Clause | https://github.com/pyca/cryptography |
| pystray (the Windows tray icon) | LGPL-3.0 | https://github.com/moses-palmer/pystray |
| Pillow (the tray icon's image) | MIT-CMU (HPND) | https://python-pillow.org |
| Segno (QR codes for sharing a channel with the Meshtastic app) | BSD-3-Clause, © Lars Heuer | https://github.com/heuer/segno |

## Loaded by the web pages from cdnjs.cloudflare.com

| Component | Version | License | Source |
|---|---|---|---|
| Leaflet | 1.9.4 | BSD-2-Clause | https://leafletjs.com |
| D3 | 7.9.0 | ISC | https://d3js.org |

## Data

- `weak_keys.py` is generated from `LOW_ENTROPY_HASHES` in the Meshtastic firmware's `src/mesh/NodeDB.h`
  (GPL-3.0, https://github.com/meshtastic/firmware).
- Protocol knowledge (packet header layout, channel hashing, encryption nonce) follows the Meshtastic
  firmware and protobuf definitions (GPL-3.0).

## Map tiles (fetched by your browser at run time)

- **OpenStreetMap** (default): map data © OpenStreetMap contributors, ODbL; tiles under the OSMF tile
  usage policy (https://operations.osmfoundation.org/policies/tiles/).
- **OpenTopoMap** (default terrain view): © OpenTopoMap, CC-BY-SA; data © OpenStreetMap contributors, SRTM.
- **Esri** (only with `[map] tiles = "esri"`): © Esri and its data providers, under Esri's terms of use.
