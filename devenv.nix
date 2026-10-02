{ pkgs, config, ... }:

let
  # The host parser check uses the same library release as sketch.yaml.
  arduinoJson = pkgs.fetchurl {
    url = "https://github.com/bblanchon/ArduinoJson/releases/download/v7.4.3/ArduinoJson-v7.4.3.h";
    hash = "sha256-q1+7gmi4RrX0vFpf7hG7LJb3uLhG9b72VAr7apzHals=";
  };
in
{
  # Use the native CLI on glibc Linux; its FHS wrapper requires user namespaces.
  packages = [ (pkgs.arduino-cli.pureGoPkg or pkgs.arduino-cli) pkgs.gcc pkgs.cmake ];

  languages.python = {
    enable = true;
    package = pkgs.python314;
    venv.enable = true;
    uv = {
      enable = true;
      sync.enable = true;
      sync.arguments = [ "--locked" ];
    };
  };

  env = {
    ARDUINO_DIRECTORIES_DATA = "${config.devenv.state}/arduino/data";
    ARDUINO_DIRECTORIES_DOWNLOADS = "${config.devenv.state}/arduino/downloads";
    ARDUINO_DIRECTORIES_USER = "${config.devenv.state}/arduino/user";
  };

  tasks = {
    "test:python" = {
      description = "Run native MySensors and Home Assistant tests over simulated USB";
      after = [ "devenv:python:uv" "firmware:check" ];
      before = [ "devenv:enterTest" ];
      exec = ''
        cd "${config.devenv.root}"
        uv run --locked python -m unittest discover -s tests -v
      '';
    };

    "firmware:check" = {
      description = "Compile and run the firmware's native C++ protocol checks";
      before = [ "devenv:enterTest" ];
      exec = ''
        cd "${config.devenv.root}"
        mkdir -p build/firmware-check/include
        ln -sf "${arduinoJson}" build/firmware-check/include/ArduinoJson.h
        cmake --fresh -S . -B build/native
        cmake --build build/native -j2
        ctest --test-dir build/native --output-on-failure
        g++ -std=c++17 -Wall -Wextra -Werror \
          -Ilib/x2d-core/src firmware/check_radio_tx.cpp -o build/firmware-check/check_radio_tx
        build/firmware-check/check_radio_tx
        g++ -std=c++17 -Wall -Wextra -Werror \
          -Ilib/x2d-core/src firmware/tx_check/check_capture.cpp -o build/firmware-check/check_tx_capture
        build/firmware-check/check_tx_capture
        g++ -std=c++17 -Wall -Wextra -Werror \
          -Ibuild/firmware-check/include -Ilib/x2d-core/src firmware/check_rx_debug.cpp \
          -o build/firmware-check/check_rx_debug
        build/firmware-check/check_rx_debug
        python tools/check_uf2_layout.py
      '';
    };

    "firmware:build" = {
      description = "Build the unqualified gateway UF2 while protecting its reserved journal";
      exec = ''
        cd "${config.devenv.root}"
        arduino-cli compile --profile yd-rp2040-4mb-journal \
          --output-dir build/firmware firmware/ha_x2d
        python tools/check_uf2_layout.py build/firmware/ha_x2d.ino.uf2
        mkdir -p dist
        cp build/firmware/ha_x2d.ino.uf2 \
          dist/ha_x2d-0.4.0-yd-rp2040-4mb-UNQUALIFIED-RADIO.uf2
      '';
    };

    "firmware:commands-build" = {
      description = "Build commands-only firmware for identities already paired in the journal";
      exec = ''
        cd "${config.devenv.root}"
        arduino-cli compile --profile yd-rp2040-4mb-journal \
          --build-property compiler.cpp.extra_flags=-DHA_X2D_COMMANDS_TX=1 \
          --output-dir build/commands firmware/ha_x2d
        python tools/check_uf2_layout.py build/commands/ha_x2d.ino.uf2
        mkdir -p dist
        cp build/commands/ha_x2d.ino.uf2 \
          dist/ha_x2d-0.4.0-yd-rp2040-4mb-COMMANDS-ONLY-UNQUALIFIED-RADIO.uf2
      '';
    };

    "firmware:rx-debug-build" = {
      description = "Build the passive PIO/DMA receiver with the same protected flash layout";
      exec = ''
        cd "${config.devenv.root}"
        arduino-cli compile --profile yd-rp2040-4mb-journal \
          --output-dir build/rx-debug firmware/rx_debug
        python tools/check_uf2_layout.py build/rx-debug/rx_debug.ino.uf2
      '';
    };

    "firmware:tx-check-build" = {
      description = "Build the digital PIO loopback check with CC1101 kept in IDLE";
      exec = ''
        cd "${config.devenv.root}"
        arduino-cli compile --profile yd-rp2040-4mb-journal \
          --output-dir build/tx-check firmware/tx_check
        python tools/check_uf2_layout.py build/tx-check/tx_check.ino.uf2
      '';
    };
  };
}
