#!/usr/bin/env python3
"""
pebble_link.py -- launches QEMU running the silk firmware and talks to it
over the real PULSE2 protocol (the same protocol/commands real Pebble
hardware and the official tooling use), to read live framebuffer frames and
send button clicks.

This is the headless "engine" half of the local emulator -- no GUI code
lives here, so it can be reused from a script, a test, or a GUI front end
(pebble_emulator.py uses this).

Requires, on top of the repo's own build dependencies:
    pip3 install pyserial construct==2.5.3 transitions pillow cobs

NOTE on construct: only the legacy 2.5.x "old-style" construct API works
with this vendored pulse2 library (`construct.Byte('name')`-style calls).
Modern construct (2.9+) renamed this API entirely. If `pip install
construct==2.5.3` fails to build on your Python version (it's an old pure-
Python package with a deprecated setup.py), download the sdist and point
PULSE2_CONSTRUCT_PATH at the extracted `construct-2.5.3/` directory instead
-- this script will add it to sys.path for you. This mirrors exactly how
this same incompatibility was worked around during this session's own
debugging.
"""

import os
import re
import struct
import subprocess
import sys
import threading
import time


BUTTON_BACK = 0
BUTTON_UP = 1
BUTTON_SELECT = 2
BUTTON_DOWN = 3

PULSE2_BULKIO_PROTOCOL = 0x3e21
PULSE2_PROMPT_PROTOCOL = 0x3e20
BULKIO_DOMAIN_FRAMEBUFFER = 3
PROMPT_DONE_RESPONSE = 101


class PebbleLinkError(Exception):
    pass


# Precomputed byte -> 8 grayscale (0 or 255) pixel values, LSB = leftmost
# pixel (confirmed against a real captured frame: this is the bit order
# that renders legible, un-mirrored text).
_BYTE_TO_PIXELS = [
    tuple(255 if (byte >> bit) & 1 else 0 for bit in range(8))
    for byte in range(256)
]


