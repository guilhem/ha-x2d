"""Reject UF2 images that can overwrite the raw journal reserved by our profile."""
import argparse
from pathlib import Path
import struct

from firmware_layout import FLASH_BASE, JOURNAL_ADDRESS
FAMILY = 0xE48BFF56


def check(data, journal_address=JOURNAL_ADDRESS):
    if not data or len(data) % 512:
        raise ValueError("Truncated UF2")
    count = len(data) // 512
    seen = set()
    addresses = set()
    for offset in range(0, len(data), 512):
        a, b, flags, address, size, index, total, family = struct.unpack_from("<8I", data, offset)
        tail, = struct.unpack_from("<I", data, offset + 508)
        if ((a, b, tail) != (0x0A324655, 0x9E5D5157, 0x0AB16F30)
                or flags != 0x2000 or family != FAMILY or size != 256
                or total != count or index >= count or index in seen
                or address % 256 or address in addresses
                or not 0x10000000 <= address < journal_address
                or address + size > journal_address):
            raise ValueError("Invalid UF2 or protected journal overlap")
        seen.add(index)
        addresses.add(address)
    return count


def check_binary(data, binary):
    """Require every active BIN byte and FF tail to match the bytes flashed by UF2."""
    count = check(data)
    if count != (len(binary) + 255) // 256:
        raise ValueError("UF2 does not cover exactly the binary")
    addresses = set()
    for offset in range(0, len(data), 512):
        address, = struct.unpack_from("<I", data, offset + 12)
        start = address - FLASH_BASE
        addresses.add(start)
        expected = binary[start:start + 256]
        expected += b"\xff" * (256 - len(expected))
        if data[offset + 32:offset + 288] != expected:
            raise ValueError("UF2 payload differs from canonical binary/FF padding")
    if addresses != set(range(0, (len(binary) + 255) // 256 * 256, 256)):
        raise ValueError("UF2 has gaps in the active image")
    return count


def canonicalize(data, binary):
    """Replace only converter padding; never repair a differing executable byte."""
    check(data)
    result = bytearray(data)
    for offset in range(0, len(result), 512):
        address, = struct.unpack_from("<I", result, offset + 12)
        start = address - FLASH_BASE
        length = max(0, min(256, len(binary) - start))
        if result[offset + 32:offset + 32 + length] != binary[start:start + length]:
            raise ValueError("UF2 active bytes differ from binary")
        result[offset + 32 + length:offset + 288] = b"\xff" * (256 - length)
    check_binary(result, binary)
    return bytes(result)


def self_check():
    block = bytearray(512)
    struct.pack_into("<8I", block, 0, 0x0A324655, 0x9E5D5157, 0x2000,
                     0x10000000, 256, 0, 1, FAMILY)
    struct.pack_into("<I", block, 508, 0x0AB16F30)
    assert check(block) == 1
    struct.pack_into("<I", block, 12, JOURNAL_ADDRESS)
    try:
        check(block)
    except ValueError:
        pass
    else:
        raise AssertionError("Journal overwrite accepted")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("image", type=Path, nargs="?")
    parser.add_argument("--binary", type=Path, help="also check every payload against the canonical BIN")
    args = parser.parse_args()
    self_check()
    if args.image:
        data = args.image.read_bytes()
        count = check_binary(data, args.binary.read_bytes()) if args.binary else check(data)
        print(f"OK: {count} blocks below journal" + ("; canonical BIN/FF padding matches" if args.binary else ""))
    elif args.binary:
        parser.error("--binary requires a UF2 image")
    else:
        print("OK: UF2 journal protection self-check")
