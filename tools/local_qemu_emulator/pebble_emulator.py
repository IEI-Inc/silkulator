#!/usr/bin/env python3
"""
pebble_emulator.py -- a small Tkinter front end for running the silk
(Pebble 2) firmware in QEMU locally, with a live display and clickable
buttons.

All the actual QEMU/PULSE2 protocol work lives in pebble_link.py (so it
can be reused headlessly); this file is just the GUI.

Usage:
    python3 pebble_build.py              # one-time (or after any firmware change)
    python3 pebble_emulator.py --repo /path/to/silkulator

Dependencies beyond the standard library:
    pip3 install pyserial construct==2.5.3 transitions pillow cobs
See pebble_link.py's module docstring for a note on the construct version.

Controls:
    Click the on-screen Back / Up / Select / Down buttons, or use the
    keyboard: Esc = Back, Up/Down arrows = Up/Down, Enter/Space = Select.
"""

import argparse
import os
import queue
import sys
import tempfile
import threading
import time
import traceback
import tkinter as tk
from tkinter import ttk

from PIL import Image, ImageTk

from pebble_link import (
    PebbleQemuLink, PebbleLinkError, decode_1bit_framebuffer,
    BUTTON_BACK, BUTTON_UP, BUTTON_SELECT, BUTTON_DOWN,
)
from pebble_control_protocol import QemuControlSocket, QemuControlError
from pebble_app_tab import PebbleAppTab

SCALE = 3
# Target gap *between the start of one read_frame() and the start of the
# next*, not an unconditional sleep tacked on after each read. The old code
# slept this long *in addition to* however long read_frame() itself took
# (a full PULSE2 OPEN+READ(s)+CLOSE round trip), so actual frame time was
# POLL_INTERVAL_S + read latency -- capping real-world throughput at ~2.5
# fps even though read_frame() alone is well under that. _poll_loop() now
# subtracts the elapsed read time from the wait, so this constant is an
# actual rate cap rather than a fixed per-frame tax.
#
# Don't drop this much below ~0.1s: read_frame() opens the bulkio
# framebuffer domain (which calls animation_private_pause() firmware-side)
# and closes it again after every single read (animation_private_resume()
# + a forced redraw) -- see pebble_link.py's read_frame() docstring.
# Polling faster than the UI can even display (UI_POLL_MS below) just
# means more pause/resume cycling on the firmware's animation system for
# frames that get thrown away anyway.
POLL_INTERVAL_S = 0.1  # how often the background thread pulls a new frame
UI_POLL_MS = 100       # how often Tkinter checks for a new frame to draw


