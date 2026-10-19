from __future__ import annotations

import socket

import pytest

from deploy.egress_proxy import (
    allowed_hosts_from_env,
    parse_client_hello_server_name,
    read_client_hello,
    resolve_allowed_destination,
)


def _client_hello(host: str, *, extension_type: int = 0) -> bytes:
    host_bytes = host.encode("ascii")
    server_name = b"\x00" + len(host_bytes).to_bytes(2, "big") + host_bytes
    server_names = len(server_name).to_bytes(2, "big") + server_name
    extension = (
        extension_type.to_bytes(2, "big") + len(server_names).to_bytes(2, "big") + server_names
    )
    body = (
        b"\x03\x03"
        + bytes(32)
        + b"\x00"
        + b"\x00\x02\x13\x01"
        + b"\x01\x00"
        + len(extension).to_bytes(2, "big")
        + extension
    )
    return b"\x01" + len(body).to_bytes(3, "big") + body


def _tls_record(payload: bytes) -> bytes:
    return b"\x16\x03\x01" + len(payload).to_bytes(2, "big") + payload


def test_proxy_extracts_exact_sni_from_client_hello() -> None:
    assert parse_client_hello_server_name(_client_hello("api.github.com")) == "api.github.com"


def test_proxy_rejects_malformed_or_missing_client_hello_sni() -> None:
    hello = _client_hello("api.github.com")
    with pytest.raises(ValueError, match="invalid or oversized"):
        parse_client_hello_server_name(hello[:-1])
    with pytest.raises(ValueError, match="valid DNS hostname"):
        parse_client_hello_server_name(_client_hello("*.github.com"))
    with pytest.raises(ValueError, match="encrypted TLS ClientHello"):
        parse_client_hello_server_name(_client_hello("api.github.com", extension_type=0xFE0D))


def test_proxy_reads_client_hello_split_across_tls_records() -> None:
    left, right = socket.socketpair()
    try:
        hello = _client_hello("api.github.com")
        first = _tls_record(hello[:2])
        second = _tls_record(hello[2:])
        left.sendall(first + second)
        server_name, wire_bytes = read_client_hello(right)
        assert server_name == "api.github.com"
        assert wire_bytes == first + second
    finally:
        left.close()
        right.close()


def test_proxy_requires_exact_non_wildcard_allowlist(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ALLOWED_HOSTS", "api.github.com, api.anthropic.com")
    assert allowed_hosts_from_env() == frozenset({"api.github.com", "api.anthropic.com"})

    monkeypatch.setenv("ALLOWED_HOSTS", "*.github.com")
    with pytest.raises(RuntimeError, match="exact hostnames"):
        allowed_hosts_from_env()


@pytest.mark.parametrize(
    ("authority", "allowed"),
    [
        ("evil.api.github.com:443", ["api.github.com"]),
        ("api.github.com:80", ["api.github.com"]),
        ("127.0.0.1:443", ["127.0.0.1"]),
        ("api.github.com@127.0.0.1:443", ["api.github.com"]),
    ],
)
def test_proxy_rejects_unlisted_hosts_and_unsafe_authorities(
    monkeypatch: pytest.MonkeyPatch,
    authority: str,
    allowed: list[str],
) -> None:
    monkeypatch.setattr(
        "deploy.egress_proxy.socket.getaddrinfo",
        lambda *_args, **_kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))
        ],
    )
    with pytest.raises(ValueError):
        resolve_allowed_destination(authority, allowed_hosts=allowed)


@pytest.mark.parametrize("address", ["127.0.0.1", "10.0.0.2", "169.254.169.254"])
def test_proxy_rejects_private_and_metadata_addresses(
    monkeypatch: pytest.MonkeyPatch,
    address: str,
) -> None:
    monkeypatch.setattr(
        "deploy.egress_proxy.socket.getaddrinfo",
        lambda *_args, **_kwargs: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 443))],
    )
    with pytest.raises(ValueError, match="non-public"):
        resolve_allowed_destination("api.github.com:443", allowed_hosts=["api.github.com"])


def test_proxy_rejects_mixed_public_and_private_dns_answers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "deploy.egress_proxy.socket.getaddrinfo",
        lambda *_args, **_kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.2", 443)),
        ],
    )
    with pytest.raises(ValueError, match="non-public"):
        resolve_allowed_destination("api.github.com:443", allowed_hosts=["api.github.com"])


def test_proxy_returns_dns_addresses_for_a_valid_exact_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected = (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))
    monkeypatch.setattr(
        "deploy.egress_proxy.socket.getaddrinfo",
        lambda *_args, **_kwargs: [expected],
    )
    assert resolve_allowed_destination("api.github.com:443", allowed_hosts=["api.github.com"]) == [
        expected
    ]
