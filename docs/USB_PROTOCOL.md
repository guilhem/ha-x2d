# USB: MySensors 2.x

The gateway firmware speaks only the standard MySensors serial protocol at
115200 baud.

The [dongle serial contract](MYSENSORS.md)
is authoritative. See the [Home Assistant guide](../home_assistant/README.md) for
installation, the global assumed-state rule, pairing and reset restrictions.

The passive RX diagnostic sketch retains its separate JSONL capture format;
it is not the gateway firmware and must not be connected to MySensors.
