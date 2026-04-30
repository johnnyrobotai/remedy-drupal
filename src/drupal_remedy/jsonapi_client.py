"""HTTP client for Drupal JSON:API — reads and writes without a browser."""

from __future__ import annotations

import logging
import re
from typing import Any

import httpx

from drupal_remedy.config import DrupalSiteConfig
from drupal_remedy.exceptions import DrupalClientError

logger = logging.getLogger(__name__)

_PRIMARY_NODE_TYPES = ("landing", "modular", "page")


def _safe_json(resp: httpx.Response) -> Any:
    """Parse JSON from a response, stripping PHP warnings that Drupal may prepend."""
    try:
        return resp.json()
    except Exception:
        text = resp.text
        idx = text.find("{")
        if idx > 0:
            import json as _json
            return _json.loads(text[idx:])
        raise

_MEDIA_FILE_FIELDS: dict[str, str] = {
    "document": "field_media_document",
    "image": "field_media_image",
    "video": "field_media_video_file",
}

_MEDIA_URL_FIELDS: dict[str, str] = {
    "remote_video": "field_media_oembed_video",
}


_PARAGRAPH_FIELDS = ("field_row", "field_item")


def _find_paragraph_data(node_data: dict[str, Any]) -> list[dict[str, Any]]:
    """Find paragraph relationship data in a node, trying known field names."""
    rels = node_data.get("relationships", {})
    for field in _PARAGRAPH_FIELDS:
        data = (rels.get(field) or {}).get("data")
        if data is not None:
            return data
    return []


def _extract_refs_from_entity(
    entity: dict[str, Any],
) -> tuple[set[str], list[tuple[str, str]]]:
    """Extract media UUIDs and paragraph references from an entity's relationships.

    Returns ``(media_uuids, paragraph_refs)`` where *paragraph_refs* is
    a list of ``(uuid, type_string)`` tuples.
    """
    media_uuids: set[str] = set()
    para_refs: list[tuple[str, str]] = []

    for _field, rel in (entity.get("relationships") or {}).items():
        data = rel.get("data")
        if data is None:
            continue
        items = data if isinstance(data, list) else [data]
        for item in items:
            ref_type = item.get("type", "")
            ref_id = item.get("id", "")
            if not ref_id:
                continue
            if ref_type.startswith("media--"):
                media_uuids.add(ref_id)
            elif ref_type.startswith("paragraph--"):
                para_refs.append((ref_id, ref_type))

    return media_uuids, para_refs


_DRUPAL_MEDIA_RE = re.compile(
    r'<drupal-media[^>]+data-entity-uuid="([^"]+)"'
)


def _parse_drupal_media_embeds(html: str) -> set[str]:
    """Extract media entity UUIDs from ``<drupal-media>`` embeds in HTML."""
    if not html or "drupal-media" not in html:
        return set()
    return set(_DRUPAL_MEDIA_RE.findall(html))


