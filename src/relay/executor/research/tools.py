"""Read-only research tools: web search and URL fetching.

Each tool body runs as a DBOS step, so inside the research workflow its result is checkpointed
and a recovered run replays it instead of hitting the network again. Deliberately absent: any
tool that sends, posts or publishes anything.
"""

from __future__ import annotations

import asyncio
import socket
from ipaddress import IPv4Address, IPv6Address, ip_address, ip_network
from typing import Any
from urllib.parse import urlsplit

import httpx
import trafilatura
from dbos import DBOS

FETCH_TIMEOUT_S = 15.0
FETCH_MAX_BYTES = 2_000_000
FETCH_MAX_CHARS = 12_000
FETCH_MAX_REDIRECTS = 5
SEARCH_MAX_RESULTS = 5
_USER_AGENT = "relay-research/0.1 (+https://github.com/; read-only research agent)"


class FetchError(RuntimeError):
    """A URL could not be fetched within the tool's limits."""


def _truncate(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    return text[:max_chars].rstrip() + f"\n\n[truncated at {max_chars} characters]"


async def _resolve_host(host: str, port: int) -> list[str]:
    """Every address ``host`` resolves to (A and AAAA). Tests replace this."""
    infos = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return sorted({str(info[4][0]) for info in infos})


def _is_public(ip: IPv4Address | IPv6Address) -> bool:
    if isinstance(ip, IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    # is_global is False for loopback, RFC 1918, link-local (incl. 169.254.169.254 cloud
    # metadata), CGNAT 100.64/10, unspecified and reserved ranges; multicast is checked apart.
    return ip.is_global and not ip.is_multicast and ip not in _CGNAT


_CGNAT = ip_network("100.64.0.0/10")


async def _check_public_url(url: str) -> None:
    """Refuse anything but http(s) URLs whose host resolves only to public addresses."""
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise FetchError("only absolute http:// or https:// URLs can be fetched")
    host = parts.hostname.rstrip(".").lower()
    if host == "localhost" or host.endswith(".localhost"):
        raise FetchError(f"refusing to fetch non-public host {host!r}")
    try:
        addresses = [ip_address(host)]
    except ValueError:
        try:
            port = parts.port or (443 if parts.scheme == "https" else 80)
            resolved = await _resolve_host(host, port)
        except (OSError, ValueError) as exc:
            raise FetchError(f"could not resolve {host!r}: {exc}") from exc
        addresses = [ip_address(a.split("%", 1)[0]) for a in resolved]
    if not addresses or not all(_is_public(a) for a in addresses):
        raise FetchError(f"refusing to fetch non-public host {host!r}")


async def fetch_url_text(
    url: str,
    *,
    client: httpx.AsyncClient | None = None,
    timeout: float = FETCH_TIMEOUT_S,
    max_bytes: int = FETCH_MAX_BYTES,
    max_chars: int = FETCH_MAX_CHARS,
    max_redirects: int = FETCH_MAX_REDIRECTS,
) -> str:
    """GET ``url`` and return its readable text (trafilatura for HTML), truncated.

    Raises ``FetchError`` for non-http(s) URLs, hosts resolving to loopback / private /
    link-local / CGNAT / multicast / unspecified / metadata addresses (re-checked on every
    redirect hop, followed manually), HTTP errors, bodies over ``max_bytes`` (checked while
    streaming, not only via Content-Length) and requests exceeding ``timeout`` seconds in total
    (a slow-drip body cannot outlive it).

    Residual risk: the check resolves the name separately from httpx's own connect, so a
    DNS-rebinding host could still flip to a private address in between.
    """
    own_client = client is None
    http = client or httpx.AsyncClient(follow_redirects=False, headers={"User-Agent": _USER_AGENT})
    current = url
    try:
        async with asyncio.timeout(timeout):
            for _hop in range(max_redirects + 1):
                await _check_public_url(current)
                async with http.stream(
                    "GET", current, timeout=timeout, follow_redirects=False
                ) as resp:
                    location = resp.headers.get("location")
                    if resp.is_redirect and location:
                        current = str(resp.url.join(location))
                        continue
                    content_type, raw = await _read_body(resp, max_bytes)
                    break
            else:
                raise FetchError(f"more than {max_redirects} redirects")
    except TimeoutError as exc:
        raise FetchError(f"timed out after {timeout:g} s") from exc
    except httpx.TimeoutException as exc:
        raise FetchError(f"timed out after {timeout:g} s") from exc
    except httpx.HTTPError as exc:
        raise FetchError(f"{type(exc).__name__}: {exc}") from exc
    finally:
        if own_client:
            await http.aclose()

    head = raw.lstrip()[:15].lower()
    looks_html = "html" in content_type or head.startswith(("<!doctype", "<html"))
    text = trafilatura.extract(raw, include_comments=False) if looks_html else raw
    if not text or not text.strip():
        raise FetchError("no readable text found")
    return _truncate(text.strip(), max_chars)


async def _read_body(resp: httpx.Response, max_bytes: int) -> tuple[str, str]:
    if resp.status_code >= 400:
        raise FetchError(f"HTTP {resp.status_code}")
    declared = resp.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > max_bytes:
        raise FetchError(f"response larger than {max_bytes} bytes")
    body = bytearray()
    async for chunk in resp.aiter_bytes():
        body += chunk
        if len(body) > max_bytes:
            raise FetchError(f"response larger than {max_bytes} bytes")
    content_type = resp.headers.get("content-type", "").lower()
    return content_type, bytes(body).decode(resp.charset_encoding or "utf-8", errors="replace")


@DBOS.step(name="relay.research.fetch_url")
async def _fetch_url_step(url: str) -> str:
    try:
        return await fetch_url_text(url)
    except FetchError as exc:
        return f"ERROR: could not fetch {url}: {exc}"


@DBOS.step(name="relay.research.web_search")
async def _web_search_step(query: str) -> list[dict[str, Any]] | str:
    from pydantic_ai.common_tools.duckduckgo import duckduckgo_search_tool

    search = duckduckgo_search_tool(max_results=SEARCH_MAX_RESULTS).function
    try:
        results = await search(query)
    except Exception as exc:  # network down, rate limited: tell the model, don't fail the task
        return f"ERROR: web search failed: {type(exc).__name__}: {exc}"
    return [dict(r) for r in results]


async def web_search(query: str) -> list[dict[str, Any]] | str:
    """Search the web (DuckDuckGo). Returns results with title, href and body snippet.

    Args:
        query: The search query.
    """
    return await _web_search_step(query)


async def fetch_url(url: str) -> str:
    """Fetch a web page (http/https only) and return its readable text, truncated.

    Args:
        url: Absolute http(s) URL to read, usually one returned by web_search.
    """
    return await _fetch_url_step(url)
