"""
pebble_app_tab.py -- the "Pebble App" tab: a minimal stand-in for the real
phone-side Pebble companion app, built into pebble_emulator.py so you can
feed it data without needing a real phone or the full BLE bridge
(tools/ble_bridge/) at all.

Talks to the firmware over the same pebble_control socket the BLE bridge
uses (see pebble_control_protocol.py), but directly from this process --
no GATT/BLE/pairing involved, since this is standing in for "the phone app
decided to tell the watch something," not "a real phone connected over the
air." If you *do* want to test over real BLE with your actual iPhone, use
tools/ble_bridge/ instead; this tab is for quick local testing/demos.

Currently implemented: Health (steps). Real HealthKit data lives on your
iPhone, and there's no public API for a plain macOS Python script to read
it directly (HealthKit itself is iOS/watchOS-only) -- so this tab supports
two ways to get a real step count in:
  1. Type it in and hit Sync. Simplest, always works.
  2. Point it at a JSON file your phone periodically exports a step count
     into (e.g. via the "Health Auto Export" app set to write to a folder
     synced to this Mac, or a Shortcuts automation that does the same).
     This tab polls that file and syncs whenever the number in it changes.

However you get a number in, "Sync Now" sends it to the firmware via
QemuProtocol_HealthSteps -- a QEMU/firmware-only testing hook (no real
watch has an equivalent inbound message; real Pebbles compute steps
on-watch from the accelerometer, see that protocol's doc comment in
qemu_serial.h), which calls straight into the same
activity_test_set_steps_and_avg() the real Activity Demo system app's
debug menu uses.
"""

import json
import threading
import time
import tkinter as tk
from tkinter import ttk, filedialog

from pebble_control_protocol import QemuControlError

FILE_POLL_INTERVAL_S = 30


def _extract_step_count(data):
    """Best-effort extraction of a "today's total steps" integer from a
    JSON blob. There's no single standard export format, so this tries a
    few shapes seen in common iOS Health-export tools rather than assuming
    one. Returns None if nothing recognizable is found -- callers should
    surface that rather than silently sending 0.
    """
    if isinstance(data, dict):
        # Flat shape: {"steps": 1234, ...} or {"stepCount": 1234, ...}
        for key in ("steps", "stepCount", "step_count"):
            if key in data and isinstance(data[key], (int, float)):
                return int(data[key])

        # "Health Auto Export" style: {"data": {"metrics": [{"name": "step_count",
        #  "data": [{"qty": 1234, ...}, ...]}, ...]}}
        metrics = data.get("data", {}).get("metrics") if isinstance(data.get("data"), dict) else None
        if isinstance(metrics, list):
            for metric in metrics:
                name = str(metric.get("name", "")).lower()
                if "step" in name:
                    points = metric.get("data") or []
                    total = 0
                    found = False
                    for point in points:
                        qty = point.get("qty")
                        if isinstance(qty, (int, float)):
                            total += qty
                            found = True
                    if found:
                        return int(total)
    return None


