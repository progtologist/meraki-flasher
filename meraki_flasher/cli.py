"""Headless command line interface.

    meraki-flasher-cli --list-ports
    meraki-flasher-cli --file MEM03_V514_260923.bin --port /dev/ttyUSB0
"""

from __future__ import annotations

import argparse
import sys
import time

from . import APP_NAME, __version__, about, chips, diag, firmware, protocol
from .isp import Cancelled
from .session import DEFAULT_BAUD, DEFAULT_CONNECT_TIMEOUT, FlashSession, SessionError
from .transport import SerialTransport, list_serial_ports


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="meraki-flasher-cli",
        description=f"{APP_NAME} - flash Meraki coffee machine MCU firmware over UART.",
        epilog=(
            "Typical use: start the command, then power-cycle the machine. "
            "Progress is printed while the APROM is programmed."
        ),
    )
    p.add_argument("-f", "--file", help="firmware .bin to program (APROM)")
    p.add_argument("-p", "--port", help="serial port, e.g. /dev/ttyUSB0 or COM3")
    p.add_argument("--baud", type=int, default=DEFAULT_BAUD, help="baud rate (default 115200)")
    p.add_argument(
        "--connect-timeout",
        type=float,
        default=DEFAULT_CONNECT_TIMEOUT,
        help="seconds to wait for the device (default 60)",
    )
    p.add_argument("--no-reset", action="store_true", help="do not run the new firmware")
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="validate the file and show the packet plan without touching the port",
    )
    p.add_argument("--list-ports", action="store_true", help="list serial ports and exit")
    p.add_argument(
        "--loopback",
        type=float,
        metavar="SECONDS",
        help="test the adapter itself: send a pattern and wait for it (jumper TX to RX)",
    )
    p.add_argument(
        "--watch-reset",
        type=float,
        metavar="SECONDS",
        help="listen only and report when the MCU actually reboots (power-cycle test)",
    )
    p.add_argument(
        "--listen",
        type=float,
        metavar="SECONDS",
        help="only listen: dump everything the machine sends (nothing is transmitted)",
    )
    p.add_argument(
        "--probe",
        action="store_true",
        help="diagnose the link: poll CMD_CONNECT at several baud rates and report any reply",
    )
    p.add_argument(
        "--probe-seconds",
        type=float,
        default=4.0,
        help="seconds spent per baud rate while probing (default 4)",
    )
    p.add_argument(
        "--bauds",
        default="115200,57600,38400,19200,9600",
        help="comma separated baud rates to try while probing",
    )
    p.add_argument("--all-ports", action="store_true", help="include non-USB serial ports")
    p.add_argument("--hexdump", action="store_true", help="trace every 64-byte packet")
    p.add_argument("--verbose", action="store_true", help="log every ISP command")
    p.add_argument("--about", action="store_true", help="show version and compatibility")
    p.add_argument("--version", action="version", version=f"{APP_NAME} {__version__}")
    return p


def _cmd_list_ports(include_all: bool) -> int:
    ports = list_serial_ports(include_all=include_all)
    if not ports:
        print("no USB serial adapters found (use --all-ports to list everything)")
        return 1
    for port in ports:
        kind = "USB" if port.is_usb else "other"
        print(f"{port.device:<24} {kind:<5} {port.description}  [{port.hwid}]")
    return 0


def _pick_port(explicit: str | None, include_all: bool) -> str:
    if explicit:
        return explicit
    ports = list_serial_ports(include_all=include_all)
    if len(ports) == 1:
        return ports[0].device
    if not ports:
        raise SessionError("no USB serial adapter found - pass --port explicitly")
    names = ", ".join(p.device for p in ports)
    raise SessionError(f"several serial ports found ({names}); pick one with --port")


def _cmd_loopback(args: argparse.Namespace) -> int:
    """Prove that the adapter can transmit: unplug it and jumper TX to RX."""
    try:
        port = _pick_port(args.port, args.all_ports)
    except SessionError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    transport = SerialTransport(port, args.baud)
    try:
        transport.open()
    except Exception as exc:  # noqa: BLE001 - reported to the user
        print(f"error: {exc}", file=sys.stderr)
        return 2

    pattern = b"\r\nMERAKI-FLASHER-LOOPBACK\r\n"
    seconds = float(args.loopback)
    sent = 0
    received = bytearray()
    print(f"loopback test on {port} at {args.baud} for {seconds:g}s")
    print("Unplug the cable from the machine and connect the adapter's TX and RX")
    print("to each other. Every byte sent should come straight back.\n")
    try:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            transport.write(pattern, purge_rx=False)
            sent += len(pattern)
            received.extend(transport.read_available(0.1))
    finally:
        transport.close()

    matches = received.count(b"MERAKI-FLASHER-LOOPBACK")
    print(f"sent {sent} bytes, received {len(received)} bytes, {matches} full echo(es)")
    if matches == 0 and received:
        print(f"first bytes back: {bytes(received[:48]).hex(' ')}")
    if matches:
        print(
            "\nPASS - the adapter transmits and receives. If the flasher still cannot\n"
            "reach the MCU, the problem is on the machine side: the adapter's TX line\n"
            "is not arriving at the MCU's RX (wrong pin, broken lead, or a connector\n"
            "that only exposes MCU TX/GND)."
        )
        return 0
    print(
        "\nFAIL - nothing came back.\n"
        "  * are TX and RX really jumpered together at the machine end?\n"
        "  * is another program holding the port?\n"
        "  * try '--baud 9600' to rule out a bad crystal on the adapter."
    )
    return 1


