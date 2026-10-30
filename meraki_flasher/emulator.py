"""A protocol-accurate ISP device emulator used for tests and demo mode.

It answers on the *master* side of a pseudo terminal, while the real
client code opens the matching ``/dev/pts/N`` path through pyserial - so
the whole stack (framing, checksums, packet indices, chunking) is
exercised end to end without any hardware.
"""

from __future__ import annotations

import os
import struct
import threading
import time

from . import protocol


class MockIspDevice(threading.Thread):
    """Emulates the LDROM ISP bootloader of an M451VE6AE."""

    def __init__(
        self,
        master_fd: int,
        *,
        device_id: int = 0x00845131,
        isp_version: int = 0x33,
        config: tuple[int, ...] | None = None,
        aprom_size: int = 96 * 1024,
        silent_for: float = 0.0,
        fail_ack_at_chunk: int | None = None,
        capability: int = 0,
        junk_prefix: bytes = b"",
    ) -> None:
        super().__init__(daemon=True, name="MockIspDevice")
        self._fd = master_fd
        self.device_id = device_id
        self.isp_version = isp_version
        self.config = list(config) if config else [0x7FAEFF66, 0x00018000] + [0] * 12
        self.aprom_size = aprom_size
        self.capability = capability
        self.silent_for = silent_for
        self.fail_ack_at_chunk = fail_ack_at_chunk
        self.junk_prefix = junk_prefix

        self.image = bytearray(b"\xff" * aprom_size)
        self.written = 0
        self.packets_received = 0
        self.chunks_received = 0
        self.resends = 0
        self.violations: list[str] = []
        self.running = False
        self.rebooted = False
        self._stop_event = threading.Event()
        self._expected_index: int | None = None
        self._cursor = 0
        self._total = 0
        self._running_sum = 0
        self._packet_checksum = 0

    # -- helpers ------------------------------------------------------
    def _ack(self, cmd_index: int, payload: bytes = b"", *, checksum: int | None = None) -> None:
        body = payload.ljust(protocol.PAYLOAD_SIZE, b"\x00")
        ack_checksum = self._packet_checksum if checksum is None else checksum
        raw = struct.pack("<HHI", ack_checksum & 0xFFFF, 0, cmd_index + 1) + body
        try:
            os.write(self._fd, self.junk_prefix + raw)
        except OSError:
            pass

    def _violation(self, message: str) -> None:
        self.violations.append(message)

    # -- protocol -----------------------------------------------------
    def _process_packet(self, raw: bytes) -> None:
        if len(raw) != protocol.PACKET_SIZE:
            self._violation(f"short packet: {len(raw)} bytes")
            return
        cmd, index = struct.unpack_from("<II", raw, 0)
        payload = raw[8:]
        self.packets_received += 1
        self._packet_checksum = protocol.checksum(raw)

        # CONNECT, SYNC_PACKNO and RESEND_PACKET re-establish or repair the
        # packet counter, so the device must not validate their index.
        if cmd == protocol.CMD_CONNECT:
            # The device resets its packet counter to 1 on CMD_CONNECT, so
            # the host continues at index 3 regardless of how many polls
            # preceded the successful one.
            self._expected_index = 3
            self._ack(index, struct.pack("<I", self.capability))
            return
        if cmd == protocol.CMD_SYNC_PACKNO:
            self._expected_index = index + 2
            self._ack(index)
            return
        if cmd == protocol.CMD_RESEND_PACKET:
            self.resends += 1
            self._expected_index = index + 2
            self._ack(index)
            return

        if self._expected_index is not None and index != self._expected_index:
            self._violation(f"unexpected index {index}, expected {self._expected_index}")
            return
        self._expected_index = index + 2

        if cmd == protocol.CMD_GET_VERSION:
            self._ack(index, struct.pack("<B", self.isp_version))
        elif cmd == protocol.CMD_GET_DEVICEID:
            self._ack(index, struct.pack("<I", self.device_id))
        elif cmd == protocol.CMD_READ_CONFIG:
            self._ack(index, struct.pack("<14I", *self.config))
        elif cmd == protocol.CMD_UPDATE_APROM:
            start, total = struct.unpack_from("<II", payload, 0)
            if start != 0:
                self._violation(f"start address 0x{start:x} (expected 0)")
            self._total = total
            self.image = bytearray(b"\xff" * self.aprom_size)
            self.written = 0
            self._running_sum = 0
            self._cursor = start
            self._store(payload[8:protocol.PAYLOAD_SIZE], index)
        elif cmd == 0:
            self._store(payload, index)
        elif cmd == protocol.CMD_RUN_APROM:
            self.running = True
            self.rebooted = True
        else:
            self._violation(f"unknown command 0x{cmd:08x}")

    def _store(self, chunk: bytes, index: int) -> None:
        self.chunks_received += 1
        if self.fail_ack_at_chunk == self.chunks_received:
            self.fail_ack_at_chunk = None
            self._ack(index, checksum=(self._packet_checksum ^ 0xFFFF))
            return
        end = min(self._cursor + len(chunk), self.aprom_size)
        stored = chunk[: end - self._cursor]
        self.image[self._cursor : end] = stored
        self._running_sum = (self._running_sum + sum(stored)) & 0xFFFF
        self._cursor += len(chunk)
        self.written = max(self.written, self._cursor)
        self._ack(index, struct.pack("<H", self._running_sum))

    # -- thread -------------------------------------------------------
    def run(self) -> None:  # noqa: D102
        deadline = time.monotonic() + self.silent_for
        buf = bytearray()
        while not self._stop_event.is_set():
            try:
                data = os.read(self._fd, 4096)
            except OSError:
                break
            if not data:
                break
            if time.monotonic() < deadline:
                continue  # device still booting: drop silently
            buf.extend(data)
            while len(buf) >= protocol.PACKET_SIZE:
                packet = bytes(buf[: protocol.PACKET_SIZE])
                del buf[: protocol.PACKET_SIZE]
                self._process_packet(packet)

    def stop(self) -> None:
        self._stop_event.set()
