"""
ppogatt_engine.py -- a from-scratch implementation of the PPoGATT wire
protocol (reset handshake, windowed sequence numbers, acking,
retransmission), independent of any particular GATT library, so it can be
unit-tested without a real Bluetooth stack and reused from the macOS
CoreBluetooth bridge.

Why this needs to exist at all: you'd think the QEMU firmware would already
speak PPoGATT and all this bridge has to do is shuttle GATT bytes back and
forth. It doesn't. Confirmed directly from source (src/fw/drivers/qemu/
qemu_serial.c's s_qemu_endpoints table and src/bluetooth-fw/qemu/
qemu_transport.c): under a QEMU build, QemuProtocol_SPP goes straight to
qemu_transport_handle_received_data(), a completely separate Transport
implementation from kernel_le_client/ppogatt/ppogatt.c. It ships raw,
unchunked, unacked Pebble Protocol bytes over a lossless local socket --
there is no PPoGATT framing anywhere inside the firmware when it's built
for QEMU. (Empirically confirmed too: telling the firmware
"connected=true" over that socket produced a bare Pebble Protocol packet
with no reset/ack wrapper whatsoever.)

So from a real phone's point of view, *this bridge* is the only thing on
the watch's end of the connection that can plausibly speak PPoGATT, and it
has to implement the real protocol faithfully -- not a shortcut -- because
a real Pebble app's PPoGATT stack (kernel_le_client/ppogatt/ppogatt.c) is
going to drive the actual reset/window/ack state machine against it exactly
as if it were talking to real silicon.

Protocol details below are transcribed directly from
src/fw/comm/ble/kernel_le_client/ppogatt/ppogatt_internal.h and .../ppogatt.c
in this repo -- not guessed, not reverse-engineered from a decompile.

IMPORTANT ROLE NOTE: on real hardware the *watch* is the PPoGATT/ARQ
"client" (it's the side that discovers the Meta/Data characteristics,
reads Meta, subscribes to Data notifications, and kicks off the reset
handshake by sending a Reset Request). This bridge stands in for the
watch, so it plays that same client role -- see PPoGATTEngine.state
transitions below, which mirror ppogatt.c's state machine on the "client"
side 1:1. The one thing that's flipped relative to default/non-reversed
PPoGATT is which GATT operations carry those bytes: in the "reversed
PPoGATT" mode this bridge uses (see bluetooth-fw/da1468x/controller/main/
src/ppogatt_emulated_server_wa.c for the real firmware's own version of
this same trick), the client's outbound writes become GATT *notifications*
the bridge sends, and the far end's notifications become GATT *writes* the
bridge receives -- a transport-level remap only. The ARQ state machine
itself (who sends Reset Request, how sn/ack bookkeeping works) is
unchanged from the default mode and is exactly mirrored here.
"""

import os
import struct
import threading
import time


# -- Wire format (ppogatt_internal.h) -------------------------------------

PPoGATTPacketTypeData = 0x0
PPoGATTPacketTypeAck = 0x1
PPoGATTPacketTypeResetRequest = 0x2
PPoGATTPacketTypeResetComplete = 0x3

PPOGATT_SN_BITS = 5
PPOGATT_SN_MOD_DIV = 1 << PPOGATT_SN_BITS  # 32
PPOGATT_V0_WINDOW_SIZE = 4
PPOGATT_MIN_VERSION = 0x00
PPOGATT_MAX_VERSION = 0x01
MFG_SERIAL_NUMBER_SIZE = 12

# Real firmware ticks this every 2s with a 3-tick (so ~5-6s) effective
# timeout; the exact cadence doesn't matter to us since we aren't sharing a
# hardware timer with anything else, just the rough magnitude.
ACK_TIMEOUT_SECS = 5.0
MAX_CONSECUTIVE_TIMEOUTS = 2
RESET_TIMEOUT_SECS = 5.0


def _pack_header(packet_type, sn):
    # PPoGATTPacket: 3 bits type, 5 bits sn, packed into a single byte, type
    # in the low bits (see the C bitfield order: `type:3` declared first in
    # a PACKED struct on a little-endian ARM target puts `type` in the
    # low 3 bits).
    return bytes([(packet_type & 0x7) | ((sn & (PPOGATT_SN_MOD_DIV - 1)) << 3)])


def _unpack_header(byte0):
    packet_type = byte0 & 0x7
    sn = (byte0 >> 3) & (PPOGATT_SN_MOD_DIV - 1)
    return packet_type, sn


def _next_sn(sn):
    return (sn + 1) % PPOGATT_SN_MOD_DIV


def _sn_distance(begin_incl, end_excl):
    return (PPOGATT_SN_MOD_DIV + end_excl - begin_incl) % PPOGATT_SN_MOD_DIV


