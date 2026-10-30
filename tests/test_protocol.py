"""Framing, checksum and chunking tests."""

from __future__ import annotations

import struct
import unittest

from meraki_flasher import protocol as p
from tests.support import FIRMWARE_NAME, image_bytes


class PacketTests(unittest.TestCase):
    def test_packet_is_64_bytes_and_little_endian(self) -> None:
        packet = p.build_packet(p.CMD_CONNECT, 1)
        self.assertEqual(len(packet), p.PACKET_SIZE)
        self.assertEqual(struct.unpack_from("<I", packet, 0)[0], p.CMD_CONNECT)
        self.assertEqual(struct.unpack_from("<I", packet, 4)[0], 1)
        self.assertEqual(packet[8:], b"\x00" * p.PAYLOAD_SIZE)

    def test_payload_is_placed_after_the_header(self) -> None:
        packet = p.build_packet(p.CMD_READ_CONFIG, 7, b"\xaa\xbb")
        self.assertEqual(packet[8:10], b"\xaa\xbb")

    def test_oversized_payload_rejected(self) -> None:
        with self.assertRaises(p.ProtocolError):
            p.build_packet(0, 1, b"x" * (p.PAYLOAD_SIZE + 1))

    def test_checksum_is_16_bit_sum(self) -> None:
        self.assertEqual(p.checksum(b""), 0)
        self.assertEqual(p.checksum(b"\x01\x02"), 3)
        self.assertEqual(p.checksum(b"\xff" * 64), (0xFF * 64) & 0xFFFF)


class AckTests(unittest.TestCase):
    def _ack(self, sent: bytes, *, index: int | None = None, checksum: int | None = None) -> bytes:
        sent_index = struct.unpack_from("<I", sent, 4)[0]
        raw = struct.pack(
            "<HHI",
            p.checksum(sent) if checksum is None else checksum,
            0,
            sent_index + 1 if index is None else index,
        )
        return raw + b"\x00" * p.PAYLOAD_SIZE

    def test_valid_ack(self) -> None:
        sent = p.build_packet(p.CMD_GET_VERSION, 3)
        ack = p.parse_ack(self._ack(sent))
        p.validate_ack(ack, sent)  # must not raise

    def test_bad_checksum_detected(self) -> None:
        sent = p.build_packet(p.CMD_GET_VERSION, 3)
        ack = p.parse_ack(self._ack(sent, checksum=0x1234))
        with self.assertRaises(p.AckMismatch):
            p.validate_ack(ack, sent)

    def test_bad_index_detected(self) -> None:
        sent = p.build_packet(p.CMD_GET_VERSION, 3)
        ack = p.parse_ack(self._ack(sent, index=99))
        with self.assertRaises(p.AckMismatch):
            p.validate_ack(ack, sent)
        # SYNC/RESEND/connect ACKs skip the index check
        p.validate_ack(ack, sent, check_index=False)

    def test_short_packet_rejected(self) -> None:
        with self.assertRaises(p.ProtocolError):
            p.parse_ack(b"\x00" * 10)

    def test_ack_word_accessors(self) -> None:
        sent = p.build_packet(p.CMD_GET_DEVICEID, 5)
        raw = self._ack(sent)
        raw = raw[:8] + struct.pack("<I", 0x00845131) + raw[12:]
        ack = p.parse_ack(raw)
        self.assertEqual(ack.u32(0), 0x00845131)


class ChunkTests(unittest.TestCase):
    def test_first_chunk_has_address_and_length(self) -> None:
        chunks = list(p.aprom_chunks(b"\x11" * 100))
        offset, cmd, payload = chunks[0]
        self.assertEqual(offset, 0)
        self.assertEqual(cmd, p.CMD_UPDATE_APROM)
        start, total = struct.unpack_from("<II", payload, 0)
        self.assertEqual((start, total), (0, 100))
        self.assertEqual(len(payload), 56)  # 8 header bytes + 48 data bytes
        self.assertEqual(payload[8:], b"\x11" * 48)

        offset, cmd, payload = chunks[1]
        self.assertEqual((offset, cmd), (48, 0))
        self.assertEqual(payload, b"\x11" * 52)

    def test_firmware_sized_image_counts_and_roundtrips(self) -> None:
        """Packet arithmetic, against the real image or the same-size fixture."""
        data = image_bytes()
        chunks = list(p.aprom_chunks(data))
        expected = 1 + -(-(len(data) - p.APROM_FIRST_CHUNK) // p.APROM_CHUNK)
        self.assertEqual(len(chunks), expected)
        # 63460 bytes is what Meraki's image (and the fixture) measure.
        self.assertEqual(len(data), 63460)
        self.assertEqual(len(chunks), 1134)
        rebuilt = b"".join(payload[8:] if cmd else payload for _, cmd, payload in chunks)
        self.assertEqual(rebuilt, data)
        self.assertTrue(FIRMWARE_NAME.endswith(".bin"))


if __name__ == "__main__":
    unittest.main()
