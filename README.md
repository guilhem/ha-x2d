# X2D shutters over USB

Control X2D shutters with **Home Assistant's built-in MySensors integration**,
a YD-RP2040 and a CC1101. The dongle owns the radio identities, associations
and rolling counters. Connect the dongle directly to Home Assistant over USB.

```text
Home Assistant / MySensors → USB → RP2040 / x2d-core → CC1101 → shutter
```

**Firmware 0.6.0-rc1:** add, disable, replace and retire shutters through native
MySensors device pages, without recompiling for each shutter. The public radio
profile is still a candidate: association and motor commands require physical
qualification. See [hardware qualification](home_assistant/README.md#hardware-qualification).

## Connect to Home Assistant

1. Build and flash the checked OTA UF2 in the [firmware guide](firmware/README.md).
2. Add **MySensors → Serial** using the dongle's `/dev/serial/by-id/…` path,
   **115200 baud** and protocol **2.3**.
3. Follow the [installation and lifecycle guide](home_assistant/README.md).
   If migrating the old journal, initialize once and pair the motors again.
4. Open the motor's association window and enable **Mode ajout** on the dongle.
   HA automatically creates the new shutter's device. After observing the motor
   response, enable **En service** there; its cover appears automatically.

Each shutter has its own state and association controls. Rename it and assign a
room in HA. Replacing its motor retains that device and its automations; retiring
it and adding another creates a fresh device. No helper, custom component or
dashboard is needed. Any dashboard is an optional way to display the same covers.

The dongle stores up to **16 registered shutters** and allocates **253 device
identities** over the journal's life. Internal slots are reusable after retirement;
public identities and radio counters never rewind. Adding and replacing motors
uses the same binary throughout the dongle's life.

There is no measured position or percentage. Read the
[state and connection guidance](home_assistant/README.md#state-and-connection-loss)
when interpreting the native cover's assumed state or cached availability.

## Build and check

On Linux, with [devenv](https://devenv.sh/getting-started/) and Nix installed:

```sh
git submodule update --init --recursive
devenv shell
devenv test
devenv tasks run firmware:build
```

The default UF2 includes the complete lifecycle and normal cover commands.
Build tasks never flash a board and check the protected journal range.

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
