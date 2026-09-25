"""Live search, bounded HTTP retrieval, and URL safety."""

from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import socket
from dataclasses import dataclass
from typing import Literal
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx

from XBotv2.core import ToolFailed, ToolOutcome, failed_text, succeeded_text
from XBotv2.sandbox.contracts import SandboxPort
from XBotv2.browser.contracts import NetworkPolicy


_REDIRECT_STATUSES = {301, 302, 303, 307, 308}
_TEXT_TYPES = ("text/", "application/json", "application/xml")
_PROXY_HEADER_LIMIT = 64 * 1024
_PROXY_COPY_SIZE = 64 * 1024
logger = logging.getLogger("xbotv2.browser.network")


@dataclass(frozen=True, slots=True)
class CheckedUrl:
    request_url: str
    connection_url: str
    host_header: str
    tls_server_name: str


async def validate_url(url: str, policy: NetworkPolicy) -> CheckedUrl:
    """Validate a URL and pin its connection to the checked DNS answer."""
    request_url = url.strip()
    parsed = urlsplit(request_url)
    if parsed.scheme not in {"http", "https"}:
        raise ValueError("URL scheme must be http or https")
    if not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("URL must contain a hostname and no credentials")

    hostname = parsed.hostname.encode("idna").decode("ascii")
    port = parsed.port
    port_suffix = f":{port}" if port is not None else ""
    header_host = f"[{hostname}]" if ":" in hostname else hostname
    host_header = f"{header_host}{port_suffix}"
    try:
        addresses = {ipaddress.ip_address(hostname)}
    except ValueError:
        addresses = await asyncio.to_thread(_resolve_addresses, hostname)
    if not addresses:
        raise ValueError("URL hostname has no resolved addresses")
    if not policy.private_access and any(
        not address.is_global for address in addresses
    ):
        raise ValueError("Private, local, or non-routable destinations are blocked")

    # Resolve once, validate every answer, and connect to one of those exact
    # addresses. The original authority remains on the HTTP/TLS request.
    address = min(addresses, key=lambda item: (item.version, int(item)))
    connection_host = f"[{address.compressed}]" if address.version == 6 else str(address)
    connection_url = urlunsplit((
        parsed.scheme,
        f"{connection_host}{port_suffix}",
        parsed.path,
        parsed.query,
        parsed.fragment,
    ))
    return CheckedUrl(
        request_url=request_url,
        connection_url=connection_url,
        host_header=host_header,
        tls_server_name=hostname,
    )


