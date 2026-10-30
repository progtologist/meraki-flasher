"""NuMicro ISP wire-protocol primitives (UART transport).

The Nuvoton NuMicro ISP protocol exchanges fixed-size 64-byte packets in
both directions.  This module knows only about framing, the checksum and
the APROM chunk layout; command sequencing lives in
:mod:`meraki_flasher.isp`.

Derived from OpenNuvoton/ISPTool ``NuvoISP/ISPLdCMD.cpp`` (Apache-2.0,
Copyright 2025 Nuvoton Technology Corporation), reimplemented in Python.

Packet layout (little-endian)::

    host -> device                    device -> host (ACK)
    +--------+--------+----------+    +----------+---------+---------+----------+
    | cmd u32| idx u32| payload  |    | cksum u16| rsvd u16| idx u32 | payload  |
    +--------+--------+----------+    +----------+---------+---------+----------+
      bytes 0..3 4..7   8..63          bytes 0..1   2..3      4..7      8..63

``cksum`` is the 16-bit sum of the *command* packet that the ACK replies
to, and ``idx`` must equal the command packet's index plus one.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from typing import Iterator

PACKET_SIZE = 64
PAYLOAD_SIZE = PACKET_SIZE - 8  # 56 bytes of payload per packet

APROM_FIRST_CHUNK = 48  # first packet also carries start address + length
APROM_CHUNK = 56        # continuation packets

# Command codes (see the NuMicro ISP Programming Guide).
CMD_UPDATE_APROM = 0x000000A0
CMD_UPDATE_CONFIG = 0x000000A1
CMD_READ_CONFIG = 0x000000A2
CMD_ERASE_ALL = 0x000000A3
CMD_SYNC_PACKNO = 0x000000A4
CMD_GET_VERSION = 0x000000A6
CMD_RUN_APROM = 0x000000AB
CMD_RUN_LDROM = 0x000000AC
CMD_RESET = 0x000000AD
CMD_CONNECT = 0x000000AE
CMD_GET_DEVICEID = 0x000000B1
CMD_UPDATE_DATAFLASH = 0x000000C3
CMD_RESEND_PACKET = 0x000000FF

# Device capability word returned by CMD_CONNECT when SPI flash is supported.
CONNECT_CAPABILITY_SPI = 0x001540EF

CMD_NAMES = {
    CMD_UPDATE_APROM: "UPDATE_APROM",
    CMD_UPDATE_CONFIG: "UPDATE_CONFIG",
    CMD_READ_CONFIG: "READ_CONFIG",
    CMD_ERASE_ALL: "ERASE_ALL",
    CMD_SYNC_PACKNO: "SYNC_PACKNO",
    CMD_GET_VERSION: "GET_VERSION",
    CMD_RUN_APROM: "RUN_APROM",
    CMD_RUN_LDROM: "RUN_LDROM",
    CMD_RESET: "RESET",
    CMD_CONNECT: "CONNECT",
    CMD_GET_DEVICEID: "GET_DEVICEID",
    CMD_UPDATE_DATAFLASH: "UPDATE_DATAFLASH",
    CMD_RESEND_PACKET: "RESEND_PACKET",
    0x00000000: "CONTINUATION",
}


class ProtocolError(Exception):
    """Raised when a packet cannot be built or interpreted."""


class AckMismatch(ProtocolError):
    """Raised when an ACK fails the checksum or packet-index check."""

    def __init__(
        self,
        message: str,
        *,
        expected_checksum: int | None = None,
        received_checksum: int | None = None,
        expected_index: int | None = None,
        received_index: int | None = None,
    ) -> None:
        super().__init__(message)
        self.expected_checksum = expected_checksum
        self.received_checksum = received_checksum
        self.expected_index = expected_index
        self.received_index = received_index


def checksum(data: bytes) -> int:
    """16-bit byte sum, as used by ``ISPLdCMD::Checksum()``."""
    return sum(data) & 0xFFFF


def build_packet(cmd: int, index: int, payload: bytes = b"") -> bytes:
    """Build one 64-byte command packet."""
    if len(payload) > PAYLOAD_SIZE:
        raise ProtocolError(
            f"payload of {len(payload)} bytes exceeds the {PAYLOAD_SIZE}-byte limit"
        )
    header = struct.pack("<II", cmd & 0xFFFFFFFF, index & 0xFFFFFFFF)
    return header + bytes(payload).ljust(PAYLOAD_SIZE, b"\x00")


@dataclass(frozen=True)
class Ack:
    """A parsed device ACK packet."""

    checksum: int
    index: int
    payload: bytes
    raw: bytes

    def u8(self, offset: int = 0) -> int:
        return self.payload[offset]

    def u16(self, offset: int = 0) -> int:
        return struct.unpack_from("<H", self.payload, offset)[0]

    def u32(self, offset: int = 0) -> int:
        return struct.unpack_from("<I", self.payload, offset)[0]

    def words(self) -> tuple[int, ...]:
        """Interpret the payload as little-endian 32-bit words."""
        count = len(self.payload) // 4
        return struct.unpack_from(f"<{count}I", self.payload, 0)


def parse_ack(raw: bytes) -> Ack:
    """Parse a 64-byte ACK packet."""
    if len(raw) != PACKET_SIZE:
        raise ProtocolError(f"ACK must be {PACKET_SIZE} bytes, got {len(raw)}")
    ack_checksum = struct.unpack_from("<H", raw, 0)[0]
    ack_index = struct.unpack_from("<I", raw, 4)[0]
    return Ack(checksum=ack_checksum, index=ack_index, payload=raw[8:], raw=raw)


def validate_ack(ack: Ack, sent_packet: bytes, check_index: bool = True) -> None:
    """Validate an ACK against the command packet it answers.

    Raises :class:`AckMismatch` when the echoed checksum (or, optionally,
    the packet index) does not match what the host sent.
    """
    expected_checksum = checksum(sent_packet)
    if ack.checksum != expected_checksum:
        raise AckMismatch(
            f"ACK checksum 0x{ack.checksum:04x} != expected 0x{expected_checksum:04x}",
            expected_checksum=expected_checksum,
            received_checksum=ack.checksum,
        )
    if not check_index:
        return
    expected_index = struct.unpack_from("<I", sent_packet, 4)[0] + 1
    if ack.index != expected_index:
        raise AckMismatch(
            f"ACK index {ack.index} != expected {expected_index}",
            expected_index=expected_index,
            received_index=ack.index,
        )


def aprom_chunks(
    data: bytes, start_address: int = 0x00000000
) -> Iterator[tuple[int, int, bytes]]:
    """Split ``data`` into ISP packets.

    Yields ``(offset, cmd, payload)`` triples: the first packet carries
    ``CMD_UPDATE_APROM`` plus the start address and total length in its
    payload, every following packet uses command ``0`` (continuation).
    """
    total = len(data)
    offset = 0
    first = True
    while offset < total:
        size = APROM_FIRST_CHUNK if first else APROM_CHUNK
        chunk = data[offset : offset + size]
        if first:
            payload = (
                struct.pack("<II", start_address & 0xFFFFFFFF, total & 0xFFFFFFFF) + chunk
            )
            cmd = CMD_UPDATE_APROM
        else:
            payload = chunk
            cmd = 0
        yield offset, cmd, payload
        offset += len(chunk)
        first = False

