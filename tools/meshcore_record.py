"""Record what a MeshCore companion radio sends over USB, frame by frame, for building and testing Lorakeet's MeshCore
link (docs/MESHCORE.md, step 4) against real traffic.

    python tools/meshcore_record.py COM12 --minutes 60 --out D:/somewhere/mc-2026-10-14.jsonl

One JSON line per frame: {"t": unix time, "hex": the frame}, plus a few {"note": ...} lines. Listening only: it asks
the radio (over USB, never over the air) for its own info and its counters every few minutes, and sends nothing on
the mesh. Needs the `meshcore` package (pip install "meshcore>=2.3.14,!=2.3.16").

A recording holds other people's radios: their public keys, names, positions and any channel message the radio can
read. Keep it private, like a mesh.db: never commit one. Tests use made-up frames instead.
"""
import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

STATS_EVERY_S = 300


async def record(port, minutes, out):
    try:
        from meshcore import EventType, MeshCore
    except ImportError:
        sys.exit("needs the meshcore package: pip install \"meshcore>=2.3.14,!=2.3.16\"")
    counts = {"frames": 0}
    with open(out, "a", encoding="utf-8") as f:
        def note(text):
            f.write(json.dumps({"t": time.time(), "note": text}) + "\n")
            f.flush()

        mc = await MeshCore.create_serial(port, 115200)
        if mc is None:
            sys.exit(f"couldn't connect to a MeshCore radio on {port}")
        reader = mc._reader            # every frame goes through here before the library parses it
        parse = reader.handle_rx

        async def capture(data):
            f.write(json.dumps({"t": time.time(), "hex": bytes(data).hex()}) + "\n")
            f.flush()
            counts["frames"] += 1
            return await parse(data)
        reader.handle_rx = capture
        note(f"recording from {port}, meshcore library {getattr(sys.modules.get('meshcore'), '__version__', '?')}")

        seen = {}
        mc.subscribe(None, lambda e: seen.__setitem__(e.type.value, seen.get(e.type.value, 0) + 1))
        await mc.commands.send_appstart()   # again, now that it's recorded: the radio's own settings (SELF_INFO)
        await mc.commands.send_device_query()
        end = time.time() + minutes * 60
        next_stats = 0
        try:
            while time.time() < end:
                if time.time() >= next_stats:
                    for ask in (mc.commands.get_bat, mc.commands.get_stats_core, mc.commands.get_stats_radio,
                                mc.commands.get_stats_packets):
                        try:
                            await ask()
                        except Exception as e:  # noqa: BLE001 - older firmware lacks some counters
                            note(f"{ask.__name__}: {e}")
                    next_stats = time.time() + STATS_EVERY_S
                rx = seen.get(EventType.RX_LOG_DATA.value, 0)
                print(f"\r{counts['frames']} frames, {rx} receptions, {int((end - time.time()) / 60)} min left  ",
                      end="", flush=True)
                await asyncio.sleep(5)
        finally:
            note(f"stopped: {counts['frames']} frames; events {json.dumps(seen, sort_keys=True)}")
            await mc.disconnect()
    print(f"\n{counts['frames']} frames in {out}")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("port", help="the radio's serial port, e.g. COM12 or /dev/ttyACM1")
    ap.add_argument("--minutes", type=float, default=60)
    ap.add_argument("--out", required=True, help="a .jsonl file outside the repository (it holds other people's data)")
    a = ap.parse_args(argv)
    if Path(a.out).resolve().is_relative_to(Path(__file__).resolve().parent.parent):
        sys.exit("--out is inside the repository: recordings hold other people's radios, so keep them elsewhere")
    asyncio.run(record(a.port, a.minutes, a.out))


if __name__ == "__main__":
    main()
