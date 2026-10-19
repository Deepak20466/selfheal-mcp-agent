"""Small HTTP CONNECT gateway with an exact public-host allowlist.

NetworkPolicy limits trusted workloads to this gateway. The gateway resolves
each authorized hostname itself, rejects non-public addresses, and connects to
the resolved address directly so DNS rebinding cannot change the destination
between validation and connect.
"""

from __future__ import annotations

import ipaddress
import logging
import os
import re
import select
import socket
import socketserver
import threading
from collections.abc import Sequence
from typing import Any, cast
from urllib.parse import urlsplit

MAX_HEADER_BYTES = 8192
MAX_CLIENT_HELLO_BYTES = 32768
MAX_TLS_RECORD_BYTES = 18432
RELAY_IDLE_TIMEOUT_SECONDS = 300
MAX_CONCURRENT_CLIENTS = 64
HEADER_TIMEOUT_SECONDS = 10
_HOST_LABEL = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
logger = logging.getLogger("selfheal.egress_proxy")


def parse_client_hello_server_name(handshake: bytes) -> str:
    """Parse one complete TLS ClientHello handshake and return its SNI name."""
    if len(handshake) < 4 or handshake[0] != 1:
        raise ValueError("first TLS handshake message is not ClientHello")
    body_length = int.from_bytes(handshake[1:4], "big")
    if body_length + 4 != len(handshake) or body_length > MAX_CLIENT_HELLO_BYTES:
        raise ValueError("invalid or oversized TLS ClientHello")
    body = handshake[4:]
    offset = 0

    def require(size: int) -> None:
        if size < 0 or offset + size > len(body):
            raise ValueError("truncated TLS ClientHello")

    require(34)  # legacy_version and random
    offset += 34
    require(1)
    session_id_length = body[offset]
    offset += 1
    require(session_id_length)
    offset += session_id_length
    require(2)
    cipher_suites_length = int.from_bytes(body[offset : offset + 2], "big")
    offset += 2
    if cipher_suites_length < 2 or cipher_suites_length % 2:
        raise ValueError("invalid TLS cipher suite list")
    require(cipher_suites_length)
    offset += cipher_suites_length
    require(1)
    compression_methods_length = body[offset]
    offset += 1
    if compression_methods_length < 1:
        raise ValueError("invalid TLS compression methods")
    require(compression_methods_length)
    offset += compression_methods_length
    require(2)
    extensions_length = int.from_bytes(body[offset : offset + 2], "big")
    offset += 2
    require(extensions_length)
    if offset + extensions_length != len(body):
        raise ValueError("invalid TLS extension length")
    extensions_end = offset + extensions_length
    server_names: list[str] = []
    while offset < extensions_end:
        require(4)
        extension_type = int.from_bytes(body[offset : offset + 2], "big")
        extension_length = int.from_bytes(body[offset + 2 : offset + 4], "big")
        offset += 4
        require(extension_length)
        extension_end = offset + extension_length
        if extension_type == 0xFE0D:
            raise ValueError("encrypted TLS ClientHello is not supported")
        if extension_type == 0:
            require(2)
            names_length = int.from_bytes(body[offset : offset + 2], "big")
            offset += 2
            if offset + names_length != extension_end:
                raise ValueError("invalid TLS server name list")
            names_end = offset + names_length
            while offset < names_end:
                require(3)
                name_type = body[offset]
                name_length = int.from_bytes(body[offset + 1 : offset + 3], "big")
                offset += 3
                require(name_length)
                if offset + name_length > names_end:
                    raise ValueError("invalid TLS server name length")
                if name_type == 0:
                    try:
                        name = body[offset : offset + name_length].decode("ascii").lower()
                    except UnicodeDecodeError as exc:
                        raise ValueError("TLS SNI must be an ASCII hostname") from exc
                    if not _valid_hostname(name):
                        raise ValueError("TLS SNI must be a valid DNS hostname")
                    server_names.append(name)
                offset += name_length
        offset = extension_end
    if len(server_names) != 1:
        raise ValueError("TLS ClientHello must contain exactly one DNS SNI name")
    return server_names[0]


