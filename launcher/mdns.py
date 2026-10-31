"""Answers for sehatdesk.local on the LAN over mDNS, so no router or DNS setup is needed."""

import logging
import socket

from launcher.common import HOSTNAME, lan_ips

logger = logging.getLogger(__name__)

SERVICE_TYPE = "_http._tcp.local."
SERVICE_NAME = f"SehatDesk.{SERVICE_TYPE}"
# How long clients may keep the address. The library default of 2 minutes makes every client ask
# again over multicast, which Wi-Fi drops often; a failed lookup is a page that will not open.
# Assumes the server has a fixed IP: after a change, a client may keep the old one this long.
HOST_TTL_SECONDS = 4 * 3600
LOOKUP_TIMEOUT_MS = 2000


class Advertiser:
    def __init__(self, port: int):
        self.port = port
        self.ips: list[str] = []
        self.zc = None
        self.info = None
        self.other_server: str | None = None  # another computer already answers for the name

    def _service_info(self, ips: list[str]):
        from zeroconf import ServiceInfo

        return ServiceInfo(
            SERVICE_TYPE,
            SERVICE_NAME,
            addresses=[socket.inet_aton(ip) for ip in ips],
            port=self.port,
            server=f"{HOSTNAME}.",
            host_ttl=HOST_TTL_SECONDS,
            properties={"path": "/"},
        )

    def start(self) -> bool:
        from zeroconf import IPVersion, Zeroconf

        self.ips = lan_ips()
        if not self.ips:
            return False
        try:
            self.zc = Zeroconf(ip_version=IPVersion.V4Only)
            self.other_server = self._find_other_server()
            if self.other_server:
                # Two answers for one name send clients to either database at random.
                logger.warning("Another SehatDesk server answers on this network")
                self.stop()
                return False
            self.info = self._service_info(self.ips)
            self.zc.register_service(self.info, allow_name_change=True)
        except Exception:  # the app still works by IP address; the window shows it
            logger.exception("mDNS could not start")
            self.stop()
            return False
        return True

    def _find_other_server(self) -> str | None:
        found = self.zc.get_service_info(SERVICE_TYPE, SERVICE_NAME, timeout=LOOKUP_TIMEOUT_MS)
        addresses = found.parsed_addresses() if found else []
        if not addresses or set(addresses) & set(self.ips):
            return None
        return addresses[0]

    def refresh(self) -> None:
        """Follow a changed address (DHCP renewal, Wi-Fi to cable, cable plugged in later), and
        take over the name once another server has gone."""
        ips = lan_ips()
        if ips == self.ips and not self.other_server:
            return
        self.stop()
        self.start()

    def stop(self) -> None:
        if self.zc:
            try:
                if self.info:
                    self.zc.unregister_service(self.info)
                self.zc.close()
            except Exception:
                logger.exception("mDNS did not stop cleanly")
        self.zc = self.info = None


def resolves(timeout: float = 4.0) -> bool:
    """Whether this computer itself can reach sehatdesk.local (else the browser opens localhost)."""
    import threading

    result = []

    def lookup():
        try:
            socket.getaddrinfo(HOSTNAME, 80, socket.AF_INET)
            result.append(True)
        except OSError:
            pass

    thread = threading.Thread(target=lookup, daemon=True)
    thread.start()
    thread.join(timeout)
    return bool(result)
