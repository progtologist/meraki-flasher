"""Link diagnostics: find out whether anything answers on a serial port at all.

Used by ``meraki-flasher-cli --probe``.  It sends CMD_CONNECT repeatedly at
several baud rates and reports *any* bytes that come back, which separates
three very different failures:

* nothing received          -> wiring, power, or the MCU is not in ISP mode
* data but no valid ACK     -> the UART works, the peer is not the ISP loader
                               (usually the application is running instead)
* ISP ACK on some baud rate -> wrong baud rate for this board
"""

from __future__ import annotations

import os
import struct
import time
from dataclasses import dataclass, field
from typing import Callable

from . import protocol
from .transport import SerialTransport, TransportError

DEFAULT_BAUDS = (115200, 57600, 38400, 19200, 9600)


@dataclass
class ProbeResult:
    baud: int
    bytes_received: int
    valid_ack: bool
    sample: bytes
    error: str | None = None
    max_gap: float = 0.0
    adapter_lost: bool = False

    @property
    def ascii_sample(self) -> str:
        text = "".join(chr(b) if 32 <= b < 127 else "." for b in self.sample)
        return text


# The machine's application prints one timestamped line per second, so a
# silence clearly longer than that means the MCU actually rebooted.  Keep the
# threshold well above the heartbeat's ~1 s jitter (a real power cycle with the
# machine off for >=3 s gives a 3 s+ silence).
RESET_GAP = 1.5


def _port_present(port: str) -> bool:
    """Cross-platform check whether a serial port is still enumerated.

    ``os.path.exists("COM5")`` is always false on Windows, so ask pyserial
    instead and only fall back to the filesystem on POSIX.
    """
    try:
        from serial.tools import list_ports

        if any(info.device == port for info in list_ports.comports()):
            return True
    except Exception:  # pragma: no cover - pyserial unavailable
        pass
    return os.path.exists(port)


def _find_ack(buf: bytes, expected_checksum: int) -> bool:
    """True if a 64-byte ACK for our CONNECT packet is inside ``buf``."""
    prefix = struct.pack("<H", expected_checksum)
    for start in range(0, max(0, len(buf) - protocol.PACKET_SIZE) + 1):
        if buf[start : start + 2] == prefix:
            return True
    return False


def probe_port(
    port: str,
    bauds: tuple[int, ...] = DEFAULT_BAUDS,
    seconds_per_baud: float = 4.0,
    log: Callable[[str], None] | None = None,
    cancel: Callable[[], bool] | None = None,
) -> list[ProbeResult]:
    """Poll CMD_CONNECT at each baud rate and collect what comes back."""
    emit = log or (lambda message: None)
    packet = protocol.build_packet(protocol.CMD_CONNECT, 1)
    expected_checksum = protocol.checksum(packet)
    results: list[ProbeResult] = []

    for baud in bauds:
        if cancel is not None and cancel():
            break
        emit(f"{baud:>7} baud: power-cycle the machine NOW, then wait...")
        transport = SerialTransport(port, baud)
        try:
            transport.open()
        except TransportError as exc:
            results.append(ProbeResult(baud, 0, False, b"", str(exc)))
            emit(f"{baud:>7} baud: cannot open the port: {exc}")
            continue

        received = bytearray()
        valid = False
        max_gap = 0.0
        last_seen: float | None = None
        adapter_lost = False
        try:
            deadline = time.monotonic() + seconds_per_baud
            while time.monotonic() < deadline:
                if cancel is not None and cancel():
                    break
                transport.write(packet, purge_rx=len(received) == 0)
                chunk = transport.read_available(0.12)
                if not chunk:
                    continue
                now = time.monotonic()
                if last_seen is not None:
                    gap = now - last_seen
                    if gap > max_gap:
                        max_gap = gap
                    if gap > RESET_GAP:
                        emit(f"{baud:>7} baud: MCU rebooted - {gap:.1f}s of silence")
                last_seen = now
                received.extend(chunk)
                if _find_ack(received, expected_checksum):
                    valid = True
                    break
        except TransportError as exc:
            adapter_lost = not _port_present(port)
            results.append(
                ProbeResult(
                    baud,
                    len(received),
                    False,
                    bytes(received[:128]),
                    str(exc),
                    max_gap,
                    adapter_lost,
                )
            )
            emit(f"{baud:>7} baud: {exc}")
            if adapter_lost:
                emit(f"{baud:>7} baud: the USB serial adapter is gone - stopping the probe")
                break
            continue
        finally:
            transport.close()

        result = ProbeResult(
            baud, len(received), valid, bytes(received[:128]), None, max_gap, adapter_lost
        )
        results.append(result)
        if valid:
            emit(
                f"{baud:>7} baud: ISP ACK received ({len(received)} bytes) - "
                "this is the right baud rate"
            )
        elif received:
            extra = f", longest silence {max_gap:.1f}s" if max_gap else ""
            emit(
                f"{baud:>7} baud: {len(received)} byte(s) but no ISP ACK{extra} "
                f"-> {result.ascii_sample!r}"
            )
        else:
            emit(f"{baud:>7} baud: no data")
    return results