def read_client_hello(client: socket.socket) -> tuple[str, bytes]:
    """Read a bounded TLS ClientHello while retaining its exact wire bytes."""
    client.settimeout(HEADER_TIMEOUT_SECONDS)
    handshake = bytearray()
    wire = bytearray()
    expected_length: int | None = None
    while expected_length is None or len(handshake) < expected_length:
        record_header = _recv_exact(client, 5)
        content_type, major_version, _minor_version, record_length = (
            record_header[0],
            record_header[1],
            record_header[2],
            int.from_bytes(record_header[3:5], "big"),
        )
        if content_type != 22 or major_version != 3 or record_length == 0:
            raise ValueError("expected a TLS handshake record")
        if (
            record_length > MAX_TLS_RECORD_BYTES
            or len(wire) + 5 + record_length > MAX_CLIENT_HELLO_BYTES
        ):
            raise ValueError("TLS ClientHello exceeded the size limit")
        record_payload = _recv_exact(client, record_length)
        wire.extend(record_header)
        wire.extend(record_payload)
        handshake.extend(record_payload)
        if expected_length is None and len(handshake) >= 4:
            if handshake[0] != 1:
                raise ValueError("first TLS handshake message is not ClientHello")
            expected_length = 4 + int.from_bytes(handshake[1:4], "big")
            if expected_length > MAX_CLIENT_HELLO_BYTES:
                raise ValueError("TLS ClientHello exceeded the size limit")
    complete_handshake = bytes(handshake[:expected_length])
    return parse_client_hello_server_name(complete_handshake), bytes(wire)


def _recv_exact(connection: socket.socket, size: int) -> bytes:
    chunks = bytearray()
    while len(chunks) < size:
        chunk = connection.recv(size - len(chunks))
        if not chunk:
            raise OSError("client closed the TLS handshake early")
        chunks.extend(chunk)
    return bytes(chunks)


def allowed_hosts_from_env() -> frozenset[str]:
    hosts = frozenset(
        host.strip().lower().rstrip(".")
        for host in os.environ.get("ALLOWED_HOSTS", "").split(",")
        if host.strip()
    )
    if not hosts or any(not _valid_hostname(host) for host in hosts):
        raise RuntimeError("ALLOWED_HOSTS must contain exact hostnames and no wildcards")
    return hosts


