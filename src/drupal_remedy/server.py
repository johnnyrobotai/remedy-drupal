"""MCP server for Drupal accessibility remediation."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from drupal_remedy.client import DrupalClient, DrupalClientError
from drupal_remedy.config import load_config
from drupal_remedy.scan_payload import expand_scan_violations, is_template_noise_rule
from drupal_remedy.tracer import ViolationTracer

logger = logging.getLogger(__name__)


async def create_and_run_server(
    config_path: str | None = None,
    env_path: str | None = None,
) -> None:
    """Build and run the MCP server on STDIO transport."""
    try:
        from mcp.server import Server
        from mcp.server.stdio import stdio_server
    except ImportError:
        raise SystemExit(
            "The 'mcp' package is not installed. "
            "Install with: pip install remedy-drupal"
        )

    yaml_p = Path(config_path) if config_path else Path("config.yaml")
    env_p = Path(env_path) if env_path else Path(".env")
    configs = load_config(yaml_path=yaml_p, env_path=env_p)
    llm_cfg = configs.pop("__llm__", None)
    configs.pop("__pdf__", None)  # unused; PDF remediation handled externally

    # Build per-campus clients.
    clients: dict[str, DrupalClient] = {}
    for code, site_cfg in configs.items():
        clients[code] = DrupalClient(site_cfg)

    server = Server("remedy-drupal")

    def _get(campus_code: str) -> DrupalClient:
        code = campus_code.upper()
        if code not in clients:
            raise DrupalClientError(
                f"No client for campus {code}. "
                f"Available: {', '.join(clients.keys())}"
            )
        return clients[code]

    # ------------------------------------------------------------------
    # Scanning tools
    # ------------------------------------------------------------------

    @server.tool()
    async def scan_page(page_url: str, campus_code: str) -> str:
        """Scan a live Drupal page for WCAG 2.1 AA violations using authenticated Drupal cookies."""
        try:
            result = await _get(campus_code).scan_page(page_url)
            return json.dumps(result, indent=2)
        except DrupalClientError as exc:
            return json.dumps({"error": str(exc)})

    @server.tool()
    async def scan_batch(urls: list[str], campus_code: str) -> str:
        """Scan multiple page URLs for accessibility violations."""
        try:
            results = await _get(campus_code).scan_batch(urls)
            summary = {
                "total": len(results),
                "passing": sum(1 for r in results if r.get("passed")),
                "failing": sum(1 for r in results if not r.get("passed")),
                "pages": results,
            }
            return json.dumps(summary, indent=2)
        except DrupalClientError as exc:
            return json.dumps({"error": str(exc)})

    @server.tool()
    async def scan_content_list(
        campus_code: str,
        content_type: str = "",
        max_pages: int = 50,
    ) -> str:
        """Crawl the admin content listing and scan each page for violations."""
        try:
            results = await _get(campus_code).scan_content_list(
                content_type=content_type, max_pages=max_pages
            )
            return json.dumps(results, indent=2)
        except DrupalClientError as exc:
            return json.dumps({"error": str(exc)})

    # ------------------------------------------------------------------
    # Reading tools
    # ------------------------------------------------------------------

    @server.tool()
    async def get_page_info(nid: int, campus_code: str) -> str:
        """Get metadata: title, content type, moderation state, paragraph count."""
        try:
            meta = await _get(campus_code).get_page_metadata(nid)
            return json.dumps(meta, indent=2)
        except DrupalClientError as exc:
            return json.dumps({"error": str(exc)})

    @server.tool()
    async def get_page_body(nid: int, campus_code: str) -> str:
        """Extract body HTML from the edit form."""
        try:
            html = await _get(campus_code).get_body_html(nid)
            return json.dumps({"nid": nid, "html": html})
        except DrupalClientError as exc:
            return json.dumps({"error": str(exc)})

    @server.tool()
    async def get_paragraph_fields(
        nid: int, campus_code: str, row_index: int = 0
    ) -> str:
        """Get all field names, types, and values from a paragraph row."""
        try:
            fields = await _get(campus_code).get_paragraph_fields(nid, row_index)
            return json.dumps(
                {"nid": nid, "row_index": row_index, "fields": fields},
                indent=2,
            )
        except DrupalClientError as exc:
            return json.dumps({"error": str(exc)})

    @server.tool()
    async def get_media_info(mid: int, campus_code: str) -> str:
        """Get fields from a media entity (document, image, video)."""
        try:
            info = await _get(campus_code).get_media_info(mid)
            return json.dumps(info, indent=2)
        except DrupalClientError as exc:
            return json.dumps({"error": str(exc)})

    # ------------------------------------------------------------------
    # Tracing tools
    # ------------------------------------------------------------------

    @server.tool()
    async def trace_violations(nid: int, campus_code: str) -> str:
        """Scan a page and trace each violation to its Drupal source entity.

        Returns traced violations with entity type/ID/field and suggested fixes,
        plus untraced violations with likely causes.
        """
        try:
            client = _get(campus_code)
            tracer = ViolationTracer(client)

            url = (await client.get_page_metadata(nid)).get(
                "canonical_url"
            ) or await client.get_page_url(nid)

            scan = await client.scan_page(url)
            violations = [
                v for v in expand_scan_violations(scan["violations"])
                if not is_template_noise_rule(v["id"])
            ]

            report = await tracer.trace_all(nid, violations)
            return json.dumps(
                {
                    "nid": nid,
                    "url": url,
                    "total": report.total_violations,
                    "traced_count": len(report.traced),
                    "traced": [_traced_dict(t) for t in report.traced],
                    "untraced_count": len(report.untraced),
                    "untraced": report.untraced,
                    "trace_rate": f"{report.trace_rate:.0%}",
                },
                indent=2,
            )
        except DrupalClientError as exc:
            return json.dumps({"error": str(exc)})

    # ------------------------------------------------------------------
    # Writing tools
    # ------------------------------------------------------------------

    @server.tool()
    async def update_page_body(
        nid: int, campus_code: str, html: str, revision_message: str = ""
    ) -> str:
        """Update a node's body HTML via the edit form."""
        try:
            result = await _get(campus_code).update_body_html(
                nid, html, revision_message=revision_message
            )
            return json.dumps(result, indent=2)
        except DrupalClientError as exc:
            return json.dumps({"error": str(exc)})

    @server.tool()
    async def update_paragraph_fields(
        nid: int, campus_code: str, row_index: int, field_values: dict
    ) -> str:
        """Update fields on a paragraph row and save."""
        try:
            result = await _get(campus_code).update_paragraph_fields(
                nid, row_index, field_values,
                revision_message="Accessibility field update",
            )
            return json.dumps(result, indent=2)
        except DrupalClientError as exc:
            return json.dumps({"error": str(exc)})

    @server.tool()
    async def update_media_fields(
        mid: int, campus_code: str, fields: dict
    ) -> str:
        """Update fields on a media entity (alt text, title, description)."""
        try:
            result = await _get(campus_code).update_media_fields(mid, fields)
            return json.dumps(result, indent=2)
        except DrupalClientError as exc:
            return json.dumps({"error": str(exc)})

    @server.tool()
    async def replace_media_file(
        mid: int, campus_code: str, file_path: str
    ) -> str:
        """Upload a replacement file for a media entity (PDF swap)."""
        try:
            result = await _get(campus_code).replace_media_file(mid, file_path)
            return json.dumps(result, indent=2)
        except DrupalClientError as exc:
            return json.dumps({"error": str(exc)})

    # ------------------------------------------------------------------
    # Remediation tools
    # ------------------------------------------------------------------

    @server.tool()
    async def remediate_page(
        nid: int, campus_code: str, max_cycles: int = 3
    ) -> str:
        """Scan a page, fix body field violations via LLM, and verify."""
        try:
            from drupal_remedy.remediator import Remediator

            client = _get(campus_code)
            llm = await _make_llm_chat(llm_cfg)
            vision = await _make_vision_chat(llm_cfg)
            rem = Remediator(client, llm, vision_chat=vision)
            result = await rem.remediate_body(nid, max_cycles=max_cycles)
            return json.dumps(result, indent=2)
        except DrupalClientError as exc:
            return json.dumps({"error": str(exc)})

    @server.tool()
    async def remediate_traced(
        nid: int, campus_code: str, max_cycles: int = 2
    ) -> str:
        """Trace violations to source entities and auto-fix them via LLM."""
        try:
            from drupal_remedy.remediator import Remediator

            client = _get(campus_code)
            llm = await _make_llm_chat(llm_cfg)
            vision = await _make_vision_chat(llm_cfg)
            rem = Remediator(client, llm, vision_chat=vision)
            result = await rem.remediate_traced(nid, max_cycles=max_cycles)
            return json.dumps(result, indent=2)
        except DrupalClientError as exc:
            return json.dumps({"error": str(exc)})

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def _startup() -> None:
        for client in clients.values():
            await client.start()
        logger.info(
            "remedy-drupal MCP server started (%d campus clients)",
            len(clients),
        )

    async def _shutdown() -> None:
        for client in clients.values():
            await client.close()
        logger.info("remedy-drupal MCP server shut down.")

    async with stdio_server() as (read_stream, write_stream):
        await _startup()
        try:
            await server.run(
                read_stream,
                write_stream,
                server.create_initialization_options(),
            )
        finally:
            await _shutdown()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _traced_dict(t: Any) -> dict:
    return {
        "violation_id": t.violation_id,
        "impact": t.impact,
        "entity_type": t.entity_type,
        "entity_id": t.entity_id,
        "field_name": t.field_name,
        "current_value": t.current_value[:200],
        "edit_url": t.edit_url,
        "suggested_fix": t.suggested_fix,
        "row_index": t.row_index,
    }


