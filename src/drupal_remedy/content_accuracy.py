"""Vision-based content accuracy checker.

Finds images in page body HTML, extracts surrounding text claims (alt text,
adjacent paragraphs, figcaptions), sends each image to the vision model, and
flags mismatches between what the text says and what the image actually shows.
"""

from __future__ import annotations

import json as _json
import logging
import re
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

_IMG_TAG_RE = re.compile(r"<img\b[^>]*>", re.IGNORECASE)
_ALT_ATTR_RE = re.compile(r'\balt\s*=\s*["\']([^"\']*)["\']', re.IGNORECASE)
_SRC_ATTR_RE = re.compile(r'\bsrc\s*=\s*["\']([^"\']+)["\']', re.IGNORECASE)


@dataclass
class ImageContext:
    """An image tag and the text claims found near it."""

    src: str
    alt_text: str
    nearby_text: list[str]
    html_snippet: str

    @property
    def claims(self) -> list[str]:
        """All non-empty text claims about this image."""
        parts: list[str] = []
        if self.alt_text.strip():
            parts.append(f"Alt text: {self.alt_text.strip()}")
        for text in self.nearby_text:
            stripped = text.strip()
            if stripped:
                parts.append(f"Nearby text: {stripped}")
        return parts

    @property
    def has_claims(self) -> bool:
        return len(self.claims) > 0


@dataclass
class ContentAccuracyIssue:
    """A mismatch between image content and surrounding text."""

    src: str
    image_description: str
    claims: list[str]
    issues: list[str]
    html_snippet: str


@dataclass
class ContentAccuracyReport:
    """Results of a content accuracy check on a page."""

    nid: int
    images_checked: int
    issues: list[ContentAccuracyIssue] = field(default_factory=list)

    @property
    def accurate(self) -> bool:
        return len(self.issues) == 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "nid": self.nid,
            "images_checked": self.images_checked,
            "issues_found": len(self.issues),
            "accurate": self.accurate,
            "issues": [
                {
                    "src": issue.src,
                    "image_description": issue.image_description,
                    "claims": issue.claims,
                    "issues": issue.issues,
                    "html_snippet": issue.html_snippet[:300],
                }
                for issue in self.issues
            ],
        }


class ContentAccuracyChecker:
    """Check that text descriptions near images match the actual image content.

    Uses the vision model to describe each image and compares against
    surrounding text claims (alt text, adjacent paragraphs, figcaptions).
    """

    def __init__(self, client: Any, vision_chat: Any) -> None:
        self._client = client
        self._vision_chat = vision_chat

    async def check_page(self, nid: int) -> ContentAccuracyReport:
        """Check all images in a page's body HTML for content accuracy."""
        html = await self._client.get_body_html(nid)
        if not html.strip():
            return ContentAccuracyReport(nid=nid, images_checked=0)

        contexts = extract_image_contexts(html)
        # Only check images that have text claims to verify
        checkable = [ctx for ctx in contexts if ctx.has_claims]

        issues: list[ContentAccuracyIssue] = []
        for ctx in checkable:
            issue = await self._check_image(ctx)
            if issue is not None:
                issues.append(issue)

        return ContentAccuracyReport(
            nid=nid,
            images_checked=len(checkable),
            issues=issues,
        )

    async def _check_image(self, ctx: ImageContext) -> ContentAccuracyIssue | None:
        """Download one image, send to vision model, check accuracy."""
        try:
            image_bytes, mime_type = await self._client.download_file(ctx.src)
        except Exception as exc:
            logger.warning("Failed to download %s: %s", ctx.src, exc)
            return None

        claims_text = "\n".join(f"- {c}" for c in ctx.claims)

        from drupal_remedy.prompts import (
            CONTENT_ACCURACY_PROMPT,
            CONTENT_ACCURACY_SCHEMA,
        )

        prompt = CONTENT_ACCURACY_PROMPT.format(claims=claims_text)

        try:
            raw = await self._vision_chat(
                image_bytes, mime_type, prompt,
                seed_offset=0, format_schema=CONTENT_ACCURACY_SCHEMA,
            )
        except TypeError:
            # Fallback for callables that don't accept format_schema (tests)
            raw = await self._vision_chat(image_bytes, mime_type, prompt)

        result = _parse_accuracy_response(raw)
        if result is None:
            logger.warning("Could not parse accuracy response for %s", ctx.src)
            return None

        if result["accurate"]:
            return None

        return ContentAccuracyIssue(
            src=ctx.src,
            image_description=result["image_description"],
            claims=ctx.claims,
            issues=result["issues"],
            html_snippet=ctx.html_snippet,
        )