class PebbleEmulatorApp:
    def __init__(self, root, link_kwargs, boot_settle_seconds):
        self.root = root
        self.root.title("Pebble 2 (silk) -- QEMU")
        self.root.resizable(False, False)

        self.link = PebbleQemuLink(**link_kwargs)
        self.boot_settle_seconds = boot_settle_seconds
        self._control_channel = None  # QemuControlSocket, set once QEMU is up

        self.frame_queue = queue.Queue(maxsize=1)
        self.status_queue = queue.Queue()
        self._stopping = False
        self._poll_thread = None
        self._photo = None  # keep a reference so Tk doesn't garbage-collect it

        self._build_ui()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        threading.Thread(target=self._boot_and_run, daemon=True).start()
        self.root.after(UI_POLL_MS, self._drain_queues)

    # -- UI layout ---------------------------------------------------

    def _build_ui(self):
        notebook = ttk.Notebook(self.root)
        notebook.grid(row=0, column=0, sticky="nsew")

        watch_tab = ttk.Frame(notebook)
        notebook.add(watch_tab, text="Watch")

        self.pebble_app_tab = PebbleAppTab(notebook, log_cb=self.status_queue.put)
        notebook.add(self.pebble_app_tab, text="Pebble App")

        self._build_watch_tab(watch_tab)

    def _build_watch_tab(self, root_frame):
        outer = ttk.Frame(root_frame, padding=10)
        outer.grid(row=0, column=0)

        self.status_var = tk.StringVar(value="Starting QEMU...")
        ttk.Label(outer, textvariable=self.status_var).grid(
            row=0, column=0, columnspan=3, sticky="w", pady=(0, 6))

        screen_w, screen_h = 144 * SCALE, 168 * SCALE
        self.canvas = tk.Canvas(outer, width=screen_w, height=screen_h,
                                 bg="#888888", highlightthickness=1,
                                 highlightbackground="#333333")
        self.canvas.grid(row=1, column=0, rowspan=4)
        self.canvas_image_id = self.canvas.create_image(0, 0, anchor="nw")

        # Buttons laid out roughly like the real watch: BACK on the left
        # edge, UP/SELECT/DOWN stacked on the right edge.
        btn_opts = dict(width=8)
        back_btn = ttk.Button(outer, text="BACK", command=lambda: self._click(BUTTON_BACK),
                               **btn_opts)
        back_btn.grid(row=1, column=1, rowspan=4, padx=(10, 0), sticky="n")

        up_btn = ttk.Button(outer, text="UP", command=lambda: self._click(BUTTON_UP), **btn_opts)
        up_btn.grid(row=1, column=2, pady=(0, 20))
        select_btn = ttk.Button(outer, text="SELECT",
                                 command=lambda: self._click(BUTTON_SELECT), **btn_opts)
        select_btn.grid(row=2, column=2, pady=20)
        down_btn = ttk.Button(outer, text="DOWN", command=lambda: self._click(BUTTON_DOWN),
                               **btn_opts)
        down_btn.grid(row=3, column=2, pady=(20, 0))

        ttk.Label(outer, text="Keys: Esc=Back  Up/Down arrows  Enter/Space=Select",
                  foreground="#666666").grid(row=5, column=0, columnspan=3, pady=(8, 0))

        self.root.bind("<Escape>", lambda e: self._click(BUTTON_BACK))
        self.root.bind("<Up>", lambda e: self._click(BUTTON_UP))
        self.root.bind("<Down>", lambda e: self._click(BUTTON_DOWN))
        self.root.bind("<Return>", lambda e: self._click(BUTTON_SELECT))
        self.root.bind("<space>", lambda e: self._click(BUTTON_SELECT))

    # -- QEMU / PULSE2 plumbing (background thread) -------------------

    def _boot_and_run(self):
        try:
            self.status_queue.put("Booting firmware (this takes ~15-20s)...")
            self.link.start(boot_settle_seconds=self.boot_settle_seconds)
            self.status_queue.put(
                f"Connected -- {self.link.width}x{self.link.height}, "
                f"{self.link.bpp}bpp")
            self._connect_control_channel()
        except PebbleLinkError as e:
            self.status_queue.put(f"FAILED TO START: {e}")
            return
        except Exception as e:
            # An "unexpected" (non-PebbleLinkError) failure here is, by
            # definition, one we didn't anticipate well enough to give a
            # helpful message for at the raise site -- str(e) can easily
            # be empty (e.g. a bare exception instance, or one whose
            # message lives only in its type/repr). Print the full
            # traceback to the terminal this GUI was launched from so the
            # real cause is always recoverable, instead of only ever
            # showing a possibly-blank one-liner in the status bar.
            traceback.print_exc()
            label = f"{type(e).__name__}: {e}" if str(e) else f"{type(e).__name__} (see terminal for traceback)"
            self.status_queue.put(f"FAILED TO START (unexpected): {label}")
            return

        self._poll_loop()

    def _connect_control_channel(self):
        """Open the pebble_control socket for the "Pebble App" tab, which
        always needs one regardless of whether --bt-bridge-socket was
        passed for an *external* bridge process -- main() defaults
        link.pebble_control_socket_path to a private path for exactly this
        case. Failure here shouldn't take down the whole GUI (the watch
        screen/buttons still work fine without it); it just means the
        Pebble App tab stays disconnected."""
        try:
            ctl = QemuControlSocket()
            ctl.connect(self.link.pebble_control_socket_path, timeout=10)
            ctl.send_bt_connected(True)
            self._control_channel = ctl
            self.pebble_app_tab.set_control_channel(ctl)
        except QemuControlError as e:
            self.status_queue.put(f"(Pebble App tab couldn't connect: {e})")

    def _poll_loop(self):
        while not self._stopping:
            frame_start = time.monotonic()
            try:
                frame = self.link.read_frame()
                pixels = decode_1bit_framebuffer(frame, self.link.width, self.link.height)
                # Keep only the latest frame; drop stale ones rather than
                # letting the queue (and thus the UI) fall behind.
                try:
                    self.frame_queue.get_nowait()
                except queue.Empty:
                    pass
                self.frame_queue.put((self.link.width, self.link.height, pixels))
            except PebbleLinkError as e:
                self.status_queue.put(f"(frame read error, retrying: {e})")
            except Exception as e:
                self.status_queue.put(f"(unexpected error, retrying: {e})")
            # Wait only the *remainder* of POLL_INTERVAL_S after subtracting
            # however long that read_frame() round trip actually took --
            # previously this slept POLL_INTERVAL_S unconditionally on top
            # of the read, so real frame time was read latency + 0.4s
            # regardless of how fast the read itself was.
            remaining = POLL_INTERVAL_S - (time.monotonic() - frame_start)
            deadline = time.monotonic() + max(remaining, 0)
            while time.monotonic() < deadline:
                if self._stopping:
                    return
                threading.Event().wait(0.02)

    def _click(self, button):
        if self._stopping or not self.link._link:
            return
        threading.Thread(target=self._click_bg, args=(button,), daemon=True).start()

    def _click_bg(self, button):
        try:
            self.link.click(button)
        except PebbleLinkError as e:
            self.status_queue.put(f"(click error: {e})")

    # -- Tkinter-side queue draining (main thread) ---------------------

    def _drain_queues(self):
        try:
            while True:
                self.status_var.set(self.status_queue.get_nowait())
        except queue.Empty:
            pass

        try:
            width, height, pixels = self.frame_queue.get_nowait()
            img = Image.frombytes("L", (width, height), pixels)
            img = img.resize((width * SCALE, height * SCALE), Image.NEAREST)
            self._photo = ImageTk.PhotoImage(img)
            self.canvas.itemconfig(self.canvas_image_id, image=self._photo)
        except queue.Empty:
            pass

        if not self._stopping:
            self.root.after(UI_POLL_MS, self._drain_queues)

    def _on_close(self):
        self._stopping = True
        self.status_var.set("Shutting down...")
        threading.Thread(target=self._shutdown_bg, daemon=True).start()

    def _shutdown_bg(self):
        self.pebble_app_tab.stop()
        try:
            if self._control_channel is not None:
                self._control_channel.close()
        except Exception:
            pass
        try:
            self.link.stop()
        except Exception:
            pass
        self.root.after(0, self.root.destroy)


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                      formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--repo", required=True,
                         help="Path to the pebble firmware repo (after running pebble_build.py)")
    parser.add_argument("--qemu", default=None,
                         help="Path to qemu-system-arm (default: auto-detect "
                              "~/coredevices/qemu/build/qemu-system-arm or PATH)")
    parser.add_argument("--boot-settle", type=int, default=12,
                         help="Seconds to wait after link-up before dismissing any "
                              "stale reset dialog and starting the live view (default: 12)")
    parser.add_argument("--bt-bridge-socket", default=None,
                         help="Expose the firmware's pebble_control port (its fake BT "
                              "transport) as a unix-domain socket at this path, for a "
                              "separate BLE bridge process (e.g. "
                              "tools/ble_bridge/pebble_ble_bridge.py) to connect to. "
                              "The 'Pebble App' tab always needs this port open regardless "
                              "-- if you don't pass this, a private path in a temp dir is "
                              "used automatically, just for that tab's own use.")
    args = parser.parse_args()

    link_kwargs = {"repo": args.repo}
    if args.qemu:
        link_kwargs["qemu_bin"] = args.qemu
    link_kwargs["pebble_control_socket_path"] = (
        args.bt_bridge_socket
        or os.path.join(tempfile.mkdtemp(prefix="pebble_control_"), "pebble_control.sock"))

    root = tk.Tk()
    app = PebbleEmulatorApp(root, link_kwargs, args.boot_settle)
    try:
        root.mainloop()
    except KeyboardInterrupt:
        app._on_close()


if __name__ == "__main__":
    main()
