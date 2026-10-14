#!/usr/bin/env python3
"""
pebble_ble_bridge.py -- macOS BLE peripheral that lets the REAL Pebble
companion app on a real iPhone connect to this QEMU-emulated silk (Pebble 2)
watch.

Run this ALONGSIDE pebble_emulator.py (pointed at the same QEMU instance via
--bt-bridge-socket), on your Mac, not in a cloud sandbox: it needs real
CoreBluetooth/BLE hardware, which only macOS's `pyobjc` bindings give you.

    # Terminal 1:
    python3 pebble_emulator.py --repo /path/to/silkulator \\
        --bt-bridge-socket /tmp/pebble_bt.sock

    # Terminal 2:
    python3 pebble_ble_bridge.py --qemu-control-socket /tmp/pebble_bt.sock

Dependencies beyond the standard library:
    pip3 install pyobjc-framework-CoreBluetooth

What this does and why (see ppogatt_engine.py's docstring for the deep
dive): the firmware's QEMU build has no PPoGATT/GATT emulation at all --
its "qemu" bluetooth-fw backend just ships raw Pebble Protocol bytes over a
local socket (pebble_control_protocol.py), tagged with a 1-byte "phone
connected" flag. A real phone's Pebble app, on the other hand, speaks real
GATT against what it expects to be real Pebble BLE silicon: a Pairing
Service (0xFED9) for bonding/MTU/connection-parameter negotiation, plus a
PPoGATT data service for the actual Pebble Protocol byte stream, framed
with sequence numbers and acks.

So this script is the actual "fake Pebble" from the phone's point of view:
  - Advertises as a BLE peripheral and hosts the Pairing Service, with the
    exact characteristic UUIDs/semantics transcribed directly from this
    repo's own src/include/bluetooth/pebble_pairing_service.h and
    pebble_bt.h (not reverse-engineered or guessed).
  - Hosts the "reversed PPoGATT" service (UUID 0x30000003 family) instead
    of the default one (0x10000000 family). This isn't a workaround on our
    end -- it's lifted straight from the real firmware's own
    src/bluetooth-fw/da1468x/controller/main/src/ppogatt_emulated_server_wa.c,
    which exists in shipping Pebbles for phones that can't run their own
    local GATT server. We need the watch-hosts-everything variant for a
    structural reason: CoreBluetooth's CBPeripheralManager API (the only
    way an app gets a GATT *server* role at all) gives you read/write/
    subscribe *requests*, never a handle to act as a GATT *client* back
    against the central that connected to you -- so this bridge cannot
    discover or use a GATT server the phone might expose for the default
    PPoGATT direction. See the module docstring in ppogatt_engine.py for
    exactly how the ARQ protocol itself (reset handshake, sequence
    numbers, acks) stays unchanged under this remap.
  - Runs a PPoGATTEngine (see ppogatt_engine.py) per connected phone to
    speak the actual ARQ protocol, and relays its reassembled Pebble
    Protocol bytes to/from the QEMU pebble_control socket via
    pebble_control_protocol.py.

KNOWN UNCERTAINTY -- read before debugging a failed connection: whether the
real iOS Pebble app will use this "reversed" PPoGATT service at all is the
single biggest open question here, and it is NOT fully resolved by source
inspection alone (see the design discussion this was built from). The real
firmware only enables reversed mode when the *phone* writes a specific bit
in its Pairing Trigger write *before* pairing, and that's phone-app
decision logic this bridge doesn't control. If the phone's app insists on
the default (phone-hosts-PPoGATT-server) architecture and never falls back,
this bridge's data channel won't be discovered even though pairing itself
may succeed. If you hit that: capture a BLE trace (Settings > Developer >
Bluetooth, or Apple's PacketLogger via Xcode's Additional Tools) showing
what services/characteristics the phone actually requests, and we can
adjust from there. This is exactly the kind of thing that can only be
nailed down against the real app on real hardware -- there's no way to
validate CoreBluetooth behavior from a cloud sandbox.
"""

