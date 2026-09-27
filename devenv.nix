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
  packages = [ (pkgs.arduino-cli.pureGoPkg or pkgs.arduino-cli) pkgs.gcc ];

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
      description = "Run USB client and Home Assistant tests with simulated hardware";
      after = [ "devenv:python:uv" ];
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
        g++ -std=c++17 -Wall -Wextra -Werror \
          -Ibuild/firmware-check/include firmware/check_protocol.cpp \
          -o build/firmware-check/check_protocol
        build/firmware-check/check_protocol
      '';
    };

    "firmware:build" = {
      description = "Build the YD-RP2040 diagnostic UF2 using the pinned Arduino profile";
      exec = ''
        cd "${config.devenv.root}"
        arduino-cli compile --profile yd-rp2040-2mb \
          --output-dir build/firmware firmware/ha_x2d
        mkdir -p dist
        cp build/firmware/ha_x2d.ino.uf2 \
          dist/ha_x2d-yd-rp2040-2mb-no-fs-UNVERIFIED-HARDWARE.uf2
      '';
    };

    "ha:package" = {
      description = "Build the Home Assistant archive with the independent Python client";
      exec = ''
        cd "${config.devenv.root}"
        python tools/build_component.py
      '';
    };
  };
}
