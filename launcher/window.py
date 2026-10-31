"""The small launcher window: starts the server process, shows its health, opens the browser."""

import logging
import os
import queue
import subprocess
import sys
import threading
import time
import tkinter as tk
import webbrowser
from tkinter import messagebox

from launcher import common
from launcher.mdns import Advertiser, resolves

logger = logging.getLogger(__name__)

START_TIMEOUT_SECONDS = 600  # a large database update takes a while
HEALTH_EVERY_SECONDS = 5
MAX_RESTARTS = 3

TEAL = "#008E81"
INK = "#1F2937"
MUTED = "#6B7280"
GREEN, AMBER, RED = "#16A34A", "#D97706", "#DC2626"


def server_command(port: int) -> list[str]:
    if getattr(sys, "frozen", False):
        return [sys.executable, "--server", "--port", str(port)]
    return [sys.executable, "-m", "launcher.main", "--server", "--port", str(port)]


class Launcher:
    def __init__(self):
        self.root = tk.Tk()
        self.root.title("SehatDesk")
        self.root.geometry("440x270")
        self.root.resizable(False, False)
        self.root.configure(bg="white")
        icon = common.resource_dir() / "sehat_desk.ico"
        if sys.platform == "win32" and icon.exists():
            self.root.iconbitmap(str(icon))
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

        self.child = None
        self.port = None
        self.url = None
        self.mdns = None
        self.stopping = False
        self.restarts = 0
        self.browser_opened = False

        # Tk may only be touched from this thread: the worker sends updates through this queue.
        self.updates = queue.Queue()

        self._build()
        self.root.after(100, self._apply_updates)
        threading.Thread(target=self.supervise, daemon=True).start()

    # --- Window -------------------------------------------------------------------------

    def _build(self) -> None:
        font = "Segoe UI" if sys.platform == "win32" else "Helvetica"
        tk.Label(self.root, text="SehatDesk", font=(font, 20, "bold"), fg=TEAL, bg="white").pack(
            anchor="w", padx=20, pady=(18, 0)
        )
        tk.Label(
            self.root,
            text=f"Hospital management · version {common.version()}",
            font=(font, 9),
            fg=MUTED,
            bg="white",
        ).pack(anchor="w", padx=20)

        row = tk.Frame(self.root, bg="white")
        row.pack(anchor="w", padx=20, pady=(16, 2))
        self.dot = tk.Label(row, text="●", font=(font, 14), fg=AMBER, bg="white")
        self.dot.pack(side="left")
        self.status = tk.Label(
            row,
            text="Starting…",
            font=(font, 11, "bold"),
            fg=INK,
            bg="white",
            wraplength=380,
            justify="left",
        )
        self.status.pack(side="left", padx=(6, 0))

        self.address = tk.Label(
            self.root, text="", font=(font, 9), fg=MUTED, bg="white", justify="left", wraplength=400
        )
        self.address.pack(anchor="w", padx=20, pady=(4, 0))

        buttons = tk.Frame(self.root, bg="white")
        buttons.pack(side="bottom", fill="x", padx=20, pady=16)
        self.open_button = tk.Button(
            buttons, text="Open in browser", command=self.open_browser, state="disabled"
        )
        self.open_button.pack(side="left")
        tk.Button(buttons, text="Logs", command=self.open_logs).pack(side="left", padx=6)
        tk.Button(buttons, text="Stop and exit", command=self.stop_and_exit).pack(side="right")

    def show(self, text: str, colour: str = AMBER, address: str | None = None) -> None:
        def apply():
            self.status.config(text=text)
            self.dot.config(fg=colour)
            self.open_button.config(state="normal" if colour == GREEN else "disabled")
            if address is not None:
                self.address.config(text=address)

        self.updates.put(apply)

    def _apply_updates(self) -> None:
        while not self.updates.empty():
            self.updates.get_nowait()()
        self.root.after(100, self._apply_updates)

    def open_browser(self) -> None:
        if self.url:
            webbrowser.open(self.url)

    def open_logs(self) -> None:
        path = str(common.log_dir())
        if sys.platform == "win32":
            os.startfile(path)  # noqa: S606
        else:
            subprocess.Popen(["open", path])

    def on_close(self) -> None:
        answer = messagebox.askyesnocancel(
            "SehatDesk",
            "Stop SehatDesk for everyone on the network?\n\n"
            "Yes: stop the server and exit.\nNo: keep it running and minimize this window.",
        )
        if answer is True:
            self.stop_and_exit()
        elif answer is False:
            self.root.iconify()

    def stop_and_exit(self) -> None:
        self.stopping = True
        self.show("Stopping…")
        threading.Thread(target=self._shutdown, daemon=True).start()

    def _shutdown(self) -> None:
        if self.mdns:
            self.mdns.stop()
        self._stop_child()
        self.updates.put(self.root.destroy)

    # --- Server process -----------------------------------------------------------------

    def _spawn(self) -> None:
        common.write_status("Starting")
        flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
        self.child = subprocess.Popen(
            server_command(self.port),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=flags,
        )

    def _stop_child(self) -> None:
        if self.child and self.child.poll() is None:
            self.child.terminate()
            try:
                self.child.wait(10)
            except subprocess.TimeoutExpired:
                self.child.kill()

    def _wait_until_ready(self) -> bool:
        deadline = time.monotonic() + START_TIMEOUT_SECONDS
        while time.monotonic() < deadline and not self.stopping:
            if common.is_healthy(self.port):
                return True
            status = common.read_status()
            if self.child.poll() is not None:
                message = status[7:] if status.startswith("error: ") else "The server stopped."
                self.show(message, RED, "Press Logs and send server.log to SehatDesk support.")
                return False
            self.show(f"{status or 'Starting'}…")
            time.sleep(1)
        return False

    def supervise(self) -> None:
        try:
            self._supervise()
        except Exception:
            logger.exception("launcher error")
            self.show("Something went wrong in the launcher.", RED)

    def _supervise(self) -> None:
        # A server left over from a launcher that was killed: use it instead of a second copy.
        self.port = common.running_port()
        if self.port is None:
            self.port = common.choose_port()
            if self.port is None:
                self.show("Ports 80, 8000 and 8001 are all in use.", RED)
                return
            self._spawn()
            if not self._wait_until_ready():
                return

        self.mdns = Advertiser(self.port)
        self.mdns.start()
        self._on_ready()

        failures = 0
        tick = 0
        while not self.stopping:
            time.sleep(HEALTH_EVERY_SECONDS)
            if self.stopping:
                return
            tick += 1
            if tick % 12 == 0:
                self.mdns.refresh()
                self._show_running()
            if self.child and self.child.poll() is not None:
                if not self._restart():
                    return
                continue
            if common.is_healthy(self.port, timeout=4.0):
                if failures:
                    self._show_running()
                failures = 0
            else:
                failures += 1
                if failures >= 2:
                    self.show("Server is not responding. Waiting…", AMBER)

    def _restart(self) -> bool:
        self.restarts += 1
        if self.restarts > MAX_RESTARTS:
            self.show("The server keeps stopping.", RED, "Press Logs and contact support.")
            return False
        self.show("The server stopped. Restarting…")
        self._spawn()
        if not self._wait_until_ready():
            return False
        self._show_running()
        return True

    def _show_running(self) -> None:
        ips = common.lan_ips()
        lines = [f"On this network: {common.base_url(common.HOSTNAME, self.port)}"]
        if ips:
            lines.append(f"If that does not open: {common.base_url(ips[0], self.port)}")
        self.show("Running", GREEN, "\n".join(lines))

    def _on_ready(self) -> None:
        host = common.HOSTNAME if resolves() else "localhost"
        self.url = common.base_url(host, self.port)
        self._show_running()
        if not self.browser_opened:
            self.browser_opened = True
            self.open_browser()

    def run(self) -> None:
        self.root.mainloop()


def main() -> int:
    Launcher().run()
    return 0