import argparse
import os
import struct
import sys
import threading

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "local_qemu_emulator"))
from pebble_control_protocol import (  # noqa: E402
    QemuControlSocket, QemuProtocol_SPP, QemuControlError,
)
from ppogatt_engine import PPoGATTEngine  # noqa: E402

try:
    from Foundation import NSObject, NSData
    from CoreBluetooth import (
        CBPeripheralManager,
        CBMutableService,
        CBMutableCharacteristic,
        CBUUID,
        CBATTRequest,
        CBCharacteristicPropertyRead,
        CBCharacteristicPropertyWrite,
        CBCharacteristicPropertyNotify,
        CBCharacteristicPropertyWriteWithoutResponse,
        CBAttributePermissionsReadable,
        CBAttributePermissionsWriteable,
        CBAttributePermissionsWriteEncryptionRequired,
        CBManagerStatePoweredOn,
        CBATTErrorSuccess,
        CBATTErrorRequestNotSupported,
        CBAdvertisementDataLocalNameKey,
        CBAdvertisementDataServiceUUIDsKey,
    )
    from PyObjCTools import AppHelper
except ImportError as e:
    print("ERROR: this script needs pyobjc's CoreBluetooth bindings, and only "
          "runs on macOS.\n  pip3 install pyobjc-framework-CoreBluetooth\n"
          f"(import failed with: {e})", file=sys.stderr)
    sys.exit(1)


def _pebble_uuid(value_32bit):
    """Expand a 32-bit value using Pebble's base UUID
    (XXXXXXXX-328E-0FBB-C642-1AA6699BDADA), transcribed directly from
    src/include/bluetooth/pebble_bt.h's PEBBLE_BT_UUID_EXPAND macro."""
    return CBUUID.UUIDWithString_(
        f"{value_32bit:08X}-328E-0FBB-C642-1AA6699BDADA")


# -- Pairing Service (0xFED9) -- src/include/bluetooth/pebble_bt.h,
#    src/include/bluetooth/pebble_pairing_service.h
PAIRING_SERVICE_UUID = CBUUID.UUIDWithString_("0000FED9-0000-1000-8000-00805F9B34FB")
CONNECTIVITY_STATUS_UUID = _pebble_uuid(1)
PAIRING_TRIGGER_UUID = _pebble_uuid(2)
GATT_MTU_UUID = _pebble_uuid(3)
CONNECTION_PARAMETERS_UUID = _pebble_uuid(5)

# -- "Reversed" / watch-hosted PPoGATT service -- see module docstring and
#    src/include/bluetooth/pebble_bt.h's PEBBLE_BT_PPOGATT_WATCH_SERVER_*
PPOGATT_WS_SERVICE_UUID = _pebble_uuid(0x30000003)
PPOGATT_WS_DATA_UUID = _pebble_uuid(0x30000004)       # notify (bridge -> phone)
PPOGATT_WS_META_UUID = _pebble_uuid(0x30000005)       # read
PPOGATT_WS_DATA_WR_UUID = _pebble_uuid(0x30000006)    # write-without-response (phone -> bridge)

DEFAULT_ATT_MTU = 23  # BLE's minimum/default, until the phone negotiates up


def _connectivity_status_bytes(connected, bonded, encrypted):
    # PebblePairingServiceConnectivityStatus (pebble_pairing_service.h):
    # a little-endian 32-bit bitfield; byte 3 is last_pairing_result (0 = ok).
    byte0 = (1 if connected else 0) | ((1 if bonded else 0) << 1) | ((1 if encrypted else 0) << 2)
    return bytes([byte0, 0, 0, 0])


def _ppogatt_meta_bytes():
    # PPoGATTMetaV1: min_version(u8)=1, max_version(u8)=1, app_uuid(16 zero
    # bytes = the "system" UUID, so the session maps to
    # TransportDestinationSystem), pp_session_type(u8)=Hybrid(1). Mirrors
    # exactly what ppogatt_emulated_server_wa.c's prv_handle_emulated_meta_read
    # returns on real hardware.
    return bytes([1, 1]) + (b"\x00" * 16) + bytes([1])


