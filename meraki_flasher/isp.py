"""NuMicro ISP command driver (UART transport).

This is a faithful Python port of the command layer in
OpenNuvoton/ISPTool ``NuvoISP/ISPLdCMD.cpp`` (Apache-2.0, Copyright 2025
Nuvoton Technology Corporation).  The command sequence matches what
NuvoISP 4.13 performs for "UART + APROM + Reset and Run":

    CMD_CONNECT (poll) -> CMD_SYNC_PACKNO -> CMD_GET_VERSION
    -> CMD_GET_DEVICEID -> CMD_READ_CONFIG -> CMD_SYNC_PACKNO
    -> CMD_UPDATE_APROM (chunks) -> CMD_RUN_APROM
"""

from __future__ import annotations

import struct
import time
from typing import Callable

from . import protocol
from .protocol import Ack, AckMismatch
from .transport import ReadTimeout

LogFn = Callable[[str], None]
ProgressFn = Callable[[int, int], None]
CancelFn = Callable[[], bool]

COMMAND_TIMEOUT = 5.0    # USBCMD_TIMEOUT
PROGRAM_TIMEOUT = 25.0   # USBCMD_TIMEOUT_LONG: the first APROM packet erases
RESET_DELAY = 0.5        # wait after CMD_RUN_APROM

# Packet index bookkeeping, taken verbatim from the reference tool:
#   ISPLdCMD::ISPLdCMD() : m_uCmdIndex(18)
#     // "Do not use 0 to avoid firmware already has index 0 occasionally."
# then +2 per write, and CMD_CONNECT/CMD_SYNC_PACKNO renumber as below.
INDEX_START = 18
INDEX_SYNC = 1
INDEX_AFTER_CONNECT = 3

# CMD_Connect() response window, straight from Thread_CheckUSBConnect():
#     dwConnectTime = 40;
#     if ((640000 / baud) > dwConnectTime) dwConnectTime = 2 * 640000 / baud;
CONNECT_WINDOW_MS = 40


def connect_window(baudrate: int) -> float:
    """Per-attempt CMD_CONNECT timeout in seconds, as the vendor computes it."""
    baud = max(1, int(baudrate))
    base = 640000 // baud
    if base > CONNECT_WINDOW_MS:
        return (2 * 640000 / baud) / 1000.0
    return CONNECT_WINDOW_MS / 1000.0


class IspError(Exception):
    """Generic ISP failure."""


class ConnectTimeout(IspError):
    """No device answered CMD_CONNECT within the given window."""


class Cancelled(IspError):
    """The caller asked to abort."""


