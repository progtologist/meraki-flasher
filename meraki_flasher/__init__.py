"""Meraki Flasher - MCU firmware updater for Meraki coffee machines.

Implements the Nuvoton NuMicro ISP (UART) programming protocol used by the
``NuvoISP.exe`` that ships in Meraki's update packages, so the same update
can be performed from Linux, macOS or Windows without Wine.
"""

__version__ = "1.0.0"
APP_NAME = "Meraki Flasher"
APP_TAGLINE = "MCU firmware updater for Meraki coffee machines"

__all__ = ["__version__", "APP_NAME", "APP_TAGLINE"]
