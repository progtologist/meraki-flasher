"""Reset-watchdog tests over a pseudo terminal."""

from __future__ import annotations

import os
import threading
import time
import unittest

from meraki_flasher import diag
from tests.support import POSIX_ONLY

if POSIX_ONLY:  # pragma: no branch - POSIX only
    import pty


@unittest.skipUnless(POSIX_ONLY, "requires a POSIX pseudo terminal")
class WatchResetTests(unittest.TestCase):
    def _run(self, pause_at: int | None, seconds: float = 3.0) -> diag.WatchResult:
        master, slave = pty.openpty()
        port = os.ttyname(slave)
        stop = threading.Event()

        def writer() -> None:
            for index in range(40):
                if stop.is_set():
                    break
                if pause_at is not None and index == pause_at:
                    time.sleep(1.8)  # a real power cycle: MCU silent, then reboots
                try:
                    os.write(master, b"    2026/10/30 21:56:35\r\n")
                except OSError:
                    return
                time.sleep(0.2)

        thread = threading.Thread(target=writer, daemon=True)
        thread.start()
        try:
            return diag.watch_reset(port, seconds=seconds)
        finally:
            stop.set()
            thread.join(timeout=1.0)
            for fd in (master, slave):
                try:
                    os.close(fd)
                except OSError:
                    pass

    def test_reports_a_reset_when_the_stream_pauses(self) -> None:
        result = self._run(pause_at=5)
        self.assertGreaterEqual(result.resets, 1)
        self.assertGreater(result.max_gap, diag.RESET_GAP)
        self.assertGreater(result.bytes_received, 0)
        self.assertIn("MCU resets detected: ", result.report())

    def test_continuous_stream_reports_no_reset(self) -> None:
        result = self._run(pause_at=None, seconds=2.0)
        self.assertEqual(result.resets, 0)
        self.assertGreater(result.bytes_received, 0)
        self.assertIn("MCU resets detected: 0", result.report())


class PortPresenceTests(unittest.TestCase):
    """``_port_present`` must work where ``os.path.exists("COM5")`` does not."""

    def test_missing_port_is_not_present(self) -> None:
        missing = r"\\.\COM_DOES_NOT_EXIST" if os.name == "nt" else "/dev/meraki-no-such-port"
        self.assertFalse(diag._port_present(missing))

    def test_existing_port_is_present(self) -> None:
        if not POSIX_ONLY:  # pragma: no cover - Windows
            self.skipTest("needs a POSIX pseudo terminal")
        master, slave = pty.openpty()
        try:
            self.assertTrue(diag._port_present(os.ttyname(slave)))
        finally:
            for fd in (master, slave):
                os.close(fd)


if __name__ == "__main__":
    unittest.main()