# ---------------------------------------------------------------------------
# HTML parsing helpers
# ---------------------------------------------------------------------------


def extract_image_contexts(html: str) -> list[ImageContext]:
    """Find all ``<img>`` tags in *html* and extract surrounding text claims."""
    results: list[ImageContext] = []

    for match in _IMG_TAG_RE.finditer(html):
        tag = match.group(0)
        src_m = _SRC_ATTR_RE.search(tag)
        if not src_m:
            continue
        src = src_m.group(1)

        # Skip tiny icons / spacer images
        if _looks_like_icon(src):
            continue

        # Extract alt text
        alt_m = _ALT_ATTR_RE.search(tag)
        alt_text = alt_m.group(1) if alt_m else ""

        # Extract nearby text (paragraphs / figcaptions before and after)
        nearby = _extract_nearby_text(html, match.start(), match.end())

        # Build a snippet for context
        window_start = max(0, match.start() - 200)
        window_end = min(len(html), match.end() + 200)
        snippet = html[window_start:window_end]

        results.append(ImageContext(
            src=src,
            alt_text=alt_text,
            nearby_text=nearby,
            html_snippet=snippet,
        ))

    return results


def _extract_nearby_text(html: str, img_start: int, img_end: int) -> list[str]:
    """Extract text from the immediately adjacent paragraph or figcaption.

    Only looks at the single closest text block before and after the ``<img>``
    tag, within a tight window.  Skips content that is separated by other
    block-level elements (other images, headings, iframes, divs).
    """
    texts: list[str] = []
    _block_boundary = re.compile(r"<(?:img|h[1-6]|iframe|div|hr)\b", re.IGNORECASE)
    _tag_re = re.compile(r"<(?:p|figcaption)[^>]*>(.*?)</(?:p|figcaption)>", re.DOTALL | re.IGNORECASE)

    # Look backward — take only the last <p>/<figcaption> before the image
    # but stop if we hit another block element
    before = html[max(0, img_start - 300):img_start]
    # Check no block boundary sits between candidate and the img
    last_match = None
    for m in _tag_re.finditer(before):
        between = before[m.end():]
        if not _block_boundary.search(between):
            last_match = m
    if last_match:
        text = re.sub(r"<[^>]+>", " ", last_match.group(1))
        text = " ".join(text.split()).strip()
        if text and len(text) > 5:
            texts.append(text)

    # Look forward — take only the first <p>/<figcaption> after the image
    after = html[img_end:min(len(html), img_end + 300)]
    first_match = _tag_re.search(after)
    if first_match:
        between = after[:first_match.start()]
        if not _block_boundary.search(between):
            text = re.sub(r"<[^>]+>", " ", first_match.group(1))
            text = " ".join(text.split()).strip()
            if text and len(text) > 5:
                texts.append(text)

    return texts


def _looks_like_icon(src: str) -> bool:
    """Heuristic: skip tiny icons, SVGs, and spacer images."""
    lower = src.lower()
    if lower.endswith(".svg"):
        return True
    for pattern in ("icon", "logo", "spacer", "pixel", "blank", "avatar"):
        if pattern in lower:
            return True
    return False


def _parse_accuracy_response(raw: str) -> dict[str, Any] | None:
    """Parse the vision model's content accuracy JSON response."""
    text = raw.strip()
    if not text:
        return None

    # Strip markdown code fences
    for prefix in ("```json", "```"):
        if text.startswith(prefix):
            text = text[len(prefix):]
            break
    if text.endswith("```"):
        text = text[:-3]
    text = text.strip()

    try:
        data = _json.loads(text)
        if isinstance(data, dict):
            return {
                "image_description": str(data.get("image_description", "")),
                "accurate": bool(data.get("accurate", True)),
                "issues": [str(i) for i in data.get("issues", [])],
            }
    except (_json.JSONDecodeError, ValueError):
        pass

    return None
