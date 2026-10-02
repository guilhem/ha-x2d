# X2D gateway protocol v2

The canonical JSONL v2 contract lives in
[x2d-core](https://github.com/guilhem/x2d-core/blob/ef5b86d7a1b965d316d2df2ff536d79db7c8db7e/docs/GATEWAY_PROTOCOL.md).
It is shared by the RP2040 USB adapter and future byte-stream adapters.

For the current USB product, keep the targeted
[Linux power rule](../firmware/99-ha-x2d-power.rules) and qualify host keep-awake.
The [firmware guide](../firmware/README.md) retains wiring, build profiles,
trial restrictions and hardware qualification limits.