class JsonApiClient:
    """Async HTTP client for Drupal's JSON:API.

    Call ``login()`` before use and ``close()`` when done.
    """

    def __init__(self, config: DrupalSiteConfig) -> None:
        self._config = config
        self._base = config.base_url.rstrip("/")
        self._csrf_token: str = ""
        self._node_cache: dict[int, tuple[str, str]] = {}

        auth = None
        if config.http_auth_username:
            auth = httpx.BasicAuth(config.http_auth_username, config.http_auth_password)

        self._http = httpx.AsyncClient(
            base_url=self._base,
            auth=auth,
            timeout=config.timeout,
            verify=config.verify_ssl,
        )

    async def login(self) -> None:
        resp = await self._http.post(
            "/user/login?_format=json",
            json={"name": self._config.auth.username, "pass": self._config.auth.password},
            headers={"Content-Type": "application/json"},
        )
        if resp.status_code != 200:
            raise DrupalClientError(
                f"Login failed ({resp.status_code}): {resp.text[:200]}"
            )
        logger.info("Logged in as %s via JSON:API", self._config.auth.username)

    async def _ensure_csrf(self) -> str:
        if not self._csrf_token:
            resp = await self._http.get("/session/token")
            resp.raise_for_status()
            self._csrf_token = resp.text.strip()
        return self._csrf_token

    def get_session_cookies(self, domain: str) -> list[dict[str, str]]:
        """Return Drupal session cookies formatted for Playwright ``add_cookies``."""
        return [
            {"name": name, "value": value, "domain": domain, "path": "/"}
            for name, value in self._http.cookies.items()
        ]

    async def close(self) -> None:
        await self._http.aclose()

    async def resolve_node(self, nid: int) -> tuple[str, str]:
        if nid in self._node_cache:
            return self._node_cache[nid]

        for node_type in _PRIMARY_NODE_TYPES:
            url = f"/jsonapi/node/{node_type}?filter[drupal_internal__nid]={nid}&fields[node--{node_type}]=title"
            resp = await self._http.get(url, headers={"Accept": "application/vnd.api+json"})
            if resp.status_code != 200:
                continue
            data = _safe_json(resp).get("data", [])
            if data:
                uuid = data[0]["id"]
                self._node_cache[nid] = (node_type, uuid)
                return node_type, uuid

        index_resp = await self._http.get("/jsonapi", headers={"Accept": "application/vnd.api+json"})
        index_resp.raise_for_status()
        links = _safe_json(index_resp).get("links", {})
        all_types = [k.replace("node--", "") for k in links if k.startswith("node--")]

        for node_type in all_types:
            if node_type in _PRIMARY_NODE_TYPES:
                continue
            url = f"/jsonapi/node/{node_type}?filter[drupal_internal__nid]={nid}&fields[node--{node_type}]=title"
            resp = await self._http.get(url, headers={"Accept": "application/vnd.api+json"})
            if resp.status_code != 200:
                continue
            data = _safe_json(resp).get("data", [])
            if data:
                uuid = data[0]["id"]
                self._node_cache[nid] = (node_type, uuid)
                return node_type, uuid

        raise DrupalClientError(f"Node {nid} not found in any content type")

    async def _get_json(self, path: str) -> dict[str, Any]:
        resp = await self._http.get(path, headers={"Accept": "application/vnd.api+json"})
        if resp.status_code != 200:
            raise DrupalClientError(
                f"GET {path} returned {resp.status_code}: {resp.text[:200]}"
            )
        return _safe_json(resp)

    async def _patch_json(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        csrf = await self._ensure_csrf()
        resp = await self._http.patch(
            path,
            json=payload,
            headers={
                "Accept": "application/vnd.api+json",
                "Content-Type": "application/vnd.api+json",
                "X-CSRF-Token": csrf,
            },
        )
        if resp.status_code not in (200, 204):
            raise DrupalClientError(
                f"PATCH {path} returned {resp.status_code}: {resp.text[:300]}"
            )
        return _safe_json(resp) if resp.status_code == 200 else {}

    # ------------------------------------------------------------------
    # Read helpers
    # ------------------------------------------------------------------

    async def _fetch_node(self, nid: int) -> tuple[str, dict[str, Any], list[dict[str, Any]]]:
        """Resolve *nid* and fetch the full node with included paragraphs.

        Returns ``(content_type, node_data, included)`` where *node_data* is
        the JSON:API resource object and *included* is the sideloaded list.

        Tries ``field_row`` first, then ``field_item``, then falls back to
        fetching without includes for content types that have neither.
        """
        node_type, uuid = await self.resolve_node(nid)
        base = f"/jsonapi/node/{node_type}/{uuid}"
        for para_field in ("field_row", "field_item"):
            try:
                result = await self._get_json(f"{base}?include={para_field}")
                node_data: dict[str, Any] = result["data"]
                included: list[dict[str, Any]] = result.get("included", [])
                return node_type, node_data, included
            except DrupalClientError as exc:
                if "not a valid relationship" in str(exc):
                    continue
                raise
        # No paragraph field — fetch without include
        result = await self._get_json(base)
        return node_type, result["data"], []

    async def get_page_metadata(self, nid: int) -> dict[str, Any]:
        """Return a metadata dict for the node identified by *nid*."""
        node_type, node_data, included = await self._fetch_node(nid)
        attrs = node_data.get("attributes", {})
        alias = (attrs.get("path") or {}).get("alias", "")
        para_data = _find_paragraph_data(node_data)
        return {
            "nid": nid,
            "title": attrs.get("title", ""),
            "content_type": node_type,
            "canonical_url": f"{self._base}{alias}" if alias else self._base,
            "has_paragraphs": len(para_data) > 0,
            "paragraph_count": len(para_data),
        }

    async def list_paragraphs(self, nid: int) -> list[dict[str, Any]]:
        """Return an ordered list of paragraph stubs for *nid*."""
        node_type, node_data, _included = await self._fetch_node(nid)
        para_data = _find_paragraph_data(node_data)
        return [
            {"index": idx, "uuid": p["id"], "type": p["type"]}
            for idx, p in enumerate(para_data)
        ]

    async def get_body_html(self, nid: int) -> str:
        """Return the body field HTML for *nid*."""
        _node_type, node_data, _included = await self._fetch_node(nid)
        body = (node_data.get("attributes") or {}).get("body") or {}
        return body.get("value", "")

    async def get_page_url(self, nid: int) -> str:
        """Return the canonical URL (base + path alias) for *nid*."""
        _node_type, node_data, _included = await self._fetch_node(nid)
        alias = ((node_data.get("attributes") or {}).get("path") or {}).get("alias", "")
        return f"{self._base}{alias}" if alias else self._base

    async def get_paragraph_fields(self, nid: int, row_index: int) -> list[dict[str, Any]]:
        """Fetch all fields for a specific paragraph row.

        Returns a list of dicts with keys: name, value, type — matching
        the format the tracer expects.
        """
        paragraphs = await self.list_paragraphs(nid)
        if row_index < 0 or row_index >= len(paragraphs):
            raise DrupalClientError(
                f"Paragraph index {row_index} out of range (0-{len(paragraphs) - 1})"
            )
        para = paragraphs[row_index]
        para_type = para["type"].replace("paragraph--", "")
        path = f"/jsonapi/paragraph/{para_type}/{para['uuid']}"
        result = await self._get_json(path)
        attrs = result.get("data", {}).get("attributes", {})

        fields = []
        for name, value in attrs.items():
            if name.startswith("field_") or name == "body":
                if isinstance(value, dict):
                    str_value = value.get("value", str(value))
                elif value is None:
                    str_value = ""
                else:
                    str_value = str(value)
                fields.append({"name": name, "value": str_value, "type": "field"})
        return fields

    async def get_media_info(self, mid: int) -> dict[str, Any]:
        """Fetch a media entity by *mid*, trying common media types."""
        media_types = ("image", "document", "remote_video", "video")
        for media_type in media_types:
            url = (
                f"/jsonapi/media/{media_type}"
                f"?filter[drupal_internal__mid]={mid}"
            )
            resp = await self._http.get(
                url, headers={"Accept": "application/vnd.api+json"}
            )
            if resp.status_code != 200:
                continue
            data = _safe_json(resp).get("data", [])
            if data:
                return data[0]
        raise DrupalClientError(f"Media {mid} not found in any media type")

    async def download_file(self, url: str) -> tuple[bytes, str]:
        """Download a file and return (bytes, content_type)."""
        resp = await self._http.get(url)
        resp.raise_for_status()
        return resp.content, resp.headers.get("content-type", "image/jpeg")

    # ------------------------------------------------------------------
    # Write helpers
    # ------------------------------------------------------------------

    async def update_body_html(self, nid: int, html: str, *, revision_message: str = "", publish: bool = True) -> dict[str, Any]:
        """Update the body field on a node via PATCH."""
        node_type, uuid = await self.resolve_node(nid)
        payload: dict[str, Any] = {
            "data": {
                "type": f"node--{node_type}",
                "id": uuid,
                "attributes": {
                    "body": {"value": html, "format": "rich_text"},
                },
            }
        }
        if revision_message:
            payload["data"]["attributes"]["revision_log"] = revision_message
        return await self._patch_json(f"/jsonapi/node/{node_type}/{uuid}", payload)

    async def update_paragraph_fields(self, nid: int, row_index: int, field_values: dict[str, str], *, revision_message: str = "", publish: bool = True) -> dict[str, Any]:
        """Update fields on a paragraph entity via PATCH."""
        paras = await self.list_paragraphs(nid)
        if row_index >= len(paras):
            raise DrupalClientError(f"Paragraph row {row_index} not found on node {nid}")
        para = paras[row_index]
        payload = {
            "data": {
                "type": para["type"],
                "id": para["uuid"],
                "attributes": field_values,
            }
        }
        para_type_short = para["type"].replace("paragraph--", "")
        return await self._patch_json(f"/jsonapi/paragraph/{para_type_short}/{para['uuid']}", payload)

    async def list_content_urls(
        self, content_type: str = "", max_pages: int = 50
    ) -> list[str]:
        """List canonical URLs for published nodes via JSON:API.

        If content_type is given (e.g. "landing"), only that type is queried.
        Otherwise queries the primary types: landing, modular, page.
        """
        urls: list[str] = []
        types = [content_type] if content_type else list(_PRIMARY_NODE_TYPES)

        for node_type in types:
            if len(urls) >= max_pages:
                break
            offset = 0
            limit = min(50, max_pages - len(urls))
            while len(urls) < max_pages:
                path = (
                    f"/jsonapi/node/{node_type}"
                    f"?fields[node--{node_type}]=title,path,drupal_internal__nid"
                    f"&filter[status]=1"
                    f"&page[limit]={limit}"
                    f"&page[offset]={offset}"
                )
                try:
                    body = await self._get_json(path)
                except DrupalClientError:
                    break  # type doesn't exist or no access
                data = body.get("data", [])
                if not data:
                    break
                for node in data:
                    alias = (node.get("attributes", {}).get("path") or {}).get("alias", "")
                    nid = node.get("attributes", {}).get("drupal_internal__nid", "")
                    if alias:
                        urls.append(f"{self._base}{alias}")
                    elif nid:
                        urls.append(f"{self._base}/node/{nid}")
                    if len(urls) >= max_pages:
                        break
                offset += len(data)
                if len(data) < limit:
                    break  # no more pages
        return urls

    async def replace_media_file(self, mid: int, file_path: str) -> dict[str, Any]:
        """Upload a replacement file for a media entity."""
        import os

        info = await self.get_media_info(mid)
        if info is None:
            raise DrupalClientError(f"Media {mid} not found")

        media_type = info["type"].replace("media--", "")
        uuid = info["id"]

        # Determine the file field name based on media type
        field_map = {
            "image": "field_media_image",
            "document": "field_media_document",
            "video": "field_media_video_file",
            "remote_video": "field_media_oembed_video",
        }
        field_name = field_map.get(media_type, "field_media_document")

        filename = os.path.basename(file_path)
        with open(file_path, "rb") as f:
            file_data = f.read()

        csrf = await self._ensure_csrf()
        resp = await self._http.post(
            f"/jsonapi/media/{media_type}/{uuid}/{field_name}",
            content=file_data,
            headers={
                "Accept": "application/vnd.api+json",
                "Content-Type": "application/octet-stream",
                "Content-Disposition": f'file; filename="{filename}"',
                "X-CSRF-Token": csrf,
            },
        )
        if resp.status_code not in (200, 201, 204):
            raise DrupalClientError(
                f"File upload failed ({resp.status_code}): {resp.text[:300]}"
            )
        return _safe_json(resp) if resp.text else {"status": "uploaded", "filename": filename}

    async def update_media_fields(self, mid: int, field_values: dict[str, str]) -> dict[str, Any]:
        """Update fields on a media entity via PATCH."""
        info = await self.get_media_info(mid)
        if info is None:
            raise DrupalClientError(f"Media {mid} not found")
        media_type = info["type"].replace("media--", "")
        uuid = info["id"]
        payload = {
            "data": {
                "type": f"media--{media_type}",
                "id": uuid,
                "attributes": field_values,
            }
        }
        return await self._patch_json(f"/jsonapi/media/{media_type}/{uuid}", payload)

    async def list_media_documents(
        self, *, page_limit: int = 0, page_size: int = 50
    ) -> list[dict[str, Any]]:
        """List published media/document entities with file URLs.

        Returns list of dicts: {mid, name, filename, file_url}.
        page_limit=0 means no cap.
        """
        results: list[dict[str, Any]] = []
        offset = 0
        while True:
            limit = min(page_size, page_limit - len(results)) if page_limit else page_size
            path = (
                f"/jsonapi/media/document"
                f"?include=field_media_document"
                f"&fields[media--document]=drupal_internal__mid,name,field_media_document"
                f"&fields[file--file]=filename,uri"
                f"&filter[status]=1"
                f"&page[limit]={limit}"
                f"&page[offset]={offset}"
            )
            try:
                body = await self._get_json(path)
            except DrupalClientError as exc:
                logger.warning(
                    "list_media_documents truncated at offset %d: %s",
                    offset, exc,
                )
                break
            data = body.get("data", [])
            if not data:
                break
            included = {item["id"]: item for item in body.get("included", [])}
            for node in data:
                attrs = node.get("attributes", {})
                mid = attrs.get("drupal_internal__mid")
                name = attrs.get("name", "")
                file_rel = (
                    (node.get("relationships", {}).get("field_media_document") or {})
                    .get("data") or {}
                )
                file_uuid = file_rel.get("id", "")
                file_node = included.get(file_uuid, {})
                file_attrs = file_node.get("attributes", {})
                filename = file_attrs.get("filename", "")
                uri = (file_attrs.get("uri") or {}).get("url", "")
                if uri and not uri.startswith("http"):
                    uri = f"{self._base}{uri}"
                results.append({
                    "mid": mid, "name": name, "filename": filename, "file_url": uri,
                })
                if page_limit and len(results) >= page_limit:
                    break
            offset += len(data)
            if (page_limit and len(results) >= page_limit) or len(data) < limit:
                break
        return results

    async def list_media_by_type(
        self, media_type: str, *, page_limit: int = 0, page_size: int = 50
    ) -> list[dict[str, Any]]:
        """List published media entities of *media_type* with file info.

        Returns list of ``{mid, uuid, name, filename, file_url, media_type}``.
        """
        results: list[dict[str, Any]] = []
        offset = 0
        file_field = _MEDIA_FILE_FIELDS.get(media_type)
        url_field = _MEDIA_URL_FIELDS.get(media_type)

        while True:
            limit = (
                min(page_size, page_limit - len(results)) if page_limit else page_size
            )
            if file_field:
                path = (
                    f"/jsonapi/media/{media_type}"
                    f"?include={file_field}"
                    f"&fields[media--{media_type}]=drupal_internal__mid,name,{file_field}"
                    f"&fields[file--file]=filename,uri"
                    f"&filter[status]=1"
                    f"&page[limit]={limit}"
                    f"&page[offset]={offset}"
                )
            elif url_field:
                path = (
                    f"/jsonapi/media/{media_type}"
                    f"?fields[media--{media_type}]=drupal_internal__mid,name,{url_field}"
                    f"&filter[status]=1"
                    f"&page[limit]={limit}"
                    f"&page[offset]={offset}"
                )
            else:
                break

            try:
                body = await self._get_json(path)
            except DrupalClientError as exc:
                logger.warning(
                    "list_media_by_type(%s) truncated at offset %d: %s",
                    media_type, offset, exc,
                )
                break

            data = body.get("data", [])
            if not data:
                break

            included = {item["id"]: item for item in body.get("included", [])}

            for entity in data:
                attrs = entity.get("attributes", {})
                mid = attrs.get("drupal_internal__mid")
                name = attrs.get("name", "")
                uuid = entity.get("id", "")
                filename = ""
                file_url = ""

                if file_field:
                    file_rel = (
                        (entity.get("relationships", {}).get(file_field) or {})
                        .get("data") or {}
                    )
                    file_uuid = file_rel.get("id", "")
                    file_entity = included.get(file_uuid, {})
                    file_attrs = file_entity.get("attributes", {})
                    filename = file_attrs.get("filename", "")
                    uri = (file_attrs.get("uri") or {}).get("url", "")
                    if uri and not uri.startswith("http"):
                        uri = f"{self._base}{uri}"
                    file_url = uri
                elif url_field:
                    file_url = attrs.get(url_field, "")

                results.append({
                    "mid": mid,
                    "uuid": uuid,
                    "name": name,
                    "filename": filename,
                    "file_url": file_url,
                    "media_type": media_type,
                })
                if page_limit and len(results) >= page_limit:
                    break

            offset += len(data)
            if (page_limit and len(results) >= page_limit) or len(data) < limit:
                break

        return results

    async def trace_media_references(self) -> dict[str, list[dict[str, Any]]]:
        """Crawl all published nodes and map media UUIDs to referencing nodes.

        Returns ``{media_uuid: [{nid, node_type}, ...]}``.
        """
        index = await self._get_json("/jsonapi")
        node_types = sorted(
            k.replace("node--", "")
            for k in index.get("links", {})
            if k.startswith("node--")
        )

        refs: dict[str, list[dict[str, Any]]] = {}
        node_count = 0

        for node_type in node_types:
            offset = 0
            use_includes = True

            while True:
                path = (
                    f"/jsonapi/node/{node_type}"
                    f"?filter[status]=1"
                    f"&page[limit]=50"
                    f"&page[offset]={offset}"
                )
                if use_includes:
                    path += "&include=field_row,field_item"

                try:
                    body = await self._get_json(path)
                except DrupalClientError:
                    if use_includes:
                        use_includes = False
                        path = (
                            f"/jsonapi/node/{node_type}"
                            f"?filter[status]=1"
                            f"&page[limit]=50"
                            f"&page[offset]={offset}"
                        )
                        try:
                            body = await self._get_json(path)
                        except DrupalClientError:
                            break
                    else:
                        break

                data = body.get("data", [])
                if not data:
                    break

                included_map = {
                    item["id"]: item for item in body.get("included", [])
                }

                for node in data:
                    nid = (node.get("attributes") or {}).get(
                        "drupal_internal__nid", 0
                    )
                    node_ref = {"nid": nid, "node_type": node_type}

                    # Direct media refs from node relationships
                    media_uuids, para_refs = _extract_refs_from_entity(node)
                    for muuid in media_uuids:
                        refs.setdefault(muuid, []).append(node_ref)

                    # Body HTML embeds
                    body_html = (
                        (node.get("attributes") or {}).get("body") or {}
                    ).get("value", "")
                    for muuid in _parse_drupal_media_embeds(body_html):
                        refs.setdefault(muuid, []).append(node_ref)

                    # Process paragraph references
                    paragraphs_to_fetch: list[tuple[str, str]] = []
                    for p_uuid, p_type in para_refs:
                        if p_uuid in included_map:
                            inc_entity = included_map[p_uuid]
                            inc_media, inc_paras = _extract_refs_from_entity(
                                inc_entity
                            )
                            for muuid in inc_media:
                                refs.setdefault(muuid, []).append(node_ref)
                            paragraphs_to_fetch.extend(inc_paras)
                        else:
                            paragraphs_to_fetch.append((p_uuid, p_type))

                    if paragraphs_to_fetch:
                        await self._fetch_paragraph_media_refs(
                            paragraphs_to_fetch, node_ref, refs, depth=1
                        )

                    node_count += 1
                    if node_count % 100 == 0:
                        logger.info("Traced %d nodes...", node_count)

                offset += len(data)
                if len(data) < 50:
                    break

        logger.info(
            "Trace complete: %d nodes, %d unique media refs",
            node_count,
            len(refs),
        )
        return refs

    async def _fetch_paragraph_media_refs(
        self,
        paragraph_refs: list[tuple[str, str]],
        node_ref: dict[str, Any],
        refs: dict[str, list[dict[str, Any]]],
        depth: int,
        max_depth: int = 5,
    ) -> None:
        """Recursively fetch paragraphs and extract media references."""
        if depth >= max_depth or not paragraph_refs:
            return

        seen: set[str] = set()
        for p_uuid, p_type in paragraph_refs:
            if p_uuid in seen:
                continue
            seen.add(p_uuid)

            para_type_short = p_type.replace("paragraph--", "")
            try:
                result = await self._get_json(
                    f"/jsonapi/paragraph/{para_type_short}/{p_uuid}"
                )
            except DrupalClientError:
                continue

            entity = result.get("data", {})
            media_uuids, nested_para_refs = _extract_refs_from_entity(entity)
            for muuid in media_uuids:
                refs.setdefault(muuid, []).append(node_ref)

            if nested_para_refs:
                await self._fetch_paragraph_media_refs(
                    nested_para_refs, node_ref, refs, depth + 1, max_depth
                )

    async def audit_media_usage(self) -> dict[str, Any]:
        """Audit which media entities are referenced by published nodes.

        Returns a dict with ``referenced``, ``unreferenced`` lists and a
        ``summary`` block.
        """
        # 1. Fetch all media across all types
        all_media: list[dict[str, Any]] = []
        for media_type in ("document", "image", "video", "remote_video"):
            try:
                media = await self.list_media_by_type(media_type)
                all_media.extend(media)
                logger.info("Fetched %d %s media entities", len(media), media_type)
            except DrupalClientError:
                logger.warning("Could not fetch %s media", media_type)

        # 2. Trace references
        refs = await self.trace_media_references()

        # 3. Split into referenced / unreferenced
        referenced: list[dict[str, Any]] = []
        unreferenced: list[dict[str, Any]] = []

        for media in all_media:
            uuid = media["uuid"]
            if uuid in refs:
                referenced.append({**media, "referenced_by": refs[uuid]})
            else:
                unreferenced.append(media)

        # 4. Summary by type
        by_type: dict[str, dict[str, int]] = {}
        for media in all_media:
            mt = media["media_type"]
            if mt not in by_type:
                by_type[mt] = {"total": 0, "referenced": 0, "unreferenced": 0}
            by_type[mt]["total"] += 1
            if media["uuid"] in refs:
                by_type[mt]["referenced"] += 1
            else:
                by_type[mt]["unreferenced"] += 1

        return {
            "referenced": referenced,
            "unreferenced": unreferenced,
            "summary": {
                "total_media": len(all_media),
                "referenced": len(referenced),
                "unreferenced": len(unreferenced),
                "by_type": by_type,
            },
        }

    async def get_media_file_url(self, mid: int) -> str:
        """Resolve the download URL for a media/document entity's file."""
        path = (
            f"/jsonapi/media/document"
            f"?filter[drupal_internal__mid]={mid}"
            f"&include=field_media_document"
            f"&fields[file--file]=uri,filename"
        )
        try:
            body = await self._get_json(path)
        except DrupalClientError:
            return ""
        for item in body.get("included", []):
            if item.get("type") == "file--file":
                uri = (item.get("attributes", {}).get("uri") or {}).get("url", "")
                if uri and not uri.startswith("http"):
                    uri = f"{self._base}{uri}"
                return uri
        return ""
