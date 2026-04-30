"""LLM-powered accessibility remediation orchestration."""

from __future__ import annotations

import asyncio
import json as _json
import logging
import re
from typing import Any

from drupal_remedy.client import DrupalClient
from drupal_remedy.prompts import (
    BODY_REMEDIATION_PROMPT,
    FIELD_FIX_PROMPT,
    IMAGE_ALT_TEXT_PROMPT,
    IMAGE_ALT_TEXT_FUNCTIONAL_PROMPT,
    IMAGE_ALT_TEXT_FALLBACK_PROMPT,
)
from drupal_remedy.scan_payload import expand_scan_violations, is_template_noise_rule
from drupal_remedy.tracer import TracedViolation, TraceReport, ViolationTracer

logger = logging.getLogger(__name__)

_IMAGE_VIOLATIONS = frozenset({"image-alt", "image-redundant-alt"})
_IMG_TAG_RE = re.compile(r"<img\b[^>]*>", re.IGNORECASE)
_ALT_ATTR_RE = re.compile(r"\balt\s*=", re.IGNORECASE)
_SRC_ATTR_RE = re.compile(r'\bsrc\s*=\s*["\']([^"\']+)["\']', re.IGNORECASE)


class Remediator:
    """Orchestrates scan → trace → fix → verify loops."""

    def __init__(
        self,
        client: DrupalClient,
        llm_chat: Any,
        vision_chat: Any = None,
    ) -> None:
        self._client = client
        self._llm_chat = llm_chat
        self._vision_chat = vision_chat
        self._tracer = ViolationTracer(client)

    async def remediate_body(
        self,
        nid: int,
        *,
        max_cycles: int = 3,
    ) -> dict[str, Any]:
        """Remediate a page's body field via scan → LLM fix → verify loop."""
        meta = await self._client.get_page_metadata(nid)
        url = meta.get("canonical_url") or await self._client.get_page_url(nid)

        scan = await self._client.scan_page(url)
        violations = _content_rule_violations(scan["violations"])
        initial = len(violations)

        if not violations:
            return _result(nid, url, "already_passing", initial, 0, 0, [])

        body = await self._client.get_body_html(nid)
        if not body.strip():
            return _result(nid, url, "no_body_field", initial, initial, 0, [])

        changes: list[dict[str, Any]] = []

        # Pre-process: fix image alts via vision model before text LLM pass
        has_image_violations = any(
            v["id"] in _IMAGE_VIOLATIONS for v in violations
        )
        if has_image_violations and self._vision_chat is not None:
            body, image_fixes = await self._fix_image_alts_in_body(body)
            if image_fixes:
                await self._client.update_body_html(
                    nid, body,
                    revision_message="A11y: vision-generated image alt text",
                )
                changes.extend(image_fixes)
                scan = await self._client.scan_page(url)
                violations = _content_rule_violations(scan["violations"])
                if not violations:
                    return _result(
                        nid, url, "fixed", initial,
                        len(violations), len(changes), changes,
                    )

        # Text LLM pass for remaining non-image violations
        for cycle in range(1, max_cycles + 1):
            violations_text = _format_violations(violations)
            fixed = await self._fix_html(body, violations_text)
            if not fixed or fixed == body:
                break

            await self._client.update_body_html(
                nid, fixed,
                revision_message=f"A11y remediation cycle {cycle}",
            )
            changes.append({"cycle": cycle, "type": "body"})
            body = fixed

            scan = await self._client.scan_page(url)
            violations = _content_rule_violations(scan["violations"])
            if not violations:
                break

        return _result(
            nid, url,
            "fixed" if not violations else "improved",
            initial, len(violations), len(changes), changes,
        )

    async def remediate_traced(
        self,
        nid: int,
        *,
        max_cycles: int = 2,
    ) -> dict[str, Any]:
        """Trace violations to source entities and apply targeted fixes."""
        meta = await self._client.get_page_metadata(nid)
        url = meta.get("canonical_url") or await self._client.get_page_url(nid)

        scan = await self._client.scan_page(url)
        violations = [
            v for v in expand_scan_violations(scan["violations"])
            if not is_template_noise_rule(v["id"])
        ]
        initial = len(violations)

        if not violations:
            return _result(nid, url, "already_passing", initial, 0, 0, [])

        report = await self._tracer.trace_all(nid, violations)
        if not report.traced:
            return {
                **_result(nid, url, "no_traceable_violations", initial, initial, 0, []),
                "untraced": [_untraced_dict(u) for u in report.untraced],
            }

        fixes: list[dict[str, Any]] = []
        for cycle in range(1, max_cycles + 1):
            for traced in report.traced:
                fix = await self._apply_traced_fix(traced)
                if fix:
                    fixes.append(fix)

            scan = await self._client.scan_page(url)
            remaining = [
                v for v in expand_scan_violations(scan["violations"])
                if not is_template_noise_rule(v["id"])
            ]
            if not remaining:
                break

            report = await self._tracer.trace_all(nid, remaining)
            if not report.traced:
                break

        final_scan = await self._client.scan_page(url)
        final = [
            v for v in expand_scan_violations(final_scan["violations"])
            if not is_template_noise_rule(v["id"])
        ]

        return {
            **_result(nid, url,
                      "fixed" if not final else "partially_fixed",
                      initial, len(final), len(fixes), fixes),
            "untraced": [_untraced_dict(u) for u in report.untraced],
        }

    async def _apply_traced_fix(
        self, traced: TracedViolation
    ) -> dict[str, Any] | None:
        """Apply an LLM fix to a single traced violation."""
        # Image violations with empty alt text are the main use case — don't skip them.
        if not traced.current_value.strip() and traced.violation_id not in _IMAGE_VIOLATIONS:
            return None

        # Vision path for image violations
        if traced.violation_id in _IMAGE_VIOLATIONS and self._vision_chat is not None:
            fixed = await self._generate_vision_alt_text(traced)
            if fixed is None:
                # Fall back to text LLM
                fixed = await self._text_fix(traced)
        else:
            fixed = await self._text_fix(traced)

        if fixed is None or fixed == traced.current_value:
            return None

        try:
            if traced.entity_type == "media":
                await self._client.update_media_fields(
                    traced.entity_id, {traced.field_name: fixed}
                )
            elif traced.entity_type == "paragraph_field":
                await self._client.update_paragraph_fields(
                    traced.entity_id,
                    traced.row_index or 0,
                    {traced.field_name: fixed},
                    revision_message=f"A11y fix: {traced.violation_id}",
                )
            elif traced.entity_type == "node_field":
                await self._client.update_body_html(
                    traced.entity_id, fixed,
                    revision_message=f"A11y fix: {traced.violation_id}",
                )
            else:
                return None
        except Exception as exc:
            logger.warning("Fix failed for %s: %s", traced.violation_id, exc)
            return None

        return {
            "violation_id": traced.violation_id,
            "entity_type": traced.entity_type,
            "entity_id": traced.entity_id,
            "field_name": traced.field_name,
            "old_value": traced.current_value[:200],
            "new_value": fixed[:200],
        }

    async def _text_fix(self, traced: TracedViolation) -> str | None:
        """Generate a fix using the text LLM."""
        prompt = FIELD_FIX_PROMPT.format(
            violation_id=traced.violation_id,
            impact=traced.impact,
            help_text=traced.help,
            html_snippet=traced.html_snippet,
            field_name=traced.field_name,
            current_value=traced.current_value,
        )
        fixed = await self._llm_chat([{"role": "user", "content": prompt}])
        return _strip_fences(fixed)

    async def _generate_vision_alt_text(
        self, traced: TracedViolation
    ) -> str | None:
        """Download image from media entity and generate alt text via vision model."""
        try:
            media = await self._client.get_media_info(traced.entity_id)
            if media is None:
                return None

            image_url = _extract_image_url(media)
            if not image_url:
                logger.warning("No image URL found for media %d", traced.entity_id)
                return None

            image_bytes, mime_type = await self._client.download_file(image_url)

            context = ""
            if traced.html_snippet:
                nearby = re.sub(r"<[^>]+>", " ", traced.html_snippet)
                nearby = " ".join(nearby.split())[:200].strip()
                if nearby:
                    context = f"\nSurrounding page text: {nearby}\n"

            prompt = IMAGE_ALT_TEXT_PROMPT.format(context_line=context)
            alt_text = await _vision_with_retry(
                self._vision_chat, image_bytes, mime_type, prompt,
            )

            if len(alt_text) > 125:
                alt_text = alt_text[:122] + "..."

            return alt_text or None
        except Exception as exc:
            logger.warning(
                "Vision alt-text generation failed for media %d: %s",
                traced.entity_id, exc,
            )
            return None

    async def _fix_image_alts_in_body(
        self, html: str
    ) -> tuple[str, list[dict[str, Any]]]:
        """Download images missing alt text from body HTML and generate alt via vision model.

        Returns the updated HTML and a list of fix records.
        """
        fixes: list[dict[str, Any]] = []
        for match in _IMG_TAG_RE.finditer(html):
            tag = match.group(0)
            if _ALT_ATTR_RE.search(tag):
                continue  # already has alt attribute

            src_match = _SRC_ATTR_RE.search(tag)
            if not src_match:
                continue

            src = src_match.group(1)
            try:
                image_bytes, mime_type = await self._client.download_file(src)
            except Exception as exc:
                logger.warning("Failed to download image %s: %s", src, exc)
                continue

            link_target, context_line = _extract_image_context(html, match.start(), match.end())
            try:
                if link_target:
                    prompt = IMAGE_ALT_TEXT_FUNCTIONAL_PROMPT.format(
                        link_target=link_target, context_line=context_line,
                    )
                else:
                    prompt = IMAGE_ALT_TEXT_PROMPT.format(context_line=context_line)

                alt_text = await _vision_with_retry(
                    self._vision_chat, image_bytes, mime_type, prompt,
                )
                if len(alt_text) > 125:
                    alt_text = alt_text[:122] + "..."
            except Exception as exc:
                logger.warning("Vision alt-text failed for %s: %s", src, exc)
                continue

            # For functional images (inside links), never leave alt empty —
            # fall back to describing the link destination.
            if not alt_text and link_target:
                slug = link_target.strip("/").split("/")[-1].replace("-", " ").title()
                alt_text = f"Link to {slug}"
                logger.info("Vision exhausted for %s, using link fallback: %s", src, alt_text)

            if not alt_text:
                continue

            # Escape double quotes in alt text for safe HTML attribute insertion
            safe_alt = alt_text.replace("&", "&amp;").replace('"', "&quot;")
            # Insert alt attribute before the closing > or />
            new_tag = tag.replace("<img ", f'<img alt="{safe_alt}" ', 1)
            html = html.replace(tag, new_tag, 1)
            fixes.append({
                "cycle": 0,
                "type": "image_alt_vision",
                "src": src,
                "alt_text": alt_text,
            })
            logger.info("Vision alt text for %s: %s", src, alt_text)

        return html, fixes

    async def _fix_html(self, html: str, violations_text: str) -> str:
        """Send HTML + violations to LLM for correction."""
        prompt = BODY_REMEDIATION_PROMPT.format(
            violations=violations_text, html=html
        )
        result = await self._llm_chat([{"role": "user", "content": prompt}])
        return _strip_fences(result)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _content_rule_violations(violations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [v for v in violations if not is_template_noise_rule(v.get("id"))]


def _result(
    nid: int, url: str, status: str,
    initial: int, final: int, cycles: int,
    changes: list,
) -> dict[str, Any]:
    return {
        "nid": nid,
        "url": url,
        "status": status,
        "initial_violations": initial,
        "final_violations": final,
        "cycles": cycles,
        "changes": changes,
    }


def _untraced_dict(item: dict) -> dict:
    return {
        "violation_id": item.get("violation_id", ""),
        "impact": item.get("impact", ""),
        "likely_cause": item.get("likely_cause", "unknown"),
    }


def _format_violations(violations: list[dict[str, Any]]) -> str:
    lines: list[str] = []
    for v in violations:
        lines.append(f"- [{v.get('impact', '?')}] {v.get('description', '')}")
        if v.get("help"):
            lines.append(f"  Fix: {v['help']}")
        for snip in v.get("html_snippets", [])[:3]:
            lines.append(f"  HTML: {snip}")
    return "\n".join(lines)


async def _vision_with_retry(
    vision_chat: Any,
    image_bytes: bytes,
    mime_type: str,
    prompt: str,
    max_attempts: int = 3,
) -> str:
    """Call vision model with retries — returns alt text or empty string.

    Parses structured JSON responses (``{"alt_text": "...", "decorative": bool}``)
    from the native Ollama API. Falls back to treating the raw response as
    plain text for OpenAI-compat mode.

    Uses a different ``seed_offset`` per attempt so retries explore
    different decoding paths instead of reproducing the same empty result.
    """
    for attempt in range(1, max_attempts + 1):
        use_prompt = prompt if attempt <= 2 else IMAGE_ALT_TEXT_FALLBACK_PROMPT
        try:
            raw = await vision_chat(image_bytes, mime_type, use_prompt, seed_offset=attempt - 1)
        except TypeError:
            # Fallback for callables that don't accept seed_offset (tests, compat)
            raw = await vision_chat(image_bytes, mime_type, use_prompt)

        alt_text, status = _parse_vision_response(raw)

        if status == "ok" and alt_text:
            return alt_text
        if status == "decorative":
            logger.info("Vision classified image as decorative (attempt %d)", attempt)
            return ""

        logger.info("Vision %s (attempt %d/%d)", status, attempt, max_attempts)
    return ""


def _parse_vision_response(raw: str) -> tuple[str, str]:
    """Parse a vision model response into ``(alt_text, status)``.

    Status is one of: ``"ok"``, ``"decorative"``, ``"empty_content"``,
    ``"invalid_json"``.
    """
    text = raw.strip()
    if not text:
        return "", "empty_content"

    # Strip markdown code fences — some models wrap JSON even with format schema
    text = _strip_fences(text)

    # Try structured JSON first
    try:
        data = _json.loads(text)
        if isinstance(data, dict):
            alt = str(data.get("alt_text", "")).strip()
            decorative = bool(data.get("decorative", False))
            if decorative:
                return "", "decorative"
            if alt:
                return alt.strip('"').strip("'"), "ok"
            return "", "empty_content"
    except (_json.JSONDecodeError, ValueError):
        pass

    # Fall back to plain-text parsing (OpenAI-compat or malformed JSON)
    alt = text.strip('"').strip("'")
    if alt:
        return alt, "ok"
    return "", "empty_content"


def _extract_image_context(html: str, start: int, end: int) -> tuple[str, str]:
    """Build a context hint for the vision model from the HTML surrounding an <img> tag.

    Returns ``(link_target, context_line)`` where *link_target* is the href
    if the image is inside an ``<a>`` tag (empty string otherwise), and
    *context_line* is nearby page text for the model.
    """
    # Grab a window around the <img> tag
    window_start = max(0, start - 200)
    window_end = min(len(html), end + 200)
    surrounding = html[window_start:window_end]

    link_target = ""

    # Check if the image is inside an <a> tag — it's functional, not decorative
    before = html[max(0, start - 300):start]
    link_match = re.search(r'<a\b[^>]*href=["\']([^"\']*)["\']', before)
    if link_match and "</a>" not in html[start - len(link_match.group(0)):start]:
        link_target = link_match.group(1)

    # Strip tags from surrounding text to give the model content context
    nearby_text = re.sub(r"<[^>]+>", " ", surrounding)
    nearby_text = " ".join(nearby_text.split())[:200].strip()

    context_line = ""
    if nearby_text:
        context_line = f"\nSurrounding page text: {nearby_text}\n"

    return link_target, context_line


def _extract_image_url(media: dict) -> str:
    """Extract the image file URL from a media entity dict."""
    for file_info in media.get("files", []):
        url = file_info.get("url", "")
        if url:
            return url
    return ""


def _strip_fences(text: str) -> str:
    result = text.strip()
    for prefix in ("```html", "```json", "```"):
        if result.startswith(prefix):
            result = result[len(prefix):]
            break
    if result.endswith("```"):
        result = result[:-3]
    return result.strip()
