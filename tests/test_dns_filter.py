from __future__ import annotations

import struct

from deploy.dns_filter import _UDPServer, query_is_allowed, refused_response, sanitized_query


def _query(name: str, query_type: int = 1) -> bytes:
    labels = b"".join(bytes([len(label)]) + label.encode() for label in name.split("."))
    return (
        struct.pack("!HHHHHH", 0x1234, 0x0100, 1, 0, 0, 0)
        + labels
        + b"\x00"
        + struct.pack("!HH", query_type, 1)
    )


def test_dns_filter_allows_exact_external_hosts_and_cluster_services() -> None:
    allow = frozenset({"api.github.com", "api.anthropic.com"})
    internal = frozenset({"selfheal-mcp", "selfheal-mcp.default.svc.cluster.local"})
    assert query_is_allowed(_query("api.github.com"), allowed_hosts=allow, internal_hosts=internal)
    assert query_is_allowed(
        _query("selfheal-mcp.default.svc.cluster.local"),
        allowed_hosts=allow,
        internal_hosts=internal,
    )
    assert query_is_allowed(
        _query("selfheal-mcp.default.svc.cluster.local"),
        allowed_hosts=allow,
        internal_hosts=internal,
    )


def test_dns_filter_rejects_unlisted_names_and_query_types() -> None:
    allow = frozenset({"api.github.com"})
    internal = frozenset({"selfheal-mcp"})
    assert not query_is_allowed(
        _query("secret.example.com"), allowed_hosts=allow, internal_hosts=internal
    )
    assert not query_is_allowed(
        _query("private.api.github.com"), allowed_hosts=allow, internal_hosts=internal
    )
    assert not query_is_allowed(
        _query("unlisted.default.svc.cluster.local"),
        allowed_hosts=allow,
        internal_hosts=internal,
    )
    assert not query_is_allowed(
        _query("api.github.com", query_type=16),
        allowed_hosts=allow,
        internal_hosts=internal,
    )


def test_dns_filter_returns_refused_for_an_unlisted_hostname() -> None:
    response = refused_response(_query("secret.example.com"))
    transaction_id, flags, questions, answers = struct.unpack_from("!HHHH", response)
    assert transaction_id == 0x1234
    assert flags & 0x8000
    assert flags & 0x000F == 5
    assert questions == 1
    assert answers == 0


def test_dns_filter_removes_edns_data_before_forwarding() -> None:
    packet = bytearray(_query("api.github.com"))
    struct.pack_into("!H", packet, 10, 1)
    packet.extend(b"\x00" + struct.pack("!HHIH", 41, 1232, 0, 4) + b"SECR")

    forwarded = sanitized_query(bytes(packet))

    assert struct.unpack_from("!H", forwarded, 10)[0] == 0
    assert b"SECR" not in forwarded
    assert forwarded == _query("api.github.com")


def test_udp_capacity_limit_does_not_close_the_shared_listener() -> None:
    server = _UDPServer(
        ("127.0.0.1", 0),
        (frozenset({"api.github.com"}), frozenset({"selfheal-mcp"}), "10.0.0.10"),
    )
    try:
        for _ in range(64):
            assert server._client_slots.acquire(blocking=False)
        server.process_request((b"", server.socket), ("127.0.0.1", 53000))
        assert server.socket.fileno() >= 0
    finally:
        for _ in range(64):
            server._client_slots.release()
        server.server_close()
