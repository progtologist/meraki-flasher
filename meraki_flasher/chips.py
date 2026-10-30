"""Small chip database used to identify the target and size the APROM image.

The numbers mirror OpenNuvoton/ISPTool ``NuvoISP/DataBase/FlashInfo.cpp``.
For the Meraki coffee machine the reported part is **M451VE6AE**: 128 KiB
of program flash with a 32 KiB Data Flash, i.e. **96 KiB of APROM** - which
is exactly what NuvoISP shows in its "APROM: 96K, Data: 32K" field.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ChipInfo:
    """Flash geometry of one part number."""

    pid: int
    part: str
    program_flash: int
    data_flash: int
    ldrom_size: int = 4 * 1024

    @property
    def aprom_size(self) -> int:
        return self.program_flash - self.data_flash


# PID -> geometry.  The Data Flash size of the M451/M452 families is
# configurable; the default configuration carried by the Meraki board is
# used here (32 KiB for the 6AE parts, 16 KiB for the 3AE parts).
KNOWN_CHIPS: tuple[ChipInfo, ...] = (
    ChipInfo(0x00845131, "M451VE6AE", 128 * 1024, 32 * 1024),
    ChipInfo(0x00845121, "M451RE6AE", 128 * 1024, 32 * 1024),
    ChipInfo(0x00845101, "M451LE6AE", 128 * 1024, 32 * 1024),
    ChipInfo(0x00845120, "M451RG6AE", 256 * 1024, 32 * 1024),
    ChipInfo(0x00845100, "M451LG6AE", 256 * 1024, 32 * 1024),
    ChipInfo(0x00845231, "M452VE6AE", 128 * 1024, 32 * 1024),
    ChipInfo(0x00845221, "M452RE6AE", 128 * 1024, 32 * 1024),
    ChipInfo(0x00845230, "M452VG6AE", 256 * 1024, 32 * 1024),
    ChipInfo(0x00945120, "M451RD3AE", 72 * 1024, 16 * 1024),
    ChipInfo(0x00945100, "M451LD3AE", 72 * 1024, 16 * 1024),
)

CHIPS_BY_PID: dict[int, ChipInfo] = {chip.pid: chip for chip in KNOWN_CHIPS}

# Part number confirmed by the vendor's own update video for this machine.
TARGET_PID = 0x00845131
TARGET_PART = "M451VE6AE"

# Fallback APROM size for the Meraki machine when the part is not recognised:
# the video shows 96 KiB, and refusing to write past it protects the Data
# Flash area that holds the machine's calibration values.
FALLBACK_APROM_SIZE = 96 * 1024


def lookup(pid: int) -> ChipInfo | None:
    """Return the geometry for a device ID, or ``None`` if unknown."""
    return CHIPS_BY_PID.get(pid)


def human_kb(size: int) -> str:
    """Format a byte count the way NuvoISP does ('96K', '128K')."""
    if size % 1024 == 0:
        return f"{size // 1024}K"
    return f"{size / 1024:.1f}K"