def _cmd_listen(args: argparse.Namespace) -> int:
    try:
        port = _pick_port(args.port, args.all_ports)
    except SessionError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    transport = SerialTransport(port, args.baud)
    try:
        transport.open()
    except Exception as exc:  # noqa: BLE001 - reported to the user
        print(f"error: {exc}", file=sys.stderr)
        return 2
    seconds = float(args.listen)
    print(f"listening on {port} at {args.baud} for {seconds:g}s - nothing is transmitted")
    print("Nothing arriving means the MCU is silent; data arriving means it is running")
    print("its application firmware and printing on the same UART.\n")
    total = 0
    try:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            data = transport.read_available(0.2)
            if not data:
                continue
            total += len(data)
            print(f"  {len(data):3d}B  {data.hex(' ')}")
            print(f"        {' '.join(chr(b) if 32 <= b < 127 else '.' for b in data)}")
    finally:
        transport.close()
    print(f"\n{total} bytes received in {seconds:g}s")
    return 0 if total else 1


def _cmd_watch_reset(args: argparse.Namespace) -> int:
    """Passive test: does a power cycle actually reset the MCU?"""
    try:
        port = _pick_port(args.port, args.all_ports)
    except SessionError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    seconds = float(args.watch_reset)
    print(f"{APP_NAME} {__version__} - reset watchdog on {port} at {args.baud}")
    print("Nothing is transmitted. The machine's application prints one timestamped")
    print(f"line per second, so a silence longer than {diag.RESET_GAP:g}s means it rebooted.")
    print(f"\n>>> POWER-CYCLE THE MACHINE NOW (switch it off for >=3s), listening for {seconds:g}s\n")
    try:
        result = diag.watch_reset(port, seconds=seconds, baud=args.baud, log=print)
    except Exception as exc:  # noqa: BLE001 - reported to the user
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print()
    print(result.report())
    if result.sample and result.binary_chunks:
        print(f"first bytes: {result.sample.hex(' ')}")
    return 0 if result.resets else 1


def _cmd_probe(args: argparse.Namespace) -> int:
    try:
        port = _pick_port(args.port, args.all_ports)
    except SessionError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    try:
        bauds = tuple(int(item) for item in str(args.bauds).split(",") if item.strip())
    except ValueError:
        print("error: --bauds expects a comma separated list of integers", file=sys.stderr)
        return 2

    print(f"{APP_NAME} {__version__} - link probe on {port}")
    print(f"{', '.join(str(b) for b in bauds)} baud, {args.probe_seconds:g}s each.")
    print("CMD_CONNECT is sent continuously; power-cycle the machine during each pass.\n")
    results = diag.probe_port(
        port,
        bauds=bauds,
        seconds_per_baud=args.probe_seconds,
        log=lambda message: print(f"\r{message}" + " " * 20),
    )
    print()
    print(diag.summarise(results))
    return 0 if any(r.valid_ack for r in results) else 1


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)

    if args.about:
        print(about.about_text(args.port))
        print(about.ABOUT_LICENSE_NOTE)
        return 0

    if args.list_ports:
        return _cmd_list_ports(args.all_ports)

    if args.probe:
        return _cmd_probe(args)

    if args.listen is not None:
        return _cmd_listen(args)

    if args.watch_reset is not None:
        return _cmd_watch_reset(args)

    if args.loopback is not None:
        return _cmd_loopback(args)

    if not args.file:
        print("error: --file is required (or use --list-ports / --about)", file=sys.stderr)
        return 2

    try:
        image = firmware.load(args.file)
    except firmware.FirmwareError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    print(f"{APP_NAME} {__version__}")
    print(f"file: {image.path}")
    print(f"{image.describe()}")
    if not image.looks_like_cortex_m(chips.FALLBACK_APROM_SIZE):
        print("warning: no plausible Cortex-M vector table at the start of the file")

    if args.dry_run:
        packets = sum(1 for _ in protocol.aprom_chunks(image.data))
        print(f"dry run: {packets} ISP packets would be sent, port untouched")
        return 0

    last = [-1]
    last_wait = [-1]

    def on_progress(done: int, total: int) -> None:
        pct = done * 100 // total
        if pct != last[0]:
            last[0] = pct
            print(f"\r  programming APROM: {pct:3d}%  ({done}/{total} bytes)", end="", flush=True)

    def on_wait(remaining: float) -> None:
        seconds = int(remaining)
        if seconds != last_wait[0]:
            last_wait[0] = seconds
            print(
                f"\r  waiting for the device: {seconds:3d}s left - power-cycle it now...",
                end="",
                flush=True,
            )

    def log(message: str) -> None:
        print(f"\r{message}" + " " * 30)

    session = FlashSession(
        log=log, hexdump=args.hexdump, verbose=args.verbose or args.hexdump
    )
    try:
        port = _pick_port(args.port, args.all_ports)
        info = session.flash(
            port=port,
            path=args.file,
            reset_and_run=not args.no_reset,
            connect_timeout=args.connect_timeout,
            baudrate=args.baud,
            progress=on_progress,
            on_wait=on_wait,
        )
    except Cancelled:
        print("\naborted by user")
        return 130
    except SessionError as exc:
        print(f"\nerror: {exc}")
        print("If the update failed, power-cycle the machine and run the command again.")
        return 1

    print()
    print(f"done: {info.part} updated, {info.summary()}")
    if not args.no_reset:
        print("the machine has been reset and is running the new firmware")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
