# Changelog

All notable changes to Meraki Flasher are documented here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.0.2] - 2026-10-30

### Fixed

- **Hang on macOS, and with a wedged adapter.** `transport.write()` ended with
  pyserial's `flush()`, which calls `termios.tcdrain()` — a call with no
  timeout that blocks forever once the peer stops draining the line. It is
  gone: `write()` is already bounded by `write_timeout`, and the protocol is
  lock-step, so nothing is lost. This also removes a freeze if the USB adapter
  is unplugged in the middle of a transfer.
- **Test harness could deadlock the pty.** `test_connect_timeout_without_device`
  stopped the emulator thread, which therefore stopped *reading* the pty; with
  no reader the buffer filled and `tcdrain` blocked, hanging the macOS CI job
  for over fifteen minutes. The emulator now has `mute()` — it keeps draining
  the link but answers nothing — and `packets_received` counts packets that
  arrive rather than packets that were answered.
- CI: the test step carries `timeout-minutes: 10`, so a future hang fails fast
  instead of tying up a runner.

## [1.0.1] - 2026-10-30

### Fixed

- **Python 3.13 compatibility of the emulator.** The private method `_handle`
  collided with `threading.Thread._handle` (introduced in 3.13) and `self._stop`
  shadowed `Thread._stop`, so every pty-based test — and therefore the GUI demo
  mode — failed with `TypeError: '_thread._ThreadHandle' object is not callable`.
  Renamed to `_process_packet` and `_stop_event`, with a regression test that
  keeps our attributes out of `threading.Thread`'s namespace.
- **CI matrix.** `ubuntu-latest` now points at Ubuntu 26.04, which has no Python
  3.9 build, and the macOS runners are arm64 (also without 3.9). The matrix now
  pins `ubuntu-24.04` and spells the cells out: Linux 3.9/3.13, macOS 3.13,
  Windows 3.9/3.13.

## [1.0.0] - 2026-10-30

First public release.

### Added

- Native implementation of the Nuvoton NuMicro ISP (UART) protocol, so Meraki
  coffee machine firmware can be flashed from Linux, macOS or Windows without
  Wine or Nuvoton tooling.
- `CMD_CONNECT` auto-detection loop with the vendor's packet numbering (initial
  index 18, +2 per packet), 40 ms connect window and `ReOpen_Port()` behaviour.
- APROM programming in 48/56-byte chunks with `CMD_RESEND_PACKET` recovery
  (10 retries per chunk), matching `ISPProc::Thread_ProgramFlash`.
- Tkinter GUI mirroring the vendor tool: port picker, APROM file selection with
  size/checksum, read-only CONFIG display, progress bar and packet log.
  Only `APROM` and `Reset and Run` are active; `Data Flash`, `Config` and
  `Erase All` are deliberately disabled.
- Headless CLI (`meraki-flasher-cli`) with `--dry-run`, `--no-reset`,
  `--hexdump`, and per-OS port discovery.
- Diagnostics that need no firmware file: `--list-ports`, `--listen`,
  `--watch-reset`, `--probe` (multi-baud) and `--loopback`.
- Protocol-accurate MCU emulator used by the test suite and by the GUI's demo
  mode, plus 33 tests covering framing, wire parity, retry paths, oversize
  guards and the reset watchdog.
- GitHub Actions CI: tests on Ubuntu/macOS/Windows with Python 3.9, 3.12 and
  3.13, plus a ruff lint job.

### Notes

- Verified on hardware: M451VE6AE (APROM 96 KiB, Data Flash 32 KiB, ISP
  firmware `0x33`) flashed with `MEM03_V514_260923.bin`.
- Meraki's firmware images and vendor binaries are **not** distributed with this
  project; the test suite falls back to a synthetic image of identical size.

[1.0.2]: https://github.com/progtologist/meraki-flasher/releases/tag/v1.0.2
[1.0.1]: https://github.com/progtologist/meraki-flasher/releases/tag/v1.0.1
[1.0.0]: https://github.com/progtologist/meraki-flasher/releases/tag/v1.0.0
