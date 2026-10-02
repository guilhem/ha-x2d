# USB: MySensors 2.x

The gateway firmware speaks only the standard MySensors serial protocol at
115200 baud.

The [core serial contract](https://github.com/guilhem/x2d-core/blob/bc843047ab44ddf081a2abf63661e9f2e0b89bfd/docs/MYSENSORS.md)
is authoritative. See the [Home Assistant guide](../home_assistant/README.md) for
installation, the global assumed-state rule, pairing and reset restrictions.

The passive RX diagnostic sketch retains its separate JSONL capture format;
it is not the gateway firmware and must not be connected to MySensors.
