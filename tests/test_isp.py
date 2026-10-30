"""Wire-level tests that pin our packets to the vendor's exact numbering."""

from __future__ import annotations

import struct
import unittest

from meraki_flasher import isp, protocol
from meraki_flasher.transport import ReadTimeout


class FakeTransport:
    """Minimal stand-in that answers every command with a valid ACK."""

    baudrate = 115200

    def __init__(self, silent_attempts: int = 0) -> None:
        self.written: list[bytes] = []
        self.reopens = 0
        self.silent_attempts = silent_attempts
        self.reads = 0

    def write(self, data: bytes, purge_rx: bool = True) -> None:
        self.written.append(bytes(data))

    def read_frame(self, count: int, timeout: float, matcher=None) -> bytes:
        self.reads += 1
        if self.reads <= self.silent_attempts:
            raise ReadTimeout("no answer")
        sent = self.written[-1]
        index = struct.unpack_from("<I", sent, 4)[0]
        frame = struct.pack("<HHI", protocol.checksum(sent), 0, index + 1)
        frame += b"\x00" * protocol.PAYLOAD_SIZE
        if matcher is not None and not matcher(frame):
            raise AssertionError("our own ACK was rejected by the matcher")
        return frame

    def reopen(self) -> None:
        self.reopens += 1


class VendorCompatibilityTests(unittest.TestCase):
    def test_initial_packet_index_is_18(self) -> None:
        """ISPLdCMD::ISPLdCMD(): m_uCmdIndex(18), 'do not use 0'."""
        transport = FakeTransport()
        driver = isp.NuMicroIsp(transport)
        driver.connect(timeout=1.0)
        self.assertEqual(transport.written[0][:4], struct.pack("<I", protocol.CMD_CONNECT))
        self.assertEqual(struct.unpack_from("<I", transport.written[0], 4)[0], 18)
        self.assertEqual(driver.index, isp.INDEX_AFTER_CONNECT)

    def test_polls_step_by_two(self) -> None:
        transport = FakeTransport(silent_attempts=3)
        driver = isp.NuMicroIsp(transport)
        driver.connect(timeout=2.0)
        indices = [struct.unpack_from("<I", packet, 4)[0] for packet in transport.written]
        self.assertEqual(indices, [18, 20, 22, 24])

    def test_port_is_reopened_after_connect(self) -> None:
        """Thread_CheckDeviceConnect() calls ReOpen_Port() before SyncPackno."""
        transport = FakeTransport()
        driver = isp.NuMicroIsp(transport)
        driver.connect(timeout=1.0)
        self.assertEqual(transport.reopens, 1)

    def test_connect_window_matches_vendor_formula(self) -> None:
        self.assertAlmostEqual(isp.connect_window(115200), 0.040)
        self.assertAlmostEqual(isp.connect_window(57600), 0.040)
        self.assertAlmostEqual(isp.connect_window(9600), 2 * 640000 / 9600 / 1000)

    def test_sync_packno_sends_index_1(self) -> None:
        transport = FakeTransport()
        driver = isp.NuMicroIsp(transport)
        driver.sync_packno()
        packet = transport.written[0]
        self.assertEqual(struct.unpack_from("<I", packet, 0)[0], protocol.CMD_SYNC_PACKNO)
        self.assertEqual(struct.unpack_from("<I", packet, 4)[0], 1)
        self.assertEqual(struct.unpack_from("<I", packet, 8)[0], 1)
        self.assertEqual(driver.index, 3)


if __name__ == "__main__":
    unittest.main()
