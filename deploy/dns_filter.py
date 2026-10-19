"""Pod-local DNS filter: cluster names plus exact approved external hosts."""

from __future__ import annotations

import ipaddress
import logging
import os
import re
import socket
import socketserver
import struct
import threading
from typing import Any

DNS_PORT = 53
MAX_DNS_PACKET_BYTES = 4096
MAX_CONCURRENT_CLIENTS = 64
DNS_TIMEOUT_SECONDS = 2
_DNS_LABEL = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
logger = logging.getLogger("selfheal.dns_filter")


def _normalized_names(value: str) -> frozenset[str]:
    return frozenset(name.strip().lower().rstrip(".") for name in value.split(",") if name.strip())


def _parse_question(packet: bytes) -> tuple[int, int, str, int, int, int] | None:
    if len(packet) < 12:
        return None
    transaction_id, flags, question_count = struct.unpack_from("!HHH", packet)
    if flags & 0x8000 or question_count != 1:
        return None

    position = 12
    labels: list[str] = []
    while position < len(packet):
        size = packet[position]
        position += 1
        if size == 0:
            break
        if size & 0xC0 or size > 63 or position + size > len(packet):
            return None
        try:
            label = packet[position : position + size].decode("ascii").lower()
        except UnicodeDecodeError:
            return None
        if not _DNS_LABEL.fullmatch(label):
            return None
        labels.append(label)
        position += size

    if not labels or position + 4 > len(packet):
        return None
    query_type, query_class = struct.unpack_from("!HH", packet, position)
    question_end = position + 4
    return transaction_id, flags, ".".join(labels), query_type, query_class, question_end


def query_is_allowed(
    packet: bytes,
    *,
    allowed_hosts: frozenset[str],
    internal_hosts: frozenset[str],
) -> bool:
    """Allow A/AAAA lookups for exact provider hosts or cluster services."""
    question = _parse_question(packet)
    if question is None:
        return False
    _transaction_id, _flags, name, query_type, query_class, _question_end = question
    if query_class != 1 or query_type not in {1, 28}:
        return False
    return name in allowed_hosts or name in internal_hosts or name.endswith(".svc.cluster.local")


def sanitized_query(packet: bytes) -> bytes:
    """Keep only the question and recursion bit; drop EDNS and extra records."""
    question = _parse_question(packet)
    if question is None:
        return b""
    transaction_id, flags, _name, _query_type, _query_class, question_end = question
    # Standard DNS QUERY opcode only. The response keeps transaction matching
    # but cannot carry client-supplied EDNS options or additional records to the
    # recursive resolver as a data-exfiltration channel.
    if flags & 0x7800:
        return b""
    header = struct.pack("!HHHHHH", transaction_id, flags & 0x0100, 1, 0, 0, 0)
    return header + packet[12:question_end]


def refused_response(packet: bytes) -> bytes:
    """Return a minimal REFUSED response without echoing attacker data elsewhere."""
    question = _parse_question(packet)
    if question is None:
        return b""
    transaction_id, flags, _name, _query_type, _query_class, question_end = question
    response_flags = 0x8000 | (flags & 0x0100) | 0x0080 | 0x0005
    header = struct.pack("!HHHHHH", transaction_id, response_flags, 1, 0, 0, 0)
    return header + packet[12:question_end]


def forward_dns_query(packet: bytes, upstream_ip: str, *, tcp: bool = False) -> bytes:
    """Forward a validated query to a numeric DNS server and validate its response."""
    upstream = ipaddress.ip_address(upstream_ip)
    if not upstream.is_private and not upstream.is_loopback and not upstream.is_link_local:
        raise ValueError("DNS upstream must be an in-cluster address")
    if tcp:
        response = _forward_tcp(packet, upstream_ip)
    else:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as client:
            client.settimeout(DNS_TIMEOUT_SECONDS)
            client.sendto(packet, (upstream_ip, DNS_PORT))
            response, _address = client.recvfrom(MAX_DNS_PACKET_BYTES)
        if len(response) >= 4 and struct.unpack_from("!H", response, 2)[0] & 0x0200:
            response = _forward_tcp(packet, upstream_ip)

    if len(response) < 12 or response[:2] != packet[:2]:
        raise OSError("invalid DNS upstream response")
    if not struct.unpack_from("!H", response, 2)[0] & 0x8000:
        raise OSError("upstream did not return a DNS response")
    return response


def _read_exact(connection: socket.socket, size: int) -> bytes:
    chunks = bytearray()
    while len(chunks) < size:
        chunk = connection.recv(size - len(chunks))
        if not chunk:
            raise OSError("upstream closed a DNS connection early")
        chunks.extend(chunk)
    return bytes(chunks)


