"""Helpers for flattening axe results and building Drupal module scan payloads."""

from __future__ import annotations

import hashlib
import uuid
from collections import Counter
from datetime import UTC, datetime
from typing import Any

from drupal_remedy.content_accuracy import ContentAccuracyReport
from drupal_remedy.tracer import ViolationTracer

TEMPLATE_NOISE_RULES = {"region", "landmark-unique", "aria-allowed-attr"}


def make_run_id(prefix: str = "scan") -> str:
    """Generate a lightweight sortable-looking run identifier."""
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def utc_now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def is_template_noise_rule(rule_id: str | None) -> bool:
    """Return True for site-template rules that content remediation should not handle."""
    return (rule_id or "") in TEMPLATE_NOISE_RULES


def expand_scan_violations(violations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Flatten rule-level axe results into one item per failing node."""
    expanded: list[dict[str, Any]] = []
    for violation in violations:
        nodes = violation.get("nodes") or []
        if nodes:
            for node in nodes:
                expanded.append({
                    **violation,
                    "html": node.get("html", ""),
                    "selector": node.get("selector", ""),
                    "target": node.get("target", []),
                    "frame_path": node.get("frame_path", []),
                    "failure_summary": node.get("failure_summary", ""),
                    "xpath": node.get("xpath"),
                    "bounding_box": node.get("bounding_box"),
                })
            continue

        for snippet in violation.get("html_snippets", [""]):
            expanded.append({
                **violation,
                "html": snippet,
                "selector": "",
                "target": [],
                "frame_path": [],
                "failure_summary": "",
                "xpath": None,
                "bounding_box": None,
            })
    return expanded


def fingerprint_issue(
    page_url: str,
    rule_id: str,
    selector: str,
    html_snippet: str,
    frame_path: list[str] | None = None,
) -> str:
    """Create a stable fingerprint for a logical issue occurrence."""
    normalized = "\n".join([
        page_url.strip(),
        rule_id.strip(),
        selector.strip(),
        " > ".join(frame_path or []).strip(),
        " ".join(html_snippet.split()),
    ])
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


async def build_scan_payload(
    client: Any,
    *,
    site_code: str,
    entity_type: str,
    entity_id: int | None,
    canonical_url: str,
    run_id: str | None = None,
    trigger_type: str = "manual",
    started_at: str | None = None,
    completed_at: str | None = None,
    include_content_accuracy: bool = False,
    content_accuracy_report: ContentAccuracyReport | None = None,
) -> dict[str, Any]:
    """Build a Drupal-module-friendly scan payload with per-issue targets."""
    page_meta: dict[str, Any] = {}
    if entity_type == "node" and entity_id is not None:
        try:
            page_meta = await client.get_page_metadata(entity_id)
        except Exception:
            page_meta = {}

    scan = await client.scan_page(canonical_url)
    expanded = expand_scan_violations(scan["violations"])
    content_issues = [
        issue for issue in expanded
        if not is_template_noise_rule(issue.get("id"))
    ]

    trace_map: dict[tuple[str, str], Any] = {}
    if entity_type == "node" and entity_id is not None and content_issues:
        tracer = ViolationTracer(client, base_url=client.base_url)
        report = await tracer.trace_all(
            entity_id,
            content_issues,
        )
        for traced in report.traced:
            trace_map[(traced.violation_id, traced.html_snippet)] = traced

    issues: list[dict[str, Any]] = []
    status_counter = Counter()
    impact_counter = Counter()
    for issue in content_issues:
        selector = issue.get("selector", "")
        frame_path = issue.get("frame_path", [])
        html_snippet = issue.get("html", "")
        fingerprint = fingerprint_issue(
            canonical_url,
            issue.get("id", ""),
            selector,
            html_snippet,
            frame_path=frame_path,
        )

        traced = trace_map.get((issue.get("id", ""), html_snippet))
        fixability = "unknown"
        trace_payload = {"trace_status": "untraced", "entities": []}
        if traced is not None:
            fixability = "auto" if traced.entity_type in {"media", "paragraph_field", "node_field"} else "manual"
            trace_payload = {
                "trace_status": "traced",
                "entities": [{
                    "entity_type": traced.entity_type,
                    "entity_id": traced.entity_id,
                    "field_name": traced.field_name,
                    "row_index": traced.row_index,
                    "edit_url": traced.edit_url,
                    "current_value_excerpt": traced.current_value[:200],
                    "suggested_fix": traced.suggested_fix,
                }],
            }

        issue_status = "open"
        status_counter[issue_status] += 1
        impact_counter[issue.get("impact", "unknown")] += 1

        issues.append({
            "fingerprint": fingerprint,
            "engine": "axe-core",
            "category": "accessibility",
            "rule_id": issue.get("id", ""),
            "rule_url": issue.get("help_url"),
            "impact": issue.get("impact", "unknown"),
            "wcag_tags": issue.get("tags", []),
            "description": issue.get("description", ""),
            "help": issue.get("help", ""),
            "help_url": issue.get("help_url", ""),
            "failure_summary": issue.get("failure_summary", ""),
            "review_state": "needs_review",
            "status": issue_status,
            "fixability": fixability,
            "occurrence_count": 1,
            "primary_target": {
                "selector": selector,
                "frame_path": frame_path,
                "html_snippet": html_snippet,
                "text_context": "",
            },
            "targets": [{
                "selector": selector,
                "xpath": issue.get("xpath"),
                "html_snippet": html_snippet,
                "bounding_box": issue.get("bounding_box"),
            }],
            "trace": trace_payload,
            "state": {
                "assigned_uid": None,
                "snoozed_until": None,
                "resolution_note": None,
                "first_seen_at": completed_at or utc_now_iso(),
                "last_seen_at": completed_at or utc_now_iso(),
            },
        })

    accuracy = content_accuracy_report
    if include_content_accuracy and accuracy is None:
        accuracy = ContentAccuracyReport(nid=entity_id or 0, images_checked=0, issues=[])

    return {
        "schema_version": "1.0",
        "run": {
            "run_id": run_id or make_run_id("scan"),
            "status": "completed",
            "trigger_type": trigger_type,
            "started_at": started_at or utc_now_iso(),
            "completed_at": completed_at or utc_now_iso(),
        },
        "page": {
            "site_code": site_code,
            "entity_type": entity_type,
            "entity_id": entity_id,
            "entity_uuid": page_meta.get("uuid"),
            "title": page_meta.get("title", ""),
            "bundle": page_meta.get("type", ""),
            "canonical_url": canonical_url,
            "path_alias": page_meta.get("path_alias", ""),
        },
        "render": {
            "engine": "playwright",
            "browser": "chromium",
            "viewport": {"width": 1440, "height": 2200},
            "authenticated": True,
            "interaction_profile": "default",
            "wait_strategy": "load+scroll+explicit_wait",
            "screenshot_url": None,
        },
        "summary": {
            "total_issues": len(issues),
            "by_impact": dict(impact_counter),
            "by_status": dict(status_counter),
            "engines": ["axe-core"],
            "content_accuracy_issues": len(accuracy.issues) if accuracy else 0,
        },
        "issues": issues,
        "content_accuracy": {
            "enabled": bool(include_content_accuracy),
            "issues": [] if not accuracy else accuracy.to_dict().get("issues", []),
        },
    }
