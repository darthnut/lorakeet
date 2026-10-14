# Contributing

Thanks for helping. Lorakeet is a hobby project maintained on a best-effort basis: issues and pull requests are
welcome, and small, focused ones are the easiest to take.

- **Trying it out?** [TESTING.md](TESTING.md) says what to try and what's useful to send back.
- **Found a bug?** Open an issue with the bug form. Leave out personal details: real radio ids and names, locations,
  keys, your `lorakeet.toml`.
- **A security problem?** Not in an issue, please: see [SECURITY.md](SECURITY.md).
- **An idea?** Open an issue first if it's big, so we can agree on the shape before you build it.

Everyone taking part follows the [code of conduct](CODE_OF_CONDUCT.md).

## Setting up

```sh
python -m venv venv                       # Python 3.11 to 3.14
venv/bin/pip install -r requirements.txt -r requirements-dev.txt     # Windows: venv\Scripts\pip
venv/bin/python -m playwright install chromium                       # once, for the browser tests
```

You don't need a radio to work on most of Lorakeet: `python server.py --demo` serves a made-up mesh (see
[demo.py](demo.py)), and the tests build their own.

To run your copy next to an installed Lorakeet, give it its own config: `LORAKEET_CONFIG=/tmp/lk.toml python
server.py`, where that file sets another `[http] port`, a temporary `[storage] data_dir`, and a `[radio] port` that
doesn't exist (or your test radio's).

## Before you send a pull request

```sh
python -m pytest                 # everything, including the pages in a browser (tests/browser)
ruff check .                     # likely bugs, unused or undefined names (ruff.toml)
python tools/release_check.py    # nothing personal in the repository
```

GitHub Actions runs the same on Linux and Windows for every push and pull request, plus macOS and Windows on ARM
for the public repository.

- **Add a test that fails without your change.** Use the synthetic data in `tests/synth.py` or the demo mesh,
  never a copy of a real database or real ids, names, places or coordinates.
- **Don't transmit while developing.** Text messages and traceroutes go out on a real mesh under your radio's name.
  Validation paths can be tested without sending.
- **New endpoint?** Add it to `api_index.py` and run `python tools/api_docs.py` (a test checks they match).
- **Changed the database layout?** Add a migration and bump `SCHEMA_VERSION` (store.py). Migrations only ever add.
- **User-visible change?** Add a line under `## Unreleased` in [CHANGELOG.md](CHANGELOG.md).

## How the code is organised

[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) is the short overview. [CLAUDE.md](CLAUDE.md) has the detail: the
code map, the rules the code follows (the database is a clean log of what was received; anything that transmits
is opt-in; nothing personal in code), and how each feature works. Lorakeet was largely written with Claude, and
that file is the working notes it uses; it's an equally good map for people.

## License

By contributing you agree that your work is licensed under the project's [GPL-3.0](LICENSE).
