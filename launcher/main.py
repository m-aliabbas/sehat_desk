"""Entry point of sehat_desk.exe.

sehat_desk.exe                       the launcher window (what the desktop icon runs)
sehat_desk.exe --server --port 80    the server process, started by the window
sehat_desk.exe --manage <command>    a Django command, e.g. restore FILE --yes
sehat_desk.exe --self-test OUT.json  bundle check, run by the build
"""

import sys

MUTEX_NAME = "SehatDeskLauncherMutex"  # also AppMutex in packaging/installer.iss


def _already_running() -> bool:
    """Windows only: holds a named mutex for the life of the window."""
    if sys.platform != "win32":
        return False
    import ctypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW(None, False, MUTEX_NAME)
    return ctypes.get_last_error() == 183  # ERROR_ALREADY_EXISTS


def main() -> int:
    args = sys.argv[1:]
    if args[:1] == ["--server"]:
        from launcher import server

        return server.run(int(args[args.index("--port") + 1]))
    if args[:1] == ["--manage"]:
        from launcher import server

        return server.manage(args[1:])
    if args[:1] == ["--self-test"]:
        from launcher import server

        return server.self_test(args[1])

    from launcher import common

    if _already_running():
        # Second click on the icon: the server is up, just show it.
        import webbrowser

        port = common.running_port()
        if port:
            webbrowser.open(common.base_url("localhost", port))
        return 0

    from launcher import window

    return window.main()


if __name__ == "__main__":
    sys.exit(main())