class BridgeDelegate(NSObject):
    """CBPeripheralManagerDelegate. PyObjC dispatches these by Objective-C
    selector name (trailing underscores stand in for ':' argument
    separators) -- this is not a typo-prone hand-rolled naming scheme, it's
    how PyObjC maps CoreBluetooth's delegate protocol.
    """

    def initWithBridge_(self, bridge):
        self = super().init()
        if self is None:
            return None
        self.bridge = bridge
        return self

    def peripheralManagerDidUpdateState_(self, peripheral):
        if peripheral.state() == CBManagerStatePoweredOn:
            self.bridge.log("Bluetooth powered on, publishing services...")
            self.bridge.publish_services()
        else:
            self.bridge.log(f"Bluetooth manager state changed: {peripheral.state()}")

    def peripheralManager_didAddService_error_(self, peripheral, service, error):
        if error is not None:
            self.bridge.log(f"ERROR adding service {service.UUID()}: {error}")
            return
        self.bridge.log(f"Service added: {service.UUID()}")
        self.bridge.on_service_added()

    def peripheralManagerDidStartAdvertising_error_(self, peripheral, error):
        if error is not None:
            self.bridge.log(f"ERROR starting advertising: {error}")
        else:
            self.bridge.log("Advertising started -- look for this watch in the "
                             "Pebble app now.")

    def peripheralManager_didReceiveReadRequest_(self, peripheral, request):
        value = self.bridge.read_value_for(request.characteristic().UUID())
        if value is None:
            peripheral.respondToRequest_withResult_(request, CBATTErrorRequestNotSupported)
            return
        request.setValue_(NSData.dataWithBytes_length_(value, len(value)))
        peripheral.respondToRequest_withResult_(request, CBATTErrorSuccess)

    def peripheralManager_didReceiveWriteRequests_(self, peripheral, requests):
        for request in requests:
            data = bytes(request.value())
            self.bridge.handle_write(request.characteristic().UUID(), data)
        if requests:
            peripheral.respondToRequest_withResult_(requests[0], CBATTErrorSuccess)

    def peripheralManager_central_didSubscribeToCharacteristic_(
            self, peripheral, central, characteristic):
        self.bridge.on_subscribe(central, characteristic.UUID())

    def peripheralManager_central_didUnsubscribeFromCharacteristic_(
            self, peripheral, central, characteristic):
        self.bridge.on_unsubscribe(central, characteristic.UUID())

    def peripheralManagerIsReadyToUpdateSubscribers_(self, peripheral):
        self.bridge.on_ready_to_update()


