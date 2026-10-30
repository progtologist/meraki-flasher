"""Test helpers.

Two things live here:

* the pty harness that pairs a pseudo terminal with the ISP device emulator
  (POSIX only - on Windows the affected test classes are skipped), and
* firmware discovery.  Meraki's firmware image is **not** part of this
  repository, so the tests run against a synthetic image of the same size
  unless the real file is available:

      MERAKI_FIRMWARE=/path/to/MEM03_V514_260923.bin   # use the real file
      MERAKI_FIRMWARE=none                             # force the fixture

  Resolution order: ``$MERAKI_FIRMWARE`` -> next to this repo -> repo root.
"""

from __future__ import annotations

import os
import struct
import time
from pathlib import Path

from meraki_flasher import firmware
from meraki_flasher.emulator import MockIspDevice
from meraki_flasher.transport import SerialTransport

try:  # POSIX only
    import pty
except ImportError:  # pragma: no cover - Windows
    pty = None  # type: ignore[assignment]

POSIX_ONLY = pty is not None

PROJECT_ROOT = Path(__file__).resolve().parents[1]
FIRMWARE_NAME = "MEM03_V514_260923.bin"


def _find_firmware() -> Path | None:
    override = os.environ.get("MERAKI_FIRMWARE", "").strip()
    if override.lower() in {"none", "off", "synthetic", "0"}:
        return None
    candidates = []
    if override:
        candidates.append(Path(override).expanduser())
    candidates.append(PROJECT_ROOT / FIRMWARE_NAME)
    candidates.append(PROJECT_ROOT.parent / FIRMWARE_NAME)
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


FIRMWARE_PATH: Path | None = _find_firmware()
REAL_FIRMWARE_AVAILABLE = FIRMWARE_PATH is not None

# Same length as the real image, so the packet arithmetic (1134 packets) is
# exercised identically in CI.
FIXTURE_SIZE = 63460


def fixture_bytes() -> bytes:
    """Deterministic image that looks like a Cortex-M firmware."""
    data = bytearray((index * 37 + 11) & 0xFF for index in range(FIXTURE_SIZE))
    struct.pack_into("<II", data, 0, 0x200045E0, 0x0000B3B9)  # SP, Thumb reset vector
    return bytes(data)


def image_bytes() -> bytes:
    """The real firmware when available, otherwise the synthetic fixture."""
    if FIRMWARE_PATH is not None:
        return FIRMWARE_PATH.read_bytes()
    return fixture_bytes()


def load_image() -> firmware.FirmwareImage:
    """Load the firmware image used by the end-to-end tests."""
    if FIRMWARE_PATH is not None:
        return firmware.load(FIRMWARE_PATH)
    return firmware.FirmwareImage(path=Path(FIRMWARE_NAME), data=fixture_bytes())


class DeviceHarness:
    """Owns a pty pair and the emulated MCU on the master side."""

    def __init__(self, **device_kwargs: object) -> None:
        if pty is None:  # pragma: no cover - Windows
            raise RuntimeError("the pty harness requires POSIX")
        self.master_fd, self.slave_fd = pty.openpty()
        self.port = os.ttyname(self.slave_fd)
        self.device = MockIspDevice(self.master_fd, **device_kwargs)
        self._transports: list[SerialTransport] = []

    def __enter__(self) -> "DeviceHarness":
        self.device.start()
        time.sleep(0.05)
        return self

    def __exit__(self, *exc_info: object) -> None:
        for transport in self._transports:
            transport.close()
        self.device.stop()
        time.sleep(0.05)
        for fd in (self.master_fd, self.slave_fd):
            try:
                os.close(fd)
            except OSError:
                pass

    def open_transport(self) -> SerialTransport:
        transport = SerialTransport(self.port)
        transport.open()
        self._transports.append(transport)
        return transport
