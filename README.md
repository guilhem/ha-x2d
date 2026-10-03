# X2D shutters over USB

Control X2D shutters with **Home Assistant's built-in MySensors integration**,
a YD-RP2040 and a CC1101. The dongle owns the radio identities, associations
and rolling counters. Connect the dongle directly to Home Assistant over USB.

```text
Home Assistant / MySensors → USB → RP2040 / x2d-core → CC1101 → shutter
```

**Experimental radio support.** Standard firmware **0.5.0** enables RF commands
for paired shutters by default. New enrollment requires a supervised trial build.
Association and motor commands still need qualification with this firmware on real hardware.
See [hardware qualification](home_assistant/README.md#hardware-qualification).

## Connect to Home Assistant

1. Build and flash the journal-protecting UF2 described in the
   [firmware guide](firmware/README.md).
2. Add **MySensors** in Settings → Devices & services. Choose **Serial**, the
   dongle's `/dev/serial/by-id/…` path, **115200 baud** and version **2.3**
   (the default 1.4 is unsuitable).
3. Add the following to `configuration.yaml`, merging any existing
   `homeassistant` section, then reload customizations or restart HA:

   ```yaml
   homeassistant:
     customize_glob:
       "cover.x2d_*":
         assumed_state: true
         device_class: shutter
   ```

4. Create the two native [pairing buttons](home_assistant/README.md#pairing-buttons),
   attached to the MySensors device, and hide their underlying switches while
   keeping them enabled. Follow the
   [pairing procedure](home_assistant/README.md#entities-and-pairing) to associate
   each shutter. Pairing currently requires a supervised trial build.

Associated shutters appear automatically, up to 16. Their initial entity IDs
start with `cover.x2d_`; keep that prefix when editing an entity ID. Display
names can be changed freely. Each cover exposes open, close and STOP, without
a position slider. Pairing and confirmation use momentary button helpers,
editable in the HA UI; MySensors supplies their hidden switch transports and
a read-only diagnostic sensor.

A completed transmission updates an **assumed** open/closed state. It does not
prove reception or measure travel. STOP, reboot, USB loss and uncertain radio
output invalidate that estimate. Native MySensors displays “open” for unknown
position by convention; `pos_unknown` in the diagnostic explains this.
Native HA can retain displayed states after unplugging the dongle: an entity
that looks available is **not** proof that commands can reach its motor.

See the [installation and pairing guide](home_assistant/README.md) for setup,
diagnostics and updates.

## Build and check

On Linux, with [devenv](https://devenv.sh/getting-started/) and Nix installed:

```sh
git submodule update --init --recursive
devenv shell
devenv test
devenv tasks run firmware:build
```

The default UF2 enables normal shutter RF commands after pairing, without
enabling new enrollment. No separate commands build is needed. Build tasks
never flash a board and check the protected journal range.

Software validation targets **HA 2026.9.4**, **pymysensors 0.26.0**, MySensors
**2.3**, Arduino-Pico **6.1.1**, and the pinned
[`x2d-core`](https://github.com/guilhem/x2d-core) submodule. Native C++ tests cover
radio scheduling, durable reservations, recovery, STOP priority, the shared
controller and serial parsing. Python tests drive the real MySensors/HA stack
over a PTY connected to that same C++ adapter, with simulated flash and radio.
CI also builds RP2040 and the [ESPHome adapter](https://github.com/guilhem/esphome-x2d).
Software checks are separate from HA OS / dongle / motor qualification.

## Source layout

| Location | Responsibility |
| --- | --- |
| `firmware/ha_x2d/` | MySensors adapter, USB identity, flash mapping, SPI and PIO/DMA output |
| `lib/x2d-core/` | Portable X2D codec, journal, STOP runtime, association controller and CC1101 driver |
| `tests/mysensors_server.cpp` | Dongle adapter with simulated radio/flash for native HA tests |
| `tests/test_mysensors.py` | Native HA and pymysensors over simulated USB |
| `firmware/rx_debug/`, `tools/` | Passive capture and offline radio analysis |

[Serial contract](docs/USB_PROTOCOL.md) · [Radio observations](docs/OBSERVATIONS_RADIO.md)

The radio transform derives from mr-sven's work under
[Apache-2.0](research/LICENSE-APACHE); see the core's LICENSE and NOTICE.

Firmware updates can use the existing USB connection: [install and update the dongle](docs/FIRMWARE_UPDATE.md).
