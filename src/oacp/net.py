"""Network guard: URL normalization, public-address checks, pinned connections.

The same rules protect two different code paths:

* the registry fetches an AgentCard from a URL supplied by an anonymous caller;
* the panel calls the A2A endpoint an agent advertises in its card.

Both are requests we make on behalf of somebody else, so both must refuse to
reach loopback, link-local, private and otherwise reserved addresses unless the
operator explicitly opts in (``OACP_ALLOW_PRIVATE_AGENTS``) for local testing.
"""

from __future__ import annotations

import hashlib
import ipaddress
import re
import socket
from urllib.parse import urlsplit, urlunsplit

MAX_URL_LENGTH = 2048

_PERCENT = re.compile(r"%[0-9a-fA-F]{2}")
_FORBIDDEN_CHARS = re.compile(r"[\x00-\x20\x7f]")

_BLOCKED_V4 = tuple(
    ipaddress.ip_network(network)
    for network in (
        "0.0.0.0/8",
        "10.0.0.0/8",
        "100.64.0.0/10",
        "127.0.0.0/8",
        "169.254.0.0/16",
        "172.16.0.0/12",
        "192.0.0.0/24",
        "192.0.2.0/24",
        "192.168.0.0/16",
        "198.18.0.0/15",
        "198.51.100.0/24",
        "203.0.113.0/24",
        "224.0.0.0/4",
        "240.0.0.0/4",
        "255.255.255.255/32",
    )
)

_BLOCKED_V6 = tuple(
    ipaddress.ip_network(network)
    for network in (
        "::/128",
        "::1/128",
        "::ffff:0:0/96",
        "2001:db8::/32",
        "fc00::/7",
        "fe80::/10",
        "ff00::/8",
    )
)


class UrlError(ValueError):
    """The URL does not satisfy the fetch preconditions (``400 BAD_URL``)."""


class BlockedAddress(Exception):
    """The target resolves to a private, reserved or loopback address."""


# --------------------------------------------------------------------- addresses


def is_public_ip(value: str) -> bool:
    """Return True when the address is globally routable for our purposes.

    IPv4-mapped IPv6 addresses are judged by the IPv4 address they carry, so
    ``::ffff:127.0.0.1`` is blocked just like ``127.0.0.1``.
    """
    address = ipaddress.ip_address(str(value).split("%", 1)[0])
    mapped = getattr(address, "ipv4_mapped", None)
    if mapped is not None:
        address = mapped
    if address.version == 4:
        return not any(address in network for network in _BLOCKED_V4)
    return not any(address in network for network in _BLOCKED_V6)


