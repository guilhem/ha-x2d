# Home Assistant: native MySensors USB

Add and manage shutters with **Home Assistant's built-in MySensors integration**.
Each shutter gets its own device page automatically. The same firmware handles
new shutters throughout its life; no private build, helper or dashboard is needed.

Firmware **0.6.0-rc1** implements this lifecycle. Its public radio profile is a
candidate, not yet qualified on the motors. See [hardware qualification](#hardware-qualification).

## Install

1. Wire the YD-RP2040 (4 MiB) and CC1101 as described in the
   [firmware guide](../firmware/README.md#wiring). Build the OTA profile with
   `devenv tasks run firmware:build` and install its checked UF2 with BOOTSEL.
2. Add **MySensors → Serial** in HA, with `/dev/serial/by-id/…`, **115200 baud**,
   protocol **2.3**. Only one process may own that port.
3. The **X2D USB 1** device appears automatically. If its state is
   **Initialisation requise**, activate **Initialiser** once. This abandons the
   previous firmware's associations; pair those motors again with the new flow.
   Never initialize a working v2 journal or erase the whole flash.
4. Merge this customization into `configuration.yaml` and reload customizations:

   ```yaml
   homeassistant:
     customize_glob:
       "cover.volet_x2d_*":
         assumed_state: true
         device_class: shutter
   ```

This customization keeps all three cover commands available with an assumed
state; it is unrelated to enrollment. Keep the `cover.volet_x2d_` entity-ID prefix.
Names and rooms can be edited freely from HA.

The MySensors integration creates a battery sensor for every virtual node even
though the dongle has no battery measurement. Disable those entities in HA;
zero is not a measurement of your motor's battery.

## Add a shutter

1. Put the motor in its manufacturer-documented association window.
2. On the dongle's device page, activate **Mode ajout**. The firmware persists a
   fresh controller, then emits one bounded two-phase association attempt.
3. A **Volet X2D N** device appears automatically, initially without a cover.
   Read its state and observe the motor. Only after its association response,
   activate **En service** on this new device.
4. The cover appears on that device. Check open/STOP and close/STOP, then give
   the device a useful name and room. Those commands are the ordinary native HA
   cover services and can be used in automations.

**Mode ajout** stays ON while an initial candidate exists. **Association** on
its device also reports that candidate. These states survive reconnects;
ON is not a scheduled emission and reconnecting never retries.

If the motor did not respond, reopen its window and activate **Nouvel essai**
once. This action switch returns OFF. Only the counters 2/3 are allowed after
an initial 0/1 attempt. A failed attempt, duplicate press or power cut cannot
refund a consumed attempt. Partial reservations can require cancellation.

To abandon the new shutter, turn **Association** or **Mode ajout** OFF. Remove
its abandoned device from HA afterwards. A new addition uses a fresh identity
and a fresh HA device, never the abandoned one.

Only one association candidate can exist at once. Up to **16 shutters** may be
registered simultaneously, including disabled and pending shutters. The journal
can allocate **253 device identities** over its life, abandoned additions
included. Replacing a motor on the same device does not consume another HA ID.

## Disable, replace or retire

**En service OFF** prevents normal operation while retaining the registration.
If radio work is still running or queued, stop it, wait for the diagnostic and
repeat the operation. An association or replacement needs an idle radio.

To replace a motor while retaining its HA name, room, entity IDs and automations:

1. Disable **En service** on the existing device.
2. Open the new motor's association window and activate **Association** on that
   device. The old binding remains stored and disabled until confirmation.
3. Observe the response, then activate **En service**. Confirmation atomically
   replaces the radio binding, without recreating the HA device.

Turning **Association OFF** before confirmation abandons the candidate and
keeps the previous binding disabled. You may explicitly reactivate it with
**En service**. Already received or queued operations cannot follow the old radio binding to
the new motor. MySensors carries no command generation: a command delayed by
the host until after explicit reactivation is indistinguishable from a new one.

For permanent retirement, disable **En service**, cancel any candidate, then
activate **Retire** while the radio is idle. This releases its internal slot.
Delete that device through HA's device page to clean the MySensors inventory.
An addition using the released slot receives a different HA node identity.
Deleting a device in HA alone does not retire it in the firmware: it will return
on discovery. Retirement does not erase the controller remembered by the motor;
use the manufacturer's procedure if motor-side removal is required.

## State and connection loss

Device states explain association, confirmation, disabled operation and errors.
The dongle's **Etat dongle** sensor reports the last outcome or refusal.

RF completion proves transmitter completion, not motor reception. There is no
position feedback or percentage. The native cover uses a binary estimate after
an open/close burst. STOP, restart, USB loss or uncertain output invalidates that
estimate; MySensors may still display "open". The state sensor keeps position
explicitly unmeasured. Receipt echoes are not motor acknowledgements.

USB loss discards queued operations and ends a running burst at a complete frame
boundary. Counters stay consumed. Reconnection publishes inventory and state;
it never restores a command, an emission permit or an actuator state from HA.
A malformed/read flood can fill the bounded output buffer and close admission;
reopen the connection to recover. Normal presentations of 16 devices are streamed
progressively instead of filling that buffer.

Native HA MySensors can retain cached states after USB loss. Use the physical
connection and the diagnostic when checking whether an operation succeeded.

## Firmware updates and first v2 initialization

Use the same flash layout and the [serial update procedure](../docs/FIRMWARE_UPDATE.md).
Stop MySensors while the updater owns the port. Ordinary v2 updates retain the
journal, HA devices and counters.

For the first transition from a valid v1 journal, **Initialiser** creates an empty
v2 inventory. It retains only the old radio identities as allocation exclusions,
so they cannot be recreated with reset counters. It commits that state before
removing the old records. A power cut resumes this finalization without radio;
no v1 snapshot may remain usable when v2 emission starts.

Disable automations using the old covers, remove the old MySensors device from
HA and reload the integration to discover the manager again. Remove obsolete
pairing button helpers if present. Add the motors with the ordinary new flow and
retarget existing automations to their new covers. No association/counter or old
entity-ID compatibility is supplied for this one-time transition.

A genuinely blank journal initializes an empty v2 inventory automatically,
without RF. Corrupt or unknown journals block radio and are never formatted automatically.
Restoring an old journal backup is not a supported recovery procedure: it could
restore consumed counters. Older flash layouts require their own first-install
procedure; v1 initialization here applies to the same-layout 0.5 firmware.

## Optional dashboard

The device pages contain the complete workflow. You may add the native covers
and their state sensors to any ordinary HA dashboard; this changes neither
initialization nor association. No supplied dashboard, template button, script
or helper is required.

## Hardware qualification

The public suffix `0x01` must be tested with fresh prefixes on both demonstrated
motors: explicit association response, confirmation, open/STOP and close/STOP,
existing remotes still working, and restart without motion. Existing observations
used private trial parameters and do not qualify this public candidate.

Additional hardware checks cover the actual journal update, unplug during a
burst, carrier suppression at reset/watchdog, STOP latency and USB power handling.
Native C++/PTY tests use simulated radio and flash; successful builds do not
establish motor compatibility or physical power-cut recovery.

References: [MySensors serial API](https://www.mysensors.org/download/serial_api_20),
[HA MySensors](https://www.home-assistant.io/integrations/mysensors/),
[HA customization](https://www.home-assistant.io/integrations/homeassistant/#manual-customization).
