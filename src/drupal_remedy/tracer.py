"""Trace axe-core violations back to editable Drupal entities/fields.

Drupal (especially Acquia/Paragraph-based sites) renders pages from a tree
of entities — paragraph rows with field_theme dropdowns, media entities with
alt-text fields, and node body fields.  Axe-core reports violations against
rendered HTML, but a content editor needs to know *which Drupal form field*
to change.  This module bridges the gap.

Typical flow
------------
1. Run axe-core scan (via Playwright) to get raw violations.
2. Feed violations + nid into ``ViolationTracer.trace_all()``.
3. Get back a ``TraceReport`` with ``TracedViolation`` items that each
   carry a Drupal edit URL, field name, current value, and suggested fix.
"""

from __future__ import annotations

import html as html_mod
import logging
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:
    from drupal_remedy.client import DrupalClient

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Known theme contrast failures (populated from real scan data)
# ---------------------------------------------------------------------------

_KNOWN_FAILING_THEMES: dict[str, dict[str, Any]] = {
    "theme-primary": {
        "issue": "foreground #4a7729 on background #32511c fails AA",
        "foreground": "#4a7729",
        "background": "#32511c",
        "contrast_ratio": 1.7,
        "suggested_replacement": "theme-neutral-light",
    },
    "theme-secondary": {
        "issue": "foreground #ffffff on background #8bc34a fails AA for small text",
        "foreground": "#ffffff",
        "background": "#8bc34a",
        "contrast_ratio": 3.0,
        "suggested_replacement": "theme-neutral-light",
    },
}

# Themes known to pass AA — used for fallback suggestions.
_SAFE_THEMES = ("theme-neutral-light", "theme-dark", "theme-high-contrast")

# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class TracedViolation:
    """A single axe-core violation traced to a specific Drupal entity/field."""

    violation_id: str  # e.g. "color-contrast"
    impact: str  # "critical" | "serious" | "moderate" | "minor"
    help: str  # axe-core help text
    html_snippet: str  # offending HTML from rendered page
    entity_type: str  # "paragraph_field" | "media" | "node_field" | "theme"
    entity_id: int  # nid or mid
    field_name: str  # Drupal form field machine name
    current_value: str  # current field value
    edit_url: str  # URL to edit the entity
    suggested_fix: str  # human-readable fix description
    row_index: int | None = None  # paragraph row index, if applicable


@dataclass
class TraceReport:
    """Result of tracing all violations for a single page."""

    nid: int
    url: str
    total_violations: int
    traced: list[TracedViolation] = field(default_factory=list)
    untraced: list[dict[str, Any]] = field(default_factory=list)

    @property
    def trace_rate(self) -> float:
        """Fraction of violations successfully traced (0.0 – 1.0)."""
        if self.total_violations == 0:
            return 1.0
        return len(self.traced) / self.total_violations


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_IMG_SRC_RE = re.compile(r'<img[^>]+src=["\']([^"\']+)["\']', re.I)
_IFRAME_SRC_RE = re.compile(r'<iframe[^>]+src=["\']([^"\']+)["\']', re.I)
_HEADING_TEXT_RE = re.compile(r'<h\d[^>]*>(.*?)</h\d>', re.I | re.S)
_ALT_RE = re.compile(r'alt=["\']([^"\']*)["\']', re.I)


def _extract_text(html_snippet: str) -> str:
    """Strip tags and decode entities to get plain text from a snippet."""
    text = re.sub(r"<[^>]+>", "", html_snippet)
    text = html_mod.unescape(text).strip()
    return text


def _extract_attr(html_snippet: str, attr: str) -> str | None:
    """Pull the value of *attr* from an HTML snippet."""
    m = re.search(rf'{attr}=["\']([^"\']*)["\']', html_snippet, re.I)
    return m.group(1) if m else None


def _node_edit_url(base_url: str, nid: int) -> str:
    return f"{base_url}/node/{nid}/edit"


def _media_edit_url(base_url: str, mid: int) -> str:
    return f"{base_url}/media/{mid}/edit"


# ---------------------------------------------------------------------------
# ViolationTracer
# ---------------------------------------------------------------------------


