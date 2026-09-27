<p align="center">
  <img src="assets/readme/hero.svg" width="100%" alt="ha-x2d — an experimental USB radio gateway: Home Assistant, an independent Python client, and RP2040 firmware with a CC1101 radio.">
</p>

An experimental **USB gateway for radio-controlled shutters in Home Assistant**,
built around a **YD-RP2040 and an SPI CC1101**. The aim is local control without
a separate MQTT broker, with a Python client other applications can reuse.

> **Early prototype.** Software checks pass, but physical hardware is still
> untested. Radio reception, pairing and shutter control are not implemented yet.

## What works today

- USB identification, connection diagnostics and CC1101 register probing.
- Home Assistant setup, gateway status and reconnection handling.
- Six Python/HA tests, native C++ checks, firmware and integration builds.

The first target is **X2D**. The France Fermetures / Well’com shutters under
study may use **X3D**; real radio captures are needed to confirm the protocol.

## Three parts, one gateway

| Component | Responsibility |
| --- | --- |
| [Home Assistant integration](home_assistant/README.md) | Setup, gateway status and diagnostics. |
| [Python client](python/README.md) | Async USB communication, usable independently of HA. |
| [RP2040 firmware](firmware/README.md) | USB commands and SPI access to the CC1101. |

## Try it locally

On **Linux with glibc**, install [devenv](https://devenv.sh/getting-started/)
and its Nix prerequisite, then run from the repository root:

```sh
devenv shell
devenv test
```

No dongle is needed for the tests. Build the two artifacts with:

```sh
devenv tasks run firmware:build
devenv tasks run ha:package
```

Both artifacts are written to `dist/`. See the [wiring and firmware guide](firmware/README.md)
and [Home Assistant installation guide](home_assistant/README.md) for next steps.

## Next: verify the radio

Capture the original remotes, identify X2D/X3D, then implement pairing and
**open / stop / close**. Compatibility with a specific shutter is still unproven.

[Architecture and trade-offs](docs/EXPLORATION.md) ·
[Radio observations](docs/OBSERVATIONS_RADIO.md) ·
[USB protocol](docs/USB_PROTOCOL.md)

Research notes are in French. The [public-sample checks](research/verify_public_samples.py)
include code adapted from mr-sven under [Apache-2.0](research/LICENSE-APACHE).
