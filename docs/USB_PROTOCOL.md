# USB: MySensors 2.x

The gateway firmware speaks only the standard MySensors serial protocol at
115200 baud. JSONL v2 and the Python/HACS client were removed.

The [core serial contract](https://github.com/guilhem/x2d-core/blob/1058d66be06f653026213301357d30bbc150c11d/docs/MYSENSORS.md)
is authoritative. See the [HA migration guide](../home_assistant/README.md) for
installation, the global assumed-state rule, pairing and reset restrictions.

The passive RX diagnostic sketch retains its separate JSONL capture format;
it is not the gateway firmware and must not be connected to MySensors.
