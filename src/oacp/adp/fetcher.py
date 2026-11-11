"""Guarded HTTPS fetching of AgentCards.

Everything here is one of the spec's "MUST" items, not a best practice:

* https only, no userinfo, no fragment (``net.normalize_url``);
* the resolved address is validated and then *pinned* into the connection, so
  the name cannot be re-resolved between check and connect (DNS rebinding);
* redirects are followed manually, at most three hops, re-validating each hop;
* connect/total timeouts and a streaming body cap (``maxCardBytes``);
* no cookies, no authorization headers, no environment proxies
  (``trust_env=False``), and TLS verification can never be switched off.
"""

from __future__ import annotations

import ssl
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urljoin

import httpx

from .. import net

DEFAULT_MAX_BYTES = 1 << 20
DEFAULT_CONNECT_TIMEOUT = 5.0
DEFAULT_TOTAL_TIMEOUT = 10.0
DEFAULT_MAX_REDIRECTS = 3
REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})


class FetchError(Exception):
    """A failed fetch, carrying the machine-readable code returned to clients."""

    def __init__(self, code: str, message: str, detail: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.detail = detail or {}


@dataclass(frozen=True)
class Document:
    """A successfully fetched JSON document, stored exactly as received."""

    url: str
    status: int
    body: bytes
    payload: dict[str, Any]
    latency_ms: int


class GuardedFetcher:
    """A fetcher that refuses to talk to anything but public HTTPS hosts."""

    def __init__(
        self,
        *,
        max_bytes: int = DEFAULT_MAX_BYTES,
        connect_timeout: float = DEFAULT_CONNECT_TIMEOUT,
        total_timeout: float = DEFAULT_TOTAL_TIMEOUT,
        max_redirects: int = DEFAULT_MAX_REDIRECTS,
        user_agent: str = "open-agent-control-panel",
        allow_private: bool = False,
        accept: str = "application/json",
    ) -> None:
        self.max_bytes = max_bytes
        self.connect_timeout = connect_timeout
        self.total_timeout = total_timeout
        self.max_redirects = max_redirects
        self.user_agent = user_agent
        # Only ever True for local development; the registry itself keeps it off.
        self.allow_private = allow_private
        # A card fetch wants JSON; the media fetcher wants bytes. Content-negotiating
        # origins answer 406 (or an error document) to the wrong Accept.
        self.accept = accept

    def fetch(self, url: str, *, expect_json: bool = True) -> Document:
        """Fetch ``url`` and return the parsed JSON object.

        Raises:
            FetchError: with ``BAD_URL``, ``CARD_UNREACHABLE``, ``CARD_TIMEOUT``,
                ``TLS_INVALID``, ``CARD_TOO_LARGE`` or ``NOT_AN_AGENT_CARD``.
        """
        try:
            current = net.normalize_url(url, allow_http=self.allow_private)
        except net.UrlError as exc:
            raise FetchError("BAD_URL", str(exc)) from exc

        started = time.monotonic()
        deadline = started + self.total_timeout

        for _hop in range(self.max_redirects + 1):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise FetchError("CARD_TIMEOUT", "fetch exceeded its total time budget")

            host, port = net.host_and_port(current)
            if self.allow_private:
                addresses = _resolve_any(host, port)
            else:
                try:
                    addresses = net.resolve_public_ips(host, port)
                except net.BlockedAddress as exc:
                    raise FetchError("CARD_UNREACHABLE", str(exc)) from exc

            status, headers, body = self._request(current, host, addresses, remaining)

            if status in REDIRECT_STATUSES:
                location = headers.get("location")
                if not location:
                    raise FetchError("CARD_UNREACHABLE", "redirect without a Location header")
                try:
                    current = net.normalize_url(urljoin(current, location))
                except net.UrlError as exc:
                    raise FetchError("CARD_UNREACHABLE", f"invalid redirect target: {exc}") from exc
                continue

            if not 200 <= status < 300:
                raise FetchError(
                    "CARD_UNREACHABLE",
                    f"origin returned HTTP {status}",
                    {"status": status},
                )

            if not expect_json:
                return Document(current, status, body, {}, int((time.monotonic() - started) * 1000))

            try:
                payload = httpx.Response(200, content=body).json()
            except ValueError as exc:
                raise FetchError("NOT_AN_AGENT_CARD", "response body is not valid JSON") from exc
            if not isinstance(payload, dict):
                raise FetchError("NOT_AN_AGENT_CARD", "response body is not a JSON object")

            return Document(
                url=current,
                status=status,
                body=body,
                payload=payload,
                latency_ms=int((time.monotonic() - started) * 1000),
            )

        raise FetchError(
            "CARD_UNREACHABLE",
            f"more than {self.max_redirects} redirects",
            {"maxRedirects": self.max_redirects},
        )

    # ----------------------------------------------------------------- internals

    def _request(
        self,
        url: str,
        host: str,
        addresses: list[str],
        remaining: float,
    ) -> tuple[int, dict[str, str], bytes]:
        pinned_url, host_header, server_hostname = net.pin_target(url, addresses[0])
        headers = {"user-agent": self.user_agent, "accept": self.accept, **host_header}
        timeout = httpx.Timeout(
            max(0.1, remaining),
            connect=min(self.connect_timeout, max(0.1, remaining)),
        )
        try:
            with httpx.Client(
                verify=True,
                follow_redirects=False,
                timeout=timeout,
                headers=headers,
                trust_env=False,
            ) as client:
                with client.stream(
                    "GET",
                    pinned_url,
                    extensions={"sni_hostname": server_hostname},
                ) as response:
                    status = response.status_code
                    if status in REDIRECT_STATUSES:
                        return status, dict(response.headers), b""
                    body = self._read_capped(response)
                    return status, dict(response.headers), body
        except FetchError:
            raise
        except httpx.TimeoutException as exc:
            raise FetchError("CARD_TIMEOUT", "fetch timed out") from exc
        except httpx.ConnectError as exc:
            if _caused_by_ssl(exc):
                raise FetchError("TLS_INVALID", "TLS certificate is not valid") from exc
            raise FetchError("CARD_UNREACHABLE", f"connection failed: {exc}") from exc
        except (httpx.HTTPError, OSError) as exc:
            raise FetchError("CARD_UNREACHABLE", f"fetch failed: {exc}") from exc

    def _read_capped(self, response: httpx.Response) -> bytes:
        chunks: list[bytes] = []
        total = 0
        for chunk in response.iter_bytes():
            total += len(chunk)
            if total > self.max_bytes:
                raise FetchError(
                    "CARD_TOO_LARGE",
                    f"response body exceeds {self.max_bytes} bytes",
                    {"maxCardBytes": self.max_bytes},
                )
            chunks.append(chunk)
        return b"".join(chunks)


def _resolve_any(host: str, port: int) -> list[str]:
    """Resolve without the public-address check (private targets allowed)."""
    import socket

    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise FetchError("CARD_UNREACHABLE", f"DNS resolution failed for {host}: {exc}") from exc
    addresses: list[str] = []
    for info in infos:
        candidate = info[4][0]
        if candidate not in addresses:
            addresses.append(candidate)
    if not addresses:
        raise FetchError("CARD_UNREACHABLE", f"DNS returned no addresses for {host}")
    return addresses


def _caused_by_ssl(exc: BaseException) -> bool:
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, ssl.SSLError):
            return True
        current = current.__cause__ or current.__context__
    return False
