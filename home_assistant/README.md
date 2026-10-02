# Home Assistant: native MySensors USB

Use Home Assistant Core **2026.9.4**, MySensors protocol **2.3**, and
**115200 baud**. Install the built-in integration; there is no custom component
or container to install. Only one process may own the USB serial port.

## Install

1. Wire the YD-RP2040 and CC1101 as described in the [firmware guide](../firmware/README.md#wiring).
   Build the **4 MiB journal profile** and run `check_uf2_layout.py` on the exact
   UF2 you will flash, as described in the [firmware guide](../firmware/README.md).
   Use BOOTSEL to copy that UF2. **Never use flash_nuke, erase the whole flash,
   mount LittleFS over the journal, or change the linker layout.**
2. Connect the dongle to the Home Assistant host and add the native
   **MySensors → Serial** integration using
   `/dev/serial/by-id/…`, **115200**, **2.3**. Preserve this MySensors entry and
   its persistence when updating firmware.
3. Merge this single rule into `configuration.yaml`, then reload Home Assistant
   customizations or restart HA:

   ```yaml
   homeassistant:
     customize_glob:
       "cover.x2d_*":
         assumed_state: true
         device_class: shutter
   ```

4. Follow the pairing procedure below. Each confirmed shutter appears
   automatically; no per-shutter YAML is needed. Choose its display name and
   add it to dashboards or automations. Keep the `cover.x2d_` entity-ID prefix
   so the customization still matches.

The default UF2 refuses RF while hardware qualification is pending. A supervised
trial build enables the pairing procedure; after pairing, the commands-only
build supports normal open, close and STOP operations.

## Entities and pairing

The fixed virtual node is **1**. Child IDs **1–16** are paired shutter slots,
**17** is “X2D Pair shutter”, **18** is “X2D Confirm pairing”, and **19** is
“X2D Diagnostic”. Empty/pending slots are not presented as covers. Native
MySensors may also create its own battery entity for this mains-powered
virtual node; disable it in HA, since the dongle reports no battery measurement.

New enrollment requires an explicitly authorized **experimental trial build**.
Neither the default nor the commands-only build enables it. Its suffix,
initial counter and allowed retry still need qualification on the target motor.

1. Put the motor into its manufacturer-documented pairing mode under supervision.
2. Turn **Pair shutter** ON once. The dongle resumes its unique pending slot or
   selects the first unused slot. It persists a new identity and reserves both
   counters before transmitting. The switch returns OFF even after refusal.
3. Read the diagnostic. Each new attempt needs a fresh ON command and must satisfy
   the compiled trial restrictions. Failed/uncertain attempts consume counters;
   rebooting cannot retry or recover those counters.
4. Only after personally observing the motor response, turn **Confirm pairing**
   ON. A reserved attempt and an idle radio are required. Confirmation is persisted
   and the new cover is presented immediately. There is no RP2040 reboot.

If power disappears after reservation, the pending identity and counters survive.
Confirmation remains possible after restarting an appropriately authorized build;
startup itself transmits nothing. Multiple pending slots and concurrent pairing
operations are refused. OFF never starts an operation. Both switches return OFF
on processing, refusal and reconnection.

## State, diagnostics and USB loss

`V_STATUS` carries a binary estimate only after a complete open/close burst.
There is no percentage or measured movement. The three commands stay available
with `assumed_state`; the display may say “open” while position is unknown.
`pos_unknown` means that STOP, restart, USB loss or uncertain transmission has
invalidated the estimate. `emitted` establishes transmitter completion only,
not motor reception. Other diagnostics identify disabled RF, unqualified pairing,
busy radio, malformed commands, exhausted counters or corrupt storage.

A requested MySensors echo acknowledges **receipt**, including a radio refusal;
it is never a motor acknowledgement. Movement indicators remain idle. The
outcome/refusal is sent independently on the diagnostic child.

On USB loss the firmware discards pending serial bytes and queued operations,
and ends an active burst at its next complete frame. It keeps every reservation.
Reconnection only republishes inventory and states: no command is replayed and
no actuator state is requested from HA. SmartSleep is not used. A host that
stops reading can exhaust the bounded response buffer; `serial_overflow` closes
admission and cancels current work. Reopen the serial connection to recover.

**Native HA limitation:** MySensors can keep cached states and available-looking
covers after disconnection. Check the physical connection and diagnostic before
inferring that a command worked. The USB power-management condition from the
[firmware guide](../firmware/README.md#usb-power) also needs verification on HA OS.

## Updates and destructive reset

Keep the same journal-preserving UF2 layout, MySensors entry and persistence.
Adding a second slot leaves previous child IDs intact; HA display-name overrides
are retained. Removing an entity does not free its slot. Slots are never recycled
within a journal. A corrupt journal blocks RF and reports `storage_corrupt`;
firmware never formats it automatically.

MySensors commands contain node/child IDs, **not the journal generation**. An old
HA reference could therefore address a different motor if a slot were reused.
A full reset is a separate, explicit maintenance operation:

1. Disable relevant automations and disconnect the dongle.
2. Remove the MySensors node/device from HA through its device page so the native
   integration also removes that node from its persisted sensor inventory.
3. Stop/unload the integration and verify its configured persistence file no
   longer contains node 1 before reusing any slots. If removing the integration
   instead, back up and remove its old persistence file **while it is stopped**.
   Remove stale entity/device references and old automations as appropriate.
4. Only then perform the separately authorized flash reset and new associations.
   Do not restore the old MySensors persistence into the new journal generation.

This procedure is not part of ordinary upgrades, and no reset command is exposed
by the dongle. Restoring an old HA backup never restores radio counters.

## Hardware qualification

Qualification is pending for this firmware on HA OS with a YD-RP2040 (4 MiB),
CC1101 and Well’com motor. Test on the actual
board: journal-preserving update; unchanged identities and counter monotonicity;
open/close/STOP; unplug during a burst; reconnect without motion; reset/watchdog
carrier suppression; STOP latency; pairing from fresh identities; and a second
association without changing earlier HA IDs or display names. Software/PTY
checks and successful firmware compilation do not establish these results.

References: [MySensors serial API](https://www.mysensors.org/download/serial_api_20),
[HA MySensors](https://www.home-assistant.io/integrations/mysensors/),
[HA native customization](https://www.home-assistant.io/integrations/homeassistant/#manual-customization),
[HA 2026.9.4 entity implementation](https://github.com/home-assistant/core/blob/2026.9.4/homeassistant/components/mysensors/entity.py).