class BrowserProxy:
    """Pin Chromium's HTTP and HTTPS connections to policy-checked DNS."""

    def __init__(self, policy: NetworkPolicy, *, network_enabled: bool) -> None:
        self._policy = policy
        self._network_enabled = network_enabled
        self._server: asyncio.Server | None = None
        self._port: int | None = None
        self._writers: set[asyncio.StreamWriter] = set()

    async def start(self) -> str:
        if self._server is not None:
            raise RuntimeError("Browser proxy is already running")
        self._server = await asyncio.start_server(
            self._handle,
            host="127.0.0.1",
            port=0,
            limit=_PROXY_HEADER_LIMIT,
        )
        address = self._server.sockets[0].getsockname()
        self._port = address[1]
        return f"http://127.0.0.1:{self._port}"

    async def close(self) -> None:
        server = self._server
        if server is None:
            return
        self._server = None
        self._port = None
        server.close()
        for writer in tuple(self._writers):
            writer.close()
        await server.wait_closed()
        if self._writers:
            await asyncio.gather(
                *(writer.wait_closed() for writer in tuple(self._writers)),
                return_exceptions=True,
            )
        self._writers.clear()

    async def _handle(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        self._writers.add(writer)
        try:
            head = await reader.readuntil(b"\r\n\r\n")
            if len(head) > _PROXY_HEADER_LIMIT:
                raise ValueError("Proxy request headers exceed the size limit")
            request_line, *header_lines = head[:-4].split(b"\r\n")
            method, target, version = request_line.decode("latin-1").split(" ", 2)
            headers = [self._parse_header(line) for line in header_lines]
            if method.upper() == "CONNECT":
                await self._tunnel(target, reader, writer)
            else:
                await self._forward_http(
                    method,
                    target,
                    version,
                    headers,
                    reader,
                    writer,
                )
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        except ValueError:
            # A policy denial must fail the navigation, not become a readable
            # HTTP page that BrowserSession could report as a successful open.
            pass
        except Exception:
            logger.exception("browser.proxy.request.failed")
            await self._error_response(writer, 502, "Browser network proxy failed")
        finally:
            self._writers.discard(writer)
            writer.close()
            try:
                await writer.wait_closed()
            except ConnectionError:
                pass

    async def _tunnel(
        self,
        authority: str,
        client_reader: asyncio.StreamReader,
        client_writer: asyncio.StreamWriter,
    ) -> None:
        _checked, remote_reader, remote_writer = await self._connect(
            f"https://{authority}/"
        )
        client_writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
        await client_writer.drain()
        try:
            await asyncio.gather(
                self._pipe(client_reader, remote_writer),
                self._pipe(remote_reader, client_writer),
                return_exceptions=True,
            )
        finally:
            remote_writer.close()
            try:
                await remote_writer.wait_closed()
            except ConnectionError:
                pass

    async def _forward_http(
        self,
        method: str,
        target: str,
        version: str,
        headers: list[tuple[str, str]],
        client_reader: asyncio.StreamReader,
        client_writer: asyncio.StreamWriter,
    ) -> None:
        checked, remote_reader, remote_writer = await self._connect(target)
        try:
            request = urlsplit(checked.request_url)
            path = request.path or "/"
            if request.query:
                path += f"?{request.query}"
            forwarded_headers = [
                (name, value)
                for name, value in headers
                if name.lower() not in {
                    "connection",
                    "host",
                    "keep-alive",
                    "proxy-authorization",
                    "proxy-connection",
                }
            ]
            forwarded_headers.extend([
                ("Host", checked.host_header),
                ("Connection", "close"),
            ])
            header = [f"{method} {path} {version}\r\n"]
            header.extend(f"{name}: {value}\r\n" for name, value in forwarded_headers)
            remote_writer.write(("".join(header) + "\r\n").encode("latin-1"))
            await remote_writer.drain()

            content_lengths = [
                value.strip()
                for name, value in headers
                if name.lower() == "content-length"
            ]
            transfer_encodings = [
                value.strip().lower()
                for name, value in headers
                if name.lower() == "transfer-encoding"
            ]
            if content_lengths and transfer_encodings:
                raise ValueError("Conflicting HTTP body framing headers")
            if content_lengths:
                lengths = {int(value) for value in content_lengths}
                if len(lengths) != 1 or next(iter(lengths)) < 0:
                    raise ValueError("Invalid HTTP Content-Length")
                await self._copy_exactly(
                    client_reader,
                    remote_writer,
                    next(iter(lengths)),
                )
            elif transfer_encodings:
                if transfer_encodings != ["chunked"]:
                    raise ValueError("Unsupported HTTP transfer encoding")
                await self._copy_chunked(client_reader, remote_writer)
            await remote_writer.drain()

            while chunk := await asyncio.wait_for(
                remote_reader.read(_PROXY_COPY_SIZE),
                timeout=self._policy.timeout,
            ):
                client_writer.write(chunk)
                await client_writer.drain()
        finally:
            remote_writer.close()
            try:
                await remote_writer.wait_closed()
            except ConnectionError:
                pass

    async def _connect(
        self,
        url: str,
    ) -> tuple[CheckedUrl, asyncio.StreamReader, asyncio.StreamWriter]:
        if not self._network_enabled:
            raise ValueError("Network access is disabled by the active sandbox policy")
        checked = await validate_url(url, self._policy)
        target = urlsplit(checked.connection_url)
        port = target.port or (443 if target.scheme == "https" else 80)
        if port == self._port and ipaddress.ip_address(target.hostname).is_loopback:
            raise ValueError("Browser proxy cannot connect to itself")
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(target.hostname, port),
            timeout=self._policy.timeout,
        )
        return checked, reader, writer

    @staticmethod
    def _parse_header(line: bytes) -> tuple[str, str]:
        name, separator, value = line.decode("latin-1").partition(":")
        if not separator or not name.strip():
            raise ValueError("Malformed HTTP proxy header")
        return name.strip(), value.strip()

    @staticmethod
    async def _copy_exactly(
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        length: int,
    ) -> None:
        remaining = length
        while remaining:
            chunk = await reader.readexactly(min(remaining, _PROXY_COPY_SIZE))
            writer.write(chunk)
            remaining -= len(chunk)

    @classmethod
    async def _copy_chunked(
        cls,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        while True:
            line = await reader.readline()
            writer.write(line)
            size = int(line.split(b";", 1)[0].strip(), 16)
            if size == 0:
                while trailer := await reader.readline():
                    writer.write(trailer)
                    if trailer == b"\r\n":
                        break
                return
            await cls._copy_exactly(reader, writer, size + 2)

    @staticmethod
    async def _pipe(
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        while chunk := await reader.read(_PROXY_COPY_SIZE):
            writer.write(chunk)
            await writer.drain()
        if writer.can_write_eof():
            writer.write_eof()
            await writer.drain()

    @staticmethod
    async def _error_response(
        writer: asyncio.StreamWriter,
        status: int,
        message: str,
    ) -> None:
        body = message.encode("utf-8", errors="replace")
        response = (
            f"HTTP/1.1 {status} Error\r\n"
            "Connection: close\r\n"
            "Content-Type: text/plain; charset=utf-8\r\n"
            f"Content-Length: {len(body)}\r\n\r\n"
        ).encode("ascii") + body
        try:
            writer.write(response)
            await writer.drain()
        except ConnectionError:
            pass


class WebAccess:
    """Own the HTTP client used by read-only Web tools."""

    def __init__(self, policy: NetworkPolicy) -> None:
        self.policy = policy
        self._client = httpx.AsyncClient(
            timeout=policy.timeout,
            follow_redirects=False,
            headers={"User-Agent": "XBotv2/0.2 web research tool"},
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def search(
        self,
        query: str,
        *,
        max_results: int,
        freshness: Literal["day", "week", "month", "year"] | None,
        backend: str,
        region: str,
        safesearch: str,
    ) -> ToolOutcome:
        query = query.strip()
        if not query:
            return failed_text("invalid_query", "Search query must not be empty")
        limit = min(max(max_results, 1), 10)
        timelimit = {
            "day": "d",
            "week": "w",
            "month": "m",
            "year": "y",
        }.get(freshness)
        try:
            from ddgs import DDGS

            results = await asyncio.to_thread(
                DDGS(timeout=max(1, int(self.policy.timeout))).text,
                query,
                region=region,
                safesearch=safesearch,
                timelimit=timelimit,
                max_results=limit,
                backend=backend,
            )
        except Exception as exc:
            return failed_text("search_failed", f"Web search failed: {exc}")
        normalized = [
            {
                "title": str(item.get("title") or ""),
                "url": str(item.get("href") or item.get("url") or ""),
                "snippet": str(item.get("body") or item.get("snippet") or ""),
                **({"date": str(item["date"])} if item.get("date") else {}),
            }
            for item in results or []
            if isinstance(item, dict) and (item.get("href") or item.get("url"))
        ]
        lines = ["[Untrusted live Web search results]"]
        for index, item in enumerate(normalized, 1):
            lines.extend([
                f"{index}. {item['title']}",
                f"   URL: {item['url']}",
                f"   {item['snippet']}",
            ])
        if not normalized:
            lines.append("No results.")
        payload = json.dumps({"query": query, "results": normalized, "untrusted": True}, ensure_ascii=False)
        return succeeded_text(
            "\n".join(lines) + "\n\n" + payload,
        )

    async def fetch(self, url: str) -> ToolOutcome:
        try:
            final_url, response, body = await self._download(url)
            content_type = response.headers.get("content-type", "").split(";", 1)[0]
            if content_type == "text/html" or not content_type:
                content, metadata = await asyncio.to_thread(
                    _extract_html, body, final_url
                )
            elif content_type.startswith(_TEXT_TYPES):
                content = _decode(body, response.encoding)
                metadata = {}
                if content_type == "application/json":
                    try:
                        content = json.dumps(
                            json.loads(content), ensure_ascii=False, indent=2
                        )
                    except json.JSONDecodeError:
                        pass
            else:
                return failed_text(
                    "unsupported_content_type",
                    f"Unsupported Web content type: {content_type or 'unknown'}",
                )
        except Exception as exc:
            return failed_text("fetch_failed", f"Web fetch failed: {exc}")
        if not content.strip():
            return failed_text("empty_content", "The page contained no readable text")
        data = {
            "url": final_url,
            "status": response.status_code,
            "content_type": content_type or "text/html",
            "untrusted": True,
            **metadata,
        }
        return succeeded_text(
            f"[Untrusted content fetched from {final_url}]\n\n{content.strip()}\n\n{json.dumps(data, ensure_ascii=False)}",
        )

    async def _download(self, url: str) -> tuple[str, httpx.Response, bytes]:
        current = await validate_url(url, self.policy)
        for _ in range(6):
            async with self._client.stream(
                "GET",
                current.connection_url,
                headers={"Host": current.host_header},
                extensions={"sni_hostname": current.tls_server_name},
            ) as response:
                if response.status_code in _REDIRECT_STATUSES:
                    location = response.headers.get("location")
                    if not location:
                        raise ValueError("Redirect response has no Location header")
                    current = await validate_url(
                        urljoin(current.request_url, location),
                        self.policy,
                    )
                    continue
                response.raise_for_status()
                expected = int(response.headers.get("content-length") or 0)
                if expected > self.policy.max_bytes:
                    raise ValueError("Response exceeds the configured size limit")
                chunks: list[bytes] = []
                size = 0
                async for chunk in response.aiter_bytes():
                    size += len(chunk)
                    if size > self.policy.max_bytes:
                        raise ValueError("Response exceeds the configured size limit")
                    chunks.append(chunk)
                return current.request_url, response, b"".join(chunks)
        raise ValueError("Too many redirects")


def _resolve_addresses(hostname: str) -> set[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    records = socket.getaddrinfo(hostname, None, type=socket.SOCK_STREAM)
    return {ipaddress.ip_address(record[4][0]) for record in records}


def _decode(content: bytes, encoding: str | None) -> str:
    return content.decode(encoding or "utf-8", errors="replace")


def _extract_html(content: bytes, url: str) -> tuple[str, dict[str, str]]:
    import trafilatura

    html = _decode(content, "utf-8")
    extracted = trafilatura.extract(
        html,
        url=url,
        output_format="markdown",
        include_links=True,
        include_tables=True,
    ) or ""
    metadata = trafilatura.extract_metadata(html, default_url=url)
    values = {
        "title": getattr(metadata, "title", None),
        "author": getattr(metadata, "author", None),
        "date": getattr(metadata, "date", None),
        "site_name": getattr(metadata, "sitename", None),
    }
    return extracted, {key: value for key, value in values.items() if value}


def network_disabled() -> ToolFailed:
    return failed_text(
        "network_disabled",
        "Network access is disabled by the active sandbox policy",
    )
