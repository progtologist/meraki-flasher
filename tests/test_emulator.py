"""Guards for the emulator's threading usage.

Python 3.13 added ``threading.Thread._handle`` (the native thread handle).
A private method called ``_handle`` on a Thread subclass is therefore
overwritten by ``Thread.start()`` and later invoked as if it were the method,
which fails with ``TypeError: '_thread._ThreadHandle' object is not callable``.
``Thread._stop`` is likewise an internal.  This test keeps our attribute
names out of that namespace on every Python version.
"""

from __future__ import annotations

import unittest

from meraki_flasher.emulator import MockIspDevice

# Internals of threading.Thread that a subclass must never shadow.
RESERVED_THREAD_NAMES = ("_handle", "_stop", "_bootstrap_inner", "_bootstrap")


class EmulatorThreadingTests(unittest.TestCase):
    def setUp(self) -> None:
        # Never started: the constructor performs no I/O.
        self.device = MockIspDevice(-1)

    def test_does_not_shadow_thread_internals(self) -> None:
        for name in RESERVED_THREAD_NAMES:
            self.assertNotIn(
                name,
                vars(self.device),
                f"instance attribute {name!r} shadows threading.Thread internals",
            )

    def test_packet_handler_is_a_method(self) -> None:
        self.assertTrue(callable(type(self.device)._process_packet))
        self.assertIn("_process_packet", dir(self.device))

    def test_stop_uses_a_separate_event(self) -> None:
        self.assertTrue(hasattr(self.device, "_stop_event"))
        self.device.stop()
        self.assertTrue(self.device._stop_event.is_set())


if __name__ == "__main__":
    unittest.main()
