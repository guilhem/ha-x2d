"""Build the single supported 4 MiB layout, and check the actual output images."""
import argparse
from pathlib import Path
import subprocess
from firmware_layout import FLASH_LENGTH, IMAGE_ALIGNMENT, MAX_IMAGE_SIZE, image_version, validate_binary
from check_uf2_layout import check, check_binary, canonicalize

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
    uf2 = image.with_suffix(".ino.uf2")
    binary = image.with_suffix(".ino.bin")
    raw = binary.read_bytes()
    if args.sketch == "ha_x2d":
        padded = validate_binary(raw)
        image_version(padded)
    else:
        padded = raw + b"\xff" * (-len(raw) % IMAGE_ALIGNMENT)
        if len(padded) > MAX_IMAGE_SIZE:
            raise ValueError("Diagnostic image exceeds the image size bound")
    # The core converter pads its final UF2 payload with zeroes. Canonicalize
    # that tail BEFORE padding BIN, so FF padding is identical on both paths.
    canonical_uf2 = canonicalize(uf2.read_bytes(), raw)
    check_binary(canonical_uf2, padded)
    binary.write_bytes(padded)
    uf2.write_bytes(canonical_uf2)
    check(canonical_uf2)
    print("OK: firmware images protect the journal and match the selected layout")


if __name__ == "__main__":
    main()