def summarise(results: list[ProbeResult]) -> str:
    """Turn probe results into an actionable message."""
    if not results:
        return "probe did not run"

    lost = [r for r in results if r.adapter_lost]
    if lost:
        return (
            "The USB serial adapter disappeared from the system during the probe\n"
            f"({lost[0].error}).\n"
            "Re-plug it, check that the device node is back (e.g. /dev/ttyUSB0 via\n"
            "--list-ports), then run the probe again without unplugging anything.\n"
            "A CH340 dropping off the bus usually means a marginal USB cable/port or\n"
            "power on the adapter - try another USB port."
        )

    working = [r for r in results if r.valid_ack]
    if working:
        bauds = ", ".join(str(r.baud) for r in working)
        return (
            f"The ISP bootloader answered at {bauds} baud.\n"
            f"Use: --baud {working[0].baud}"
        )

    talking = [r for r in results if r.bytes_received]
    if talking:
        best = max(talking, key=lambda r: r.bytes_received)
        if best.max_gap > RESET_GAP:
            return (
                f"The MCU did reboot during the run (longest silence {best.max_gap:.1f}s) but\n"
                "the ISP bootloader never answered CMD_CONNECT at any baud rate.\n"
                "=> The MCU boots straight into its application, i.e. a plain power cycle\n"
                "   does not open the LDROM ISP window on this unit.\n"
                f"First bytes seen: {best.ascii_sample!r}"
            )
        return (
            "Bytes are arriving, but the MCU was never reset during the pass\n"
            f"(longest silence {best.max_gap:.1f}s, i.e. just the 1 s heartbeat).\n"
            "=> Your power cycle did not reach the MCU while the probe was running.\n"
            "   Switch the machine OFF at its mains switch for at least 3 seconds *while\n"
            "   this command runs*, then look for a 'MCU rebooted' line followed by an\n"
            "   'ISP ACK received' line."
        )
    return (
        "Nothing at all was received on any baud rate.\n"
        "That points at the physical link or the machine's state:\n"
        "  * is the machine powered on?\n"
        "  * is the cable in the machine's service/flash connector (not the display)?\n"
        "  * TX/RX swapped or a 5 V/3.3 V level problem?\n"
        "  * did you power-cycle the machine while the probe was running?\n"
        "Try '--probe-seconds 8' and power-cycle two or three times during the run."
    )


@dataclass
class WatchResult:
    """What a passive listen observed."""

    seconds: float
    bytes_received: int = 0
    chunks: int = 0
    gaps: list[float] = field(default_factory=list)
    binary_chunks: int = 0
    sample: bytes = b""

    @property
    def resets(self) -> int:
        return len(self.gaps)

    @property
    def max_gap(self) -> float:
        return max(self.gaps) if self.gaps else 0.0

    def report(self) -> str:
        lines = [
            f"listened for {self.seconds:.1f}s: {self.bytes_received} bytes in "
            f"{self.chunks} bursts"
        ]
        if self.gaps:
            gaps = ", ".join(f"{g:.1f}s" for g in self.gaps)
            lines.append(f"MCU resets detected: {len(self.gaps)} (silences: {gaps})")
            lines.append(
                "=> A power cycle does reach the MCU, so the LDROM ISP window does open."
            )
        else:
            lines.append("MCU resets detected: 0")
            lines.append(
                "=> The MCU never reset while this command was running. Either the power\n"
                "   cycle happened before it started, or the supply you switch off does not\n"
                "   feed the MCU. Switch the machine off for >=10s *during* the run (or pull\n"
                "   its mains plug) and watch for a 'MCU rebooted' line."
            )
        if self.binary_chunks:
            lines.append(
                f"{self.binary_chunks} burst(s) looked like binary protocol data, not text - "
                "worth capturing (see sample below)."
            )
        return "\n".join(lines)


def watch_reset(
    port: str,
    seconds: float = 30.0,
    baud: int = 115200,
    log: Callable[[str], None] | None = None,
    cancel: Callable[[], bool] | None = None,
) -> WatchResult:
    """Listen without transmitting and report resets of the machine's MCU.

    The application prints one timestamped line per second, so a silence
    longer than :data:`RESET_GAP` means the MCU rebooted.  Because nothing
    is transmitted, the ISP bootloader will time out and boot the
    application - this run answers "did my power cycle reach the MCU?",
    which ``--probe`` cannot distinguish.
    """
    emit = log or (lambda message: None)
    transport = SerialTransport(port, baud)
    transport.open()
    result = WatchResult(seconds=seconds)
    start = time.monotonic()
    last_seen: float | None = None
    try:
        while True:
            elapsed = time.monotonic() - start
            if elapsed >= seconds:
                break
            if cancel is not None and cancel():
                break
            chunk = transport.read_available(0.1)
            if not chunk:
                continue
            now = time.monotonic()
            if last_seen is not None:
                gap = now - last_seen
                if gap > RESET_GAP:
                    result.gaps.append(gap)
                    emit(f"  +{now - start:5.1f}s  MCU rebooted ({gap:.1f}s of silence)")
            last_seen = now
            result.bytes_received += len(chunk)
            result.chunks += 1
            if not result.sample:
                result.sample = chunk[:64]
            printable = sum(1 for b in chunk if 32 <= b < 127 or b in (9, 10, 13))
            if printable < len(chunk) * 0.7:
                result.binary_chunks += 1
            preview = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk[:48])
            emit(f"  +{now - start:5.1f}s  {len(chunk):3d}B  {preview}")
    finally:
        transport.close()
    return result