class NuMicroIsp:
    """Command-level driver for one device on one serial port."""

    def __init__(
        self,
        transport: object,
        log: LogFn | None = None,
        hexdump: bool = False,
        command_timeout: float = COMMAND_TIMEOUT,
        program_timeout: float = PROGRAM_TIMEOUT,
        verbose: bool = True,
    ) -> None:
        self.transport = transport
        self.index = INDEX_START
        self.capabilities = 0
        self.command_timeout = command_timeout
        self.program_timeout = program_timeout
        self._log_fn = log
        self.hexdump = hexdump
        self.verbose = verbose
        self._sent = b""
        self._sent_index = 0

    # -- logging ------------------------------------------------------
    def _log(self, message: str) -> None:
        if self._log_fn is not None:
            self._log_fn(message)

    def _trace(self, direction: str, data: bytes) -> None:
        if not self.hexdump:
            return
        self._log(f"  {direction} {len(data):3d}B  {data.hex(' ')}")

    # -- framing ------------------------------------------------------
    def _write(self, cmd: int, payload: bytes = b"", purge: bool = True) -> bytes:
        packet = protocol.build_packet(cmd, self.index, payload)
        self._sent = packet
        self._sent_index = self.index
        name = protocol.CMD_NAMES.get(cmd, f"0x{cmd:08X}")
        self._trace("TX", packet)
        self.transport.write(packet, purge_rx=purge)
        if self.verbose:
            self._log(f"TX {name:<12} index={self._sent_index:<7d} payload={len(payload):2d}B")
        self.index += 2
        return packet

    def _read_ack(self, timeout: float, check_index: bool = True) -> Ack:
        expected_checksum = protocol.checksum(self._sent)
        expected_index = self._sent_index + 1

        def matcher(frame: bytes) -> bool:
            if struct.unpack_from("<H", frame, 0)[0] != expected_checksum:
                return False
            if check_index and struct.unpack_from("<I", frame, 4)[0] != expected_index:
                return False
            return True

        raw = self.transport.read_frame(protocol.PACKET_SIZE, timeout, matcher)
        self._trace("RX", raw)
        ack = protocol.parse_ack(raw)
        protocol.validate_ack(ack, self._sent, check_index=check_index)
        return ack

    # -- commands -----------------------------------------------------
    def connect(
        self,
        timeout: float = 60.0,
        cancel: CancelFn | None = None,
        on_wait: Callable[[float], None] | None = None,
    ) -> Ack:
        """Poll CMD_CONNECT until the device answers.

        The LDROM bootloader only listens for ~300 ms after reset, so the
        host has to keep polling - this is why NuvoISP asks you to power
        cycle the machine after clicking Connect.
        """
        deadline = time.monotonic() + timeout
        attempts = 0
        window = connect_window(getattr(self.transport, "baudrate", 115200))
        previous_verbose = self.verbose
        # A poll every ~40 ms would flood the log; report progress instead.
        self.verbose = False
        try:
            while True:
                if cancel is not None and cancel():
                    raise Cancelled("cancelled while waiting for the device")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise ConnectTimeout(
                        f"no device answered CMD_CONNECT within {timeout:.0f}s "
                        f"({attempts} attempts)"
                    )
                attempts += 1
                # Do NOT purge while hunting for the device: the ACK may
                # arrive just after a poll window, and the reader
                # resynchronises past application chatter anyway.
                self._write(protocol.CMD_CONNECT, purge=False)
                try:
                    ack = self._read_ack(min(window, remaining), check_index=False)
                except (ReadTimeout, AckMismatch):
                    if on_wait is not None:
                        on_wait(max(0.0, deadline - time.monotonic()))
                    continue
                break
        finally:
            self.verbose = previous_verbose

        # ISPProc::Thread_CheckDeviceConnect() calls ReOpen_Port() first:
        # for UART that closes and reopens the port, which drops and
        # re-asserts DTR and throws away anything stale in the buffers.
        reopen = getattr(self.transport, "reopen", None)
        if reopen is not None:
            try:
                reopen()
            except Exception as exc:  # noqa: BLE001 - keep the handshake result
                self._log(f"warning: could not reopen the port ({exc})")

        self.index = INDEX_AFTER_CONNECT
        self.capabilities = ack.u32(0)
        spi = ", SPI flash" if self.capabilities == protocol.CONNECT_CAPABILITY_SPI else ""
        self._log(
            f"connected after {attempts} attempt(s), "
            f"capability 0x{self.capabilities:08x}{spi}"
        )
        return ack

    def sync_packno(self) -> None:
        """Reset both packet counters; the ACK index is not validated."""
        self.index = INDEX_SYNC
        self._write(protocol.CMD_SYNC_PACKNO, struct.pack("<I", INDEX_SYNC))
        self._read_ack(self.command_timeout, check_index=False)

    def get_version(self) -> int:
        self._write(protocol.CMD_GET_VERSION)
        return self._read_ack(self.command_timeout).u8(0)

    def get_device_id(self) -> int:
        self._write(protocol.CMD_GET_DEVICEID)
        return self._read_ack(self.command_timeout).u32(0)

    def read_config(self) -> list[int]:
        """Read the 14 CONFIG dwords (56 bytes of payload)."""
        self._write(protocol.CMD_READ_CONFIG)
        ack = self._read_ack(self.command_timeout)
        return list(ack.words()[:14])

    def cmd_resend(self) -> None:
        """CMD_RESEND_PACKET: roll the device's write pointer back one chunk."""
        self._write(protocol.CMD_RESEND_PACKET)
        self._read_ack(self.program_timeout, check_index=False)

    # -- APROM programming --------------------------------------------
    def update_aprom(
        self,
        data: bytes,
        progress: ProgressFn | None = None,
        cancel: CancelFn | None = None,
        resend_retries: int = 10,
    ) -> Ack:
        """Program ``data`` at APROM address 0, mirroring ISPProc.cpp.

        The first packet erases the target region, hence the long timeout.
        A failed chunk is retried after CMD_RESEND_PACKET; a failure on the
        very first chunk aborts immediately, exactly like the reference
        implementation.
        """
        total = len(data)
        offset = 0
        ack: Ack | None = None
        while offset < total:
            if cancel is not None and cancel():
                raise Cancelled("cancelled while programming")
            first = offset == 0
            size = protocol.APROM_FIRST_CHUNK if first else protocol.APROM_CHUNK
            chunk = data[offset : offset + size]
            if first:
                payload = struct.pack("<II", 0, total) + chunk
                cmd = protocol.CMD_UPDATE_APROM
                timeout = self.program_timeout
            else:
                payload = chunk
                cmd = 0
                timeout = self.command_timeout

            attempts = 0
            while True:
                try:
                    self._write(cmd, payload)
                    ack = self._read_ack(timeout, check_index=True)
                    break
                except (ReadTimeout, AckMismatch) as exc:
                    attempts += 1
                    self._log(
                        f"chunk at 0x{offset:06x} failed: {exc} "
                        f"(attempt {attempts}/{resend_retries})"
                    )
                    if first or attempts >= resend_retries:
                        raise IspError(
                            f"programming failed at offset 0x{offset:06x}: {exc}"
                        ) from exc
                    try:
                        self.cmd_resend()
                    except (ReadTimeout, AckMismatch, IspError) as resend_exc:
                        raise IspError(
                            f"resend request failed at offset 0x{offset:06x}: {resend_exc}"
                        ) from resend_exc
            offset += len(chunk)
            if progress is not None:
                progress(offset, total)

        if ack is None:
            raise IspError("nothing to program")
        return ack

    def run_aprom(self) -> None:
        """CMD_RUN_APROM: reset and boot the new firmware (no ACK)."""
        self._write(protocol.CMD_RUN_APROM)
        time.sleep(RESET_DELAY)


