"""URL normalization, address classification and connection pinning."""

from __future__ import annotations

import pytest

from oacp import net


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("HTTPS://Geo.Example.com/Card.json", "https://geo.example.com/Card.json"),
        ("https://geo.example.com:443/a", "https://geo.example.com/a"),
        ("https://geo.example.com/%2fcard", "https://geo.example.com/%2Fcard"),
        ("https://geo.example.com/a?x=%2f", "https://geo.example.com/a?x=%2F"),
        ("https://geo.example.com/a/", "https://geo.example.com/a/"),
        ("https://geo.example.com:8443/a", "https://geo.example.com:8443/a"),
    ],
)
def test_normalize_url_rewrites_only_the_three_allowed_things(raw: str, expected: str) -> None:
    assert net.normalize_url(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "http://geo.example.com/card",
        "https://user:pw@geo.example.com/card",
        "https://geo.example.com/card#frag",
        "https://例子.测试/card",
        "https:///card",
        "",
        "https://geo.example.com/卡 片",
    ],
)
def test_normalize_url_rejects_invalid_input(raw: str) -> None:
    with pytest.raises(net.UrlError):
        net.normalize_url(raw)


def test_normalize_url_keeps_trailing_slash_distinct() -> None:
    assert net.normalize_url("https://a.example/card") != net.normalize_url("https://a.example/card/")


@pytest.mark.parametrize(
    "address, expected",
    [
        ("127.0.0.1", False),
        ("10.1.2.3", False),
        ("169.254.169.254", False),
        ("192.168.1.1", False),
        ("100.64.0.1", False),
        ("8.8.8.8", True),
        ("::1", False),
        ("::ffff:127.0.0.1", False),
        ("2001:db8::1", False),
        ("2606:4700:4700::1111", True),
    ],
)
def test_is_public_ip(address: str, expected: bool) -> None:
    assert net.is_public_ip(address) is expected


def test_resolve_rejects_mixed_answers() -> None:
    def resolver(host, port, **kwargs):
        return [
            (2, 1, 6, "", ("93.184.216.34", port)),
            (2, 1, 6, "", ("127.0.0.1", port)),
        ]

    with pytest.raises(net.BlockedAddress):
        net.resolve_public_ips("agent.example", 443, resolver=resolver)


def test_pin_target_keeps_host_header_and_sni() -> None:
    url, headers, sni = net.pin_target("https://agent.example:8443/a2a?x=1", "93.184.216.34")
    assert url == "https://93.184.216.34:8443/a2a?x=1"
    assert headers == {"host": "agent.example:8443"}
    assert sni == "agent.example"


def test_pin_target_ipv6_literal() -> None:
    url, headers, sni = net.pin_target("https://agent.example/a2a", "2606:4700::1")
    assert url == "https://[2606:4700::1]/a2a"
    assert headers == {"host": "agent.example"}
    assert sni == "agent.example"


@pytest.mark.parametrize(
    "endpoint, allow_private, accepted",
    [
        ("https://agent.example/a2a", False, True),
        ("https://agent.example/a2a", True, True),
        ("http://127.0.0.1:9000/a2a", False, False),
        ("http://127.0.0.1:9000/a2a", True, True),
        ("ftp://agent.example/a2a", True, False),
    ],
)
def test_validate_endpoint(endpoint: str, allow_private: bool, accepted: bool) -> None:
    if accepted:
        assert net.validate_endpoint(endpoint, allow_private=allow_private) == endpoint
    else:
        with pytest.raises(net.UrlError):
            net.validate_endpoint(endpoint, allow_private=allow_private)
