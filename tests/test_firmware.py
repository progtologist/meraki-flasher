"""Firmware loading / validation tests."""

from __future__ import annotations

import unittest
from pathlib import Path

from meraki_flasher import chips, firmware
from tests.support import (
    FIRMWARE_PATH,
    PROJECT_ROOT,
    REAL_FIRMWARE_AVAILABLE,
    fixture_bytes,
    load_image,
)


class FirmwareTests(unittest.TestCase):
    def test_fixture_looks_like_firmware(self) -> None:
        """The synthetic image must pass the same sanity checks."""
        data = fixture_bytes()
        self.assertEqual(len(data), 63460)
        image = firmware.FirmwareImage(path=Path("fixture.bin"), data=data)
        self.assertTrue(image.looks_like_cortex_m(chips.FALLBACK_APROM_SIZE))
        self.assertFalse(image.looks_like_cortex_m(1024))

    @unittest.skipUnless(REAL_FIRMWARE_AVAILABLE, "Meraki firmware not available")
    def test_real_image_metadata(self) -> None:
        assert FIRMWARE_PATH is not None
        image = firmware.load(FIRMWARE_PATH)
        self.assertEqual(image.size, 63460)
        # The value NuvoISP displays next to the file size.
        self.assertEqual(image.checksum, 0x2BE4)
        self.assertEqual(image.describe(), "size: 62.0K Bytes, checksum: 2be4")

    def test_image_used_by_the_suite_is_loadable(self) -> None:
        image = load_image()
        self.assertGreater(image.size, 1024)
        self.assertTrue(image.looks_like_cortex_m(chips.FALLBACK_APROM_SIZE))

    def test_missing_file(self) -> None:
        with self.assertRaises(firmware.FirmwareError):
            firmware.load(PROJECT_ROOT / "no-such-firmware.bin")

    def test_size_guard(self) -> None:
        image = load_image()
        firmware.check_fits(image, 96 * 1024)  # must not raise
        with self.assertRaises(firmware.FirmwareError):
            firmware.check_fits(image, 32 * 1024)

    def test_human_size(self) -> None:
        self.assertEqual(firmware.human_size(512), "512 Bytes")
        self.assertEqual(firmware.human_size(63460), "62.0K Bytes")

    def test_chip_lookup(self) -> None:
        chip = chips.lookup(chips.TARGET_PID)
        self.assertIsNotNone(chip)
        assert chip is not None
        self.assertEqual(chip.part, chips.TARGET_PART)
        self.assertEqual(chip.aprom_size, 96 * 1024)
        self.assertEqual(chip.data_flash, 32 * 1024)
        self.assertIsNone(chips.lookup(0xDEADBEEF))
        self.assertEqual(chips.human_kb(96 * 1024), "96K")


if __name__ == "__main__":
    unittest.main()

