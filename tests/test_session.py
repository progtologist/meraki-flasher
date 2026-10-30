"""End-to-end tests: the real client stack against the emulated MCU on a pty."""

from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path

from meraki_flasher import firmware
from meraki_flasher.session import FlashSession, SessionError
from tests.support import POSIX_ONLY, DeviceHarness, load_image


@unittest.skipUnless(POSIX_ONLY, "requires a POSIX pseudo terminal")
class SessionTests(unittest.TestCase):
    def test_full_flash_end_to_end(self) -> None:
        logs: list[str] = []
        image = load_image()
        with DeviceHarness() as harness:
            session = FlashSession(log=logs.append)
            session.open(harness.port)
            info = session.connect(timeout=5)
            self.assertEqual(info.part, "M451VE6AE")
            self.assertEqual(info.device_id, 0x00845131)
            self.assertEqual(info.isp_version, 0x33)
            self.assertEqual(info.aprom_size, 96 * 1024)
            self.assertEqual(info.summary(), "APROM: 96K, Data: 32K, FW Ver: 0x33")
            self.assertEqual(info.config[0], 0x7FAEFF66)

            session.program(image)
            session.run()
            session.close()

            time.sleep(0.1)
            self.assertEqual(bytes(harness.device.image[: image.size]), image.data)
            self.assertEqual(harness.device.violations, [])
            self.assertTrue(harness.device.rebooted)
            self.assertGreater(harness.device.packets_received, 1100)
            self.assertTrue(any("programming complete" in line for line in logs))

    def test_device_that_boots_late_is_still_found(self) -> None:
        """The LDROM bootloader only listens for ~300 ms after reset."""
        image = load_image()
        with DeviceHarness(silent_for=0.6) as harness:
            session = FlashSession()
            session.open(harness.port)
            session.connect(timeout=5)
            session.program(image)
            session.close()
            time.sleep(0.1)
            self.assertEqual(bytes(harness.device.image[: image.size]), image.data)

    def test_corrupted_ack_is_recovered_with_resend(self) -> None:
        image = load_image()
        logs: list[str] = []
        with DeviceHarness(fail_ack_at_chunk=3) as harness:
            session = FlashSession(log=logs.append)
            session.open(harness.port)
            session.connect(timeout=5)
            session.program(image)
            session.close()
            time.sleep(0.1)
            self.assertEqual(bytes(harness.device.image[: image.size]), image.data)
            self.assertGreaterEqual(harness.device.resends, 1)
            self.assertTrue(any("chunk at 0x" in line for line in logs))

    def test_uart_chatter_before_the_answer_is_tolerated(self) -> None:
        """Application UART output on the same line must not desync the protocol."""
        image = load_image()
        noise = b"Project name :MEM03 , App_version = V5.1.4\r\n\x00\xff"
        with DeviceHarness(junk_prefix=noise) as harness:
            session = FlashSession()
            session.open(harness.port)
            session.connect(timeout=5)
            session.program(image)
            session.close()
            time.sleep(0.1)
            self.assertEqual(bytes(harness.device.image[: image.size]), image.data)
            self.assertEqual(harness.device.violations, [])

    def test_oversized_image_is_refused_before_programming(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            big = Path(tmp) / "too-big.bin"
            big.write_bytes(b"\xff" * (100 * 1024))
            with DeviceHarness() as harness:
                session = FlashSession()
                session.open(harness.port)
                session.connect(timeout=5)
                image = firmware.load(big)
                with self.assertRaises(SessionError):
                    session.program(image)
                session.close()
            self.assertEqual(harness.device.written, 0)

    def test_connect_timeout_without_device(self) -> None:
        with DeviceHarness() as harness:
            session = FlashSession()
            session.open(harness.port)
            harness.device.stop()
            with self.assertRaises(SessionError):
                session.connect(timeout=0.5)
            session.close()


if __name__ == "__main__":
    unittest.main()
