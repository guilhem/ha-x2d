# MySensors serial adapter

Wire: `node;child;command;echo;type;payload\n`, **115200 baud**, MySensors **2.3**.
Headers are unsigned bytes, command 0–4 and echo 0–1. Ordinary payloads are at
most 25 bytes; stream payloads are at most 50 hexadecimal characters. Input is
bounded to 95 bytes before LF and accepts CRLF. Malformed input is discarded
through the next LF. The 4096-byte output ring never blocks radio service.
An actual overflow closes admission and cancels work at a frame boundary until
the serial connection reopens.

## Devices and children

| Node | Child | Presentation | Meaning |
| --- | --- | --- | --- |
| 1 | 17 | S_BINARY (3), V_STATUS (2) | Mode ajout: initial candidate exists |
| 1 | 19 | S_CUSTOM (23), V_VAR1 (24) | Last readable outcome/refusal |
| 1 | 20 | S_BINARY (3), V_STATUS (2) | Initialiser: explicit v1 initialization action, returns OFF |
| 2–254 | 1 | S_COVER (5) | V_STATUS (2), V_UP (29), V_DOWN (30), V_STOP (31) |
| 2–254 | 2 | S_BINARY (3), V_STATUS (2) | En service |
| 2–254 | 3 | S_BINARY (3), V_STATUS (2) | Association candidate exists |
| 2–254 | 4 | S_BINARY (3), V_STATUS (2) | Nouvel essai: bounded retry action, returns OFF |
| 2–254 | 5 | S_BINARY (3), V_STATUS (2) | Retire: permanent retirement, requires disabled/no candidate |
| 2–254 | 6 | S_CUSTOM (23), V_VAR1 (24) | Readable shutter lifecycle state |

Node 1 is the manager, sketch **X2D USB**. Each logical shutter presents its own
S_ARDUINO_NODE and sketch **Volet X2D**. Child names include its public node ID;
the cover name starts **X2D Volet N Commandes**. HA therefore creates separate
device pages automatically, without button helpers or a dashboard.

Nodes are allocated monotonically and never reused in the journal's life.
There are 16 reusable internal slots and 253 public node identities, including
abandoned initial additions. Explicit motor replacement keeps the public node
and children; retirement followed by addition gets a new node. A pending device
presents controls/state immediately; its cover appears only after confirmation.

## Commands and state

SET STATUS on switches accepts exactly `0`/`1`. Initial ADD ON allocates and
starts a candidate; a repeated ON while it exists cannot start another attempt.
ADD OFF cancels an initial candidate. Association ON on a paired, disabled device
opens a replacement; Association OFF cancels its candidate. En service ON confirms
a candidate after the human has observed the motor response, or reactivates the
old binding; OFF disables operation. RETRY ON claims the only second attempt.
RETIRE ON requires disabled service, no candidate and idle radio.

The initial attempt reserves counters 0/1; the explicit retry requires next=2
and reserves 2/3. Attempts and counters remain consumed after failure, cancellation
or restart. A partial reservation can require cancellation. No operation, including
discovery, reconnect or SET requesting the same state, restores an emission permit.

Lifecycle mutations require idle RF. Every queued radio job captures the private
incarnation of its current/candidate controller. Admission, reservation and terminal
updates verify that incarnation; slot reuse cannot transfer old queued work.
Commands delayed by the host until after explicit reactivation carry no radio
incarnation on the MySensors wire and cannot be distinguished from new commands.
A replacement candidate retains the disabled primary binding until confirmation.
Cancellation drops only the candidate; confirmation swaps bindings atomically.

Recognized commands requesting an echo receive a receipt echo, not a success or
motor acknowledgement. Authoritative firmware switch values follow refusals as
well as accepted operations. REQ returns SET snapshots; it never imports cached
HA actuator states. Unsupported types, percentages, tilt, malformed commands and
broadcast actuator requests cannot submit RF. No RF identity or counter is exposed
in diagnostics.

## Discovery and timing

Version, discovery, presentation and heartbeat support the manager, each owned
logical node and broadcasts. Presentations are incremental, one line per tick;
space for command/state responses is retained before advancing discovery. Reads
and presentations do not perform RF or mutate the journal.

The queue holds 16 requests with a 30-second waiting limit. STOP preempts movements
at a full frame boundary. Active bursts retain their watchdog; enrollment uses
the existing two-phase 24-copy gesture with a six-second active limit. Expired
waiting jobs do not reserve counters or transmit. USB loss drops queued serial
bytes and jobs, stops a running burst at a boundary, and keeps reservations.

## Native HA state limits

The cover has no percentage or measured movement. STOP=1 and UP/DOWN=0 precede
command echoes; V_STATUS=0 follows a completed close burst, while open/unknown
use 1. STOP, reboot, disconnect or uncertain output invalidates that estimate.
The shutter state remains explicit about position being unmeasured. Use the
assumed-state customization in the [HA guide](../home_assistant/README.md).

Native HA creates a battery sensor for each node and may retain available-looking
cached states after USB loss. The firmware supplies neither a battery measurement
nor a guarantee of reception. Dashboard cards are optional views of these same
native entities and have no lifecycle responsibility.

## Initialization and updates

Valid v1 storage requires explicit Initialiser. It commits an empty v2 inventory
with the old identities as allocation exclusions, then removes old records before
allowing RF. Bindings/counters are not migrated; motors must be associated again.
An interrupted initialization resumes finalization without emission. Unknown or
corrupt storage blocks operations and is never automatically formatted.

A genuinely blank journal initializes its empty inventory at first boot, without
RF; discovery and reads do not reset storage or create associations.

Clean old HA node/device inventory when making this one-time transition. Later
v2 updates keep public nodes and counters. Deleting a HA device alone does not
retire it in firmware; perform Retire first. No protocol-level automatic deletion
is invented. Restore of an old journal backup is unsupported.

OTA remains on node 1, child 255, C_STREAM, independently of shutter node IDs.
See the [OTA contract](FIRMWARE_UPDATE.md#mysensors-wire-contract).
