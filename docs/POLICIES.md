# Project policies

> **DRAFT for the maintainer's review.** Each point is marked **(practice)**, what the project already does, or
> **(proposal)**, a promise it doesn't make yet. Nothing here is final until this banner is gone.

## Version numbers

- **(practice)** Versions are `x.y.z`, in the `VERSION` file. Between releases it's `x.y.z-dev`, and CHANGELOG.md
  collects changes under `## Unreleased` (a test enforces both).
- **(practice)** While the version starts with `0.`, Lorakeet is a preview: releases are GitHub pre-releases, and
  anything can still change.
- **(proposal)** In 0.x, `y` goes up for releases with notable new features or anything that needs action when
  updating (re-running the installer, a setting to change), and `z` for fixes and small additions. From 1.0,
  [semantic versioning](https://semver.org): `x` for anything that breaks an existing setup.

## Your data

- **(practice)** Logged data is never deleted or rewritten by an update. The database's layout version is stored in
  the file (`SCHEMA_VERSION`); updates only ever add tables, columns and indexes.
- **(practice)** An older Lorakeet opening a newer database says so and keeps logging.
- **(practice)** Settings stored in the database (alerts, watched radios) win over `lorakeet.toml`'s defaults once
  they exist, so a changed default never changes a running install.

## Settings in lorakeet.toml

- **(practice)** An unknown setting is reported and ignored, never fatal; a wrong value stops Lorakeet with a message
  naming the setting.
- **(proposal)** A renamed or removed setting keeps working, with a warning saying what replaced it, for at least
  one `y` release, and the CHANGELOG says so.

## Several Lorakeets (stations, hubs, peers)

- **(practice)** Stations, hubs and peers talk through `/api/ingest` with a protocol number (currently 1).
- **(proposal)** A hub accepts stations and peers running the previous `y` release, so they don't all have to be
  updated on the same day. A protocol change that can't keep that promise is called out in the CHANGELOG.

## Meshtastic firmware

- **(practice)** Tested on firmware 2.7.26 (Heltec V4, Heltec Mesh Node T1, SenseCAP T1000-E). Firmware 2.8
  renumbering and the known 2.8.1 log change are handled; 2.8 isn't tested on a real radio yet.
- **(practice)** Per-reception logging depends on the firmware's debug-log format; if it stops recognising the
  lines, Lorakeet raises an alert instead of failing silently.
- **(proposal)** Supported: the current stable Meshtastic release line. The previous line works on a best-effort
  basis. Alpha firmware is not supported, though problems with it are welcome as issues.

## Python and operating systems

- **(practice)** Python 3.11 to 3.14 (3.15 once the Meshtastic library supports it). The installer never uses the
  Microsoft Store's Python.
- **(practice)** Tested on every push: Windows and Linux (x64), Python 3.11 and 3.12; the install scripts end to end
  on fresh Windows and Linux machines. **(proposal)** macOS (Apple silicon) and Windows on ARM on the public
  repository.
- **(proposal)** Supported: Windows 10 and 11, Linux including Raspberry Pi OS (64-bit), macOS on a best-effort
  basis (no autostart yet).

## Releases

- **(practice)** A release passes the tests on every platform above and `tools/release_check.py` (nothing personal
  in the repository), is tagged `vx.y.z`, and gets a GitHub release with the CHANGELOG's notes.
- **(practice)** Security fixes go into the latest release only (see [SECURITY.md](../SECURITY.md)).