class PebbleAppTab(ttk.Frame):
    def __init__(self, parent, log_cb=None):
        super().__init__(parent, padding=10)
        self._log_cb = log_cb or (lambda msg: None)
        self._ctl = None  # set via set_control_channel() once QEMU is up
        self._watch_path = None
        self._last_synced_steps = None
        self._stopping = False

        self._build_ui()

        self._watch_thread = threading.Thread(target=self._watch_loop, daemon=True)
        self._watch_thread.start()

    # -- wiring from the main app ---------------------------------------

    def set_control_channel(self, ctl):
        """Called once by the main app after it connects its own
        pebble_control socket for this tab to share. Safe to call from a
        background thread."""
        self._ctl = ctl
        self.after(0, lambda: self.phone_status_var.set("Phone app: connected to watch"))
        self._log("Pebble App tab connected to the watch.")

    def stop(self):
        self._stopping = True

    # -- UI ----------------------------------------------------------------

    def _build_ui(self):
        self.phone_status_var = tk.StringVar(value="Phone app: waiting for watch to boot...")
        ttk.Label(self, textvariable=self.phone_status_var).grid(
            row=0, column=0, columnspan=3, sticky="w", pady=(0, 10))

        health_frame = ttk.Labelframe(self, text="Health", padding=10)
        health_frame.grid(row=1, column=0, columnspan=3, sticky="ew")

        self.health_enabled_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            health_frame, text="Share Health (steps) with this Pebble",
            variable=self.health_enabled_var,
            command=self._on_health_enabled_toggle,
        ).grid(row=0, column=0, columnspan=3, sticky="w")

        ttk.Label(health_frame, text="Steps today:").grid(row=1, column=0, sticky="w", pady=(8, 0))
        self.steps_var = tk.StringVar(value="0")
        ttk.Entry(health_frame, textvariable=self.steps_var, width=10).grid(
            row=1, column=1, sticky="w", pady=(8, 0))
        ttk.Button(health_frame, text="Sync Now", command=self._sync_manual_steps).grid(
            row=1, column=2, sticky="w", padx=(8, 0), pady=(8, 0))

        self.watch_file_var = tk.StringVar(value="(no export file selected)")
        ttk.Button(health_frame, text="Watch export file...",
                   command=self._choose_watch_file).grid(
            row=2, column=0, sticky="w", pady=(8, 0))
        ttk.Label(health_frame, textvariable=self.watch_file_var,
                  foreground="#666666").grid(
            row=2, column=1, columnspan=2, sticky="w", pady=(8, 0))

        ttk.Label(
            health_frame,
            text=("Real HealthKit data lives on your iPhone -- a plain Mac script can't\n"
                  "read it directly. Either type a step count above, or export it from your\n"
                  "phone periodically (e.g. the \"Health Auto Export\" app, or a Shortcuts\n"
                  "automation) into a JSON file synced to this Mac, and watch that file here."),
            foreground="#666666", justify="left",
        ).grid(row=3, column=0, columnspan=3, sticky="w", pady=(10, 0))

        log_frame = ttk.Labelframe(self, text="Activity", padding=6)
        log_frame.grid(row=2, column=0, columnspan=3, sticky="nsew", pady=(10, 0))
        self.log_text = tk.Text(log_frame, width=56, height=10, state="disabled",
                                 font=("Courier", 10))
        self.log_text.grid(row=0, column=0, sticky="nsew")

    # -- actions ---------------------------------------------------------

    def _on_health_enabled_toggle(self):
        if self.health_enabled_var.get():
            self._log("Health sharing enabled.")
        else:
            self._log("Health sharing disabled -- no more syncing until re-enabled.")

    def _sync_manual_steps(self):
        try:
            steps = int(self.steps_var.get())
        except ValueError:
            self._log(f"'{self.steps_var.get()}' isn't a whole number of steps.")
            return
        self._send_steps(steps, source="manual")

    def _choose_watch_file(self):
        path = filedialog.askopenfilename(
            title="Select a Health export JSON file",
            filetypes=[("JSON files", "*.json"), ("All files", "*.*")])
        if path:
            self._watch_path = path
            self.watch_file_var.set(path)
            self._log(f"Watching {path} for step count changes "
                      f"(every {FILE_POLL_INTERVAL_S}s while Health sharing is on).")

    def _send_steps(self, steps, source):
        if self._ctl is None:
            self._log("Can't sync yet -- the watch hasn't finished booting.")
            return
        try:
            self._ctl.send_health_steps(steps)
            self._last_synced_steps = steps
            self._log(f"Synced {steps} steps to the watch ({source}).")
            self.after(0, lambda: self.steps_var.set(str(steps)))
        except QemuControlError as e:
            self._log(f"Failed to sync steps: {e}")

    def _log(self, msg):
        self._log_cb(msg)
        def append():
            self.log_text.configure(state="normal")
            self.log_text.insert("end", msg + "\n")
            self.log_text.see("end")
            self.log_text.configure(state="disabled")
        self.after(0, append)

    # -- background file watcher -----------------------------------------

    def _watch_loop(self):
        while not self._stopping:
            time.sleep(FILE_POLL_INTERVAL_S)
            if self._stopping:
                return
            if not (self.health_enabled_var.get() and self._watch_path):
                continue
            try:
                with open(self._watch_path) as f:
                    data = json.load(f)
            except (OSError, json.JSONDecodeError) as e:
                self._log(f"Couldn't read export file: {e}")
                continue
            steps = _extract_step_count(data)
            if steps is None:
                self._log("Export file didn't contain a recognizable step count "
                          "(looked for 'steps'/'stepCount', and Health Auto Export's "
                          "metrics format).")
                continue
            if steps != self._last_synced_steps:
                self._send_steps(steps, source="export file")
