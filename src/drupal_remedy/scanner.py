"""Accessibility scanning with pluggable backends."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

logger = logging.getLogger(__name__)

_AXE_CDN = "https://cdnjs.cloudflare.com/ajax/libs/axe-core/4.10.2/axe.min.js"

_AXE_OPTIONS = """{
    runOnly: {type: "tag", values: ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "best-practice"]},
    iframes: true,
    resultTypes: ["violations", "incomplete"]
}"""


@dataclass
class ScanResult:
    url: str
    violation_count: int
    passed: bool
    violations: list[dict[str, Any]] = field(default_factory=list)


@runtime_checkable
class Scanner(Protocol):
    async def scan_page(self, url: str) -> ScanResult: ...
    async def scan_page_anonymous(self, url: str) -> ScanResult: ...


class AxeCoreScanner:
    """Scan pages using Playwright + axe-core. Playwright starts lazily."""

    def __init__(self, *, http_credentials: dict[str, str] | None = None, headless: bool = True, verify_ssl: bool = True) -> None:
        self._http_credentials = http_credentials
        self._headless = headless
        self._verify_ssl = verify_ssl
        self._pw: Any = None
        self._browser: Any = None

    async def _ensure_browser(self) -> None:
        if self._browser is not None:
            return
        from playwright.async_api import async_playwright
        self._pw = await async_playwright().start()
        self._browser = await self._pw.chromium.launch(headless=self._headless)

    async def close(self) -> None:
        if self._browser:
            try:
                await self._browser.close()
            except Exception as exc:
                logger.debug("Ignoring browser close error during shutdown: %s", exc)
            self._browser = None
        if self._pw:
            try:
                await self._pw.stop()
            except Exception as exc:
                logger.debug("Ignoring Playwright stop error during shutdown: %s", exc)
            self._pw = None

    @property
    def browser(self) -> Any:
        return self._browser

    async def scan_page(self, url: str, *, cookies: list[dict[str, str]] | None = None) -> ScanResult:
        await self._ensure_browser()
        ctx = await self._browser.new_context(
            http_credentials=self._http_credentials,
            ignore_https_errors=not self._verify_ssl,
        )
        if cookies:
            await ctx.add_cookies(cookies)
        try:
            page = await ctx.new_page()
            await self._navigate_and_wait(page, url)
            return await self._run_axe(page, url)
        finally:
            await ctx.close()

    async def scan_page_anonymous(self, url: str) -> ScanResult:
        await self._ensure_browser()
        ctx = await self._browser.new_context(
            http_credentials=self._http_credentials,
            ignore_https_errors=not self._verify_ssl,
        )
        try:
            page = await ctx.new_page()
            await self._navigate_and_wait(page, url)
            return await self._run_axe(page, url)
        finally:
            await ctx.close()

    async def scan_batch(self, urls: list[str], max_concurrent: int = 3) -> list[ScanResult]:
        sem = asyncio.Semaphore(max_concurrent)
        async def _scan(u: str) -> ScanResult:
            async with sem:
                try:
                    return await self.scan_page_anonymous(u)
                except Exception as exc:
                    logger.warning("Failed to scan %s: %s", u, exc)
                    return ScanResult(url=u, violation_count=-1, passed=False, violations=[])
        return await asyncio.gather(*[_scan(u) for u in urls])

    async def _navigate_and_wait(self, page: Any, url: str) -> None:
        """Navigate to URL using load event and explicit waits.

        Uses wait_until='load' instead of 'networkidle' because Drupal
        pages have long-polling resources (admin toolbar, analytics) that
        prevent networkidle from ever resolving.
        """
        await page.goto(url, wait_until="load", timeout=45_000)
        await page.wait_for_timeout(5000)
        # Scroll to trigger lazy-loaded content
        await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        await page.wait_for_timeout(2000)
        await page.evaluate("window.scrollTo(0, 0)")
        await page.wait_for_timeout(1000)

    async def _run_axe(self, page: Any, url: str) -> ScanResult:
        has_axe = await page.evaluate("typeof window.axe !== 'undefined'")
        if not has_axe:
            await page.add_script_tag(url=_AXE_CDN)
            await page.wait_for_function("typeof window.axe !== 'undefined'")
        raw = await page.evaluate(f"axe.run(document, {_AXE_OPTIONS})")
        violations = []
        for v in raw.get("violations", []):
            nodes = []
            snippets = []
            selectors = []
            for node in v.get("nodes", []):
                target = node.get("target", [])
                target_parts = [part for part in target if isinstance(part, str)]
                selector = target_parts[-1] if target_parts else ""
                frame_path = target_parts[:-1] if len(target_parts) > 1 else []
                snippets.append(node.get("html", ""))
                if selector:
                    selectors.append(selector)
                nodes.append({
                    "target": target_parts,
                    "selector": selector,
                    "frame_path": frame_path,
                    "html": node.get("html", ""),
                    "failure_summary": node.get("failureSummary", ""),
                    "xpath": None,
                    "bounding_box": None,
                })
            violations.append({
                "id": v["id"],
                "impact": v.get("impact", "unknown"),
                "tags": v.get("tags", []),
                "description": v.get("description", ""),
                "help": v.get("help", ""),
                "help_url": v.get("helpUrl", ""),
                "html_snippets": snippets,
                "selectors": selectors,
                "nodes": nodes,
                "node_count": len(snippets),
            })
        return ScanResult(url=url, violation_count=len(violations), passed=len(violations) == 0, violations=violations)