async def _make_llm_chat(llm_cfg: Any) -> Any:
    """Create an LLM chat callable from config.

    Uses the native Ollama ``/api/chat`` endpoint when *api_mode* is
    ``"native"``; falls back to OpenAI-compatible ``/v1/chat/completions``
    otherwise.
    """
    if not llm_cfg:
        raise DrupalClientError("No LLM config — set llm section in config.yaml")

    import httpx

    api_key = llm_cfg.api_key
    base_url = llm_cfg.base_url
    model = llm_cfg.text_model
    native = getattr(llm_cfg, "api_mode", "openai_compat") == "native"
    endpoint_path = _chat_endpoint_path(base_url, native=native)

    client = httpx.AsyncClient(
        base_url=base_url,
        headers={"Authorization": f"Bearer {api_key}"},
        timeout=60.0,
    )

    options: dict[str, Any] = {"temperature": llm_cfg.temperature, "top_p": llm_cfg.top_p}
    if llm_cfg.seed is not None:
        options["seed"] = llm_cfg.seed

    if native:
        async def chat(messages: list[dict]) -> str:
            payload: dict[str, Any] = {
                "model": model,
                "messages": messages,
                "stream": False,
                "options": options,
            }
            if llm_cfg.disable_thinking:
                payload["think"] = False
            resp = await client.post(endpoint_path, json=payload)
            resp.raise_for_status()
            return resp.json()["message"]["content"]
    else:
        async def chat(messages: list[dict]) -> str:
            resp = await client.post(
                endpoint_path,
                json={"model": model, "messages": messages, "max_tokens": llm_cfg.text_max_tokens},
            )
            resp.raise_for_status()
            return resp.json()["choices"][0]["message"]["content"]

    return chat