def _forward_tcp(packet: bytes, upstream_ip: str) -> bytes:
    with socket.create_connection((upstream_ip, DNS_PORT), timeout=DNS_TIMEOUT_SECONDS) as client:
        client.sendall(struct.pack("!H", len(packet)) + packet)
        response_length = struct.unpack("!H", _read_exact(client, 2))[0]
        if response_length > MAX_DNS_PACKET_BYTES:
            raise OSError("DNS upstream response exceeded the packet limit")
        return _read_exact(client, response_length)


def _process_query(packet: bytes, server: Any, *, tcp: bool = False) -> bytes:
    if len(packet) > MAX_DNS_PACKET_BYTES or not query_is_allowed(
        packet,
        allowed_hosts=server.allowed_hosts,
        internal_hosts=server.internal_hosts,
    ):
        logger.warning("dns_filter.query_denied")
        return refused_response(packet)
    try:
        query = sanitized_query(packet)
        if not query:
            return refused_response(packet)
        return forward_dns_query(query, server.upstream_ip, tcp=tcp)
    except (OSError, ValueError):
        logger.warning("dns_filter.upstream_failed")
        return refused_response(packet)


class _UDPHandler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        packet, connection = self.request
        response = _process_query(packet, self.server)
        if response:
            connection.sendto(response, self.client_address)


class _TCPHandler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        self.connection.settimeout(DNS_TIMEOUT_SECONDS + 1)
        try:
            length = struct.unpack("!H", _read_exact(self.connection, 2))[0]
            if length > MAX_DNS_PACKET_BYTES:
                return
            packet = _read_exact(self.connection, length)
            response = _process_query(packet, self.server, tcp=True)
            if response:
                self.wfile.write(struct.pack("!H", len(response)) + response)
        except OSError:
            return


class _BoundedThreadingMixin(socketserver.ThreadingMixIn):
    daemon_threads = True
    block_on_close = False

    def _init_slots(self) -> None:
        self._client_slots = threading.BoundedSemaphore(MAX_CONCURRENT_CLIENTS)

    def process_request(self, request: Any, client_address: Any) -> None:
        if not self._client_slots.acquire(blocking=False):
            # UDPServer passes its shared listening socket in the request
            # tuple; closing it here would permanently stop DNS after a
            # burst. TCP requests carry a distinct accepted client socket.
            if not isinstance(request, tuple):
                request.close()
            logger.warning("dns_filter.client_limit_reached")
            return
        try:
            socketserver.ThreadingMixIn.process_request(self, request, client_address)
        except BaseException:
            self._client_slots.release()
            raise

    def process_request_thread(self, request: Any, client_address: Any) -> None:
        try:
            socketserver.ThreadingMixIn.process_request_thread(self, request, client_address)
        finally:
            self._client_slots.release()


class _UDPServer(_BoundedThreadingMixin, socketserver.UDPServer):
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], settings: tuple[Any, ...]) -> None:
        self.allowed_hosts, self.internal_hosts, self.upstream_ip = settings
        self._init_slots()
        super().__init__(address, _UDPHandler)


class _TCPServer(_BoundedThreadingMixin, socketserver.TCPServer):
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], settings: tuple[Any, ...]) -> None:
        self.allowed_hosts, self.internal_hosts, self.upstream_ip = settings
        self._init_slots()
        super().__init__(address, _TCPHandler)


def main() -> None:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    upstream_ip = os.environ.get("UPSTREAM_DNS_IP", "")
    try:
        upstream = ipaddress.ip_address(upstream_ip)
    except ValueError as exc:
        raise RuntimeError("UPSTREAM_DNS_IP must be the cluster DNS service IP") from exc
    if not upstream.is_private:
        raise RuntimeError("UPSTREAM_DNS_IP must be a private cluster DNS address")

    settings = (
        _normalized_names(os.environ.get("ALLOWED_HOSTS", "")),
        _normalized_names(os.environ.get("INTERNAL_HOSTS", "")),
        upstream_ip,
    )
    if not settings[0] or not settings[1]:
        raise RuntimeError("ALLOWED_HOSTS and INTERNAL_HOSTS must both be configured")

    # Bind only in this pod's network namespace; Kubernetes NetworkPolicy
    # controls which workload can send queries to the in-pod listener.
    udp_server = _UDPServer(("0.0.0.0", DNS_PORT), settings)  # noqa: S104
    tcp_server = _TCPServer(("0.0.0.0", DNS_PORT), settings)  # noqa: S104
    tcp_thread = threading.Thread(target=tcp_server.serve_forever, daemon=True)
    tcp_thread.start()
    try:
        udp_server.serve_forever(poll_interval=0.5)
    finally:
        udp_server.server_close()
        tcp_server.shutdown()
        tcp_server.server_close()


if __name__ == "__main__":
    main()
