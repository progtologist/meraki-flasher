"""Tkinter GUI - a drop-in replacement for the NuvoISP workflow.

Layout mirrors the vendor tool: connection interface + port, status/part
number, APROM file selection, CONFIG read-out, the programming checkbox
row and a Start button.  Only APROM programming and "Reset and Run" are
active; Data Flash, Config and Erase All are shown but disabled because
they are not part of the vendor update procedure and carry real risk.
"""

from __future__ import annotations

import os
import queue
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from typing import Any

try:  # POSIX only - used by the demo-mode emulator
    import pty
except ImportError:  # pragma: no cover - Windows
    pty = None  # type: ignore[assignment]

from . import APP_NAME, APP_TAGLINE, __version__, about, chips, firmware
from .emulator import MockIspDevice
from .isp import Cancelled
from .session import DEFAULT_CONNECT_TIMEOUT, FlashSession, SessionError
from .transport import list_serial_ports


def _timestamp() -> str:
    return time.strftime("%H:%M:%S")


class Tooltip:
    """Minimal hover help, used to explain the greyed-out checkboxes."""

    def __init__(self, widget: tk.Widget, text: str) -> None:
        self.widget = widget
        self.text = text
        self._tip: tk.Toplevel | None = None
        widget.bind("<Enter>", self._show)
        widget.bind("<Leave>", self._hide)

    def _show(self, _event: object = None) -> None:
        if self._tip is not None or not self.text:
            return
        x = self.widget.winfo_rootx() + 20
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + 4
        self._tip = tk.Toplevel(self.widget)
        self._tip.wm_overrideredirect(True)
        self._tip.wm_geometry(f"+{x}+{y}")
        tk.Label(
            self._tip,
            text=self.text,
            justify="left",
            background="#ffffe0",
            relief="solid",
            borderwidth=1,
            padx=6,
            pady=3,
            wraplength=380,
        ).pack()

    def _hide(self, _event: object = None) -> None:
        if self._tip is not None:
            self._tip.destroy()
            self._tip = None


class DemoDevice:
    """Emulated MCU on a pseudo terminal, so the GUI can be tried without hardware."""

    def __init__(self) -> None:
        if pty is None:  # pragma: no cover - Windows
            raise RuntimeError("demo mode needs a POSIX pseudo terminal (Linux/macOS)")
        self.master_fd, self.slave_fd = pty.openpty()
        self.port = os.ttyname(self.slave_fd)
        self.device = MockIspDevice(self.master_fd)
        self.device.start()

    def close(self) -> None:
        self.device.stop()
        for fd in (self.master_fd, self.slave_fd):
            try:
                os.close(fd)
            except OSError:
                pass