async def _make_vision_chat(llm_cfg: Any) -> Any:
    """Create a vision chat callable from config.

    Uses the native Ollama ``/api/chat`` endpoint with the ``images``
    array and structured output ``format`` when *api_mode* is
    ``"native"``.  Falls back to OpenAI-compatible data-URI format
    otherwise.

    Returns the raw ``message.content`` string (JSON when using
    structured outputs on the native API).
    """
    if not llm_cfg or not getattr(llm_cfg, "vision_model", ""):
        return None

    import base64
    import httpx

    from drupal_remedy.prompts import ALT_TEXT_SCHEMA

    api_key = llm_cfg.api_key
    base_url = llm_cfg.base_url
    model = llm_cfg.vision_model
    native = getattr(llm_cfg, "api_mode", "openai_compat") == "native"
    endpoint_path = _chat_endpoint_path(base_url, native=native)

    client = httpx.AsyncClient(
        base_url=base_url,
        headers={"Authorization": f"Bearer {api_key}"},
        timeout=90.0,
    )

    options: dict[str, Any] = {"temperature": llm_cfg.temperature, "top_p": llm_cfg.top_p}
    if llm_cfg.seed is not None:
        options["seed"] = llm_cfg.seed

    if native:
        async def vision_chat(image_bytes: bytes, mime_type: str, prompt: str, *, seed_offset: int = 0, format_schema: dict | None = None) -> str:
            b64 = base64.b64encode(image_bytes).decode("ascii")
            opts = {**options}
            if llm_cfg.seed is not None:
                opts["seed"] = llm_cfg.seed + seed_offset
            payload: dict[str, Any] = {
                "model": model,
                "messages": [{"role": "user", "content": prompt, "images": [b64]}],
                "stream": False,
                "format": format_schema if format_schema is not None else ALT_TEXT_SCHEMA,
                "options": opts,
            }
            if llm_cfg.disable_thinking:
                payload["think"] = False
            resp = await client.post(endpoint_path, json=payload)
            resp.raise_for_status()
            return resp.json()["message"]["content"]
    else:
        async def vision_chat(image_bytes: bytes, mime_type: str, prompt: str, *, seed_offset: int = 0, format_schema: dict | None = None) -> str:
            b64 = base64.b64encode(image_bytes).decode("ascii")
            messages = [
                {
                    "role": "user",
                    "content": [
                        {"type": "image_url", "image_url": {"url": f"data:{mime_type};base64,{b64}"}},
                        {"type": "text", "text": prompt},
                    ],
                }
            ]
            resp = await client.post(
                endpoint_path,
                json={"model": model, "messages": messages, "max_tokens": llm_cfg.vision_max_tokens, "temperature": llm_cfg.temperature},
            )
            resp.raise_for_status()
            return resp.json()["choices"][0]["message"]["content"]

    return vision_chat


def _chat_endpoint_path(base_url: str, *, native: bool) -> str:
    """Return the chat path for root or API-scoped Ollama/OpenAI base URLs."""
    from urllib.parse import urlparse

    path = urlparse(base_url).path.rstrip("/")
    if native:
        return "/chat" if path.endswith("/api") else "/api/chat"
    return "/chat/completions" if path.endswith("/v1") else "/v1/chat/completions"
