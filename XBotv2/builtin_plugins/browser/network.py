"""Live search, bounded HTTP retrieval, and URL safety."""

from __future__ import annotations

import asyncio
import ipaddress
import json
import socket
from dataclasses import dataclass
from typing import Any, Literal
from urllib.parse import urljoin, urlsplit

import httpx

from xbotv2.api import ToolResult


_REDIRECT_STATUSES = {301, 302, 303, 307, 308}
_TEXT_TYPES = ("text/", "application/json", "application/xml")


@dataclass(frozen=True)
class NetworkOptions:
    timeout_seconds: float = 20.0
    max_response_bytes: int = 5_000_000
    allow_private: bool = False


class UrlPolicy:
    """Reject non-Web URLs and private network destinations."""

    def __init__(self, *, allow_private: bool = False) -> None:
        self.allow_private = allow_private

    async def check(self, url: str) -> str:
        parsed = urlsplit(url.strip())
        if parsed.scheme not in {"http", "https"}:
            raise ValueError("URL scheme must be http or https")
        if not parsed.hostname or parsed.username or parsed.password:
            raise ValueError("URL must contain a hostname and no credentials")
        if self.allow_private:
            return url.strip()
        try:
            addresses = {ipaddress.ip_address(parsed.hostname)}
        except ValueError:
            addresses = await asyncio.to_thread(_resolve_addresses, parsed.hostname)
        if not addresses or any(not address.is_global for address in addresses):
            raise ValueError("Private, local, or non-routable destinations are blocked")
        return url.strip()


class WebAccess:
    """Own the HTTP client used by read-only Web tools."""

    def __init__(self, options: NetworkOptions) -> None:
        self.options = options
        self.policy = UrlPolicy(allow_private=options.allow_private)
        self._client = httpx.AsyncClient(
            timeout=options.timeout_seconds,
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
    ) -> ToolResult:
        query = query.strip()
        if not query:
            return ToolResult.failure("invalid_query", "Search query must not be empty")
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
                DDGS(timeout=max(1, int(self.options.timeout_seconds))).text,
                query,
                region=region,
                safesearch=safesearch,
                timelimit=timelimit,
                max_results=limit,
                backend=backend,
            )
        except Exception as exc:
            return ToolResult.failure("search_failed", f"Web search failed: {exc}")
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
        return ToolResult.success(
            "\n".join(lines),
            data={"query": query, "results": normalized, "untrusted": True},
        )

    async def fetch(self, url: str) -> ToolResult:
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
                return ToolResult.failure(
                    "unsupported_content_type",
                    f"Unsupported Web content type: {content_type or 'unknown'}",
                )
        except Exception as exc:
            return ToolResult.failure("fetch_failed", f"Web fetch failed: {exc}")
        if not content.strip():
            return ToolResult.failure("empty_content", "The page contained no readable text")
        data = {
            "url": final_url,
            "status": response.status_code,
            "content_type": content_type or "text/html",
            "untrusted": True,
            **metadata,
        }
        return ToolResult.success(
            f"[Untrusted content fetched from {final_url}]\n\n{content.strip()}",
            data=data,
        )

    async def _download(self, url: str) -> tuple[str, httpx.Response, bytes]:
        current = await self.policy.check(url)
        for _ in range(6):
            async with self._client.stream("GET", current) as response:
                if response.status_code in _REDIRECT_STATUSES:
                    location = response.headers.get("location")
                    if not location:
                        raise ValueError("Redirect response has no Location header")
                    current = await self.policy.check(urljoin(current, location))
                    continue
                response.raise_for_status()
                expected = int(response.headers.get("content-length") or 0)
                if expected > self.options.max_response_bytes:
                    raise ValueError("Response exceeds the configured size limit")
                chunks: list[bytes] = []
                size = 0
                async for chunk in response.aiter_bytes():
                    size += len(chunk)
                    if size > self.options.max_response_bytes:
                        raise ValueError("Response exceeds the configured size limit")
                    chunks.append(chunk)
                return current, response, b"".join(chunks)
        raise ValueError("Too many redirects")


def _resolve_addresses(hostname: str) -> set[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    records = socket.getaddrinfo(hostname, None, type=socket.SOCK_STREAM)
    return {ipaddress.ip_address(record[4][0]) for record in records}


def _decode(content: bytes, encoding: str | None) -> str:
    return content.decode(encoding or "utf-8", errors="replace")


def _extract_html(content: bytes, url: str) -> tuple[str, dict[str, Any]]:
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


def network_available(sandbox: Any) -> ToolResult | None:
    if sandbox is not None and not sandbox.network:
        return ToolResult.failure(
            "network_disabled",
            "Network access is disabled by the active sandbox policy",
        )
    return None
