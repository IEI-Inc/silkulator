#!/usr/bin/env python3
"""
pebble_control_protocol.py -- pure-stdlib client for QEMU's "pebble_control"
host<->firmware control channel (see hw/arm/pebble_control.c in the
coredevices/qemu fork).

This is the *second* host-facing channel the emulator exposes, separate from
the PULSE2 dbgserial link that pebble_link.py/pebble_emulator.py use for the
screen + buttons. pebble_control sits on USART2 ("serial_hd(1)" in QEMU's
-serial numbering) and speaks a tiny framed protocol that is NOT PULSE2 --
no pulse2/construct dependency needed here, just struct + socket.

Why this exists: the firmware's `qemu` bluetooth-fw backend
(src/bluetooth-fw/qemu/*) never touches real BLE/HCI silicon. It fakes its
entire internal BT/GATT/pairing state machine in software, and reduces to
two things over this channel:
  - QemuProtocol_BluetoothConnection: a 1-byte "connected" flag the host
    sends in; this is what bt_driver_pebble_pairing_service and friends
    treat as "a phone (of some kind) is here".
  - QemuProtocol_SPP: raw Pebble Protocol (PP) session bytes, tunneled
    straight through to/from CommSession once a session transport is open.
So a real macOS BLE peripheral bridge (standing in for the phone to a real
Pebble app) only has to do real GATT/PPoGATT/pairing on the phone-facing
side, then relay decoded PP bytes through this channel -- the firmware
genuinely does not care that there's no real radio underneath it.

Wire format (from pebble_control.c, confirmed directly against source --
do not guess at this, it is exact):

    struct QemuCommChannelHdr {    // all fields network byte order (big-endian)
        uint16_t signature;         // QEMU_HEADER_SIGNATURE = 0xFEED
        uint16_t protocol;          // one of QemuProtocol
        uint16_t len;               // number of bytes of `data` that follow
    };
    <data, `len` bytes>
    struct QemuCommChannelFooter {
        uint16_t signature;         // QEMU_FOOTER_SIGNATURE = 0xBEEF
    };

QEMU_MAX_DATA_LEN is 2048 bytes per packet on the firmware side.

To actually reach this channel, QEMU must be launched with USART2
(serial_hd(1), i.e. the *second* -serial argument) pointed at a chardev the
host can connect to, instead of the "null" chardev the stock command line
uses. See PebbleQemuLink's `pebble_control_socket_path` option in
pebble_link.py, which does exactly this with a unix-domain socket chardev
(`-chardev socket,...,server=on,wait=off` + `-serial chardev:<id>`).
"""

import socket
import struct
import time


QEMU_HEADER_SIGNATURE = 0xFEED
QEMU_FOOTER_SIGNATURE = 0xBEEF
QEMU_MAX_DATA_LEN = 2048

# QemuProtocol enum (pebble_control.c / qemu_serial_private.h)
QemuProtocol_SPP = 1
QemuProtocol_Tap = 2
QemuProtocol_BluetoothConnection = 3
QemuProtocol_Compass = 4
QemuProtocol_Battery = 5
QemuProtocol_Accel = 6
QemuProtocol_Vibration = 7
QemuProtocol_Button = 8
QemuProtocol_TimeFormat = 9
QemuProtocol_TimelinePeek = 10
QemuProtocol_ContentSize = 11
# Not a real Pebble-hardware protocol -- a QEMU/firmware-only testing hook
# (see qemu_serial.h's QemuProtocolHealthStepsHeader doc comment) that lets a
# host-side "Pebble App" stand-in push a step count straight into the
# on-watch activity tracker, bypassing the need to synthesize a plausible
# accelerometer waveform.
QemuProtocol_HealthSteps = 12

_HDR_STRUCT = struct.Struct(">HHH")  # signature, protocol, len
_FOOTER_STRUCT = struct.Struct(">H")  # signature


class QemuControlError(Exception):
    pass


