"""Loading and sanity-checking firmware images."""

from __future__ import annotations

import struct
from dataclasses import dataclass
from pathlib import Path


class FirmwareError(Exception):
    """Raised when a firmware file cannot be used."""


@dataclass(frozen=True)
class FirmwareImage:
    """A firmware file loaded into memory."""

    path: Path
    data: bytes

    @property
    def size(self) -> int:
        return len(self.data)

    @property
    def checksum(self) -> int:
        """16-bit byte sum - the value NuvoISP shows next to the file size."""
        return sum(self.data) & 0xFFFF

    def describe(self) -> str:
        return f"size: {human_size(self.size)}, checksum: {self.checksum:04x}"

    def looks_like_cortex_m(self, flash_size: int) -> bool:
        """Cheap sanity check of the vector table.

        A Cortex-M image starts with the initial stack pointer followed by
        the reset vector, which must have the Thumb bit set and point into
        the flash region.
        """
        if self.size < 8:
            return False
        stack_pointer, reset_vector = struct.unpack_from("<II", self.data, 0)
        if stack_pointer == 0 or stack_pointer == 0xFFFFFFFF:
            return False
        if not reset_vector & 1:
            return False
        return (reset_vector & ~1) < flash_size


def human_size(size: int) -> str:
    """Format like NuvoISP: '62.0K Bytes' / '512 Bytes'."""
    if size >= 1024:
        return f"{size / 1024:.1f}K Bytes"
    return f"{size} Bytes"


def load(path: str | Path) -> FirmwareImage:
    """Read a firmware file from disk."""
    p = Path(path)
    try:
        data = p.read_bytes()
    except OSError as exc:
        raise FirmwareError(f"cannot read {p}: {exc}") from exc
    if not data:
        raise FirmwareError(f"{p} is empty")
    return FirmwareImage(path=p, data=data)


def check_fits(image: FirmwareImage, aprom_size: int) -> None:
    """Refuse images that would spill out of the APROM area."""
    if aprom_size <= 0:
        return
    if image.size > aprom_size:
        raise FirmwareError(
            f"image is {human_size(image.size)} but APROM is only "
            f"{human_size(aprom_size)} - refusing to overwrite Data Flash"
        )
