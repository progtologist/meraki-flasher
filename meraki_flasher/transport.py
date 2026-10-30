"""Serial transport used by the ISP driver.

Only :mod:`pyserial` is required, so the same code path works on Linux,
macOS and Windows.  All ISP timing is handled by :meth:`read_exact`, which
accumulates bytes until the requested amount arrives or a deadline passes.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable

import serial
from serial.tools import list_ports


class TransportError(Exception):
    """Raised when the serial port cannot be opened or written to."""


class ReadTimeout(TransportError):
    """Raised when a full packet did not arrive before the deadline."""


@dataclass(frozen=True)
class PortInfo:
    device: str
    description: str
    hwid: str
    is_usb: bool

    def label(self) -> str:
        if self.is_usb and self.description and self.description not in ("n/a", "USB Serial"):
            return f"{self.device}  ({self.description})"
        return self.device


def _looks_like_usb(info: object) -> bool:
    """Heuristic: real USB adapters report a VID/PID or a usb hwid."""
    hwid = getattr(info, "hwid", "") or ""
    if "VID:PID" in hwid.upper() or "USB" in hwid.upper():
        return True
    vid = getattr(info, "vid", None)
    return vid is not None


def list_serial_ports(include_all: bool = False) -> list[PortInfo]:
    """Enumerate serial ports, hiding the legacy ``/dev/ttyS*`` noise.

    On macOS ``/dev/cu.*`` entries are preferred over ``/dev/tty.*``
    because the latter block waiting for carrier detect.
    """
    ports: list[PortInfo] = []
    for info in list_ports.comports():
        device = info.device
        is_usb = _looks_like_usb(info)
        if device.startswith("/dev/tty.") and not include_all:
            # macOS: the matching /dev/cu.* entry is listed separately.
            continue
        if not is_usb and not include_all:
            continue
        ports.append(
            PortInfo(
                device=device,
                description=info.description or "",
                hwid=info.hwid or "",
                is_usb=is_usb,
            )
        )
    ports.sort(key=lambda p: (not p.is_usb, p.device))
    return ports


class SerialTransport:
    """Thin wrapper around :class:`serial.Serial` with deadline reads."""

    def __init__(self, port: str, baudrate: int = 115200) -> None:
        self.port = port
        self.baudrate = baudrate
        self._serial: serial.Serial | None = None

    # -- lifecycle -----------------------------------------------------
    def open(self) -> None:
        try:
            self._serial = serial.Serial(
                port=self.port,
                baudrate=self.baudrate,
                bytesize=serial.EIGHTBITS,
                parity=serial.PARITY_NONE,
                stopbits=serial.STOPBITS_ONE,
                timeout=0.02,
                write_timeout=2.0,
                xonxoff=False,
                rtscts=False,
                dsrdtr=False,
            )
        except (serial.SerialException, OSError, ValueError) as exc:
            raise TransportError(f"cannot open {self.port}: {exc}") from exc
        # NuvoISP enables DTR (CUartIO: fDtrControl = DTR_CONTROL_ENABLE).
        try:
            self._serial.dtr = True
        except (OSError, serial.SerialException):
            pass
        self.reset_input_buffer()

    def close(self) -> None:
        if self._serial is not None:
            try:
                self._serial.close()
            finally:
                self._serial = None

    @property
    def is_open(self) -> bool:
        return self._serial is not None and self._serial.is_open

    def reopen(self) -> None:
        """Close and reopen the port (the vendor's ``ReOpen_Port``).

        Closing drops DTR and re-opening asserts it again; both buffers are
        flushed, so no stale application output can leak into the session.
        """
        self.close()
        self.open()

    # -- io ------------------------------------------------------------
    def reset_input_buffer(self) -> None:
        if self._serial is not None:
            try:
                self._serial.reset_input_buffer()
            except (OSError, serial.SerialException):
                pass

    def write(self, data: bytes, purge_rx: bool = True) -> None:
        """Write a packet.

        ``purge_rx`` mirrors ``CUartIO::WriteFile()``, which calls
        ``PurgeComm(PURGE_RXCLEAR)`` before every write: stale bytes from a
        power-on glitch or from the application's own UART output must never
        be consumed as if they were part of an ACK.
        """
        if self._serial is None:
            raise TransportError("port is not open")
        if purge_rx:
            self.reset_input_buffer()
        try:
            self._serial.write(data)
            self._serial.flush()
        except (OSError, serial.SerialException) as exc:
            self.close()
            raise TransportError(f"write failed: {exc}") from exc

    def read_available(self, timeout: float) -> bytes:
        """Return whatever bytes arrive within ``timeout`` (may be empty)."""
        if self._serial is None:
            raise TransportError("port is not open")
        deadline = time.monotonic() + timeout
        buf = bytearray()
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            self._serial.timeout = min(remaining, 0.02)
            try:
                waiting = self._serial.in_waiting
                chunk = self._serial.read(waiting or 1)
            except (OSError, serial.SerialException) as exc:
                self.close()
                raise TransportError(f"read failed: {exc}") from exc
            if chunk:
                buf.extend(chunk)
        return bytes(buf)

    def read_exact(self, count: int, timeout: float) -> bytes:
        """Read exactly ``count`` bytes or raise :class:`ReadTimeout`."""
        if self._serial is None:
            raise TransportError("port is not open")
        deadline = time.monotonic() + timeout
        buf = bytearray()
        while len(buf) < count:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ReadTimeout(
                    f"timeout after {timeout:.2f}s, got {len(buf)}/{count} bytes"
                )
            self._serial.timeout = min(remaining, 0.05)
            try:
                want = count - len(buf)
                waiting = self._serial.in_waiting
                chunk = self._serial.read(min(want, waiting) if waiting else min(want, 1))
            except (OSError, serial.SerialException) as exc:
                self.close()
                raise TransportError(f"read failed: {exc}") from exc
            if chunk:
                buf.extend(chunk)
        return bytes(buf)

    def read_frame(
        self,
        count: int,
        timeout: float,
        matcher: Callable[[bytes], bool] | None = None,
        grace: float = 0.2,
    ) -> bytes:
        """Read one ``count``-byte frame, resynchronising past any junk.

        With ``matcher`` set, every ``count``-byte window in the received
        stream is tested and the first window that satisfies it is returned.
        This keeps the protocol locked even if the MCU's application firmware
        prints text on the same UART right before the ISP window opens.

        ``grace`` bounds how long we keep looking once at least one full
        frame has arrived without matching, so a corrupt ACK fails fast
        instead of burning the whole read timeout.
        """
        if self._serial is None:
            raise TransportError("port is not open")
        deadline = time.monotonic() + timeout
        buf = bytearray()
        last_data: float | None = None
        while True:
            if matcher is None:
                if len(buf) >= count:
                    return bytes(buf[:count])
            elif len(buf) >= count:
                for start in range(0, len(buf) - count + 1):
                    frame = bytes(buf[start : start + count])
                    if matcher(frame):
                        return frame
                if last_data is not None and (time.monotonic() - last_data) > grace:
                    raise ReadTimeout(
                        f"received {len(buf)} bytes without a matching frame"
                    )
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ReadTimeout(
                    f"timeout after {timeout:.2f}s, "
                    f"received {len(buf)} bytes without a matching frame"
                )
            self._serial.timeout = min(remaining, 0.02)
            try:
                waiting = self._serial.in_waiting
                chunk = self._serial.read(waiting if waiting else 1)
            except (OSError, serial.SerialException) as exc:
                self.close()
                raise TransportError(f"read failed: {exc}") from exc
            if chunk:
                buf.extend(chunk)
                last_data = time.monotonic()
                if len(buf) > 8 * count:  # never grow without bound
                    del buf[: len(buf) - 8 * count]