class QemuControlSocket:
    """A blocking-mode client for the pebble_control unix-domain socket.

    Not thread-safe on its own -- callers that need to send and receive
    concurrently should wrap sends with a lock (receiving happens from a
    single dedicated reader thread/loop in practice).
    """

    def __init__(self):
        self._sock = None
        self._recv_buf = bytearray()

    def connect(self, path, timeout=10):
        """Connect to the unix-domain socket at `path`. QEMU creates this
        socket file (and starts listening on it) as soon as it parses its
        -chardev argument, well before the firmware has even started
        booting, but retry briefly in case we race the process spawn.
        """
        deadline = time.monotonic() + timeout
        last_err = None
        while time.monotonic() < deadline:
            try:
                sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                sock.connect(path)
                self._sock = sock
                return
            except (FileNotFoundError, ConnectionRefusedError) as e:
                last_err = e
                time.sleep(0.1)
        raise QemuControlError(
            f"Could not connect to pebble_control socket at {path!r} "
            f"within {timeout}s: {last_err!r}")

    def close(self):
        if self._sock is not None:
            try:
                self._sock.close()
            finally:
                self._sock = None

    # -- sending ---------------------------------------------------------

    def send(self, protocol, data=b""):
        if self._sock is None:
            raise QemuControlError("send() called before connect()")
        if len(data) > QEMU_MAX_DATA_LEN:
            raise QemuControlError(
                f"data too long ({len(data)} > {QEMU_MAX_DATA_LEN} byte max per packet); "
                "caller must chunk")
        hdr = _HDR_STRUCT.pack(QEMU_HEADER_SIGNATURE, protocol, len(data))
        footer = _FOOTER_STRUCT.pack(QEMU_FOOTER_SIGNATURE)
        self._sock.sendall(hdr + data + footer)

    def send_spp(self, data):
        """Convenience: send raw Pebble Protocol bytes over QemuProtocol_SPP."""
        self.send(QemuProtocol_SPP, data)

    def send_bt_connected(self, connected):
        self.send(QemuProtocol_BluetoothConnection, bytes([1 if connected else 0]))

    def send_health_steps(self, steps):
        """Set today's step count on the watch directly (testing hook, see
        QemuProtocol_HealthSteps's doc comment -- no real watch accepts this
        over the air, it's QEMU/firmware-only)."""
        self.send(QemuProtocol_HealthSteps, struct.pack(">H", steps & 0xFFFF))

    # -- receiving ---------------------------------------------------------

    def _fill(self, nbytes):
        """Ensure at least `nbytes` are available at the front of
        _recv_buf, blocking on the socket as needed."""
        while len(self._recv_buf) < nbytes:
            chunk = self._sock.recv(4096)
            if not chunk:
                raise QemuControlError("pebble_control socket closed by QEMU")
            self._recv_buf.extend(chunk)

    def recv(self):
        """Block until one complete packet arrives; return (protocol, data).

        Raises QemuControlError on a framing error (bad signature) or if
        the socket is closed. A bad header signature is treated as fatal
        rather than resynchronized-past, since pebble_control.c itself
        doesn't define any recovery for it either (see its own DPRINTF-only
        handling) -- in practice this should never happen on a clean
        connection.
        """
        self._fill(_HDR_STRUCT.size)
        signature, protocol, length = _HDR_STRUCT.unpack(
            bytes(self._recv_buf[:_HDR_STRUCT.size]))
        if signature != QEMU_HEADER_SIGNATURE:
            raise QemuControlError(
                f"bad header signature 0x{signature:04x} (expected 0x{QEMU_HEADER_SIGNATURE:04x}); "
                "stream is desynchronized")
        total = _HDR_STRUCT.size + length + _FOOTER_STRUCT.size
        self._fill(total)
        data = bytes(self._recv_buf[_HDR_STRUCT.size:_HDR_STRUCT.size + length])
        (footer_sig,) = _FOOTER_STRUCT.unpack(
            bytes(self._recv_buf[_HDR_STRUCT.size + length:total]))
        del self._recv_buf[:total]
        if footer_sig != QEMU_FOOTER_SIGNATURE:
            raise QemuControlError(
                f"bad footer signature 0x{footer_sig:04x} (expected 0x{QEMU_FOOTER_SIGNATURE:04x}); "
                "stream is desynchronized")
        return protocol, data
