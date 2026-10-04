# Update the USB dongle

The **0.6.0-rc1** gateway accepts firmware updates over its existing MySensors USB
connection. The supported board is the **YD-RP2040 with 4 MiB flash**.

## First installation

Build with `devenv tasks run firmware:build`, then copy
`dist/ha_x2d-0.6.0-rc1-yd-rp2040-4mb.uf2` to the board in BOOTSEL mode.
The build checks the actual UF2 and binary before publishing them locally.
The wrapper `python tools/build_firmware.py` is also available in `devenv shell`.
A direct Arduino CLI build without the wrapper's flash reservation is refused
by the gateway at runtime and is unsuitable for deployment.

This layout deliberately breaks compatibility with firmware through 0.4.1.
Old associations and counters are **not migrated**. The old journal at
`0x101FF000` is inside the new staging filesystem and is permanently overwritten
when that filesystem is first formatted/used. Establish a new journal and
pair shutters again using the supervised procedure. Before changing layouts,
stop the HA integration and remove its old MySensors node/device inventory;
discover new device pages after pairing.
Do not interpret an empty new journal as permission to replay old RF counters.

## First journal v2 use on the 0.5 layout

This release preserves the 0.5 bootloader, partition map and reserved journal
address. Installing it over 0.5 does not import the old associations/counters.
After boot, a valid v1 journal blocks radio and reports **Initialisation requise**.
Activate **Initialiser** in the native MySensors manager device once, then add
motors through the [device-page lifecycle](../home_assistant/README.md).

Initialization durably stores an empty v2 inventory and up to 16 old RF identities
as allocation exclusions before erasing the v1 bank. A restart finishes the same
reset without transmission. No usable v1 snapshot may remain before radio resumes.
Corrupt/unknown versions are blocked instead of automatically formatted.

Disable old cover automations, remove the old HA MySensors device and obsolete
button helpers, then reload the integration. Pair the motors again and update
their references. Ordinary later v2 updates preserve devices and counters.

## Subsequent serial updates

1. Update only when the shutters are idle: **STOP is refused throughout staging
   and until reboot**. Stop or unload Home Assistant's MySensors integration and close all serial
   monitors. The sender must be the only process using the port.
2. Obtain a gateway `.bin` built by the wrapper with the same Arduino-Pico
   version and layout. Keep the corresponding UF2 for BOOTSEL recovery.
3. In the development environment, run:

   ```sh
   uv run python tools/update_firmware.py \
     /dev/serial/by-id/THE_GATEWAY_PORT \
     dist/ha_x2d-0.6.0-rc1-yd-rp2040-4mb.bin \
     --version 6 --device-id YOUR_16_DIGIT_USB_ID
   ```

   The ID is the physical flash ID exposed as the USB serial number. The
   sender checks the dongle's reply before offering any firmware blocks.
4. Successful transfer means **staged for installation**. The dongle then
   reboots and the bootloader installs the application. Wait for USB to return,
   restart the HA integration and check its reported sketch version. The sender
   does not certify that the new application booted or that RF works.

The update pauses new RF work, discards queued commands and waits for an active
burst to end at a frame boundary before touching staging flash. Commands sent
while updating are refused. Interrupted transfers leave the running application
intact; retry from the beginning. A completed durable boot command survives USB
loss. The dongle reboots after the sender acknowledges receipt of `ota_staged`, or after a
three-second limit.

## Flash and recovery

| Region | Address | Use |
| --- | --- | --- |
| Immutable boot prefix | `0x10000000–0x10003000` | boot2, Arduino-Pico OTA loader, partition table |
| Application reservation | `0x10003000–0x101EF000` | gateway executable; transferred binary limited to 1,048,560 bytes |
| Raw journal | `0x101EF000–0x101FF000` | 64 KiB of associations and consumed counters |
| LittleFS | `0x101FF000–0x103FF000` | dedicated firmware staging; may be formatted |
| Core EEPROM reservation | `0x103FF000–0x10400000` | Arduino-Pico reservation |

The incoming binary must have exactly the running dongle's first 12 KiB, a
valid application vector table and the gateway product marker. The dongle
checks the CRC again from the closed staging file, pads the file to complete
4 KiB sectors and reads back the boot command before acknowledging staging.
The command copies **only the application**, never the bootloader, journal or
filesystem. A power cut during application installation can resume the same
command on the next boot. Software simulations cover interrupted sector copies;
actual power-cut recovery still needs qualification on the board.

Changing Arduino-Pico, its bootloader or the partition map requires a new UF2
installation. There is no automatic rollback of an application that boots but
malfunctions: recover with BOOTSEL and a known-good UF2 built for this layout.
These serial transfers use CRC for accidental corruption and an ID check for
accidental targeting. They do not authenticate firmware cryptographically.

## MySensors wire contract

Updates address node **1**, child **255**, command **4** (`C_STREAM`), echo **0**.
Integer fields are unsigned 16-bit little-endian words encoded as ASCII hex.

| Stream type | Payload |
| --- | --- |
| 0, discovery request | empty; dongle replies with `I_LOG_MESSAGE` (`ota_id:<USB_ID>`) and type 0 current config |
| 0, current config | `type, version, blocks=0, crc=0`; current type `0x5832`, version `6` |
| 1, firmware offer | `type, version, block_count, CRC16` |
| 2, block request | `type, version, block_index` |
| 3, block response | same three words plus 16 firmware bytes |

The full binary is padded with `FF` to 16 bytes for transfer. CRC16 MODBUS
uses polynomial `0xA001`, initial value `0xFFFF` and no final XOR. The receiver expects one
block index at a time; retries can leave duplicate requests in the serial buffers. A missing block is requested every 500 ms, for at most
five attempts; duplicates of the last accepted block never rewrite storage.
Invalid or out-of-order replies abort the transfer. Completion is the internal
log `ota_staged`; the sender then replies with internal `I_REBOOT` (13),
empty payload, to acknowledge receipt. Failures use `ota_error:<reason>`. Version is the controller's
transfer label, not a signed claim about the binary's sketch version.
