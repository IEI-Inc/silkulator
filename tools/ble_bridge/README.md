# BLE bridge: connect a real Pebble app to the QEMU emulator

This lets your actual iPhone's Pebble companion app connect to the
QEMU-emulated silk watch, instead of (or alongside) the Tkinter
screen/button emulator.

## Why two separate processes

`pebble_emulator.py` (screen + buttons) talks to QEMU over PULSE2 on
USART1. This bridge talks to QEMU over a *different* port, USART2 ("pebble
control"), which is where the firmware's fake Bluetooth transport lives.
They're independent -- run both against the same QEMU instance if you want
the screen/buttons and a real phone connection at the same time, or just
the bridge on its own.

## Setup (one-time)

```
pip3 install pyobjc-framework-CoreBluetooth
```

(You already have the rest -- `construct`, `pyserial`, etc. -- from
setting up the regular emulator.)

## Running it

```
# Terminal 1 -- QEMU + screen/button GUI, with the BT port exposed:
python3 tools/local_qemu_emulator/pebble_emulator.py \
    --repo /path/to/silkulator \
    --bt-bridge-socket /tmp/pebble_bt.sock

# Terminal 2 -- the actual BLE bridge:
python3 tools/ble_bridge/pebble_ble_bridge.py \
    --qemu-control-socket /tmp/pebble_bt.sock
```

Wait for `pebble_emulator.py` to finish booting (you'll see the watchface
show up) before starting the bridge -- it needs the socket to already
exist. Then on your iPhone: open the Pebble app, add a new watch, and look
for "Pebble 2 HR Lime" (or whatever `--device-name` you passed) in the
scan list.

If you only care about BLE and don't need the screen, you can skip the
Tkinter GUI entirely and drive `PebbleQemuLink` headlessly with the same
`pebble_control_socket_path` option -- see `pebble_link.py`.

## What should happen, step by step

1. Bridge starts advertising. You'll see `Advertising started...` in its
   log.
2. You pick it in the Pebble app. macOS should prompt you to confirm
   pairing/bonding (this is CoreBluetooth auto-triggering bonding because
   the Pairing Trigger characteristic requires an encrypted link to write
   to -- see `pebble_ble_bridge.py`'s docstring).
3. The bridge logs `Pairing trigger written -- treating link as bonded`.
4. The bridge logs `Central subscribed to <ppogatt data char>`, then
   `PPoGATT: sending Reset Request`.
5. If the phone's PPoGATT stack recognizes our "reversed" service and
   replies, you'll see `PPoGATT: session OPEN (txw=... rxw=...)`, and from
   then on the watch should start actually talking to the app (time sync,
   app list, etc.).

## The one thing that's genuinely unverified

Step 5 above is the real unknown. Confirmed facts, straight from this
repo's own firmware source:

- The watch is supposed to be the PPoGATT **GATT client**, and in the
  *default* architecture, the **phone** hosts the PPoGATT GATT **server**
  (service UUID family `10000000-...`).
- A real, shipping Dialog DA1468x Pebble also has a **"reversed"** mode
  (service family `30000003-...`, see
  `src/bluetooth-fw/da1468x/controller/main/src/ppogatt_emulated_server_wa.c`)
  where the **watch** hosts everything instead, built specifically for
  phones that can't run their own local GATT server reliably.
- This bridge uses the reversed mode, because macOS's CoreBluetooth
  `CBPeripheralManager` API (the only way to be a GATT server at all)
  gives you zero way to act as a GATT *client* back against the thing
  that connected to you -- there's no public API path to the default
  architecture from a peripheral-role macOS/iOS app. That's a platform
  constraint, not a choice.
- What's genuinely unknown: whether the real iOS Pebble app's PPoGATT
  stack will discover and use our watch-hosted service when it never
  itself requested reversed mode (that flag normally gets set by the
  *phone* app, before pairing -- not something we control). It might just
  work (lots of GATT clients discover-and-use whatever services exist),
  or the app might specifically insist on the service family it expects
  for a first-time connection.

If step 5 never happens: that's the thing to chase next. The most useful
diagnostic is a real BLE packet trace -- on the Mac, Xcode's "Additional
Tools for Xcode" package includes PacketLogger, which can log everything
exchanged. A trace showing which service/characteristic UUIDs the phone's
app actually probes for would tell us definitively whether it's trying the
default (`10000000-...`) family instead, and that'd be the next thing to
solve for (possibly via the `CBCentralManager.retrieveConnectedPeripherals`
trick to claw back a GATT-client handle on the same link -- untried here,
but a real, documented pattern for exactly this situation).

## Known rough edges (first bring-up, not polished)

- Connection Parameters characteristic writes are no-op'd. Shouldn't block
  a basic connection, but the phone app may retry/complain if it expects a
  response to its connection-parameter negotiation.
- PPoGATT retransmit/timeout tuning (`ppogatt_engine.py`'s
  `ACK_TIMEOUT_SECS` etc.) is a reasonable first guess, not tuned against
  real radio conditions -- expect to adjust if you see spurious resets
  under real-world BLE latency/loss.
- No reconnection-without-full-reset handling (e.g. app backgrounding then
  foregrounding) -- a dropped connection currently means restarting the
  bridge.