class MerakiFlasher(tk.Tk):
    """Main window."""

    def __init__(self) -> None:
        super().__init__()
        self.title(f"{APP_NAME} {__version__} - MCU Firmware Update")
        self.minsize(720, 640)

        self.session = FlashSession(log=self._log_from_worker)
        self.events: queue.Queue[tuple[str, Any]] = queue.Queue()
        self.worker: threading.Thread | None = None
        self.cancel_event = threading.Event()
        self.demo: DemoDevice | None = None
        self.image: firmware.FirmwareImage | None = None
        self.info = None

        self.port_var = tk.StringVar()
        self.show_all_var = tk.BooleanVar(value=False)
        self.demo_var = tk.BooleanVar(value=False)
        self.status_var = tk.StringVar(value="Disconnected")
        self.part_var = tk.StringVar()
        self.info_var = tk.StringVar()
        self.file_var = tk.StringVar()
        self.file_meta_var = tk.StringVar(value="File not loaded.")
        self.config0_var = tk.StringVar()
        self.config1_var = tk.StringVar()
        self.progress_var = tk.DoubleVar(value=0.0)
        self.percent_var = tk.StringVar(value="")
        self.aprom_var = tk.BooleanVar(value=True)
        self.dataflash_var = tk.BooleanVar(value=False)
        self.config_var = tk.BooleanVar(value=False)
        self.reset_var = tk.BooleanVar(value=True)
        self.erase_var = tk.BooleanVar(value=False)

        self._build_ui()
        self.aprom_var.trace_add("write", lambda *_: self._update_buttons())
        self._refresh_ports()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(60, self._drain_events)

    # -- ui construction ----------------------------------------------
    def _build_ui(self) -> None:
        menubar = tk.Menu(self)
        file_menu = tk.Menu(menubar, tearoff=False)
        file_menu.add_command(label="Open firmware...", command=self._browse_file)
        file_menu.add_command(label="Save log...", command=self._save_log)
        file_menu.add_separator()
        file_menu.add_command(label="Quit", command=self._on_close)
        menubar.add_cascade(label="File", menu=file_menu)

        device_menu = tk.Menu(menubar, tearoff=False)
        device_menu.add_command(label="Refresh ports", command=self._refresh_ports)
        device_menu.add_command(label="Disconnect", command=self._disconnect)
        menubar.add_cascade(label="Device", menu=device_menu)

        help_menu = tk.Menu(menubar, tearoff=False)
        help_menu.add_command(label=f"About {APP_NAME}", command=self._show_about)
        menubar.add_cascade(label="Help", menu=help_menu)
        self.config(menu=menubar)

        outer = ttk.Frame(self, padding=10)
        outer.pack(fill="both", expand=True)
        outer.columnconfigure(0, weight=1)

        header = ttk.Frame(outer)
        header.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        ttk.Label(header, text=APP_NAME, font=("TkDefaultFont", 16, "bold")).pack(side="left")
        ttk.Label(header, text=f"  {APP_TAGLINE}", foreground="#555555").pack(side="left", pady=(6, 0))

        # -- connection interface -------------------------------------
        conn = ttk.LabelFrame(outer, text="Connection Interface", padding=8)
        conn.grid(row=1, column=0, sticky="ew")
        conn.columnconfigure(1, weight=1)

        ttk.Label(conn, text="Interface").grid(row=0, column=0, sticky="w")
        interface = ttk.Combobox(conn, values=["UART"], state="disabled", width=10)
        interface.current(0)
        interface.grid(row=1, column=0, sticky="w", padx=(0, 8))

        ttk.Label(conn, text="Port").grid(row=0, column=1, sticky="w")
        self.port_combo = ttk.Combobox(conn, textvariable=self.port_var, state="readonly")
        self.port_combo.grid(row=1, column=1, sticky="ew")
        self.refresh_button = ttk.Button(conn, text="Refresh", width=9, command=self._refresh_ports)
        self.refresh_button.grid(row=1, column=2, padx=(6, 0))

        options = ttk.Frame(conn)
        options.grid(row=2, column=0, columnspan=3, sticky="w", pady=(6, 0))
        ttk.Checkbutton(
            options, text="Show all ports", variable=self.show_all_var, command=self._refresh_ports
        ).pack(side="left")
        self.demo_check = ttk.Checkbutton(
            options,
            text="Demo (emulated device)",
            variable=self.demo_var,
            command=self._toggle_demo,
            state="normal" if pty is not None else "disabled",
        )
        self.demo_check.pack(side="left", padx=(16, 0))
        if pty is None:  # pragma: no cover - Windows
            Tooltip(self.demo_check, "Demo mode requires Linux or macOS.")

        # -- status ----------------------------------------------------
        status = ttk.LabelFrame(outer, text="Status", padding=8)
        status.grid(row=2, column=0, sticky="ew", pady=(8, 0))
        status.columnconfigure(1, weight=1)

        self.connect_button = ttk.Button(status, text="Connect", width=12, command=self._toggle_connect)
        self.connect_button.grid(row=0, column=0, rowspan=2, sticky="w")
        self.status_label = ttk.Label(status, textvariable=self.status_var, foreground="#b00020")
        self.status_label.grid(row=0, column=1, sticky="w", padx=12)

        part_row = ttk.Frame(status)
        part_row.grid(row=1, column=1, sticky="ew", padx=12, pady=(6, 0))
        ttk.Label(part_row, text="Part No.").pack(side="left")
        self.part_entry = ttk.Entry(part_row, textvariable=self.part_var, width=16, state="readonly")
        self.part_entry.pack(side="left", padx=(6, 16))
        ttk.Label(part_row, textvariable=self.info_var).pack(side="left")

        # -- load file -------------------------------------------------
        files = ttk.LabelFrame(outer, text="Load File", padding=8)
        files.grid(row=3, column=0, sticky="ew", pady=(8, 0))
        files.columnconfigure(1, weight=1)

        self.browse_button = ttk.Button(files, text="APROM", width=10, command=self._browse_file)
        self.browse_button.grid(row=0, column=0, sticky="w")
        ttk.Label(files, text="File Name:").grid(row=0, column=1, sticky="w", padx=(10, 6))
        self.file_entry = ttk.Entry(files, textvariable=self.file_var, state="readonly")
        self.file_entry.grid(row=0, column=2, columnspan=2, sticky="ew")
        files.columnconfigure(2, weight=1)
        ttk.Label(files, textvariable=self.file_meta_var).grid(
            row=1, column=2, columnspan=2, sticky="w", pady=(4, 0)
        )

        ttk.Button(files, text="Data Flash", width=10, state="disabled").grid(
            row=2, column=0, sticky="w", pady=(6, 0)
        )
        ttk.Label(files, text="not used by this update", foreground="#777777").grid(
            row=2, column=2, columnspan=2, sticky="w", pady=(6, 0)
        )

        # -- config bits ------------------------------------------------
        cfg = ttk.LabelFrame(outer, text="Config Bits", padding=8)
        cfg.grid(row=4, column=0, sticky="ew", pady=(8, 0))
        self.read_config_button = ttk.Button(
            cfg, text="Read CONFIG", width=12, state="disabled", command=self._read_config
        )
        self.read_config_button.grid(row=0, column=0, sticky="w")
        ttk.Label(cfg, text="Config 0.1:").grid(row=0, column=1, padx=(10, 4))
        ttk.Entry(cfg, textvariable=self.config0_var, width=12, state="readonly").grid(row=0, column=2)
        ttk.Entry(cfg, textvariable=self.config1_var, width=12, state="readonly").grid(
            row=0, column=3, padx=(4, 0)
        )
        ttk.Label(cfg, text="read only - never written", foreground="#777777").grid(
            row=0, column=4, padx=(10, 0)
        )

        # -- programming ------------------------------------------------
        prog = ttk.LabelFrame(outer, text="Programming", padding=8)
        prog.grid(row=5, column=0, sticky="ew", pady=(8, 0))

        self.aprom_check = ttk.Checkbutton(prog, text="APROM", variable=self.aprom_var)
        self.aprom_check.grid(row=0, column=0, sticky="w")
        self.dataflash_check = ttk.Checkbutton(
            prog, text="Data Flash", variable=self.dataflash_var, state="disabled"
        )
        self.dataflash_check.grid(row=0, column=1, sticky="w", padx=(14, 0))
        Tooltip(
            self.dataflash_check,
            "Not used by Meraki's update procedure. Data Flash holds the\n"
            "machine's settings (voltage, grind factors, UID, serial number).",
        )
        self.config_check = ttk.Checkbutton(
            prog, text="Config", variable=self.config_var, state="disabled"
        )
        self.config_check.grid(row=0, column=2, sticky="w", padx=(14, 0))
        Tooltip(
            self.config_check,
            "Deliberately disabled: writing CONFIG bits can disable the ISP\n"
            "bootloader or the debug interface and is not part of the update.",
        )
        self.reset_check = ttk.Checkbutton(prog, text="Reset and Run", variable=self.reset_var)
        self.reset_check.grid(row=0, column=3, sticky="w", padx=(14, 0))
        self.erase_check = ttk.Checkbutton(
            prog, text="Erase All", variable=self.erase_var, state="disabled"
        )
        self.erase_check.grid(row=0, column=4, sticky="w", padx=(14, 0))
        Tooltip(
            self.erase_check,
            "Deliberately disabled: a chip erase also wipes CONFIG and the\n"
            "Data Flash settings and is never needed for a firmware update.",
        )

        self.start_button = ttk.Button(prog, text="Start", width=12, state="disabled", command=self._start)
        self.start_button.grid(row=0, column=5, sticky="e", padx=(14, 0))
        self.stop_button = ttk.Button(prog, text="Stop", width=8, state="disabled", command=self._stop)
        self.stop_button.grid(row=0, column=6, sticky="e", padx=(6, 0))
        prog.columnconfigure(5, weight=1)

        # -- progress + log ---------------------------------------------
        progress_row = ttk.Frame(outer)
        progress_row.grid(row=6, column=0, sticky="ew", pady=(8, 0))
        progress_row.columnconfigure(0, weight=1)
        self.progress = ttk.Progressbar(
            progress_row, variable=self.progress_var, maximum=100.0
        )
        self.progress.grid(row=0, column=0, sticky="ew")
        ttk.Label(progress_row, textvariable=self.percent_var, width=6, anchor="e").grid(
            row=0, column=1, padx=(8, 0)
        )

        log_frame = ttk.LabelFrame(outer, text="Log", padding=6)
        log_frame.grid(row=7, column=0, sticky="nsew", pady=(8, 0))
        outer.rowconfigure(7, weight=1)
        self.log_text = tk.Text(log_frame, height=12, wrap="none", state="disabled")
        scroll = ttk.Scrollbar(log_frame, orient="vertical", command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=scroll.set)
        self.log_text.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")

        footer = ttk.Frame(outer)
        footer.grid(row=8, column=0, sticky="ew", pady=(8, 0))
        ttk.Label(
            footer, text=f"{APP_NAME} {__version__} - unofficial tool", foreground="#777777"
        ).pack(side="left")
        ttk.Button(footer, text="Save log...", command=self._save_log).pack(side="right")
        ttk.Button(footer, text="About", command=self._show_about).pack(side="right", padx=(0, 6))

    # -- port handling -------------------------------------------------
    def _refresh_ports(self) -> None:
        if self.demo_var.get() and self.demo is not None:
            self.port_combo.configure(values=[self.demo.port], state="disabled")
            self.port_var.set(self.demo.port)
            return

        ports = list_serial_ports(include_all=self.show_all_var.get())
        self._port_paths = {p.label(): p.device for p in ports}
        labels = list(self._port_paths)
        if not labels:
            labels = ["<no USB serial adapter found>"]
        self.port_combo.configure(values=labels, state="readonly")
        current = self.port_var.get()
        if current not in labels:
            self.port_var.set(labels[0])

    def _selected_port(self) -> str | None:
        if self.demo_var.get() and self.demo is not None:
            return self.demo.port
        return getattr(self, "_port_paths", {}).get(self.port_var.get())

    def _toggle_demo(self) -> None:
        if self.demo_var.get():
            try:
                self.demo = DemoDevice()
            except RuntimeError as exc:
                self.demo_var.set(False)
                messagebox.showwarning(APP_NAME, str(exc), parent=self)
                return
            self._log(f"demo mode: emulated M451VE6AE on {self.demo.port}")
        else:
            if self.demo is not None:
                self.demo.close()
                self.demo = None
                self._log("demo mode off")
        self._refresh_ports()

    # -- file handling -------------------------------------------------
    def _browse_file(self) -> None:
        start_dir = None
        if self.image is not None:
            start_dir = str(self.image.path.parent)
        path = filedialog.askopenfilename(
            title="Select APROM firmware",
            initialdir=start_dir,
            filetypes=[("Firmware image", "*.bin"), ("All files", "*.*")],
        )
        if not path:
            return
        try:
            image = firmware.load(path)
        except firmware.FirmwareError as exc:
            messagebox.showerror(APP_NAME, str(exc), parent=self)
            return
        self.image = image
        self.file_var.set(str(image.path))
        extra = "" if image.looks_like_cortex_m(chips.FALLBACK_APROM_SIZE) else "  (unexpected header!)"
        self.file_meta_var.set(f"{image.describe()}{extra}")
        self._log(f"loaded {image.path.name}: {image.describe()}{extra}")
        self._update_buttons()

    # -- logging -------------------------------------------------------
    def _log_from_worker(self, message: str) -> None:
        """Called from the worker thread - must not touch Tk directly."""
        self.events.put(("log", message))

    def _log(self, message: str) -> None:
        self.log_text.configure(state="normal")
        self.log_text.insert("end", f"[{_timestamp()}] {message}\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    # -- event pump ----------------------------------------------------
    def _drain_events(self) -> None:
        try:
            while True:
                kind, payload = self.events.get_nowait()
                if kind == "log":
                    self._log(str(payload))
                elif kind == "status":
                    text, colour = payload  # type: ignore[misc]
                    self.status_var.set(text)
                    self.status_label.configure(foreground=colour)
                elif kind == "connected":
                    self._on_connected(payload)
                elif kind == "disconnected":
                    self._on_disconnected()
                elif kind == "disconnected_keep":
                    self._on_disconnected(keep=True)
                elif kind == "config":
                    values = payload  # type: ignore[assignment]
                    if values:
                        self.config0_var.set(f"0x{values[0]:08X}")
                        self.config1_var.set(f"0x{values[1]:08X}")
                elif kind == "progress":
                    done, total = payload  # type: ignore[misc]
                    percent = (done * 100.0 / total) if total else 0.0
                    self.progress_var.set(percent)
                    self.percent_var.set(f"{percent:3.0f}%")
                elif kind == "busy":
                    self._set_busy(bool(payload))
                elif kind == "error":
                    self._log(f"ERROR: {payload}")
                    messagebox.showerror(APP_NAME, str(payload), parent=self)
                elif kind == "finished":
                    self._log(str(payload).splitlines()[0])
                    messagebox.showinfo(APP_NAME, str(payload), parent=self)
        except queue.Empty:
            pass
        self.after(60, self._drain_events)

    # -- state helpers -------------------------------------------------
    def _set_status(self, text: str, colour: str) -> None:
        self.status_var.set(text)
        self.status_label.configure(foreground=colour)

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        self._update_buttons()

    def _update_buttons(self) -> None:
        busy = getattr(self, "_busy", False)
        connected = self.info is not None
        self.connect_button.configure(
            text="Disconnect" if connected else "Connect",
            state="disabled" if busy else "normal",
        )
        for widget in (self.browse_button, self.refresh_button):
            widget.configure(state="disabled" if busy else "normal")
        self.read_config_button.configure(
            state="normal" if (connected and not busy) else "disabled"
        )
        self.start_button.configure(
            state="normal"
            if (connected and self.image is not None and not busy and self.aprom_var.get())
            else "disabled"
        )
        self.stop_button.configure(state="normal" if busy else "disabled")

    def _on_connected(self, info: Any) -> None:
        self.info = info
        self.part_var.set(info.part)
        self.info_var.set(info.summary())
        if info.config:
            self.config0_var.set(f"0x{info.config[0]:08X}")
            self.config1_var.set(f"0x{info.config[1]:08X}")
        self._set_status("Connected", "#0a7d32")
        self._log(f"connected: {info.part} (PID 0x{info.device_id:08X}) - {info.summary()}")
        self._set_busy(False)

    def _on_disconnected(self, keep: bool = False) -> None:
        self.info = None
        if not keep:
            self.part_var.set("")
            self.info_var.set("")
            self.config0_var.set("")
            self.config1_var.set("")
            self._set_status("Disconnected", "#b00020")
        self._set_busy(False)

    # -- connect / disconnect ------------------------------------------
    def _toggle_connect(self) -> None:
        if self.info is not None:
            self._disconnect()
            return
        port = self._selected_port()
        if not port:
            messagebox.showwarning(
                APP_NAME,
                "No serial adapter selected.\n\nPlug in the Meraki USB serial cable "
                "and press Refresh.",
                parent=self,
            )
            return
        self.cancel_event.clear()
        self._set_busy(True)
        self.progress_var.set(0.0)
        self.percent_var.set("")
        self._set_status("Opening port...", "#b00020")
        self.worker = threading.Thread(target=self._connect_worker, args=(port,), daemon=True)
        self.worker.start()

    def _connect_worker(self, port: str) -> None:
        shown = [-1]

        def on_wait(remaining: float) -> None:
            seconds = int(remaining)
            if seconds != shown[0]:
                shown[0] = seconds
                self.events.put(
                    (
                        "status",
                        (
                            "Waiting for device connection - power cycle the machine now "
                            f"({seconds}s left)",
                            "#b00020",
                        ),
                    )
                )

        try:
            self.session.open(port)
            self.events.put(
                ("status", ("Waiting for device connection...", "#b00020"))
            )
            info = self.session.connect(
                DEFAULT_CONNECT_TIMEOUT, cancel=self.cancel_event.is_set, on_wait=on_wait
            )
        except Cancelled:
            self.session.close()
            self.events.put(("status", ("Disconnected", "#b00020")))
            self.events.put(("busy", False))
            return
        except SessionError as exc:
            self.session.close()
            self.events.put(("error", str(exc)))
            self.events.put(("disconnected", None))
            return
        self.events.put(("connected", info))

    def _disconnect(self) -> None:
        if self.worker is not None and self.worker.is_alive():
            self.cancel_event.set()
            self._set_status("Stopping...", "#b00020")
            return
        self.session.close()
        self._on_disconnected()

    # -- read config ---------------------------------------------------
    def _read_config(self) -> None:
        if self.session.isp is None:
            return
        self._set_busy(True)

        def worker() -> None:
            try:
                values = self.session.isp.read_config()
                self.events.put(("config", values))
                self.events.put(("log", "CONFIG read (read-only, nothing written)."))
            except Exception as exc:  # noqa: BLE001 - surfaced to the user
                self.events.put(("error", f"cannot read CONFIG: {exc}"))
            finally:
                self.events.put(("busy", False))

        self.worker = threading.Thread(target=worker, daemon=True)
        self.worker.start()

    # -- programming ---------------------------------------------------
    def _start(self) -> None:
        if self.image is None:
            messagebox.showwarning(APP_NAME, "Load an APROM file first.", parent=self)
            return
        if not self.aprom_var.get():
            messagebox.showwarning(APP_NAME, "The APROM checkbox must be selected.", parent=self)
            return
        self.cancel_event.clear()
        self._set_busy(True)
        self.progress_var.set(0.0)
        self.percent_var.set("0%")
        self._set_status("Programming...", "#b00020")
        self._log(
            "starting update: APROM"
            + (", reset and run" if self.reset_var.get() else ", no reset")
        )
        self.worker = threading.Thread(target=self._flash_worker, daemon=True)
        self.worker.start()

    def _flash_worker(self) -> None:
        image = self.image
        assert image is not None
        ok = False
        try:
            self.session.program(
                image,
                progress=lambda done, total: self.events.put(("progress", (done, total))),
                cancel=self.cancel_event.is_set,
            )
            if self.reset_var.get():
                self.events.put(("status", ("Resetting the MCU...", "#b00020")))
                self.session.run()
        except Cancelled:
            self.events.put(("error", "Update cancelled. The machine may need a power cycle."))
        except SessionError as exc:
            self.events.put(
                (
                    "error",
                    f"{exc}\n\nThe update did not finish. Power-cycle the machine and "
                    "start again - the bootloader is still intact.",
                )
            )
        else:
            ok = True
            self.events.put(("progress", (image.size, image.size)))
            self.events.put(("status", ("Update complete", "#0a7d32")))
            self.events.put(
                (
                    "finished",
                    f"Update complete.\n\n{image.path.name}\n{image.describe()}\n\n"
                    "Check the version on the machine's settings screen.",
                )
            )
        finally:
            # The device rebooted into the new firmware, so the connection is
            # gone either way; keep the part/version read-out on success.
            self.session.close()
            self.events.put(("disconnected_keep" if ok else "disconnected", None))

    def _stop(self) -> None:
        self.cancel_event.set()
        self._set_status("Stopping...", "#b00020")

    # -- about / log / close -------------------------------------------
    def _show_about(self) -> None:
        port = self._selected_port()
        window = tk.Toplevel(self)
        window.title(f"About {APP_NAME}")
        window.transient(self)
        window.minsize(560, 420)

        frame = ttk.Frame(window, padding=12)
        frame.pack(fill="both", expand=True)
        header = ttk.Label(frame, text=APP_NAME, font=("TkDefaultFont", 15, "bold"))
        header.pack(anchor="w")
        ttk.Label(frame, text=APP_TAGLINE, foreground="#555555").pack(anchor="w", pady=(0, 8))
        ttk.Label(frame, text=about.ABOUT_LICENSE_NOTE, foreground="#555555").pack(
            anchor="w", pady=(0, 8)
        )

        text = tk.Text(frame, wrap="word", height=18, width=68, relief="solid", borderwidth=1)
        text.insert("1.0", about.about_text(port))
        text.configure(state="disabled")
        text.pack(fill="both", expand=True)

        buttons = ttk.Frame(frame)
        buttons.pack(fill="x", pady=(10, 0))

        def copy_details() -> None:
            self.clipboard_clear()
            self.clipboard_append(about.about_text(port))
            self._log("about details copied to the clipboard")

        ttk.Button(buttons, text="Copy details", command=copy_details).pack(side="left")
        ttk.Button(buttons, text="Close", command=window.destroy).pack(side="right")

    def _save_log(self) -> None:
        path = filedialog.asksaveasfilename(
            title="Save log",
            defaultextension=".log",
            filetypes=[("Log file", "*.log"), ("All files", "*.*")],
        )
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(self.log_text.get("1.0", "end"))
        except OSError as exc:
            messagebox.showerror(APP_NAME, f"Cannot save log: {exc}", parent=self)
            return
        self._log(f"log saved to {path}")

    def _on_close(self) -> None:
        if getattr(self, "_busy", False):
            if not messagebox.askyesno(
                APP_NAME,
                "An operation is still running. Quit anyway?",
                parent=self,
            ):
                return
        self.cancel_event.set()
        if self.worker is not None and self.worker.is_alive():
            self.worker.join(timeout=2.0)
        self.session.close()
        if self.demo is not None:
            self.demo.close()
            self.demo = None
        self.destroy()


def main(argv: list[str] | None = None) -> int:
    app = MerakiFlasher()
    app.mainloop()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())





