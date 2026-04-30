"""DrupalClient facade — composes JsonApiClient and Scanner."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from drupal_remedy.config import DrupalSiteConfig
from drupal_remedy.exceptions import DrupalClientError
from drupal_remedy.jsonapi_client import JsonApiClient
from drupal_remedy.scanner import AxeCoreScanner, ScanResult

logger = logging.getLogger(__name__)

# Re-export for backward compatibility — consumers import DrupalClientError from here.
__all__ = ["DrupalClient", "DrupalClientError"]


class DrupalClient:
    """High-level client for Drupal sites.

    Composes a JsonApiClient for data reads/writes and an
    AxeCoreScanner for accessibility scanning (lazy-started).

    Call ``start()`` before use and ``close()`` when done.
    """

    def __init__(self, site_config: DrupalSiteConfig, *, headless: bool | None = None) -> None:
        self._config = site_config
        self._headless = headless if headless is not None else site_config.headless
        self._api = JsonApiClient(site_config)
        self._scanner: AxeCoreScanner | None = None

    @property
    def base_url(self) -> str:
        return self._config.base_url

    @property
    def http_credentials(self) -> dict[str, str] | None:
        if self._config.http_auth_username:
            return {"username": self._config.http_auth_username, "password": self._config.http_auth_password}
        return None

    @property
    def browser(self) -> Any:
        return self._scanner.browser if self._scanner else None

    @property
    def context(self) -> Any:
        return None

    async def start(self) -> None:
        await self._api.login()
        logger.info("DrupalClient started for %s (%s)", self._config.campus_code, self._config.base_url)

    async def close(self) -> None:
        await self._api.close()
        if self._scanner:
            await self._scanner.close()
            self._scanner = None
        logger.info("DrupalClient closed for %s", self._config.campus_code)

    async def _ensure_scanner(self) -> AxeCoreScanner:
        if self._scanner is None:
            self._scanner = AxeCoreScanner(
                http_credentials=self.http_credentials,
                headless=self._headless,
                verify_ssl=self._config.verify_ssl,
            )
        return self._scanner

    # Data reads
    async def get_page_metadata(self, nid: int) -> dict[str, Any]:
        return await self._api.get_page_metadata(nid)

    async def list_paragraphs(self, nid: int) -> list[dict[str, Any]]:
        return await self._api.list_paragraphs(nid)

    async def get_paragraph_fields(self, nid: int, row_index: int) -> list[dict[str, Any]]:
        return await self._api.get_paragraph_fields(nid, row_index)

    async def get_body_html(self, nid: int) -> str:
        return await self._api.get_body_html(nid)

    async def get_page_url(self, nid: int) -> str:
        return await self._api.get_page_url(nid)

    async def get_media_info(self, mid: int) -> dict[str, Any] | None:
        return await self._api.get_media_info(mid)

    async def download_file(self, url: str) -> tuple[bytes, str]:
        return await self._api.download_file(url)

    # Data writes
    async def update_body_html(self, nid: int, html: str, *, revision_message: str = "", publish: bool = True) -> dict[str, Any]:
        return await self._api.update_body_html(nid, html, revision_message=revision_message, publish=publish)

    async def update_paragraph_fields(self, nid: int, row_index: int, field_values: dict[str, str], *, revision_message: str = "", publish: bool = True) -> dict[str, Any]:
        return await self._api.update_paragraph_fields(nid, row_index, field_values, revision_message=revision_message, publish=publish)

    async def update_media_fields(self, mid: int, field_values: dict[str, str]) -> dict[str, Any]:
        return await self._api.update_media_fields(mid, field_values)

    # Scanning
    async def scan_page(self, url: str) -> dict[str, Any]:
        """Scan with Drupal session cookies (authenticated — sees access-controlled pages)."""
        scanner = await self._ensure_scanner()
        from urllib.parse import urlparse
        domain = urlparse(self._config.base_url).hostname
        cookies = self._api.get_session_cookies(domain)
        result = await scanner.scan_page(url, cookies=cookies)
        return {"url": result.url, "violation_count": result.violation_count, "passed": result.passed, "violations": result.violations}

    async def scan_page_anonymous(self, url: str) -> dict[str, Any]:
        scanner = await self._ensure_scanner()
        result = await scanner.scan_page_anonymous(url)
        return {"url": result.url, "violation_count": result.violation_count, "passed": result.passed, "violations": result.violations}

    async def scan_batch(self, urls: list[str], max_concurrent: int = 3) -> list[dict[str, Any]]:
        sem = asyncio.Semaphore(max_concurrent)

        async def _scan(url: str) -> dict[str, Any]:
            async with sem:
                try:
                    return await self.scan_page(url)
                except Exception as exc:
                    logger.warning("Failed to scan %s: %s", url, exc)
                    return {
                        "url": url,
                        "violation_count": -1,
                        "passed": False,
                        "violations": [],
                    }

        return await asyncio.gather(*[_scan(url) for url in urls])

    async def scan_content_list(self, content_type: str = "", max_pages: int = 50) -> list[dict[str, Any]]:
        urls = await self._api.list_content_urls(content_type=content_type, max_pages=max_pages)
        return await self.scan_batch(urls)

    async def list_media_documents(self, *, page_limit: int = 0) -> list[dict[str, Any]]:
        return await self._api.list_media_documents(page_limit=page_limit)

    async def get_media_file_url(self, mid: int) -> str:
        return await self._api.get_media_file_url(mid)

    async def replace_media_file(self, mid: int, file_path: str) -> dict[str, Any]:
        return await self._api.replace_media_file(mid, file_path)

    async def audit_media_usage(self) -> dict[str, Any]:
        return await self._api.audit_media_usage()
