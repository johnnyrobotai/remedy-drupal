"""LLM prompt templates for Drupal accessibility remediation."""

BODY_REMEDIATION_PROMPT = """\
You are an expert web accessibility specialist. The HTML fragment below
is the body content of a Drupal page that has WCAG 2.1 Level AA violations
detected by axe-core scanning.

Fix ALL of the following violations while preserving the content and
structure. Do not remove any content. Only modify the HTML to resolve
the accessibility issues.

## Violations to fix:

{violations}

## Current HTML:

{html}

Return ONLY the corrected HTML fragment. No explanation, no markdown
fences -- just the raw HTML. Do NOT wrap it in <!DOCTYPE html> or <html>
tags; this is a body fragment, not a full document.
"""

FIELD_FIX_PROMPT = """\
You are an expert web accessibility specialist. A Drupal form field
contains content that causes a WCAG 2.1 Level AA violation.

## Violation
- Rule: {violation_id}
- Impact: {impact}
- Fix guidance: {help_text}
- Offending HTML on the rendered page: {html_snippet}

## Current field value (from Drupal edit form)
Field name: {field_name}
Current value: {current_value}

Fix the field value to resolve the accessibility violation.
Preserve the content meaning and structure.
Return ONLY the corrected field value -- no explanation, no fences.
"""

THEME_SUGGESTION_PROMPT = """\
You are a web accessibility specialist evaluating Drupal paragraph themes
for WCAG 2.1 Level AA color-contrast compliance.

The current paragraph theme "{current_theme}" produces a contrast ratio
of {contrast_ratio}:1 (foreground {foreground} on background {background}).
WCAG AA requires at least 4.5:1 for normal text and 3:1 for large text.

Suggest which available paragraph theme would provide better contrast
while preserving the visual intent. Return ONLY the machine name of the
recommended theme (e.g. "dark", "light", "high_contrast"). No explanation.
"""

# JSON schema for structured alt-text responses from the vision model.
ALT_TEXT_SCHEMA = {
    "type": "object",
    "properties": {
        "alt_text": {"type": "string"},
        "decorative": {"type": "boolean"},
    },
    "required": ["alt_text", "decorative"],
    "additionalProperties": False,
}

IMAGE_ALT_TEXT_PROMPT = """\
Return JSON only matching this schema:
{{"type":"object","properties":{{"alt_text":{{"type":"string"}},"decorative":{{"type":"boolean"}}}},"required":["alt_text","decorative"]}}

If the image is purely decorative, set decorative=true and alt_text="".
Otherwise set decorative=false and write concise WCAG-friendly alt text under 125 characters.
Describe what is shown and include any essential visible text.
Do not start with "image of" or "photo of".
{context_line}
"""

IMAGE_ALT_TEXT_FUNCTIONAL_PROMPT = """\
Return JSON only matching this schema:
{{"type":"object","properties":{{"alt_text":{{"type":"string"}},"decorative":{{"type":"boolean"}}}},"required":["alt_text","decorative"]}}

This image links to "{link_target}" on a college website. It is functional, NOT decorative.
Set decorative=false and write concise WCAG-friendly alt text under 125 characters.
Describe what is shown, include any visible text, and convey the link purpose.
{context_line}
"""

# -- Content accuracy checking ------------------------------------------------

CONTENT_ACCURACY_SCHEMA = {
    "type": "object",
    "properties": {
        "image_description": {"type": "string"},
        "accurate": {"type": "boolean"},
        "issues": {
            "type": "array",
            "items": {"type": "string"},
        },
    },
    "required": ["image_description", "accurate", "issues"],
    "additionalProperties": False,
}

CONTENT_ACCURACY_PROMPT = """\
Return JSON only matching this schema:
{{"type":"object","properties":{{"image_description":{{"type":"string"}},"accurate":{{"type":"boolean"}},"issues":{{"type":"array","items":{{"type":"string"}}}}}},"required":["image_description","accurate","issues"]}}

You are checking whether text near an image accurately describes what the image shows.

## Text claims about this image:
{claims}

## Instructions:
1. Describe what you actually see in the image in "image_description" (one sentence, under 150 characters).
2. Set "accurate" to true ONLY if ALL text claims reasonably match the image content.
3. If any claim does NOT match, set "accurate" to false and list each mismatch in "issues" — state what the text claims vs what the image actually shows.

Be strict: if text says "graduation ceremony" but the image shows a science lab, that is inaccurate.
Ignore minor differences in wording — focus on factual mismatches.
"""

IMAGE_ALT_TEXT_FALLBACK_PROMPT = """\
Return JSON only matching this schema:
{{"type":"object","properties":{{"alt_text":{{"type":"string"}},"decorative":{{"type":"boolean"}}}},"required":["alt_text","decorative"]}}

Describe this image in one sentence under 125 characters. Include any visible text.
Set decorative=false.
"""
