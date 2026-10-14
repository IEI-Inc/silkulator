# silk QEMU local emulator

Run the reconstructed Pebble 2 (silk) firmware in QEMU on your own machine,
with a live screen and clickable buttons -- built on the real PULSE2
protocol (the same one actual Pebble hardware and the official tooling
use), not a log hack.

## Files

- `pebble_build.py` -- builds the firmware (waf) and the QEMU flash images.
- `pebble_link.py` -- the engine: launches QEMU, speaks PULSE2 to it,
  reads framebuffer frames, sends button clicks. No GUI code -- reusable
  from scripts too.
- `pebble_emulator.py` -- the Tkinter GUI: live screen + Back/Up/Select/Down
  buttons.

## One-time setup

You need, already installed and on `PATH`:

- **python2** -- waf (the firmware's build system) is Python-2-only.
- **arm-none-eabi-gcc** -- the ARM cross toolchain.
- A build of **coredevices/qemu** (the fork with the `pebble-silk-bb`
  machine -- stock upstream QEMU doesn't have it) at
  `~/coredevices/qemu/build/qemu-system-arm`, or pass `--qemu /path/to/it`.
- **python3** (3.8+) with:
  ```
  pip3 install pyserial construct==2.5.3 transitions pillow cobs
  ```
  Note on `construct`: only the old ("2.5.x") API works with the vendored
  `python_libs/pulse2` library -- modern construct (2.9+) renamed it
  entirely. If `pip install construct==2.5.3` fails to build (it's an old
  pure-Python package with a deprecated `setup.py` that can choke on
  newer `setuptools`), download the sdist from PyPI, extract it, and set
  `PULSE2_CONSTRUCT_PATH=/path/to/construct-2.5.3` when running the
  emulator -- it'll be added to `sys.path` automatically.
- **tkinter** -- usually bundled with python3; on Debian/Ubuntu if it's
  missing: `sudo apt install python3-tk`.

## Usage

```
python3 pebble_build.py --repo /path/to/silkulator
python3 pebble_emulator.py --repo /path/to/silkulator
```

(`--repo` can be omitted from `pebble_build.py` if you run it from inside
the repo checkout.)

The window shows the live watch screen (scaled 3x) plus Back/Up/Select/Down
buttons; keyboard shortcuts work too (Esc/Up/Down/Enter/Space). Boot takes
~15-20 seconds -- the status line at the top tracks progress.

## How it works, briefly

QEMU is launched with `-serial pty` for the debug-serial UART (not
`-serial file`, which is read-only, and not `-monitor`/`-s`, which some
sandboxed environments refuse to let QEMU open as a listening socket).
`pebble_link.py` opens that pty and speaks the real PULSE2 link protocol
over it: LCP/NCP bring-up, then the reliable transport's bulkio channel
(port `0x3e21`) to read the live framebuffer (the same mechanism
`pulse_bulkio_framebuffer.c` exposes on real hardware), and the prompt
console (port `0x3e20`) to send `click short N` commands -- the literal
command the official `click-short` commander tool sends.

## Known quirk

The QEMU SPI flash image persists boot/reset state across runs, so the
firmware's "Your Pebble just reset" dialog can reappear on startup --
`pebble_link.py` dismisses it automatically with one BACK click before
handing control to the GUI.

Separately: this is a from-scratch reconstruction of files Google's dump
omitted, built and debugged this session, not stock production firmware.
Some UI paths can still hang (for example, repeatedly pressing Down deep
enough into the Settings list) -- if a click times out, the status line
will say so and the view keeps polling; it isn't a bug in this tool.
