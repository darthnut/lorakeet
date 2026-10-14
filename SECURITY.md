# Security

## Reporting a problem

Please report security problems **privately**, not in a public issue: on this repository's **Security** tab,
choose **Report a vulnerability**. Only the maintainers see the report, and we can work out a fix together before
anything is public.

Include what you found, how to reproduce it, and which version you ran (`python server.py --version`, or the
tooltip on the logo). Leave out anything personal: your `lorakeet.toml`, real radio ids, keys, tokens, locations.

Lorakeet is a hobby project maintained on a best-effort basis, so there's no guaranteed response time, but security
reports come first. You'll be credited in the release notes unless you'd rather not be.

## What counts

Anything that lets someone do more than Lorakeet says they can, for example:

- reaching the dashboard's full access (sending, radio settings, keys, pairing) from another device without logging
  in, or from a website through your browser (cross-site requests, DNS rebinding);
- getting past the login, or its lockouts;
- a station, peer or pairing code being used to write data it shouldn't, claim another station, or read what isn't
  shared (peers get rounded locations and no private messages unless you choose otherwise);
- a secret leaving the computer or landing in a log, response or backup it shouldn't: channel keys, the radio's
  private and admin keys, Wi-Fi and MQTT passwords, the Bluetooth PIN, sync tokens, the login password;
- anything an LLM connected through `mcp_server.py` can do beyond its `[mcp]` settings;
- the installer or updater running something it shouldn't.

## What doesn't

- **The internet.** The dashboard is plain HTTP for your own network. Putting its port on the internet isn't
  supported; the login keeps guests on your network away from the radio, nothing more.
- **Programs already running on the Lorakeet computer.** They have the same access you do.
- **The Meshtastic mesh itself.** Anyone in radio range hears the same packets; what Lorakeet decodes is what your
  radio can already read. Problems in the firmware or the Meshtastic library belong with
  [Meshtastic](https://github.com/meshtastic).

## Supported versions

Fixes go into the latest release. Lorakeet is in preview (0.x): update to the newest version before reporting, if
you can.
