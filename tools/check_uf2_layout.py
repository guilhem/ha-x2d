"""Reject UF2 images that can overwrite the raw journal reserved by our profile."""
import argparse
from pathlib import Path
import struct

from firmware_layout import JOURNAL_ADDRESS
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
    args = parser.parse_args()
    self_check()
    if args.image:
        print(f"OK: {check(args.image.read_bytes())} blocks below journal")
    else:
        print("OK: UF2 journal protection self-check")
