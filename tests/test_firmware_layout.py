"""The real binary layout and app-only bootloader command must protect persistence."""
from pathlib import Path
import struct
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
from firmware_layout import (APP_OFFSET, FLASH_BASE, FS_START, FS_END, JOURNAL_ADDRESS,
                             IMAGE_MARKER, MAX_IMAGE_SIZE, PARTITION, validate_binary)
from check_uf2_layout import FAMILY, check


def image(size=APP_OFFSET + 513):
    data = bytearray(b"\xff" * size)
    struct.pack_into("<4I", data, APP_OFFSET - 16, *PARTITION)
    struct.pack_into("<2I", data, APP_OFFSET, 0x20042000, FLASH_BASE + APP_OFFSET + 33)
    data[APP_OFFSET + 64:APP_OFFSET + 64 + len(IMAGE_MARKER)] = IMAGE_MARKER
    return data


class FirmwareLayoutChecks(unittest.TestCase):
    def test_binary_and_vector_guards(self):
        padded = validate_binary(image())
        self.assertEqual(len(padded) % 16, 0)
        self.assertEqual(padded[-15:], b"\xff" * 15)
        for offset, value in [(APP_OFFSET - 4, 0x1FF000), (APP_OFFSET - 16, FS_START + 4096),
                              (APP_OFFSET, 0x20042008), (APP_OFFSET + 4, 0x10000001),
                              (APP_OFFSET + 4, FLASH_BASE + APP_OFFSET + 32)]:
            with self.subTest(offset=offset, value=value):
                data = image()
                struct.pack_into("<I", data, offset, value)
                with self.assertRaises(ValueError):
                    validate_binary(data)
        data = image(); data[APP_OFFSET + 64] = 0
        with self.assertRaises(ValueError): validate_binary(data)
        with self.assertRaises(ValueError): validate_binary(b"\xff" * (MAX_IMAGE_SIZE + 1))
        with self.assertRaises(ValueError): validate_binary(b"\xff" * APP_OFFSET)

    def test_uf2_cannot_write_any_journal_page(self):
        for address in [JOURNAL_ADDRESS, JOURNAL_ADDRESS + 256, FS_START, FS_END]:
            block = bytearray(512)
            struct.pack_into("<8I", block, 0, 0x0A324655, 0x9E5D5157, 0x2000,
                             address, 256, 0, 1, FAMILY)
            struct.pack_into("<I", block, 508, 0x0AB16F30)
            with self.subTest(address=address), self.assertRaises(ValueError): check(block)

    def test_app_only_copy_resumes_at_every_sector_cut(self):
        # Model the pinned bootloader's 4KiB erase/program loop with our command,
        # cutting at each completed sector and replaying from its first sector.
        stage = validate_binary(image(APP_OFFSET + 24593))
        stage += b"\xff" * (-len(stage) % 4096)
        self.assertLessEqual(FLASH_BASE + len(stage), JOURNAL_ADDRESS)
        journal_offset = JOURNAL_ADDRESS - FLASH_BASE
        flash = bytearray(b"\x37" * (FS_START - FLASH_BASE))
        flash[:APP_OFFSET] = stage[:APP_OFFSET]
        initial_prefix = flash[:APP_OFFSET]
        initial_journal = flash[journal_offset:]
        destinations = range(APP_OFFSET, len(stage), 4096)
        for cut in range(len(destinations) + 1):
            candidate = flash.copy()
            for offset in list(destinations)[:cut]:
                candidate[offset:offset + 4096] = stage[offset:offset + 4096]
            for offset in destinations:  # bootloader restarts the same durable command
                candidate[offset:offset + 4096] = stage[offset:offset + 4096]
            self.assertEqual(candidate[:APP_OFFSET], initial_prefix)
            self.assertEqual(candidate[journal_offset:], initial_journal)
            self.assertEqual(candidate[APP_OFFSET:len(stage)], stage[APP_OFFSET:])


if __name__ == "__main__": unittest.main()