class Bridge:
    def __init__(self, qemu_control_socket_path, device_name):
        self.device_name = device_name
        self.qemu_control_socket_path = qemu_control_socket_path

        self.delegate = BridgeDelegate.alloc().initWithBridge_(self)
        self.manager = CBPeripheralManager.alloc().initWithDelegate_queue_(
            self.delegate, None)

        self.characteristics = {}  # uuid -> CBMutableCharacteristic
        self._services_added = 0
        self._mtu = DEFAULT_ATT_MTU
        self._bonded = False
        self._pending_notify_queue = []  # list of (characteristic, data)

        self.qemu = QemuControlSocket()
        self.engine = PPoGATTEngine(
            send_to_phone=self._notify_data,
            deliver_pp_bytes=self._deliver_to_qemu,
            get_mtu=lambda: max(self._mtu - 3, 20),  # -3 for ATT opcode+handle
            log=self.log,
        )

    def log(self, msg):
        print(f"[bridge] {msg}", flush=True)

    # -- GATT service/characteristic construction -------------------------

    def _make_char(self, uuid, properties, permissions, initial_value=None):
        value = None
        if initial_value is not None:
            value = NSData.dataWithBytes_length_(initial_value, len(initial_value))
        char = CBMutableCharacteristic.alloc().initWithType_properties_value_permissions_(
            uuid, properties, None, permissions)
        self.characteristics[uuid] = char
        return char

    def publish_services(self):
        pairing_service = CBMutableService.alloc().initWithType_primary_(
            PAIRING_SERVICE_UUID, True)
        connectivity = self._make_char(
            CONNECTIVITY_STATUS_UUID,
            CBCharacteristicPropertyRead | CBCharacteristicPropertyNotify,
            CBAttributePermissionsReadable)
        pairing_trigger = self._make_char(
            PAIRING_TRIGGER_UUID,
            CBCharacteristicPropertyWrite,
            # Writing to this characteristic is what should kick off real
            # BLE bonding -- requiring link encryption to even accept the
            # write makes CoreBluetooth drive the system pairing dialog
            # automatically, before our delegate ever sees the write.
            CBAttributePermissionsWriteEncryptionRequired)
        mtu_char = self._make_char(
            GATT_MTU_UUID,
            CBCharacteristicPropertyRead | CBCharacteristicPropertyNotify,
            CBAttributePermissionsReadable)
        conn_params = self._make_char(
            CONNECTION_PARAMETERS_UUID,
            CBCharacteristicPropertyRead | CBCharacteristicPropertyNotify
            | CBCharacteristicPropertyWrite,
            CBAttributePermissionsReadable | CBAttributePermissionsWriteable)
        pairing_service.setCharacteristics_(
            [connectivity, pairing_trigger, mtu_char, conn_params])
        self.manager.addService_(pairing_service)

        ppogatt_service = CBMutableService.alloc().initWithType_primary_(
            PPOGATT_WS_SERVICE_UUID, True)
        data_char = self._make_char(
            PPOGATT_WS_DATA_UUID,
            CBCharacteristicPropertyNotify,
            CBAttributePermissionsReadable)
        meta_char = self._make_char(
            PPOGATT_WS_META_UUID,
            CBCharacteristicPropertyRead,
            CBAttributePermissionsReadable)
        data_wr_char = self._make_char(
            PPOGATT_WS_DATA_WR_UUID,
            CBCharacteristicPropertyWriteWithoutResponse,
            CBAttributePermissionsWriteable)
        ppogatt_service.setCharacteristics_([data_char, meta_char, data_wr_char])
        self.manager.addService_(ppogatt_service)

    def on_service_added(self):
        self._services_added += 1
        if self._services_added == 2:
            self.log("Both services published, starting advertisement...")
            self.manager.startAdvertising_({
                CBAdvertisementDataLocalNameKey: self.device_name,
                CBAdvertisementDataServiceUUIDsKey: [PAIRING_SERVICE_UUID],
            })

    # -- reads / writes / subscriptions ------------------------------------

    def read_value_for(self, uuid):
        if uuid == CONNECTIVITY_STATUS_UUID.UUIDString():
            pass  # fallthrough below handles string/CBUUID both
        s = uuid.UUIDString() if hasattr(uuid, "UUIDString") else str(uuid)
        if s == CONNECTIVITY_STATUS_UUID.UUIDString():
            return _connectivity_status_bytes(True, self._bonded, self._bonded)
        if s == GATT_MTU_UUID.UUIDString():
            return struct.pack("<H", self._mtu)
        if s == PPOGATT_WS_META_UUID.UUIDString():
            return _ppogatt_meta_bytes()
        return None

    def handle_write(self, uuid, data):
        s = uuid.UUIDString() if hasattr(uuid, "UUIDString") else str(uuid)
        if s == PAIRING_TRIGGER_UUID.UUIDString():
            # By the time this fires, CoreBluetooth already required (and
            # thus triggered) link encryption/bonding for us.
            self._bonded = True
            self.log("Pairing trigger written -- treating link as bonded, "
                     "notifying connectivity status")
            self._notify(CONNECTIVITY_STATUS_UUID,
                         _connectivity_status_bytes(True, True, True))
        elif s == GATT_MTU_UUID.UUIDString():
            pass  # phone-initiated MTU exchange is handled by CoreBluetooth itself
        elif s == CONNECTION_PARAMETERS_UUID.UUIDString():
            pass  # not implemented: acceptable to no-op for a first bring-up
        elif s == PPOGATT_WS_DATA_WR_UUID.UUIDString():
            self.engine.handle_packet_from_phone(data)
        else:
            self.log(f"Unexpected write to {s}")

    def on_subscribe(self, central, uuid):
        s = uuid.UUIDString() if hasattr(uuid, "UUIDString") else str(uuid)
        self.log(f"Central subscribed to {s}")
        if s == PPOGATT_WS_DATA_UUID.UUIDString():
            self.engine.handle_remote_subscribed()
        elif s == CONNECTIVITY_STATUS_UUID.UUIDString():
            self._notify(CONNECTIVITY_STATUS_UUID,
                         _connectivity_status_bytes(True, self._bonded, self._bonded))

    def on_unsubscribe(self, central, uuid):
        self.log(f"Central unsubscribed from "
                 f"{uuid.UUIDString() if hasattr(uuid, 'UUIDString') else uuid}")

    def on_ready_to_update(self):
        # CoreBluetooth asked for more room in its outbound notification
        # queue; flush anything we were holding back.
        pending = self._pending_notify_queue
        self._pending_notify_queue = []
        for characteristic, data in pending:
            self._notify_raw(characteristic, data)

    # -- notification plumbing ---------------------------------------------

    def _notify(self, uuid, data):
        char = self.characteristics.get(uuid)
        if char is None:
            return
        self._notify_raw(char, data)

    def _notify_raw(self, characteristic, data):
        ok = self.manager.updateValue_forCharacteristic_onSubscribedCentrals_(
            NSData.dataWithBytes_length_(data, len(data)), characteristic, None)
        if not ok:
            # CoreBluetooth's outbound queue is full; it will call
            # peripheralManagerIsReadyToUpdateSubscribers_ once there's
            # room, and on_ready_to_update() will retry this.
            self._pending_notify_queue.append((characteristic, data))

    def _notify_data(self, packet_bytes):
        """PPoGATTEngine's send_to_phone callback."""
        char = self.characteristics.get(PPOGATT_WS_DATA_UUID)
        if char is None:
            return
        self._notify_raw(char, packet_bytes)

    # -- QEMU relay ----------------------------------------------------------

    def _deliver_to_qemu(self, data):
        """PPoGATTEngine's deliver_pp_bytes callback -- reassembled, in-order
        Pebble Protocol bytes, ready to ship to the firmware."""
        try:
            self.qemu.send_spp(data)
        except QemuControlError as e:
            self.log(f"Failed to relay bytes to QEMU: {e}")

    def _qemu_reader_loop(self):
        while True:
            try:
                protocol, data = self.qemu.recv()
            except QemuControlError as e:
                self.log(f"QEMU control socket reader stopped: {e}")
                return
            if protocol == QemuProtocol_SPP:
                self.engine.queue_pp_bytes(data)
            # Other protocols (tap, compass, battery, ...) aren't BLE-bridge
            # concerns; this script only cares about the SPP/PP byte stream.

    def start_qemu_relay(self):
        self.log(f"Connecting to QEMU control socket at {self.qemu_control_socket_path} ...")
        self.qemu.connect(self.qemu_control_socket_path, timeout=15)
        self.log("Connected. Telling firmware a phone is connected...")
        self.qemu.send_bt_connected(True)
        threading.Thread(target=self._qemu_reader_loop, daemon=True).start()


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                      formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--qemu-control-socket", required=True,
                         help="Path to the unix socket pebble_emulator.py was started "
                              "with --bt-bridge-socket pointed at.")
    parser.add_argument("--device-name", default="Pebble 2 HR Lime",
                         help="BLE advertised local name (default mimics a real silk's "
                              "factory name so the Pebble app's scanner recognizes it).")
    args = parser.parse_args()

    bridge = Bridge(args.qemu_control_socket, args.device_name)
    # Connect to QEMU first so we fail fast with a clear message if the
    # emulator isn't running yet, before we start advertising to the world.
    bridge.start_qemu_relay()

    print("Bridge running. Open the Pebble app on your iPhone and look for "
          f"\"{args.device_name}\". Ctrl-C to stop.")
    AppHelper.runConsoleEventLoop()


if __name__ == "__main__":
    main()
