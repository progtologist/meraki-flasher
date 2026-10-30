"""Guards for the emulator's threading usage.

Python 3.13 added ``threading.Thread._handle`` (the native thread handle), and
``Thread._stop`` has always been internal.  A Thread subclass that defines an
attribute or method with one of those names has it replaced by CPython and then
invoked as if it were the method, which fails with
``TypeError: '_thread._ThreadHandle' object is not callable``.

These tests keep our names out of that namespace, on every Python version:
the baseline instance gives us the names CPython itself sets, so only *our*
additions are checked.
"""

from __future__ import annotations

import threading
import unittest

from meraki_flasher.emulator import MockIspDevice

# ``run`` is the documented extension point of Thread, so it may be overridden.
ALLOWED_OVERRIDES = {"run"}


class EmulatorThreadingTests(unittest.TestCase):
    def setUp(self) -> None:
        # Never started: the constructor performs no I/O.
        self.device = MockIspDevice(-1)
        self.cpython_attributes = set(vars(threading.Thread(target=lambda: None)))
        self.thread_namespace = set(dir(threading.Thread))

    def test_instance_attributes_do_not_shadow_thread_internals(self) -> None:
        ours = set(vars(self.device)) - self.cpython_attributes
        shadowing = sorted(ours & self.thread_namespace)
        self.assertEqual(
            shadowing, [], f"instance attributes shadow threading.Thread: {shadowing}"
        )

    def test_methods_do_not_shadow_thread_internals(self) -> None:
        ours = {
            name
            for name in vars(type(self.device))
            if not name.startswith("__")
        } - ALLOWED_OVERRIDES
        shadowing = sorted(ours & self.thread_namespace)
        self.assertEqual(shadowing, [], f"methods shadow threading.Thread: {shadowing}")

    def test_packet_handler_is_a_real_method(self) -> None:
        self.assertTrue(callable(type(self.device)._process_packet))
        self.assertIn("_process_packet", dir(self.device))

    def test_stop_uses_a_separate_event(self) -> None:
        self.assertTrue(hasattr(self.device, "_stop_event"))
        self.device.stop()
        self.assertTrue(self.device._stop_event.is_set())


if __name__ == "__main__":
    unittest.main()