def decode_1bit_framebuffer(data, width, height):
    """Decode a raw silk/Pebble-2 1bpp framebuffer dump (as read from the
    PULSE2 bulkio Framebuffer domain) into `height` rows of `width`
    grayscale (0/255) byte values, suitable for PIL's Image.frombytes('L', ...).

    Row layout (from src/fw/applib/graphics/1_bit/framebuffer.h):
    each row is FRAMEBUFFER_WORDS_PER_ROW = (width/32 + 1) 32-bit words,
    i.e. stride = that many words * 4 bytes -- there's padding at the end
    of every row beyond the `width` real pixels.
    """
    stride = ((width // 32) + 1) * 4
    out = bytearray(width * height)
    for y in range(height):
        row = data[y * stride:(y + 1) * stride]
        out_off = y * width
        x = 0
        for b in row:
            pixels = _BYTE_TO_PIXELS[b]
            n = min(8, width - x)
            out[out_off + x:out_off + x + n] = pixels[:n]
            x += n
            if x >= width:
                break
    return bytes(out)


def _patch_transitions_lockedmachine():
    """python_libs/pulse2/pebble/pulse2/ppp.py was written against an old
    transitions.extensions.LockedMachine that auto-created a reentrant
    `self.rlock` attribute (used throughout as `with self.machine.rlock:`,
    including recursively from within transition callbacks). Modern
    `transitions` replaced this with a `machine_context` list of plain,
    non-reentrant locks and dropped the `rlock` attribute entirely, which
    both breaks the attribute access AND -- if you naively point `.rlock`
    at machine_context[0] -- deadlocks the first time a callback re-enters
    the lock from the same thread. The fix is a real threading.RLock(),
    matching what "rlock" always meant here.
    """
    import threading as _threading
    import transitions.extensions

    if getattr(transitions.extensions.LockedMachine, "_pulse2_rlock_patched", False):
        return

    orig_init = transitions.extensions.LockedMachine.__init__

    def patched_init(self, *a, **kw):
        orig_init(self, *a, **kw)
        self.rlock = _threading.RLock()

    transitions.extensions.LockedMachine.__init__ = patched_init
    transitions.extensions.LockedMachine._pulse2_rlock_patched = True


def _import_pulse2(repo_root):
    pulse2_path = os.path.join(repo_root, "python_libs", "pulse2")
    if pulse2_path not in sys.path:
        sys.path.insert(0, pulse2_path)

    extra_construct = os.environ.get("PULSE2_CONSTRUCT_PATH")
    if extra_construct and extra_construct not in sys.path:
        sys.path.insert(0, extra_construct)

    _patch_transitions_lockedmachine()

    from pebble import pulse2  # noqa: imported for the caller
    return pulse2


class PebbleQemuLink:
    """Owns one QEMU child process running the silk firmware, plus a live
    PULSE2 link to its dbgserial console for reading framebuffer frames and
    sending button clicks.
    """

    def __init__(self, repo, qemu_bin=None, kernel=None, spi=None,
                 machine="pebble-silk-bb", extra_qemu_args=None):
        self.repo = os.path.abspath(repo)
        self.qemu_bin = qemu_bin or self._default_qemu_bin()
        self.kernel = kernel or os.path.join(self.repo, "build", "qemu_micro_flash.bin")
        self.spi = spi or os.path.join(self.repo, "build", "qemu_spi_flash.bin")
        self.machine = machine
        self.extra_qemu_args = extra_qemu_args or []

        self.width = None
        self.height = None
        self.bpp = None
        self.fb_length = None

        self._proc = None
        self._iface = None
        self._link = None
        self._fb_sock = None
        self._fb_fd = None
        self._prompt_sock = None
        # RLock, not Lock: read_frame() can recursively re-enter while
        # still holding this lock when it transparently reopens the
        # framebuffer domain after the firmware has rebooted underneath
        # us (see read_frame's docstring).
        self._fb_lock = threading.RLock()
        self._prompt_lock = threading.Lock()

    @staticmethod
    def _default_qemu_bin():
        # Most common layout: a sibling checkout of coredevices/qemu, built
        # in-tree. Fall back to whatever's on PATH.
        for candidate in (
            os.path.expanduser("~/coredevices/qemu/build/qemu-system-arm"),
            os.path.expanduser("~/qemu/build/qemu-system-arm"),
        ):
            if os.path.isfile(candidate):
                return candidate
        import shutil
        found = shutil.which("qemu-system-arm")
        if found:
            return found
        raise PebbleLinkError(
            "Could not find qemu-system-arm. Pass qemu_bin= explicitly -- "
            "it needs to be a build of the coredevices/qemu fork with the "
            "pebble-silk-bb machine, not stock upstream QEMU.")

    # -- lifecycle ---------------------------------------------------

    def start(self, boot_settle_seconds=10, dismiss_reset_dialog=True):
        for path, label in ((self.qemu_bin, "qemu binary"),
                             (self.kernel, "internal flash image"),
                             (self.spi, "SPI flash image")):
            if not os.path.isfile(path):
                raise PebbleLinkError(f"{label} not found: {path}\n"
                                       "Did you run pebble_build.py first?")

        cmd = [self.qemu_bin, "-M", self.machine,
               "-kernel", self.kernel, "-mtdblock", self.spi,
               "-serial", "null", "-serial", "null", "-serial", "pty",
               "-display", "none", "-no-reboot"] + self.extra_qemu_args

        self._proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            bufsize=1, universal_newlines=True)

        pty_path = None
        deadline = time.time() + 15
        startup_log = []
        while time.time() < deadline:
            line = self._proc.stdout.readline()
            if not line:
                time.sleep(0.05)
                continue
            startup_log.append(line.rstrip())
            m = re.search(r"char device redirected to (\S+) \(label serial2\)", line)
            if m:
                pty_path = m.group(1)
                break
        if not pty_path:
            self._kill_proc()
            raise PebbleLinkError(
                "QEMU didn't report a pty for the debug serial within 15s. "
                "Output so far:\n  " + "\n  ".join(startup_log))

        # Keep draining qemu's own stdout forever so its pipe never fills
        # and blocks the child.
        threading.Thread(target=self._drain_qemu_stdout, daemon=True).start()

        # IMPORTANT: open the PULSE2 link *immediately* after finding the
        # pty, not after a delay. The dbgserial UART carries a continuous
        # stream of PULSE2-framed boot log traffic; nothing reading the
        # pty means its small kernel buffer fills and the firmware's
        # blocking UART write stalls boot. Interface() starts a background
        # thread that drains it from the first byte.
        pulse2 = _import_pulse2(self.repo)
        if sys.platform == "darwin":
            # pyserial's default DBGSERIAL_PORT_SETTINGS asks for a
            # 1,000,000 baud rate. That's not one of macOS's standard
            # termios speeds, so pyserial falls back to a custom-baud-rate
            # ioctl (IOSSIOSPEED) to set it. That ioctl is only implemented
            # for real serial devices -- a BSD pty (which is what QEMU's
            # "-serial pty" hands back here) doesn't support it, so the
            # open fails with OSError: [Errno 25] Inappropriate ioctl for
            # device. The baud rate is meaningless over a pty anyway (QEMU
            # doesn't rate-limit a pty-backed chardev), so just ask for a
            # standard rate pyserial can set without the custom ioctl.
            pulse2.link.DBGSERIAL_PORT_SETTINGS = dict(
                pulse2.link.DBGSERIAL_PORT_SETTINGS, baudrate=115200)
        self._iface = pulse2.Interface.open_dbgserial(url=pty_path)
        self._link = self._iface.get_link(timeout=30)
        if self._link is None:
            self.stop()
            raise PebbleLinkError("PULSE2 link never came up (LCP negotiation timed out).")

        self._prompt_sock = self._link.open_socket("reliable", PULSE2_PROMPT_PROTOCOL, timeout=30)
        if self._prompt_sock is None:
            self.stop()
            raise PebbleLinkError("Could not open the PULSE2 prompt console socket.")

        if dismiss_reset_dialog:
            # The QEMU SPI flash image persists a "last boot was a reset/
            # crash" flag across runs, which pushes crashed_ui.c's "Your
            # Pebble just reset" dialog on every boot until a button
            # dismisses it (it has no auto-timeout). One harmless BACK
            # click clears it if it's showing; it's a no-op otherwise.
            time.sleep(boot_settle_seconds)
            try:
                self.click(BUTTON_BACK)
            except PebbleLinkError:
                pass
            time.sleep(1.5)

        self._fb_sock = self._link.open_socket("reliable", PULSE2_BULKIO_PROTOCOL, timeout=30)
        if self._fb_sock is None:
            self.stop()
            raise PebbleLinkError("Could not open the PULSE2 bulkio socket.")

        self._open_framebuffer_domain()

    def stop(self):
        try:
            if self._fb_fd is not None and self._fb_sock is not None:
                self._fb_sock.send(struct.pack("<BB", 2, self._fb_fd))  # CLOSE
                try:
                    self._fb_sock.receive(block=True, timeout=3)
                except Exception:
                    pass
        except Exception:
            pass
        for sock in (self._fb_sock, self._prompt_sock):
            try:
                if sock is not None:
                    sock.close()
            except Exception:
                pass
        try:
            if self._iface is not None:
                self._iface.close()
        except Exception:
            pass
        self._kill_proc()

    def _kill_proc(self):
        if self._proc is not None and self._proc.poll() is None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._proc.kill()

    def _drain_qemu_stdout(self):
        try:
            while True:
                line = self._proc.stdout.readline()
                if not line:
                    break
        except Exception:
            pass

    # -- framebuffer ---------------------------------------------------

    def _open_framebuffer_domain(self):
        with self._fb_lock:
            self._fb_sock.send(struct.pack("<BB", 1, BULKIO_DOMAIN_FRAMEBUFFER))  # OPEN
            resp = self._fb_sock.receive(block=True, timeout=10)
            if resp[0] != 0x80:
                raise PebbleLinkError(f"Unexpected OPEN response: {resp!r}")
            self._fb_fd = resp[1]

            self._fb_sock.send(struct.pack("<BB", 6, self._fb_fd))  # STAT
            resp = self._fb_sock.receive(block=True, timeout=10)
            if resp[0] != 0x85:
                raise PebbleLinkError(f"Unexpected STAT response: {resp!r}")
            _flags, width, height, bpp, length = struct.unpack("<BBBBI", resp[2:10])
            self.width, self.height, self.bpp, self.fb_length = width, height, bpp, length

    def read_frame(self, _retry=True):
        """Read one full framebuffer snapshot. Returns raw packed bytes
        (1 bit per pixel, MSB... see pebble_emulator.py's decode for the
        exact row layout) -- exactly `self.fb_length` bytes.

        IMPORTANT chunk sizing: the firmware's READ handler
        (prv_domain_read_cb in pulse_bulkio.c) auto-continues on its own
        -- if a single READ command's requested length is bigger than its
        own internal max_read_len (PULSE_MAX_SEND_SIZE(520) -
        sizeof(ReadResponse)(6) = 514 bytes, from pulse_protocol_impl.h),
        it keeps sending additional, unasked-for response packets (self-
        rescheduling via system_task_add_callback) until the whole
        requested length is satisfied -- all from the ONE command we
        sent. The link-layer MTU (~1494 bytes) is much bigger than that,
        so requesting up to the MTU (as an earlier version of this method
        did, and as it turns out the one-shot screenshot script that
        first proved this protocol out also did, without ever surfacing
        this bug because it never issued a second read on the same
        connection) causes exactly that auto-continuation: firmware sends
        back 2-3 unsolicited packets per command, we only receive() one
        of them before sending our next command, and the firmware's
        shared per-fd transfer_state gets stomped by that next command
        while old auto-continued packets are still in flight -- corrupting
        the transfer and eventually desyncing the whole fd (confirmed via
        per-chunk tracing: a later command received a stale response left
        over from an earlier one). Capping each request to the firmware's
        own actual single-packet limit guarantees one command : one
        response, with no auto-continuation ever triggered.
        """
        if self._fb_fd is None:
            raise PebbleLinkError("read_frame() called before start()")
        with self._fb_lock:
            data = bytearray()
            pos = 0
            length = self.fb_length
            # PULSE_MAX_SEND_SIZE(520) - sizeof(ReadResponse)(6); see the
            # docstring above for why this must not simply be the MTU.
            chunk_size = 514
            while pos < length:
                want = min(chunk_size, length - pos)
                self._fb_sock.send(struct.pack("<BBII", 3, self._fb_fd, pos, want))  # READ
                resp = self._fb_sock.receive(block=True, timeout=10)
                if resp[0] == 0xc1:  # BULKIO_RESP_INTERNAL_ERROR
                    if not _retry:
                        raise PebbleLinkError(
                            f"Framebuffer read failed even after reopening: {resp!r}")
                    self._open_framebuffer_domain()
                    return self.read_frame(_retry=False)
                if resp[0] != 0x82:
                    raise PebbleLinkError(f"Unexpected READ response: {resp!r}")
                payload = resp[6:]
                if not payload:
                    break
                # The firmware may pad the final chunk to its own internal
                # transfer unit; never let the buffer grow past the real
                # framebuffer length.
                remaining = length - pos
                payload = payload[:remaining]
                data.extend(payload)
                pos += len(payload)
            return bytes(data[:length])

    # -- buttons ---------------------------------------------------

    def click(self, button, kind="short", hold_ms=20):
        """Send a button click over the PULSE2 prompt console, the same
        command the official `click-short`/`click-long` commander tools
        use. Blocks until the firmware acknowledges completion.
        """
        if button not in (BUTTON_BACK, BUTTON_UP, BUTTON_SELECT, BUTTON_DOWN):
            raise ValueError(f"button out of range: {button}")
        if kind == "short":
            command = f"click short {button}"
        elif kind == "long":
            command = f"click multiple {button} 1 {hold_ms} 0"
        else:
            raise ValueError("kind must be 'short' or 'long'")

        with self._prompt_lock:
            self._prompt_sock.send(command.encode())
            deadline = time.time() + 10
            while time.time() < deadline:
                try:
                    resp = self._prompt_sock.receive(block=True, timeout=5)
                except Exception as e:
                    raise PebbleLinkError(f"No response to '{command}': {e}")
                if len(resp) >= 1 and resp[0] == PROMPT_DONE_RESPONSE:
                    return
            raise PebbleLinkError(f"Timed out waiting for '{command}' to complete")