class PPoGATTEngine:
    """One PPoGATT session with a single connected phone.

    Callbacks the owner must provide:
      send_to_phone(payload_bytes) -- deliver one complete PPoGATT packet
          (header byte + chunk) to the phone, i.e. as a GATT notification
          on the Data characteristic (or notification-equivalent in
          whatever reversed-mode remap is in use).
      deliver_pp_bytes(data) -- hand fully-reassembled, in-order Pebble
          Protocol bytes up to whatever is relaying them to the QEMU
          pebble_control socket.
      get_mtu() -- current negotiated ATT MTU (bytes), used to size
          outbound chunks. Must subtract its own overhead; this engine
          subtracts the PPoGATT header (1 byte) only -- caller's get_mtu()
          should already reflect the *payload* capacity of one GATT
          write/notification (i.e. ATT_MTU - 3 already accounted for).
    """

    def __init__(self, send_to_phone, deliver_pp_bytes, get_mtu, log=None):
        self._send_to_phone = send_to_phone
        self._deliver_pp_bytes = deliver_pp_bytes
        self._get_mtu = get_mtu
        self._log = log or (lambda msg: None)

        self._lock = threading.RLock()
        self._state = "awaiting_subscribe"  # -> awaiting_reset_complete -> open
        self._version = PPOGATT_MAX_VERSION

        self._next_expected_data_sn = 0  # inbound

        self._next_data_sn = 0           # outbound
        self._next_expected_ack_sn = 0   # outbound
        self._tx_window_size = PPOGATT_V0_WINDOW_SIZE
        self._rx_window_size = PPOGATT_V0_WINDOW_SIZE
        self._outstanding = {}  # sn -> payload bytes, awaiting ack
        self._send_order = []   # sns in the order they were sent, oldest first

        self._out_queue = bytearray()  # raw PP bytes not yet chunked/sent

        self._ack_deadline = None
        self._reset_deadline = None
        self._timeout_count = 0

        self._stopping = False
        self._timer_thread = threading.Thread(target=self._timer_loop, daemon=True)
        self._timer_thread.start()

    def stop(self):
        with self._lock:
            self._stopping = True

    # -- driven by GATT events ------------------------------------------

    def handle_remote_subscribed(self):
        """Call when the phone subscribes (enables notifications) to the
        Data characteristic. This is the real trigger, on real hardware,
        for the watch to kick off the reset handshake."""
        with self._lock:
            if self._state != "awaiting_subscribe":
                return
            self._start_reset(self_initiated=True)

    def handle_packet_from_phone(self, value):
        """Call with the raw bytes written by the phone to the Data-write
        characteristic (or notified by it, in non-reversed mode)."""
        if not value:
            self._log("PPoGATT: zero-length packet from phone, ignoring")
            return
        packet_type, sn = _unpack_header(value[0])
        payload = bytes(value[1:])
        with self._lock:
            if packet_type == PPoGATTPacketTypeResetRequest:
                self._log("PPoGATT: got Reset Request from phone")
                self._handle_reset_request()
                return
            if self._state == "open":
                if packet_type == PPoGATTPacketTypeData:
                    self._handle_data(sn, payload)
                elif packet_type == PPoGATTPacketTypeAck:
                    self._handle_ack(sn)
                else:
                    self._log(f"PPoGATT: unexpected ResetComplete while open")
            elif self._state == "awaiting_reset_complete":
                if packet_type == PPoGATTPacketTypeResetComplete:
                    self._handle_reset_complete(payload)
                else:
                    self._log(f"PPoGATT: ignoring packet type {packet_type} "
                              f"while resetting")
            else:
                self._log(f"PPoGATT: ignoring packet in state {self._state}")

    # -- driven by the QEMU-side relay -----------------------------------

    def queue_pp_bytes(self, data):
        """Raw Pebble Protocol bytes that arrived from the firmware
        (QemuProtocol_SPP) and need to go out to the phone."""
        with self._lock:
            self._out_queue.extend(data)
            self._pump_output()

    # -- internals: reset handshake ---------------------------------------

    def _start_reset(self, self_initiated):
        self._next_expected_data_sn = 0
        self._next_data_sn = 0
        self._next_expected_ack_sn = 0
        self._outstanding.clear()
        self._send_order.clear()
        self._out_queue.clear()
        self._tx_window_size = self._rx_window_size = PPOGATT_V0_WINDOW_SIZE
        self._timeout_count = 0

        if self_initiated:
            self._state = "awaiting_reset_complete"
            self._self_initiated_reset = True
            payload = bytes([self._version]) + (b"SILKBRDG\x00\x00\x00\x00")[:MFG_SERIAL_NUMBER_SIZE]
            packet = _pack_header(PPoGATTPacketTypeResetRequest, 0) + payload
            self._log("PPoGATT: sending Reset Request")
        else:
            self._state = "awaiting_reset_complete"
            self._self_initiated_reset = False
            # V1 Reset Complete payload: our rx/tx window sizes
            payload = bytes([self._rx_window_size, self._tx_window_size])
            packet = _pack_header(PPoGATTPacketTypeResetComplete, 0) + payload
            self._log("PPoGATT: sending Reset Complete (remote-initiated)")

        self._reset_deadline = time.monotonic() + RESET_TIMEOUT_SECS
        self._send_to_phone(packet)

    def _handle_reset_request(self):
        # A Reset Request from the phone at any point means: start over,
        # we are now doing a remote-initiated reset.
        self._start_reset(self_initiated=False)

    def _handle_reset_complete(self, payload):
        if len(payload) >= 2:
            remote_max_rx, remote_max_tx = payload[0], payload[1]
            self._tx_window_size = min(self._tx_window_size, remote_max_rx)
            self._rx_window_size = min(self._rx_window_size, remote_max_tx)

        if getattr(self, "_self_initiated_reset", False):
            # Mirror ppogatt.c: upon seeing the phone's Reset Complete while
            # we were the one who initiated the reset, we echo back our own
            # Reset Complete too. Only then is the session open.
            packet = _pack_header(PPoGATTPacketTypeResetComplete, 0)
            self._send_to_phone(packet)

        self._state = "open"
        self._reset_deadline = None
        self._timeout_count = 0
        self._log(f"PPoGATT: session OPEN (txw={self._tx_window_size} "
                  f"rxw={self._rx_window_size})")
        self._pump_output()

    # -- internals: inbound data/ack --------------------------------------

    def _handle_data(self, sn, payload):
        if sn == self._next_expected_data_sn:
            # Ack immediately -- we aren't implementing the firmware's
            # coalesced-ack optimization, simplicity over squeezing out
            # throughput is the right tradeoff for a bridge like this.
            ack = _pack_header(PPoGATTPacketTypeAck, sn)
            self._send_to_phone(ack)
            self._next_expected_data_sn = _next_sn(sn)
            self._deliver_pp_bytes(payload)
        else:
            self._log(f"PPoGATT: out-of-order data sn={sn} expected="
                      f"{self._next_expected_data_sn}, dropping (relying on "
                      f"sender retransmit)")

    def _handle_ack(self, sn):
        if sn not in self._outstanding:
            self._log(f"PPoGATT: ack for unknown/already-acked sn={sn}, ignoring")
            return
        # Acks are cumulative up to and including sn, in sn order.
        while self._send_order and self._send_order[0] != _next_sn(sn):
            acked_sn = self._send_order.pop(0)
            self._outstanding.pop(acked_sn, None)
            self._next_expected_ack_sn = _next_sn(acked_sn)
            if acked_sn == sn:
                break
        self._timeout_count = 0
        self._ack_deadline = time.monotonic() + ACK_TIMEOUT_SECS if self._outstanding else None
        self._pump_output()

    # -- internals: outbound data chunking/windowing ----------------------

    def _pump_output(self):
        if self._state != "open":
            return
        max_payload = max(self._get_mtu() - 1, 1)  # -1 for the PPoGATT header byte
        while self._out_queue and len(self._outstanding) < self._tx_window_size:
            chunk = bytes(self._out_queue[:max_payload])
            del self._out_queue[:len(chunk)]
            sn = self._next_data_sn
            packet = _pack_header(PPoGATTPacketTypeData, sn) + chunk
            self._outstanding[sn] = chunk
            self._send_order.append(sn)
            self._next_data_sn = _next_sn(sn)
            if self._ack_deadline is None:
                self._ack_deadline = time.monotonic() + ACK_TIMEOUT_SECS
            self._send_to_phone(packet)

    # -- timers ------------------------------------------------------------

    def _timer_loop(self):
        while True:
            time.sleep(0.5)
            with self._lock:
                if self._stopping:
                    return
                now = time.monotonic()
                if self._reset_deadline is not None and now >= self._reset_deadline:
                    self._log("PPoGATT: reset handshake timed out, retrying")
                    self._start_reset(self_initiated=True)
                elif self._ack_deadline is not None and now >= self._ack_deadline:
                    self._timeout_count += 1
                    if self._timeout_count >= MAX_CONSECUTIVE_TIMEOUTS:
                        self._log("PPoGATT: too many ack timeouts, resetting session")
                        self._start_reset(self_initiated=True)
                    else:
                        self._log("PPoGATT: ack timeout, retransmitting outstanding window")
                        self._retransmit_outstanding()

    def _retransmit_outstanding(self):
        self._ack_deadline = time.monotonic() + ACK_TIMEOUT_SECS
        for sn in self._send_order:
            payload = self._outstanding[sn]
            packet = _pack_header(PPoGATTPacketTypeData, sn) + payload
            self._send_to_phone(packet)
