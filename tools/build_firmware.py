"""Build the single supported 4 MiB layout, and check the actual output images."""
import argparse
from pathlib import Path
import subprocess
from firmware_layout import FLASH_LENGTH, validate_binary
from check_uf2_layout import check

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("sketch", choices=["ha_x2d", "rx_debug", "tx_check"], nargs="?", default="ha_x2d")
    parser.add_argument("--output-dir", type=Path, default=ROOT / "build/firmware")
    parser.add_argument("--cpp-flags", default="")
    args = parser.parse_args()
    output = args.output_dir.resolve()
    command = ["arduino-cli", "compile", "--profile", "yd-rp2040-4mb-ota",
               "--build-property", f"build.flash_length={FLASH_LENGTH}",
               "--build-property", f"upload.maximum_size={FLASH_LENGTH}",
               "--output-dir", str(output)]
    if args.cpp_flags:
        command += ["--build-property", f"compiler.cpp.extra_flags={args.cpp_flags}"]
    command += [str(ROOT / "firmware" / args.sketch)]
    subprocess.run(command, check=True)
    image = output / f"{args.sketch}.ino"
    check(image.with_suffix(".ino.uf2").read_bytes())
    if args.sketch == "ha_x2d":
        validate_binary(image.with_suffix(".ino.bin").read_bytes())
    print("OK: firmware images protect the journal and match the selected layout")


if __name__ == "__main__":
    main()
