# Update the USB dongle

The **0.6.0-rc2** gateway accepts firmware updates over its existing MySensors USB
connection. The supported board is the **YD-RP2040 with 4 MiB flash**.

## First installation

Build with `devenv tasks run firmware:build`, then copy
`dist/ha_x2d-0.6.0-rc2-yd-rp2040-4mb.uf2` to the board in BOOTSEL mode.
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
     dist/ha_x2d-0.6.0-rc2-yd-rp2040-4mb.bin \
     --version 7 --device-id YOUR_16_DIGIT_USB_ID
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

## Native Home Assistant updates

The native update interface is under development: it needs
[pymysensors #947](https://github.com/theolind/pymysensors/pull/947) and the
corresponding Home Assistant MySensors changes. It is not available in released
Home Assistant or pymysensors yet. A gateway running the former four-word OTA
announcement first needs the serial migration above.

With both changes installed, open **MySensors → Configure**, choose the dongle,
and upload the wrapper-built BIN. Keep the detected firmware type and enter the
numeric OTA version compiled into that image (`7` for `0.6.0-rc2`). Importing
prepares the candidate without transferring it. Open the dongle's **Firmware**
entity and select **Install** when the shutters are idle.

Home Assistant uses its existing connection, reports progress, and waits for the
matching running image and a fresh application announcement. An uninstalled
candidate survives a reload or restart, but an interrupted installation never
resumes automatically. Only the dongle advertises OTA; virtual shutters have no
firmware update entity. Associations and counters remain in the reserved journal.

The 2026-10-05 hardware trial migrated a gateway with the existing sender, then
used a Home Assistant development instance's options flow and `update.install`
over an SSH/USB relay. The installed `0.6.0-rc2` reported type `0x5832`, version
`7`, 11,272 blocks, CRC `0x1039` and loader `1`; heartbeat and candidate cleanup
were confirmed. A raw flash read matched the 180,352-byte built BIN, and the
64 KiB journal was identical before migration, after migration and after HA
installation. The production HA integration was restored afterward. This trial
did not change production HA code, operate shutters, validate browser interaction,
or test a physical power cut.

## Flash and recovery

| Region | Address | Use |
| --- | --- | --- |
| Immutable boot prefix | `0x10000000–0x10003000` | boot2, Arduino-Pico OTA loader, partition table |
| Application reservation | `0x10003000–0x101EF000` | gateway executable; transferred binary limited to 1,048,448 bytes |
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
| 0, current config | `type, version, blocks, crc, bootloader_version`; type `0x5832`, version `7`, loader generation `1` |
| 1, firmware offer | `type, version, block_count, CRC16` |
| 2, block request | `type, version, block_index` |
| 3, block response | same three words plus 16 firmware bytes |

The canonical full binary is padded with `FF` to the next 128-byte boundary.
An already aligned binary gains no page. The wrapper replaces the Arduino
converter's zero tail with `FF` and checks every UF2 payload against this BIN;
the UF2 transport uses 256-byte pages, with any remaining tail also `FF`.
The four image words describe the **actually running** executable, including its
immutable boot prefix and padding. The gateway derives the length from
`__flash_binary_end` and reads the CRC from flash, without circular checksum
metadata. The fifth word identifies the retained OTA loader generation, not an
image checksum or transfer label. CRC16 MODBUS
uses polynomial `0xA001`, initial value `0xFFFF` and no final XOR. The receiver expects one
block index at a time; retries can leave duplicate requests in the serial buffers. A missing block is requested every 500 ms, for at most
five attempts; duplicates of the last accepted block never rewrite storage.
Invalid or out-of-order replies abort the transfer. Staging completion is the internal
log `ota_staged`; the sender then replies with internal `I_REBOOT` (13),
empty payload, to acknowledge receipt. Failures use `ota_error:<reason>`. The offered version must match the compiled product identity
`HA-X2D YD-RP2040 OTA/1:<wire_version>:<sketch_version>` in the staged bytes.
The 0.6.0-rc2 identity uses wire version 7. This identity check is not a signature.


The gateway announces its five-word config at startup **before presentation**,
and in response to `I_PRESENTATION` for node 1 or broadcast presentation. Virtual
shutter nodes never announce OTA. An empty targeted `1;255;3;0;13;` requests a
real reboot before installation; its next startup config solicits an offer from
an explicitly armed controller session. Echoes, shutter reboots, broadcasts and
nonempty reboot payloads cannot reset the dongle. After commit it automatically
reboots within three seconds; the migration script's receipt can accelerate it.

Ordinary boot, presentation and a reply identifying the current image do not
install anything or touch staging. The controller must arm a session before
sending its single reboot and offering a selected image; cached files alone must
never trigger updates. Successful native controller installation requires the
new five-word active config followed by fresh application presentation/sketch or
heartbeat evidence. Expected USB resets may reconnect in discovery/confirmation,
but commands and an interrupted block transfer are never replayed.

The standalone script still accepts the former four-word announcement for a
one-time upgrade from the old running gateway, and retains staging-only success
semantics. Its input is the new canonical BIN and its supplied version must match
the compiled identity; it does not require a pre-transfer reboot.

## Native reboot simulation

`build/native/mysensors_server --ota-reboot --ota-active=active.bin
--ota-file=staged.bin [--control-fd=N] [--journal=journal.bin]` runs the real C++
adapter against test-only storage. The active file contains the compiled identity
above; padding/CRC are derived from its bytes. A committed reboot copies staged
bytes to the active file, recreates the application runtime, and keeps journal
and radio state. With a control FD, every reset sends `B` and waits for `C` before
fresh startup evidence; a PTY bridge models USB disappearance/re-enumeration.
Without `--ota-reboot` the existing staging tests keep their no-reset behavior.
This is software evidence only; bootloader power-cut and physical USB/RF behavior
require board qualification.
