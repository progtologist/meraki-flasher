"""High-level flashing session, independent of any UI.

A :class:`FlashSession` owns the serial port and the ISP driver and walks
through the same steps a technician performs with NuvoISP:

1. open the port                     (STEP 4: interface + COM port)
2. poll CMD_CONNECT                  (STEP 8: Connect, then power cycle)
3. read version / part / CONFIG      (informational, like the vendor UI)
4. program the APROM image           (STEP 10: Start)
5. CMD_RUN_APROM                     (STEP 7: Reset and Run)

Everything is reported through plain callables so that the Tkinter GUI,
the CLI and the tests can all reuse it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from . import chips, firmware
from .isp import CancelFn, Cancelled, ConnectTimeout, IspError, NuMicroIsp
from .transport import ReadTimeout, SerialTransport, TransportError

LogFn = Callable[[str], None]
ProgressFn = Callable[[int, int], None]

DEFAULT_BAUD = 115200
DEFAULT_CONNECT_TIMEOUT = 60.0


class SessionError(Exception):
    """Raised for problems that are not plain ISP protocol failures."""


@dataclass
class DeviceInfo:
    """What the device reported after connecting."""

    port: str
    device_id: int
    isp_version: int
    config: list[int] = field(default_factory=list)
    chip: chips.ChipInfo | None = None

    @property
    def part(self) -> str:
        if self.chip is not None:
            return self.chip.part
        return f"0x{self.device_id:08X}"

    @property
    def aprom_size(self) -> int:
        if self.chip is not None:
            return self.chip.aprom_size
        return chips.FALLBACK_APROM_SIZE

    def summary(self) -> str:
        data_flash = self.chip.data_flash if self.chip else chips.FALLBACK_APROM_SIZE
        return (
            f"APROM: {chips.human_kb(self.aprom_size)}, "
            f"Data: {chips.human_kb(data_flash)}, "
            f"FW Ver: 0x{self.isp_version:02x}"
        )


class FlashSession:
    """Owns the port and runs one update."""

    def __init__(self, log: LogFn | None = None, hexdump: bool = False, verbose: bool = True) -> None:
        self._log_fn = log
        self.hexdump = hexdump
        self.verbose = verbose
        self.transport: SerialTransport | None = None
        self.isp: NuMicroIsp | None = None
        self.info: DeviceInfo | None = None
        self.port = ""

    # -- plumbing -----------------------------------------------------
    def _log(self, message: str) -> None:
        if self._log_fn is not None:
            self._log_fn(message)

    def open(self, port: str, baudrate: int = DEFAULT_BAUD) -> None:
        self.close()
        transport = SerialTransport(port, baudrate)
        try:
            transport.open()
        except TransportError as exc:
            raise SessionError(str(exc)) from exc
        self.transport = transport
        self.port = port
        self.isp = NuMicroIsp(
            transport, log=self._log, hexdump=self.hexdump, verbose=self.verbose
        )
        self._log(f"opened {port} at {baudrate} 8N1")

    def close(self) -> None:
        if self.transport is not None:
            self.transport.close()
        self.transport = None
        self.isp = None
        self.info = None

    @property
    def is_open(self) -> bool:
        return self.transport is not None and self.transport.is_open

    # -- steps --------------------------------------------------------
    def connect(
        self,
        timeout: float = DEFAULT_CONNECT_TIMEOUT,
        cancel: CancelFn | None = None,
        on_wait: Callable[[float], None] | None = None,
    ) -> DeviceInfo:
        if self.isp is None:
            raise SessionError("port is not open")
        self._log(f"waiting for the device (up to {timeout:.0f}s) - power cycle it now")
        try:
            self.isp.connect(timeout=timeout, cancel=cancel, on_wait=on_wait)
        except (ConnectTimeout, IspError) as exc:
            raise SessionError(str(exc)) from exc
        except Cancelled:
            raise
        except TransportError as exc:
            raise SessionError(
                f"lost the serial adapter while waiting: {exc}. "
                "Re-plug the USB cable and try again."
            ) from exc

        try:
            # ISPProc::Thread_CheckDeviceConnect(): after the connect
            # handshake the reference tool always resets the packet counter
            # before reading version / device ID.
            self.isp.sync_packno()
            version = self.isp.get_version()
        except (ReadTimeout, IspError) as exc:
            raise SessionError(f"cannot read the ISP version: {exc}") from exc
        try:
            device_id = self.isp.get_device_id()
        except (ReadTimeout, IspError) as exc:
            raise SessionError(f"cannot read the device ID: {exc}") from exc
        try:
            config = self.isp.read_config()
        except (ReadTimeout, IspError) as exc:
            self._log(f"warning: cannot read CONFIG: {exc}")
            config = []

        chip = chips.lookup(device_id)
        info = DeviceInfo(
            port=self.port,
            device_id=device_id,
            isp_version=version,
            config=config,
            chip=chip,
        )
        self.info = info
        if chip is None:
            self._log(
                f"warning: unknown device ID 0x{device_id:08X}; "
                f"assuming {chips.human_kb(chips.FALLBACK_APROM_SIZE)} of APROM"
            )
        else:
            self._log(f"detected {chip.part} (PID 0x{device_id:08X})")
        self._log(info.summary())
        if config:
            self._log(
                f"CONFIG0=0x{config[0]:08X} CONFIG1=0x{config[1]:08X} "
                "(read only, never written)"
            )
        return info

    def program(
        self,
        image: firmware.FirmwareImage,
        progress: ProgressFn | None = None,
        cancel: CancelFn | None = None,
    ) -> None:
        if self.isp is None:
            raise SessionError("port is not open")
        if self.info is None:
            raise SessionError("not connected")
        try:
            firmware.check_fits(image, self.info.aprom_size)
        except firmware.FirmwareError as exc:
            raise SessionError(str(exc)) from exc

        if not image.looks_like_cortex_m(self.info.aprom_size):
            self._log(
                "warning: the file does not start with a plausible Cortex-M "
                "vector table - check that you selected the right firmware"
            )
        self._log(f"programming {image.path.name}: {image.describe()}")
        try:
            self.isp.sync_packno()
            ack = self.isp.update_aprom(image.data, progress=progress, cancel=cancel)
        except Cancelled:
            raise
        except IspError as exc:
            raise SessionError(str(exc)) from exc
        except TransportError as exc:
            raise SessionError(
                f"lost the serial adapter while programming: {exc}. "
                "Power-cycle the machine and try again."
            ) from exc

        # The device returns its own running data checksum in the final ACK.
        # Its algorithm is not necessarily the file byte-sum shown by NuvoISP
        # (the real M451 reports a different value), so it is informational.
        device_sum = ack.u16(0)
        if device_sum == image.checksum:
            self._log(f"device data checksum 0x{device_sum:04x} matches the file checksum")
        else:
            self._log(
                f"device reported data checksum 0x{device_sum:04x} "
                f"(device algorithm differs from the file checksum {image.checksum:04x})"
            )
        self._log("programming complete")

    def run(self) -> None:
        if self.isp is None:
            raise SessionError("port is not open")
        self._log("CMD_RUN_APROM: resetting the MCU into the new firmware")
        self.isp.run_aprom()

    def flash(
        self,
        port: str,
        path: str | Path,
        reset_and_run: bool = True,
        connect_timeout: float = DEFAULT_CONNECT_TIMEOUT,
        baudrate: int = DEFAULT_BAUD,
        progress: ProgressFn | None = None,
        cancel: CancelFn | None = None,
        on_wait: Callable[[float], None] | None = None,
    ) -> DeviceInfo:
        """Open, connect, program and (optionally) run in one call."""
        image = firmware.load(path)
        self._log(f"loaded {image.path}: {image.describe()}")
        self.open(port, baudrate)
        try:
            info = self.connect(connect_timeout, cancel=cancel, on_wait=on_wait)
            self.program(image, progress=progress, cancel=cancel)
            if reset_and_run:
                self.run()
        finally:
            self.close()
        return info