def _valid_hostname(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
        return False
    except ValueError:
        pass
    labels = host.split(".")
    return len(labels) >= 2 and all(_HOST_LABEL.fullmatch(label) for label in labels)


def resolve_allowed_destination(
    authority: str,
    *,
    allowed_hosts: Sequence[str],
) -> list[tuple[int, int, int, str, tuple[object, ...]]]:
    """Validate a CONNECT authority and resolve it to public socket addresses."""
    parsed = urlsplit(f"//{authority}")
    host = (parsed.hostname or "").lower().rstrip(".")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("invalid destination port") from exc
    if (
        not host
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path
        or parsed.query
        or parsed.fragment
        or port != 443
        or not _valid_hostname(host)
    ):
        raise ValueError("only HTTPS CONNECT destinations on port 443 are allowed")
    if host not in {item.lower().rstrip(".") for item in allowed_hosts}:
        raise ValueError("destination hostname is not allowlisted")

    try:
        addresses = cast(
            list[tuple[int, int, int, str, tuple[object, ...]]],
            socket.getaddrinfo(host, port, type=socket.SOCK_STREAM),
        )
    except OSError as exc:
        raise ValueError("destination hostname did not resolve") from exc
    if not addresses:
        raise ValueError("destination hostname did not resolve")

    # Reject the whole answer if even one address is private or otherwise
    # special-use. This avoids selecting a public answer from a mixed response
    # while leaving a rebinding path to a private address available.
    for family, _socktype, _protocol, _canonical, sockaddr in addresses:
        try:
            address = ipaddress.ip_address(str(sockaddr[0]))
        except ValueError as exc:
            raise ValueError("destination resolved to an invalid address") from exc
        if not address.is_global:
            raise ValueError("destination resolved to a non-public address")
        if family not in (socket.AF_INET, socket.AF_INET6):
            raise ValueError("destination resolved to an unsupported address family")
    return addresses


class _ProxyHandler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        request = bytearray()
        self.request.settimeout(HEADER_TIMEOUT_SECONDS)
        try:
            while not request.endswith(b"\r\n\r\n"):
                chunk = self.request.recv(1)
                if not chunk:
                    return
                request.extend(chunk)
                if len(request) > MAX_HEADER_BYTES:
                    self._respond(431, "Request Header Fields Too Large")
                    return
        except OSError:
            return

        try:
            request_line = bytes(request).split(b"\r\n", 1)[0].decode("ascii")
            method, authority, version = request_line.split(" ")
            if method != "CONNECT" or version not in {"HTTP/1.0", "HTTP/1.1"}:
                self._respond(405, "CONNECT Required")
                return
            host = (urlsplit(f"//{authority}").hostname or "").lower().rstrip(".")
            addresses = resolve_allowed_destination(
                authority,
                allowed_hosts=self.server.allowed_hosts,  # type: ignore[attr-defined]
            )
        except (UnicodeDecodeError, ValueError) as exc:
            logger.warning("egress_proxy.connect_denied reason=%s", exc)
            self._respond(403, "Destination Denied")
            return

        upstream: socket.socket | None = None
        for family, socktype, protocol, _canonical, sockaddr in addresses:
            candidate = socket.socket(family, socktype, protocol)
            candidate.settimeout(8)
            try:
                candidate.connect(sockaddr)
            except OSError:
                candidate.close()
                continue
            candidate.settimeout(None)
            upstream = candidate
            break
        if upstream is None:
            logger.warning("egress_proxy.upstream_unavailable host=%s", host)
            self._respond(502, "Upstream Unavailable")
            return

        try:
            self.request.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            try:
                server_name, client_hello = read_client_hello(self.request)
            except (OSError, ValueError) as exc:
                logger.warning("egress_proxy.tls_client_hello_denied reason=%s", exc)
                return
            if server_name != host:
                logger.warning(
                    "egress_proxy.tls_sni_denied connect_host=%s sni=%s", host, server_name
                )
                return
            self.request.settimeout(None)
            upstream.sendall(client_hello)
            logger.info(
                "egress_proxy.connect_allowed host=%s sni=%s ip=%s",
                host,
                server_name,
                upstream.getpeername()[0],
            )
            self._relay(upstream)
        finally:
            upstream.close()

    def _respond(self, status: int, reason: str) -> None:
        try:
            self.request.sendall(
                f"HTTP/1.1 {status} {reason}\r\nConnection: close\r\n"
                "Content-Length: 0\r\n\r\n".encode("ascii")
            )
        except OSError:
            pass

    def _relay(self, upstream: socket.socket) -> None:
        peers = (self.request, upstream)
        while True:
            readable, _writable, exceptional = select.select(
                peers, (), peers, RELAY_IDLE_TIMEOUT_SECONDS
            )
            if exceptional or not readable:
                return
            for source in readable:
                try:
                    payload = source.recv(65536)
                except OSError:
                    return
                if not payload:
                    return
                destination = upstream if source is self.request else self.request
                try:
                    destination.sendall(payload)
                except OSError:
                    return


class _ProxyServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True
    block_on_close = False

    def __init__(self, address: tuple[str, int], allowed_hosts: frozenset[str]) -> None:
        self.allowed_hosts = allowed_hosts
        self._client_slots = threading.BoundedSemaphore(MAX_CONCURRENT_CLIENTS)
        super().__init__(address, _ProxyHandler)

    def process_request(
        self,
        request: socket.socket | tuple[bytes, socket.socket],
        client_address: Any,
    ) -> None:
        if isinstance(request, tuple):
            request[1].close()
            return
        if not self._client_slots.acquire(blocking=False):
            request.close()
            logger.warning("egress_proxy.client_limit_reached")
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._client_slots.release()
            raise

    def process_request_thread(
        self,
        request: socket.socket | tuple[bytes, socket.socket],
        client_address: Any,
    ) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._client_slots.release()


def main() -> None:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    allowed_hosts = allowed_hosts_from_env()
    # The container has no hostNetwork access; bind the service port on its
    # own pod interface so Kubernetes Services can reach it.
    host = os.environ.get("LISTEN_HOST", "0.0.0.0")  # noqa: S104
    port = int(os.environ.get("LISTEN_PORT", "3128"))
    with _ProxyServer((host, port), allowed_hosts) as server:
        server.serve_forever(poll_interval=0.5)


if __name__ == "__main__":
    main()
