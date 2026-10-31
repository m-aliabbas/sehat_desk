"""SehatDesk Print: the small program on each PC that prints without a print window.

sehat_print.exe                 the window, started at sign-in
sehat_print.exe --self-test     bundle check, run by the build
"""

import sys
import threading

MUTEX_NAME = "SehatDeskPrintMutex"


def _already_running() -> bool:
    if sys.platform != "win32":
        return False
    import ctypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW(None, False, MUTEX_NAME)
    return ctypes.get_last_error() == 183  # ERROR_ALREADY_EXISTS


def self_test() -> int:
    import PIL
    import pypdfium2

    from print_helper import printers, render

    if sys.platform == "win32":
        import pystray
        import win32print
        import win32ui

        assert pystray and win32print and win32ui
    assert PIL and pypdfium2 and printers and render
    return 0


def _hide_in_tray(root) -> bool:
    """A tray icon with no Exit: staff cannot stop printing by accident. Windows only."""
    if sys.platform != "win32":
        return False
    import pystray
    from PIL import Image, ImageDraw

    picture = Image.new("RGB", (64, 64), "#112d4e")
    ImageDraw.Draw(picture).rectangle((14, 24, 50, 46), fill="white")
    menu = pystray.Menu(
        pystray.MenuItem("Open", lambda: root.after(0, root.deiconify), default=True)
    )
    pystray.Icon("SehatDesk Print", picture, "SehatDesk Print", menu).run_detached()
    return True


def window() -> int:
    import tkinter as tk
    from tkinter import ttk

    from print_helper import agent

    config = agent.load_config()
    config.setdefault("server", agent.DEFAULT_SERVER)
    root = tk.Tk()
    root.title("SehatDesk Print")
    root.geometry("440x170")
    in_tray = _hide_in_tray(root)
    # Closing keeps printing.
    root.protocol("WM_DELETE_WINDOW", root.withdraw if in_tray else root.iconify)
    frame = ttk.Frame(root, padding=16)
    frame.pack(fill="both", expand=True)
    server = tk.StringVar(value=config["server"])
    status = tk.StringVar(value="Starting...")
    ttk.Label(frame, text="SehatDesk address").grid(row=0, column=0, sticky="w", pady=4)
    ttk.Entry(frame, textvariable=server, width=32).grid(row=0, column=1, pady=4)
    stop = [threading.Event()]

    def show(text):
        root.after(0, status.set, text)

    def start():
        stop[0].set()
        stop[0] = threading.Event()
        threading.Thread(
            target=agent.loop, args=(config, show, stop[0].is_set), daemon=True
        ).start()

    def press_save():
        address = server.get().strip()
        if address != config["server"]:  # another server: this PC joins it afresh
            config.clear()
            config["server"] = address
            agent.save_config(config)
            start()

    ttk.Button(frame, text="Use this address", command=press_save).grid(
        row=1, column=1, sticky="e", pady=8
    )
    ttk.Label(frame, textvariable=status, wraplength=400).grid(
        row=2, column=0, columnspan=2, sticky="w", pady=8
    )
    start()
    if config.get("token"):
        root.withdraw() if in_tray else root.iconify()
    root.mainloop()
    return 0


def main() -> int:
    if sys.argv[1:2] == ["--self-test"]:
        return self_test()
    if _already_running():
        return 0
    return window()


if __name__ == "__main__":
    sys.exit(main())
