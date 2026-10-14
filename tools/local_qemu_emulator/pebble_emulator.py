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
import queue
import sys
import threading
import tkinter as tk
from tkinter import ttk

from PIL import Image, ImageTk

from pebble_link import (
    PebbleQemuLink, PebbleLinkError, decode_1bit_framebuffer,
    BUTTON_BACK, BUTTON_UP, BUTTON_SELECT, BUTTON_DOWN,
)

SCALE = 3
POLL_INTERVAL_S = 0.4  # how often the background thread pulls a new frame
UI_POLL_MS = 100       # how often Tkinter checks for a new frame to draw


class PebbleEmulatorApp:
    def __init__(self, root, link_kwargs, boot_settle_seconds):
        self.root = root
        self.root.title("Pebble 2 (silk) -- QEMU")
        self.root.resizable(False, False)

        self.link = PebbleQemuLink(**link_kwargs)
        self.boot_settle_seconds = boot_settle_seconds

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
        outer = ttk.Frame(self.root, padding=10)
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
        except PebbleLinkError as e:
            self.status_queue.put(f"FAILED TO START: {e}")
            return
        except Exception as e:
            self.status_queue.put(f"FAILED TO START (unexpected): {e}")
            return

        self._poll_loop()

    def _poll_loop(self):
        while not self._stopping:
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
            for _ in range(int(POLL_INTERVAL_S * 10)):
                if self._stopping:
                    return
                threading.Event().wait(0.1)

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
    args = parser.parse_args()

    link_kwargs = {"repo": args.repo}
    if args.qemu:
        link_kwargs["qemu_bin"] = args.qemu

    root = tk.Tk()
    app = PebbleEmulatorApp(root, link_kwargs, args.boot_settle)
    try:
        root.mainloop()
    except KeyboardInterrupt:
        app._on_close()


if __name__ == "__main__":
    main()