def resolve_public_ips(host: str, port: int, *, resolver=socket.getaddrinfo) -> list[str]:
    """Resolve ``host`` and reject the request if **any** answer is non-public.

    Mixed answers are refused outright instead of being filtered down to the
    public ones: a name that resolves to both a public address and a private one
    is itself the signal we are looking for (DNS rebinding).
    """
    try:
        infos = resolver(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise BlockedAddress(f"DNS resolution failed for {host}: {exc}") from exc

    addresses: list[str] = []
    for info in infos:
        candidate = info[4][0]
        if candidate not in addresses:
            addresses.append(candidate)
    if not addresses:
        raise BlockedAddress(f"DNS returned no addresses for {host}")

    blocked = [address for address in addresses if not is_public_ip(address)]
    if blocked:
        raise BlockedAddress(f"{host} resolves to a restricted address ({blocked[0]})")
    return addresses


# --------------------------------------------------------------------------- URLs


def normalize_url(url: str, *, allow_http: bool = False) -> str:
    """Validate a card URL and return its canonical form (ADP Core §4.1).

    ``allow_http`` exists for local development only; with it enabled the
    registry is no longer ADP-compliant, so production deployments keep it off.

    Normalization does exactly three things and nothing else: lower-case the
    scheme and host, drop the default ``:443`` port, and upper-case percent
    escapes. Any semantic rewrite (adding a trailing slash, resolving ``..``,
    reordering query parameters) is deliberately **not** performed: the stored
    URL is what the probe re-fetches, so two spellings of one endpoint should not
    turn into two entries.
    """
    if not isinstance(url, str):
        raise UrlError("url must be a string")
    candidate = url.strip()
    if not candidate:
        raise UrlError("url must not be empty")
    if len(candidate) > MAX_URL_LENGTH:
        raise UrlError(f"url exceeds {MAX_URL_LENGTH} characters")
    if _FORBIDDEN_CHARS.search(candidate):
        raise UrlError("url must not contain whitespace or control characters")

    parts = urlsplit(candidate)
    scheme = parts.scheme.lower()
    if scheme != "https" and not (allow_http and scheme == "http"):
        raise UrlError("url must use https")
    if parts.username or parts.password:
        raise UrlError("url must not contain userinfo")
    if parts.fragment:
        raise UrlError("url must not contain a fragment")

    host = parts.hostname
    if not host:
        raise UrlError("url is missing a host")
    if not host.isascii():
        raise UrlError("url host must be ASCII (punycode the domain first)")
    try:
        port = parts.port
    except ValueError as exc:
        raise UrlError("url has an invalid port") from exc

    host = host.lower()
    if ":" in host:
        host = f"[{host}]"
    netloc = host if port in (None, 443) else f"{host}:{port}"

    return urlunsplit((scheme, netloc, _upper_percent(parts.path), _upper_percent(parts.query), ""))


def _upper_percent(text: str) -> str:
    """Upper-case the hex digits of percent escapes (``%2f`` -> ``%2F``)."""
    return _PERCENT.sub(lambda match: match.group(0).upper(), text)


def derive_id(normalized_url: str) -> str:
    """``a_`` + the first 16 hex characters of ``sha256(normalised url)``.

    This is the RECOMMENDED derivation of ADP §4.1, not a rule: §4.1 leaves the
    algorithm to the implementation and fixes four properties instead (URL-safe,
    stable within an entry, at most one entry per url, opaque to consumers). We pick
    this one because it makes "one url, one row" structural — concurrent
    registrations land on the same id with no uniqueness table — and because any
    implementation can recompute it. The price is 64 bits, which is why the schema
    4 → 5 migration refuses to merge two rows that collide (§10).

    Consumers MUST NOT parse it: it is a handle for one entry, not an identity. The
    A2A signature says who an agent is, and two registries may give one url
    different ids, so it must never be a cross-registry correlation key.
    """
    digest = hashlib.sha256(normalized_url.encode("utf-8")).hexdigest()
    return "a_" + digest[:16]


def host_and_port(url: str) -> tuple[str, int]:
    """Split an absolute URL into its hostname and effective port."""
    parts = urlsplit(url)
    host = parts.hostname or ""
    port = parts.port or (443 if parts.scheme.lower() == "https" else 80)
    return host, port


def host_of(url: str) -> str:
    """The lower-case hostname of an already-normalized URL."""
    return (urlsplit(url).hostname or "").lower()


def validate_endpoint(url: str, *, allow_private: bool) -> str:
    """Validate an A2A endpoint URL advertised in an agent card.

    Endpoints are used verbatim (their path is meaningful), so this performs
    checks only. ``http://`` is accepted solely when private targets are allowed,
    which is what makes local agent development possible.
    """
    if not isinstance(url, str) or not url.strip():
        raise UrlError("endpoint url must be a non-empty string")
    candidate = url.strip()
    parts = urlsplit(candidate)
    scheme = parts.scheme.lower()
    allowed_schemes = {"https", "http"} if allow_private else {"https"}
    if scheme not in allowed_schemes:
        raise UrlError(f"endpoint must use {' or '.join(sorted(allowed_schemes))}")
    if parts.username or parts.password:
        raise UrlError("endpoint must not contain userinfo")
    if parts.fragment:
        raise UrlError("endpoint must not contain a fragment")
    if not parts.hostname:
        raise UrlError("endpoint is missing a host")
    if not parts.hostname.isascii():
        raise UrlError("endpoint host must be ASCII")
    return candidate


def pin_target(url: str, address: str) -> tuple[str, dict[str, str], str]:
    """Point a request at a specific resolved address without losing the host.

    Returns ``(pinned_url, headers, server_hostname)``:

    * the URL's host is replaced by ``address`` so the socket cannot be
      re-resolved (no DNS rebinding window);
    * ``Host`` keeps the original hostname (and the port when non-default);
    * ``server_hostname`` must be handed to the HTTP client as the TLS SNI name
      so certificate verification still checks the certificate for the name we
      validated, not for the IP.
    """
    parts = urlsplit(url)
    host = parts.hostname or ""
    port = parts.port or (443 if parts.scheme.lower() == "https" else 80)
    literal = f"[{address}]" if ":" in address else address
    netloc = literal if (port == 443 and parts.scheme.lower() == "https") or (
        port == 80 and parts.scheme.lower() == "http"
    ) else f"{literal}:{port}"
    pinned = urlunsplit((parts.scheme, netloc, parts.path, parts.query, ""))
    host_header = host if netloc == literal else f"{host}:{port}"
    return pinned, {"host": host_header}, host
