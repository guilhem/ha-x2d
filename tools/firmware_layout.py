"""The single supported YD-RP2040 4 MiB OTA flash layout."""
import struct

FLASH_BASE = 0x10000000
APP_OFFSET = 0x3000
JOURNAL_ADDRESS = 0x101EF000
JOURNAL_BYTES = 0x10000
FS_START = JOURNAL_ADDRESS + JOURNAL_BYTES
FS_END = 0x103FF000
FLASH_LENGTH = JOURNAL_ADDRESS - FLASH_BASE
IMAGE_ALIGNMENT = 128
MAX_IMAGE_SIZE = (65535 * 16) // IMAGE_ALIGNMENT * IMAGE_ALIGNMENT
IMAGE_MARKER = b"HA-X2D YD-RP2040 OTA/1:"
PARTITION = (FS_START, FS_END, FS_END, FLASH_LENGTH)


def validate_binary(data):
    """Reject foreign layouts and non-gateway images; return canonical FF-padded data."""
    if not APP_OFFSET + 8 <= len(data) <= MAX_IMAGE_SIZE:
        raise ValueError("Firmware must contain the application and fit MySensors' 65535 blocks")
    if struct.unpack_from("<4I", data, APP_OFFSET - 16) != PARTITION:
        raise ValueError("Firmware has an incompatible OTA flash layout")
    stack, entry = struct.unpack_from("<2I", data, APP_OFFSET)
    if (not 0x20000000 < stack <= 0x20042000 or stack % 8
            or not entry & 1 or not FLASH_BASE + APP_OFFSET <= (entry & ~1) < FLASH_BASE + len(data)):
        raise ValueError("Invalid RP2040 application vectors")
    if IMAGE_MARKER not in data[APP_OFFSET:]:
        raise ValueError("Firmware is not an OTA-capable HA-X2D gateway")
    return data + b"\xff" * (-len(data) % IMAGE_ALIGNMENT)


def image_version(data):
    """The compiled OTA identity follows the retained product marker."""
    import re
    identities = re.findall(re.escape(IMAGE_MARKER) + rb"([0-9]{1,5}):", data[APP_OFFSET:])
    if len(identities) != 1 or int(identities[0]) > 65535:
        raise ValueError("Firmware lacks a unique compiled OTA version identity")
    return int(identities[0])
