"""About-dialog content, shared by the GUI and ``meraki-flasher-cli --about``."""

from __future__ import annotations

import platform
import sys

from . import APP_NAME, APP_TAGLINE, __version__


def runtime_line(port: str | None = None) -> str:
    """One line describing where this is running, for support requests."""
    parts = [
        platform.system() or sys.platform,
        f"Python {platform.python_version()}",
    ]
    try:
        import serial

        parts.append(f"pyserial {serial.__version__}")
    except Exception:  # pragma: no cover - pyserial is a hard requirement
        parts.append("pyserial unavailable")
    if port:
        parts.append(f"port {port}")
    # ASCII only: this text is also printed to consoles with a legacy code page.
    return " | ".join(parts)


def about_text(port: str | None = None) -> str:
    return f"""{APP_NAME} {__version__}
{APP_TAGLINE}

Implements the Nuvoton NuMicro ISP (UART) programming protocol and is a
drop-in replacement for the update procedure described in Meraki's
"Firmware Update Instruction":

  * works with Meraki update packages whose NuvoISP.exe is the Nuvoton
    NuMicro ISP Programming Tool (v4.13 in the 5.1.4 package)
  * verified target: M451VE6AE (APROM 96 KiB, Data Flash 32 KiB,
    ISP firmware 0x33), e.g. MEM03_V514_260923.bin

The protocol implementation is a Python port of OpenNuvoton/ISPTool
(NuvoISP/ISPLdCMD.cpp, Apache-2.0, Copyright 2025 Nuvoton Technology
Corporation). See NOTICE for details.

Only the APROM area is programmed: CONFIG bits, the LDROM bootloader and
the Data Flash area that holds the machine's settings are never touched.

Runtime: {runtime_line(port)}

Unofficial tool - not affiliated with or endorsed by Merakitech or
Nuvoton. Flashing is at your own risk.
"""


ABOUT_LICENSE_NOTE = (
    "Apache License 2.0 - see LICENSE and NOTICE in the installation folder."
)