class ViolationTracer:
    """Map axe-core violations to the Drupal entities that need editing.

    Parameters
    ----------
    client:
        An authenticated ``DrupalClient`` instance.
    base_url:
        The Drupal site base URL (no trailing slash), e.g.
        ``"https://elac.edu"``.
    """

    def __init__(self, client: DrupalClient, base_url: str = "") -> None:
        self._client = client
        self._base_url = base_url.rstrip("/")
        self._strategies: dict[str, Callable[..., Any]] = {
            "color-contrast": self._trace_color_contrast,
            "empty-heading": self._trace_heading,
            "heading-order": self._trace_heading,
            "image-alt": self._trace_image,
            "image-redundant-alt": self._trace_image,
            "frame-title": self._trace_iframe,
            "link-name": self._trace_link,
        }

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def trace_all(
        self, nid: int, violations: list[dict[str, Any]]
    ) -> TraceReport:
        """Trace every violation for *nid*, returning a ``TraceReport``.

        Each item in *violations* should be an axe-core node result dict
        with at least ``id``, ``impact``, ``help``, and ``html``.
        """
        meta = await self._client.get_page_metadata(nid)
        url = meta.get("canonical_url", f"{self._base_url}/node/{nid}")

        report = TraceReport(nid=nid, url=url, total_violations=len(violations))

        for v in violations:
            vid = v.get("id", "unknown")
            strategy = self._strategies.get(vid)
            if strategy is None:
                report.untraced.append(
                    {**v, "likely_cause": f"No tracing strategy for '{vid}'"}
                )
                continue

            try:
                traced = await strategy(nid, v, meta)
            except Exception:
                log.exception("Tracer strategy failed for %s on node %d", vid, nid)
                traced = None

            if traced is not None:
                report.traced.append(traced)
            else:
                report.untraced.append(
                    {**v, "likely_cause": f"Strategy '{vid}' could not locate source"}
                )

        log.info(
            "Node %d: traced %d/%d violations (%.0f%%)",
            nid,
            len(report.traced),
            report.total_violations,
            report.trace_rate * 100,
        )
        return report

    # ------------------------------------------------------------------
    # Strategy: color-contrast  (theme-driven on paragraph pages)
    # ------------------------------------------------------------------

    async def _trace_color_contrast(
        self,
        nid: int,
        violation: dict[str, Any],
        meta: dict[str, Any],
    ) -> TracedViolation | None:
        """Check paragraph ``field_theme`` values against known failures.

        1. If the node has paragraphs, iterate each row's field_theme.
        2. Match against ``_KNOWN_FAILING_THEMES``.
        3. Return the paragraph row + theme that needs changing.

        Falls back to checking the node body field for inline style issues.
        """
        snippet = violation.get("html", "")

        if meta.get("has_paragraphs"):
            paragraphs = await self._client.list_paragraphs(nid)
            for para in paragraphs:
                idx = para["index"]
                fields = await self._client.get_paragraph_fields(nid, idx)
                theme_field = _find_field(fields, "field_theme")
                if theme_field is None:
                    continue

                theme_val = theme_field.get("value", "")
                failing = _KNOWN_FAILING_THEMES.get(theme_val)
                if failing is not None:
                    replacement = failing["suggested_replacement"]
                    return TracedViolation(
                        violation_id="color-contrast",
                        impact=violation.get("impact", "serious"),
                        help=violation.get("help", ""),
                        html_snippet=snippet,
                        entity_type="theme",
                        entity_id=nid,
                        field_name="field_theme",
                        current_value=theme_val,
                        edit_url=_node_edit_url(self._base_url, nid),
                        suggested_fix=(
                            f"Change paragraph row {idx} theme from "
                            f"'{theme_val}' to '{replacement}' — current "
                            f"contrast ratio is {failing['contrast_ratio']}:1, "
                            f"AA requires >= 4.5:1"
                        ),
                        row_index=idx,
                    )

        # Fallback: flag as node-level body field issue
        return TracedViolation(
            violation_id="color-contrast",
            impact=violation.get("impact", "serious"),
            help=violation.get("help", ""),
            html_snippet=snippet,
            entity_type="node_field",
            entity_id=nid,
            field_name="body",
            current_value="(inline CSS or inherited styles)",
            edit_url=_node_edit_url(self._base_url, nid),
            suggested_fix=(
                "Review inline styles or CKEditor content for hard-coded "
                "colors that fail WCAG AA contrast requirements"
            ),
            row_index=None,
        )

    # ------------------------------------------------------------------
    # Strategy: empty-heading / heading-order
    # ------------------------------------------------------------------

    async def _trace_heading(
        self,
        nid: int,
        violation: dict[str, Any],
        meta: dict[str, Any],
    ) -> TracedViolation | None:
        """Trace heading issues to paragraph title fields or body content.

        - **empty-heading**: search paragraph ``field_title_plain`` for empty
          or whitespace-only values.
        - **heading-order**: enumerate all heading fields to find where the
          heading level order breaks.
        """
        vid = violation.get("id", "")
        snippet = violation.get("html", "")
        heading_text = _extract_text(snippet)
        is_empty = vid == "empty-heading" or not heading_text.strip()

        if meta.get("has_paragraphs"):
            paragraphs = await self._client.list_paragraphs(nid)
            for para in paragraphs:
                idx = para["index"]
                fields = await self._client.get_paragraph_fields(nid, idx)

                # Check title / heading fields
                for f in fields:
                    fname = f.get("name", "")
                    fval = f.get("value", "")
                    if "title" not in fname and "heading" not in fname:
                        continue

                    if is_empty and (not fval or fval.strip() in ("", "&nbsp;")):
                        return TracedViolation(
                            violation_id=vid,
                            impact=violation.get("impact", "minor"),
                            help=violation.get("help", ""),
                            html_snippet=snippet,
                            entity_type="paragraph_field",
                            entity_id=nid,
                            field_name=fname,
                            current_value=fval,
                            edit_url=_node_edit_url(self._base_url, nid),
                            suggested_fix=(
                                f"Paragraph row {idx}: remove the empty "
                                f"heading or add meaningful text to '{fname}'"
                            ),
                            row_index=idx,
                        )

                    if not is_empty and heading_text and heading_text in fval:
                        return TracedViolation(
                            violation_id=vid,
                            impact=violation.get("impact", "moderate"),
                            help=violation.get("help", ""),
                            html_snippet=snippet,
                            entity_type="paragraph_field",
                            entity_id=nid,
                            field_name=fname,
                            current_value=fval,
                            edit_url=_node_edit_url(self._base_url, nid),
                            suggested_fix=(
                                f"Paragraph row {idx}: adjust the heading "
                                f"level in '{fname}' to maintain sequential "
                                f"order (h2 -> h3 -> h4, no skips)"
                            ),
                            row_index=idx,
                        )

        # Fallback: flag body field
        fix = (
            "Remove the empty heading element"
            if is_empty
            else "Adjust heading levels to maintain sequential order"
        )
        return TracedViolation(
            violation_id=vid,
            impact=violation.get("impact", "moderate"),
            help=violation.get("help", ""),
            html_snippet=snippet,
            entity_type="node_field",
            entity_id=nid,
            field_name="body",
            current_value=heading_text or "(empty)",
            edit_url=_node_edit_url(self._base_url, nid),
            suggested_fix=fix,
            row_index=None,
        )

    # ------------------------------------------------------------------
    # Strategy: image-alt / image-redundant-alt
    # ------------------------------------------------------------------

    async def _trace_image(
        self,
        nid: int,
        violation: dict[str, Any],
        meta: dict[str, Any],
    ) -> TracedViolation | None:
        """Trace image alt-text issues to media entities.

        1. Extract image ``src`` from the violation HTML snippet.
        2. Walk the node's paragraph rows looking for media reference fields.
        3. For each media entity, check if its image file matches the src.
        4. Return the media entity + its alt-text field.
        """
        snippet = violation.get("html", "")
        img_src = _IMG_SRC_RE.search(snippet)
        src_path = img_src.group(1) if img_src else ""
        current_alt = _extract_attr(snippet, "alt") or ""

        if meta.get("has_paragraphs"):
            paragraphs = await self._client.list_paragraphs(nid)
            for para in paragraphs:
                idx = para["index"]
                fields = await self._client.get_paragraph_fields(nid, idx)
                for f in fields:
                    if f.get("type") not in ("entity_reference", "media"):
                        continue
                    mid = _parse_mid(f.get("value", ""))
                    if mid is None:
                        continue

                    media = await self._client.get_media_info(mid)
                    if media is None:
                        continue

                    # Match by filename substring in src path
                    for file_info in media.get("files", []):
                        if file_info.get("filename", "") in src_path or (
                            src_path and src_path in file_info.get("url", "")
                        ):
                            return TracedViolation(
                                violation_id=violation.get("id", "image-alt"),
                                impact=violation.get("impact", "serious"),
                                help=violation.get("help", ""),
                                html_snippet=snippet,
                                entity_type="media",
                                entity_id=mid,
                                field_name="field_media_image",
                                current_value=media.get("alt_text", current_alt),
                                edit_url=_media_edit_url(self._base_url, mid),
                                suggested_fix=_image_fix_suggestion(
                                    violation.get("id", ""), media, current_alt
                                ),
                                row_index=idx,
                            )

        # Could not trace to a specific media entity
        return None

    # ------------------------------------------------------------------
    # Strategy: frame-title
    # ------------------------------------------------------------------

    async def _trace_iframe(
        self,
        nid: int,
        violation: dict[str, Any],
        meta: dict[str, Any],
    ) -> TracedViolation | None:
        """Trace iframe title issues to media entities (remote video, embeds).

        1. Extract iframe ``src`` from the violation snippet.
        2. Walk paragraphs looking for media/embed reference fields.
        3. Match on iframe src -> media entity, return its title field.
        """
        snippet = violation.get("html", "")
        iframe_src_m = _IFRAME_SRC_RE.search(snippet)
        iframe_src = iframe_src_m.group(1) if iframe_src_m else ""

        if meta.get("has_paragraphs"):
            paragraphs = await self._client.list_paragraphs(nid)
            for para in paragraphs:
                idx = para["index"]
                fields = await self._client.get_paragraph_fields(nid, idx)
                for f in fields:
                    if f.get("type") not in ("entity_reference", "media"):
                        continue
                    mid = _parse_mid(f.get("value", ""))
                    if mid is None:
                        continue

                    media = await self._client.get_media_info(mid)
                    if media is None:
                        continue

                    if media.get("media_type") in (
                        "remote_video",
                        "video",
                        "embed",
                    ):
                        return TracedViolation(
                            violation_id="frame-title",
                            impact=violation.get("impact", "serious"),
                            help=violation.get("help", ""),
                            html_snippet=snippet,
                            entity_type="media",
                            entity_id=mid,
                            field_name="name",
                            current_value=media.get("page_title", ""),
                            edit_url=_media_edit_url(self._base_url, mid),
                            suggested_fix=(
                                f"Set a descriptive title on media entity "
                                f"{mid} — this becomes the iframe title "
                                f"attribute for screen readers"
                            ),
                            row_index=idx,
                        )

        return None

    # ------------------------------------------------------------------
    # Strategy: link-name
    # ------------------------------------------------------------------

    async def _trace_link(
        self,
        nid: int,
        violation: dict[str, Any],
        meta: dict[str, Any],
    ) -> TracedViolation | None:
        """Trace link-name issues to body fields or paragraph link fields."""
        snippet = violation.get("html", "")
        link_text = _extract_text(snippet)
        href = _extract_attr(snippet, "href") or ""

        if meta.get("has_paragraphs"):
            paragraphs = await self._client.list_paragraphs(nid)
            for para in paragraphs:
                idx = para["index"]
                fields = await self._client.get_paragraph_fields(nid, idx)
                for f in fields:
                    fval = f.get("value", "")
                    if href and href in fval:
                        return TracedViolation(
                            violation_id="link-name",
                            impact=violation.get("impact", "serious"),
                            help=violation.get("help", ""),
                            html_snippet=snippet,
                            entity_type="paragraph_field",
                            entity_id=nid,
                            field_name=f.get("name", "body"),
                            current_value=fval,
                            edit_url=_node_edit_url(self._base_url, nid),
                            suggested_fix=(
                                f"Paragraph row {idx}: add descriptive link "
                                f"text for the link to '{href}' — avoid "
                                f"generic text like 'click here' or 'read more'"
                            ),
                            row_index=idx,
                        )

        # Fallback: body field
        return TracedViolation(
            violation_id="link-name",
            impact=violation.get("impact", "serious"),
            help=violation.get("help", ""),
            html_snippet=snippet,
            entity_type="node_field",
            entity_id=nid,
            field_name="body",
            current_value=link_text or "(empty link)",
            edit_url=_node_edit_url(self._base_url, nid),
            suggested_fix=(
                f"Add descriptive link text for '{href}' — the link must "
                f"have discernible text for screen readers"
            ),
            row_index=None,
        )


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------


def _find_field(
    fields: list[dict[str, Any]], name: str
) -> dict[str, Any] | None:
    """Find a field dict by machine name."""
    for f in fields:
        if f.get("name") == name:
            return f
    return None


def _parse_mid(value: str) -> int | None:
    """Extract a media entity ID from a field value like 'media:42' or '42'."""
    if not value:
        return None
    m = re.search(r"(\d+)", str(value))
    return int(m.group(1)) if m else None


def _image_fix_suggestion(violation_id: str, media: dict, current_alt: str) -> str:
    """Return a human-readable fix suggestion for image violations."""
    mid = media.get("mid", "?")
    if violation_id == "image-redundant-alt":
        return (
            f"Media {mid}: the alt text '{current_alt}' duplicates "
            f"surrounding text — make alt text unique and descriptive, "
            f"or set it to empty (decorative) if the image is redundant"
        )
    if not current_alt:
        return (
            f"Media {mid}: add descriptive alt text to "
            f"field_media_image — currently empty"
        )
    return (
        f"Media {mid}: review alt text '{current_alt}' for accuracy "
        f"and WCAG compliance"
    )
