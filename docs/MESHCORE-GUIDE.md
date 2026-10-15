# Running a MeshCore station

Lorakeet started out logging [Meshtastic](https://meshtastic.org) meshes. It can now log a
[MeshCore](https://github.com/meshcore-dev/MeshCore) mesh too: plug a MeshCore companion radio into a computer and
Lorakeet records everything it hears, the same way it does for a Meshtastic radio.

> **New in 0.5.0.** It works, but it has seen far less real traffic than the
> Meshtastic side, so expect rough edges and please [report what you find](#something-wrong).

**A MeshCore station only listens.** Lorakeet never makes a MeshCore radio transmit: what goes to the radio
(asking who it is, its counters, the messages it holds) travels over the USB cable, never over the air.

MeshCore and Meshtastic can't hear each other, so one radio logs one or the other. To log both, plug in one radio
of each kind: see [Two radios in one Lorakeet](#two-radios-in-one-lorakeet).

## What you need

- **A MeshCore companion radio running the USB companion firmware**, connected to the computer by a USB cable that
  carries data. Tested on MeshCore Companion USB 1.17.x, on a Heltec V4 (ESP32-S3) and a Heltec Mesh Node T1 (nRF52).
  Bluetooth and Wi-Fi companion firmware aren't supported: it has to be the USB one.
- **The radio already set up for your mesh.** Its frequency, bandwidth, spreading factor, name and so on are set
  with MeshCore's own tools (the MeshCore app or its configuration tool), not with Lorakeet. Ask your local
  MeshCore community which settings they use.
- **Lorakeet with its optional `meshcore` Python package.** It isn't installed by default, since only MeshCore
  stations need it.

## Setting it up

### When installing

The installer asks whether you want MeshCore support and installs the `meshcore` package if you do. To answer up
front:

- **Windows:** run `install.ps1 -MeshCore`.
- **Mac, Linux or a Raspberry Pi:** run `./install.sh --meshcore`.

Already installed without it? Add the package with Lorakeet's own Python, from the Lorakeet folder:

```sh
venv/bin/python -m pip install -r requirements-meshcore.txt          # Mac, Linux, Raspberry Pi
venv\Scripts\python -m pip install -r requirements-meshcore.txt      # Windows
```

### Choosing the radio

The **setup page** that opens on first run has a radio type choice: **Meshtastic** or **MeshCore**. Pick MeshCore
and choose the radio's port.

Or set it by hand in `lorakeet.toml` (copy `lorakeet.example.toml` if you don't have one yet), then restart
Lorakeet:

```toml
[radio]
protocol = "meshcore"
port = "COM7"            # Windows; on Linux, e.g. "/dev/serial/by-id/usb-Espressif_..."
```

A MeshCore radio is never auto-detected: `port` must be set. On Linux, use the radio's permanent name under
`/dev/serial/by-id/` rather than `/dev/ttyACM0`, so it can't swap places with another USB device after a restart
(`ls /dev/serial/by-id/` lists them).

The station takes its id from the radio's own public key: `mc:` and 12 hex digits, such as `mc:a0000017c2be`.
Meshtastic radios keep their usual ids, such as `!a0000001`.

## Two radios in one Lorakeet

One Lorakeet can log several radios at once, say a Meshtastic radio and a MeshCore one in the same box. `[radio]`
is the main radio (the live map and the Radio page are its); each extra radio gets a `[[radios]]` entry after it:

```toml
[radio]
port = "/dev/serial/by-id/usb-Heltec_..."           # the Meshtastic radio

[[radios]]
protocol = "meshcore"
port = "/dev/serial/by-id/usb-Espressif_..."        # required, and different from every other radio's
name = "Garage (MeshCore)"                          # optional: the station's name
sync_token = "..."                                  # only if this Lorakeet syncs to a hub
```

- **Each radio is its own station,** with its own id, name and logs, in the same database.
- **With a hub, each radio needs its own pairing.** Make a pairing code for it on the hub's Stations page and put
  its token in that entry's `sync_token`.
- **Radios start 20 seconds apart.** Opening a radio's port restarts it, and radios booting together can draw more
  power than a shared USB supply allows (a Raspberry Pi can cut every port at once). So the second radio connects
  a little after the first: that's expected.

## What's logged and where to see it

- **Every reception,** duplicates included: the packet's route, the path of repeaters it came through, its signal
  (SNR and RSSI) and its packet hash (the same for every copy of one transmission). See them in the packet browser
  under **MeshCore receptions**, where you can filter by payload type or by a repeater the packet came through.
  A repeater in a path is a short prefix of its public key, so a 1-byte one can match more than one radio.
- **The first hearing of each packet** in **Packets**, tagged **MeshCore** (ADVERT, GRP_TXT and so on; stored as
  `MC_ADVERT`, `MC_GRP_TXT`, ...). The **Network** filter shows Meshtastic, MeshCore or both.
- **Adverts:** MeshCore radios announce their name, type (chat, repeater, room or sensor) and, if set, their
  place. Lorakeet puts them in the radio list and on the map. When the database holds both kinds of radio, the map
  has a **Meshtastic / MeshCore / Both** switch, and MeshCore radios are tagged **MC**.
- **Messages:** channel messages the radio can read (on channels it has set up) appear in **Messages**, as do direct
  messages to the radio itself.
- **The radio's own health:** its battery, uptime, noise floor and packet counters are recorded about every five
  minutes.
- **Packet anatomy:** open a MeshCore packet's anatomy to see it byte by byte, straight from the bytes the radio
  received: header, path, payload, packet hash, and every hearing of it. **Public** channel messages are decrypted
  (its key is published); for a **hashtag channel**, type the channel's name to try it. A decryption is shown only
  when the message's MAC proves the key right, never as a guess.

## Not supported yet

- **Sending** messages or traces from a MeshCore station.
- **Changing radio settings** from the Radio page: for a MeshCore radio it's read-only. Use MeshCore's tools (and
  let Lorakeet's logging pause while they hold the radio).
- **Airtime** figures for MeshCore stations.
- **Station comparison** between MeshCore stations (it compares Meshtastic stations).
- **The network graph and traffic replay built from MeshCore paths.** Planned.
- **Remote commands** (the `[remote]` settings are Meshtastic-only).

### Something wrong?

Please open an issue at <https://github.com/darthnut/lorakeet/issues>. **Settings → Help and diagnostics** saves
a file for the report with personal details taken out.

## Privacy when sharing with a peer hub

A peer hub gets MeshCore data under the same share levels as Meshtastic data:

- **Locations are rounded,** including the places in MeshCore adverts, unless you chose to share exact locations
  with that peer.
- **Raw packet bytes are withheld unless the share allows them.** A MeshCore packet's own bytes can hold an exact
  location (an advert) or a channel message anyone with the channel's key could read (the Public channel's key is
  well known). So unless the share level includes all message text, a peer gets receptions without their bytes,
  apart from adverts, and an advert's bytes only go to a peer allowed exact locations.

## Troubleshooting

**"the meshcore package isn't installed"** in Lorakeet's log: the optional package is missing.
Install it with the pip command in [Setting it up](#when-installing) and restart Lorakeet. A message that "this
meshcore package doesn't run on this Python" means a version that needs a newer Python: the same command fixes it.

**The radio isn't found.** Check that `port` names the right port (Device Manager on Windows, `ls /dev/serial/by-id/`
on Linux) and that the cable carries data. A set port is used strictly: if it disappears, Lorakeet waits for it
rather than trying another one. On Linux, make sure you're in the `dialout` group (see the README). Only one program
can use the port at a time, so close MeshCore's configuration tool when you're done with it.

**The radio stopped answering.** On boards with an Espressif (ESP32) USB chip, Lorakeet notices a radio that stays
silent through several connection attempts and restarts it automatically, at most every ten minutes. It's logged,
and nothing on the radio is changed.

**Connected, but nothing is heard.** Usually there's no MeshCore repeater in range. MeshCore traffic is much
sparser than Meshtastic's: radios advertise rarely, and repeaters do most of the talking. Check that the radio's
frequency settings match your local mesh, give it time, and try a higher spot or a better antenna.
