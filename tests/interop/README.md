# Native OTA interoperability

These tests connect the real asynchronous `pymysensors` controller to a PTY.
The AVR peer requests blocks before application presentation. The RP2040 peer
runs the actual C++ gateway, receiver and image verifier with simulated flash
and radio. Tests cover descending requests, duplicate blocks, USB reconnection,
wrong running images, interruption before and after commit, and no automatic
replay. The simulated journal must remain byte-for-byte unchanged.
Losing the startup configuration on both reboots also exercises the controller's
bounded presentation probes without sending a second reboot or restarting OTA.

The Home Assistant test runs the real options flow, persistent candidate store,
update entity and `update.install` service. It starts at a completed native
file-upload token; it does not test the browser or HTTP upload endpoint.
Importing and reloading must not start installation, and only the dongle gets
an update entity.

These suites require [pymysensors #947](https://github.com/theolind/pymysensors/pull/947)
and the pending native OTA changes in Home Assistant.
They are kept separate from the normal tests, whose environment uses released
dependencies. No unreleased PyPI version is substituted in the lock file.

Build the simulator first:

```sh
git submodule update --init --recursive
cmake -S . -B build/native
cmake --build build/native -j2
```

In an environment with the modified library's dependencies installed:

```sh
export PYMYSENSORS_SRC=/path/to/pymysensors
PYTHONPATH="$PYMYSENSORS_SRC" python -m unittest discover \
  -s tests/interop -p test_native_ota.py -v
```

For the complete Home Assistant path, prepare its development environment and
install the modified library there. From this repository:

```sh
export CORE_SRC=/path/to/homeassistant-core
PYTHONPATH="$CORE_SRC:$PYMYSENSORS_SRC" "$CORE_SRC/.venv/bin/python" \
  -m unittest discover -s tests/interop -p test_native_ha_ota.py -v
```

These checks do not qualify real USB timing, physical power-cut recovery,
radio transmission, or shutter operation. Physical trial results must be
reported separately.
