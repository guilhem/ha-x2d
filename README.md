# X2D shutters over USB

Control X2D shutters with **Home Assistant's built-in MySensors integration**,
a YD-RP2040 and a CC1101. The dongle owns the radio identities, associations
and rolling counters. No HACS component, companion service or MQTT broker.

```text
Home Assistant / MySensors → USB → RP2040 / x2d-core → CC1101 → shutter
```

**Experimental radio support.** Default builds cannot transmit. The historical
JSONL firmware controlled one France Fermetures / Well’com motor, including
open, close and STOP, with its original remotes still working. This evidence
does **not** qualify the MySensors firmware, fresh enrollment or other motors.
See [hardware qualification](home_assistant/README.md#hardware-qualification).

## Connect to Home Assistant

1. Build and flash the journal-protecting UF2 described in the
   [firmware guide](firmware/README.md). Existing paired slots are retained.
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

Associated shutters appear automatically, up to 16. Their initial entity IDs
start with `cover.x2d_`; keep that prefix when editing an entity ID. Display
names can be changed freely. Each cover exposes open, close and STOP, without
a position slider. MySensors also presents momentary pairing/confirmation
switches and a read-only diagnostic sensor.

A completed transmission updates an **assumed** open/closed state. It does not
prove reception or measure travel. STOP, reboot, USB loss and uncertain radio
output invalidate that estimate. Native MySensors displays “open” for unknown
position by convention; `pos_unknown` in the diagnostic explains this.
Native HA can retain displayed states after unplugging the dongle: an entity
that looks available is **not** proof that commands can reach its motor.

See the [installation, association and migration guide](home_assistant/README.md)
before replacing the old HACS integration. A normal migration requires **no
motor reassociation**, but HA entity references and automations must be updated.

## Build and check

On Linux, with [devenv](https://devenv.sh/getting-started/) and Nix installed:

```sh
git submodule update --init --recursive
devenv shell
devenv test
devenv tasks run firmware:build
```

The default UF2 has RF disabled. `firmware:commands-build` explicitly enables
commands for slots already paired in the journal, without enabling enrollment.
Build tasks never flash a board. Both builds check the protected journal range.

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
| `firmware/ha_x2d/` | USB identity, flash mapping, SPI and PIO/DMA output |
| `lib/x2d-core/` | Shared association controller, codec, journal, STOP runtime and MySensors adapter |
| `tests/test_mysensors.py` | Native HA and pymysensors over simulated USB |
| `firmware/rx_debug/`, `tools/` | Passive capture and offline radio analysis |

[Serial contract](docs/USB_PROTOCOL.md) · [Radio observations](docs/OBSERVATIONS_RADIO.md)

Older research notes are historical and may describe the removed JSONL/HACS
architecture. The radio transform derives from mr-sven's work under
[Apache-2.0](research/LICENSE-APACHE); see the core's LICENSE and NOTICE.
