#!/usr/bin/env python3
"""Export ELAC dev site structure via JSON:API for local Drupal replication.

Discovers entity types, fields, and relationships from JSON:API and generates:
1. A JSON inventory file (exported_config/site_structure.json)
2. A Drush PHP script to recreate the structure locally (exported_config/recreate_structure.php)

Usage: python scripts/export_site_config.py [campus_code]
       Defaults to ELAC if not specified.
"""

import asyncio
import json
import logging
import sys
from pathlib import Path
from typing import Any

import httpx

from drupal_remedy.config import DrupalSiteConfig, load_config
from drupal_remedy.exceptions import DrupalClientError

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

JSONAPI_ACCEPT = {"Accept": "application/vnd.api+json"}

OUTPUT_DIR = Path("exported_config")


class SiteExporter:
    """Discover and export Drupal site structure via JSON:API."""

    def __init__(self, config: DrupalSiteConfig) -> None:
        self._config = config
        self._base = config.base_url.rstrip("/")
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
            raise DrupalClientError(f"Login failed ({resp.status_code}): {resp.text[:200]}")
        logger.info("Logged in as %s", self._config.auth.username)

    async def close(self) -> None:
        await self._http.aclose()

    async def _get_json(self, path: str) -> dict[str, Any]:
        resp = await self._http.get(path, headers=JSONAPI_ACCEPT)
        if resp.status_code != 200:
            raise DrupalClientError(f"GET {path} returned {resp.status_code}")
        return resp.json()

    async def _fetch_all_pages(self, path: str, *, page_size: int = 50) -> list[dict[str, Any]]:
        """Paginate through a JSON:API collection."""
        results: list[dict[str, Any]] = []
        offset = 0
        while True:
            sep = "&" if "?" in path else "?"
            url = f"{path}{sep}page[limit]={page_size}&page[offset]={offset}"
            try:
                body = await self._get_json(url)
            except DrupalClientError:
                break
            data = body.get("data", [])
            if not data:
                break
            results.extend(data)
            offset += len(data)
            if len(data) < page_size:
                break
        return results

    # ------------------------------------------------------------------
    # JSON:API index — canonical source of machine names
    # ------------------------------------------------------------------

    async def fetch_resource_index(self) -> dict[str, list[str]]:
        """Fetch the JSON:API index and group resource types by entity type."""
        index = await self._get_json("/jsonapi")
        links = sorted(k for k in index.get("links", {}) if k != "self" and "--" in k)
        grouped: dict[str, list[str]] = {}
        for link in links:
            entity_type, bundle = link.split("--", 1)
            grouped.setdefault(entity_type, []).append(bundle)
        return grouped

    # ------------------------------------------------------------------
    # Config entity discovery (accessible ones)
    # ------------------------------------------------------------------

    async def fetch_node_types(self) -> list[dict[str, Any]]:
        """Fetch all node type definitions."""
        items = await self._fetch_all_pages("/jsonapi/node_type/node_type")
        types = []
        for item in items:
            attrs = item.get("attributes", {})
            types.append({
                "uuid": item["id"],
                "machine_name": attrs.get("drupal_internal__type", ""),
                "label": attrs.get("name", ""),
                "description": attrs.get("description", ""),
                "dependencies": attrs.get("dependencies", {}),
                "third_party_settings": attrs.get("third_party_settings", {}),
            })
        logger.info("Found %d node types", len(types))
        return types

    async def fetch_paragraph_types(self, index_bundles: list[str]) -> list[dict[str, Any]]:
        """Build paragraph type list from JSON:API index + config entity labels."""
        # Config entities only expose label, so map uuid->label
        items = await self._fetch_all_pages("/jsonapi/paragraphs_type/paragraphs_type")
        label_by_uuid: dict[str, str] = {}
        for item in items:
            label_by_uuid[item["id"]] = item.get("attributes", {}).get("label", "")

        # Build types from index machine names + matched labels
        types = []
        for bundle in sorted(index_bundles):
            # Try to find matching label (match by index order)
            types.append({
                "machine_name": bundle,
                "label": "",  # will be filled below
            })

        # Match labels to machine names by order (both sorted alphabetically from API)
        label_list = list(label_by_uuid.values())
        for i, t in enumerate(types):
            if i < len(label_list):
                t["label"] = label_list[i]
            else:
                t["label"] = t["machine_name"].replace("_", " ").title()

        logger.info("Found %d paragraph types", len(types))
        return types

    async def fetch_media_types(self, index_bundles: list[str]) -> list[dict[str, Any]]:
        """Build media type list from JSON:API index + config entity labels."""
        items = await self._fetch_all_pages("/jsonapi/media_type/media_type")
        label_list = [item.get("attributes", {}).get("label", "") for item in items]

        types = []
        for bundle in sorted(index_bundles):
            types.append({
                "machine_name": bundle,
                "label": "",
            })

        for i, t in enumerate(types):
            if i < len(label_list):
                t["label"] = label_list[i]
            else:
                t["label"] = t["machine_name"].replace("_", " ").title()

        logger.info("Found %d media types", len(types))
        return types

    async def fetch_taxonomy_vocabularies(self) -> list[dict[str, Any]]:
        """Fetch all taxonomy vocabulary definitions."""
        items = await self._fetch_all_pages("/jsonapi/taxonomy_vocabulary/taxonomy_vocabulary")
        vocabs = []
        for item in items:
            attrs = item.get("attributes", {})
            vocabs.append({
                "uuid": item["id"],
                "machine_name": attrs.get("drupal_internal__vid", ""),
                "label": attrs.get("name", ""),
                "description": attrs.get("description", ""),
                "weight": attrs.get("weight", 0),
            })
        logger.info("Found %d taxonomy vocabularies", len(vocabs))
        return vocabs

    # ------------------------------------------------------------------
    # Field discovery via sample entities
    # ------------------------------------------------------------------

    def _infer_field_type(self, name: str, value: Any) -> str:
        """Infer Drupal field type from JSON:API attribute value."""
        if value is None:
            return "unknown"
        if isinstance(value, bool):
            return "boolean"
        if isinstance(value, int):
            return "integer"
        if isinstance(value, float):
            return "decimal"
        if isinstance(value, str):
            return "string"
        if isinstance(value, dict):
            if "value" in value and "format" in value:
                return "text_formatted"  # text_long, text_with_summary
            if "value" in value and "summary" in value:
                return "text_with_summary"
            if "uri" in value and "url" in value:
                return "link"
            if "alias" in value:
                return "path"
            if "value" in value:
                return "text"
            if "latitude" in value or "lat" in value:
                return "geofield"
            return "map"
        if isinstance(value, list):
            if value and isinstance(value[0], str):
                return "list_string"
            if value and isinstance(value[0], dict):
                if "value" in value[0]:
                    return "text_list"
                return "map_list"
            return "list"
        return "unknown"

    async def discover_paragraph_fields_from_nodes(
        self, node_types: list[dict[str, Any]]
    ) -> dict[str, dict[str, Any]]:
        """Discover paragraph fields by fetching nodes, then recursively fetching nested paragraphs.

        Returns {paragraph_bundle: {attributes: {...}, relationships: {...}}}.
        """
        seen: dict[str, dict[str, Any]] = {}
        # Queue of (uuid, type_string) to fetch individually
        fetch_queue: list[tuple[str, str]] = []

        # Step 1: Get top-level paragraphs from node includes
        for nt in node_types:
            name = nt["machine_name"]
            para_field = "field_row" if name == "landing" else "field_item"
            path = f"/jsonapi/node/{name}?page[limit]=5&include={para_field}"
            try:
                body = await self._get_json(path)
            except DrupalClientError:
                continue

            for item in body.get("included", []):
                item_type = item.get("type", "")
                if not item_type.startswith("paragraph--"):
                    continue
                bundle = item_type.replace("paragraph--", "")
                if bundle not in seen:
                    fields = self._extract_fields(item)
                    seen[bundle] = fields
                    logger.info("  paragraph/%s: %d attrs, %d rels (via node/%s)",
                                bundle, len(fields["attributes"]),
                                len(fields["relationships"]), name)
                # Queue nested paragraph refs for fetching
                for _field, rel in (item.get("relationships") or {}).items():
                    rel_data = rel.get("data")
                    if rel_data is None:
                        continue
                    items_list = rel_data if isinstance(rel_data, list) else [rel_data]
                    for ref in items_list:
                        ref_type = ref.get("type", "")
                        if ref_type.startswith("paragraph--"):
                            ref_bundle = ref_type.replace("paragraph--", "")
                            if ref_bundle not in seen:
                                fetch_queue.append((ref.get("id", ""), ref_type))

        # Step 2: Fetch individual nested paragraphs to discover their fields
        fetched_uuids: set[str] = set()
        depth = 0
        max_depth = 4

        while fetch_queue and depth < max_depth:
            next_queue: list[tuple[str, str]] = []
            depth += 1
            for uuid, type_str in fetch_queue:
                if uuid in fetched_uuids:
                    continue
                fetched_uuids.add(uuid)
                bundle = type_str.replace("paragraph--", "")
                if bundle in seen:
                    continue

                path = f"/jsonapi/paragraph/{bundle}/{uuid}"
                try:
                    body = await self._get_json(path)
                except DrupalClientError:
                    continue

                entity = body.get("data", {})
                if not entity:
                    continue

                fields = self._extract_fields(entity)
                seen[bundle] = fields
                logger.info("  paragraph/%s: %d attrs, %d rels (depth %d)",
                            bundle, len(fields["attributes"]),
                            len(fields["relationships"]), depth)

                # Queue any further nested paragraphs
                for _field, rel in (entity.get("relationships") or {}).items():
                    rel_data = rel.get("data")
                    if rel_data is None:
                        continue
                    items_list = rel_data if isinstance(rel_data, list) else [rel_data]
                    for ref in items_list:
                        ref_type = ref.get("type", "")
                        if ref_type.startswith("paragraph--"):
                            ref_bundle = ref_type.replace("paragraph--", "")
                            if ref_bundle not in seen:
                                next_queue.append((ref.get("id", ""), ref_type))

            fetch_queue = next_queue

        logger.info("Discovered fields for %d paragraph types total", len(seen))
        return seen

    async def discover_fields_for_entity(
        self, entity_type: str, bundle: str
    ) -> dict[str, Any]:
        """Fetch a sample entity and discover its fields from attributes/relationships."""
        path = f"/jsonapi/{entity_type}/{bundle}?page[limit]=1"
        try:
            body = await self._get_json(path)
        except DrupalClientError:
            return {"attributes": {}, "relationships": {}}

        data = body.get("data", [])
        if not data:
            return {"attributes": {}, "relationships": {}}

        return self._extract_fields(data[0])

    def _extract_fields(self, entity: dict[str, Any]) -> dict[str, Any]:
        """Extract field definitions from a JSON:API entity."""
        attrs = entity.get("attributes", {})
        rels = entity.get("relationships", {})

        # Discover attribute fields
        attribute_fields = {}
        for name, value in attrs.items():
            # Skip internal Drupal fields
            if name in (
                "drupal_internal__nid", "drupal_internal__vid",
                "drupal_internal__mid", "drupal_internal__tid",
                "drupal_internal__id",
                "langcode", "revision_created", "revision_log_message",
                "status", "title", "created", "changed", "promote",
                "sticky", "default_langcode", "revision_translation_affected",
                "content_translation_source", "content_translation_outdated",
                "moderation_state", "name", "weight", "description",
                "revision_default", "path", "publish_on", "unpublish_on",
                "parent_id", "parent_type", "parent_field_name",
                "behavior_settings",
            ):
                continue
            if name.startswith("field_") or name == "body":
                attribute_fields[name] = {
                    "type": self._infer_field_type(name, value),
                    "sample_value": _truncate_value(value),
                }

        # Discover relationship fields
        relationship_fields = {}
        for name, rel in rels.items():
            # Skip internal relationships
            if name in (
                "node_type", "revision_uid", "uid", "paragraph_type",
                "media_type", "vid", "bundle",
            ):
                continue
            if name.startswith("field_") or name in ("parent_id", "parent_type"):
                rel_data = rel.get("data")
                target_type = "unknown"
                cardinality = "single"
                if isinstance(rel_data, list):
                    cardinality = "multiple"
                    if rel_data:
                        target_type = rel_data[0].get("type", "unknown")
                elif isinstance(rel_data, dict):
                    target_type = rel_data.get("type", "unknown")
                elif rel_data is None:
                    target_type = "unknown"
                relationship_fields[name] = {
                    "target_type": target_type,
                    "cardinality": cardinality,
                }

        return {
            "attributes": attribute_fields,
            "relationships": relationship_fields,
        }

    # ------------------------------------------------------------------
    # Full export
    # ------------------------------------------------------------------

    async def export_all(self) -> dict[str, Any]:
        """Run full site structure discovery."""
        logger.info("Starting full site structure export...")

        # Phase 1: Get JSON:API index for canonical machine names
        resource_index = await self.fetch_resource_index()
        all_resource_types = []
        for entity_type, bundles in sorted(resource_index.items()):
            for bundle in bundles:
                all_resource_types.append(f"{entity_type}--{bundle}")

        # Phase 2: Config entities
        node_types = await self.fetch_node_types()
        paragraph_types = await self.fetch_paragraph_types(resource_index.get("paragraph", []))
        media_types = await self.fetch_media_types(resource_index.get("media", []))
        taxonomies = await self.fetch_taxonomy_vocabularies()

        # Phase 3: Discover fields for node types (direct query works)
        logger.info("Discovering fields for %d node types...", len(node_types))
        for nt in node_types:
            name = nt["machine_name"]
            nt["fields"] = await self.discover_fields_for_entity("node", name)
            logger.info("  node/%s: %d attrs, %d rels", name,
                        len(nt["fields"]["attributes"]),
                        len(nt["fields"]["relationships"]))

        # Phase 4: Discover paragraph fields from node includes
        # (paragraphs can't be listed directly — they're revision entities)
        logger.info("Discovering paragraph fields from node includes...")
        para_field_map = await self.discover_paragraph_fields_from_nodes(node_types)
        for pt in paragraph_types:
            pt["fields"] = para_field_map.get(pt["machine_name"], {"attributes": {}, "relationships": {}})

        # Phase 5: Discover media fields (only standard types are listable)
        logger.info("Discovering fields for media types...")
        for mt in media_types:
            name = mt["machine_name"]
            mt["fields"] = await self.discover_fields_for_entity("media", name)
            if mt["fields"]["attributes"] or mt["fields"]["relationships"]:
                logger.info("  media/%s: %d attrs, %d rels", name,
                            len(mt["fields"]["attributes"]),
                            len(mt["fields"]["relationships"]))

        return {
            "site": {
                "base_url": self._base,
                "drupal_version": "10.5.x",
                "platform": "Acquia Cloud",
            },
            "resource_types": all_resource_types,
            "node_types": node_types,
            "paragraph_types": paragraph_types,
            "media_types": media_types,
            "taxonomy_vocabularies": taxonomies,
        }


def _truncate_value(value: Any, max_len: int = 100) -> Any:
    """Truncate a sample value for readability."""
    if isinstance(value, str) and len(value) > max_len:
        return value[:max_len] + "..."
    if isinstance(value, dict):
        result = {}
        for k, v in value.items():
            result[k] = _truncate_value(v, max_len)
        return result
    if isinstance(value, list) and len(value) > 3:
        return [_truncate_value(v, max_len) for v in value[:3]] + [f"... ({len(value)} total)"]
    return value


def generate_drush_script(structure: dict[str, Any]) -> str:
    """Generate a Drush PHP script to recreate the site structure."""
    lines = [
        '<?php',
        '/**',
        ' * Drush script to recreate ELAC site structure on a local Drupal instance.',
        ' *',
        ' * Usage: ddev drush php:script recreate_structure.php',
        ' *',
        ' * Prerequisites: All contrib modules must be installed and enabled first.',
        ' */',
        '',
        'use Drupal\\node\\Entity\\NodeType;',
        'use Drupal\\paragraphs\\Entity\\ParagraphsType;',
        'use Drupal\\media\\Entity\\MediaType;',
        'use Drupal\\taxonomy\\Entity\\Vocabulary;',
        'use Drupal\\field\\Entity\\FieldStorageConfig;',
        'use Drupal\\field\\Entity\\FieldConfig;',
        '',
        '$messenger = \\Drupal::messenger();',
        '',
    ]

    # Node types
    lines.append('// --- Node Types ---')
    for nt in structure["node_types"]:
        name = nt["machine_name"]
        label = nt["label"].replace("'", "\\'")
        desc = (nt.get("description") or "").replace("'", "\\'")
        lines.append(f"if (!NodeType::load('{name}')) {{")
        lines.append(f"  $type = NodeType::create([")
        lines.append(f"    'type' => '{name}',")
        lines.append(f"    'name' => '{label}',")
        lines.append(f"    'description' => '{desc}',")
        lines.append(f"  ]);")
        lines.append(f"  $type->save();")
        lines.append(f"  $messenger->addMessage('Created node type: {name}');")
        lines.append(f"}} else {{")
        lines.append(f"  $messenger->addMessage('Node type already exists: {name}');")
        lines.append(f"}}")
        lines.append('')

    # Paragraph types
    lines.append('// --- Paragraph Types ---')
    for pt in structure["paragraph_types"]:
        name = pt["machine_name"]
        label = pt["label"].replace("'", "\\'")
        lines.append(f"if (!ParagraphsType::load('{name}')) {{")
        lines.append(f"  $type = ParagraphsType::create([")
        lines.append(f"    'id' => '{name}',")
        lines.append(f"    'label' => '{label}',")
        lines.append(f"  ]);")
        lines.append(f"  $type->save();")
        lines.append(f"  $messenger->addMessage('Created paragraph type: {name}');")
        lines.append(f"}} else {{")
        lines.append(f"  $messenger->addMessage('Paragraph type already exists: {name}');")
        lines.append(f"}}")
        lines.append('')

    # Media types
    lines.append('// --- Media Types ---')
    for mt in structure["media_types"]:
        name = mt["machine_name"]
        label = mt["label"].replace("'", "\\'")
        source = mt.get("source", "file")
        source_config = mt.get("source_configuration", {})
        source_field = source_config.get("source_field", "")
        lines.append(f"if (!MediaType::load('{name}')) {{")
        lines.append(f"  $type = MediaType::create([")
        lines.append(f"    'id' => '{name}',")
        lines.append(f"    'label' => '{label}',")
        lines.append(f"    'source' => '{source}',")
        if source_field:
            lines.append(f"    'source_configuration' => ['source_field' => '{source_field}'],")
        lines.append(f"  ]);")
        lines.append(f"  $type->save();")
        lines.append(f"  $messenger->addMessage('Created media type: {name}');")
        lines.append(f"}} else {{")
        lines.append(f"  $messenger->addMessage('Media type already exists: {name}');")
        lines.append(f"}}")
        lines.append('')

    # Taxonomy vocabularies
    lines.append('// --- Taxonomy Vocabularies ---')
    for vocab in structure["taxonomy_vocabularies"]:
        name = vocab["machine_name"]
        label = vocab["label"].replace("'", "\\'")
        desc = (vocab.get("description") or "").replace("'", "\\'")
        lines.append(f"if (!Vocabulary::load('{name}')) {{")
        lines.append(f"  $vocab = Vocabulary::create([")
        lines.append(f"    'vid' => '{name}',")
        lines.append(f"    'name' => '{label}',")
        lines.append(f"    'description' => '{desc}',")
        lines.append(f"  ]);")
        lines.append(f"  $vocab->save();")
        lines.append(f"  $messenger->addMessage('Created vocabulary: {name}');")
        lines.append(f"}} else {{")
        lines.append(f"  $messenger->addMessage('Vocabulary already exists: {name}');")
        lines.append(f"}}")
        lines.append('')

    # Fields for node types
    lines.append('// --- Fields for Node Types ---')
    _generate_field_creation(lines, structure["node_types"], "node")

    # Fields for paragraph types
    lines.append('// --- Fields for Paragraph Types ---')
    _generate_field_creation(lines, structure["paragraph_types"], "paragraph")

    # Fields for media types
    lines.append('// --- Fields for Media Types ---')
    _generate_field_creation(lines, structure["media_types"], "media")

    lines.append('')
    lines.append("$messenger->addMessage('Site structure recreation complete!');")
    return '\n'.join(lines)


# Mapping from inferred JSON:API types to Drupal field storage types
_TYPE_MAP = {
    "text_formatted": "text_long",
    "text_with_summary": "text_with_summary",
    "text": "string",
    "string": "string",
    "boolean": "boolean",
    "integer": "integer",
    "decimal": "decimal",
    "link": "link",
    "path": "path",
    "list_string": "list_string",
    "map": "map",
    "unknown": "string",
    "geofield": "geofield",
}

# Mapping from JSON:API relationship target types to Drupal field types
_REL_TYPE_MAP = {
    "paragraph--": "entity_reference_revisions",
    "media--": "entity_reference",
    "node--": "entity_reference",
    "taxonomy_term--": "entity_reference",
    "file--": "file",
}


def _generate_field_creation(
    lines: list[str],
    entity_types: list[dict[str, Any]],
    entity_type_id: str,
) -> None:
    """Generate PHP code to create fields for a set of entity types."""
    for et in entity_types:
        bundle = et["machine_name"]
        fields = et.get("fields", {})
        attrs = fields.get("attributes", {})
        rels = fields.get("relationships", {})

        if not attrs and not rels:
            continue

        lines.append(f"// Fields for {entity_type_id}/{bundle}")

        # Attribute fields
        for field_name, field_info in attrs.items():
            inferred = field_info["type"]
            drupal_type = _TYPE_MAP.get(inferred, "string")
            _add_field_code(lines, entity_type_id, bundle, field_name, drupal_type)

        # Relationship fields
        for field_name, rel_info in rels.items():
            target = rel_info["target_type"]
            drupal_type = "entity_reference"
            target_entity_type = "node"

            for prefix, ftype in _REL_TYPE_MAP.items():
                if target.startswith(prefix):
                    drupal_type = ftype
                    target_entity_type = prefix.rstrip("-")
                    break

            cardinality = -1 if rel_info["cardinality"] == "multiple" else 1
            _add_ref_field_code(
                lines, entity_type_id, bundle, field_name,
                drupal_type, target_entity_type, cardinality,
            )

        lines.append('')


def _add_field_code(
    lines: list[str],
    entity_type: str,
    bundle: str,
    field_name: str,
    field_type: str,
) -> None:
    """Generate PHP to create a field storage + field instance."""
    lines.append(f"if (!FieldStorageConfig::loadByName('{entity_type}', '{field_name}')) {{")
    lines.append(f"  FieldStorageConfig::create([")
    lines.append(f"    'field_name' => '{field_name}',")
    lines.append(f"    'entity_type' => '{entity_type}',")
    lines.append(f"    'type' => '{field_type}',")
    lines.append(f"    'cardinality' => 1,")
    lines.append(f"  ])->save();")
    lines.append(f"}}")
    lines.append(f"if (!FieldConfig::loadByName('{entity_type}', '{bundle}', '{field_name}')) {{")
    lines.append(f"  FieldConfig::create([")
    lines.append(f"    'field_name' => '{field_name}',")
    lines.append(f"    'entity_type' => '{entity_type}',")
    lines.append(f"    'bundle' => '{bundle}',")
    lines.append(f"    'label' => '{field_name}',")
    lines.append(f"  ])->save();")
    lines.append(f"}}")


def _add_ref_field_code(
    lines: list[str],
    entity_type: str,
    bundle: str,
    field_name: str,
    field_type: str,
    target_entity_type: str,
    cardinality: int,
) -> None:
    """Generate PHP to create a reference field storage + instance."""
    lines.append(f"if (!FieldStorageConfig::loadByName('{entity_type}', '{field_name}')) {{")
    lines.append(f"  FieldStorageConfig::create([")
    lines.append(f"    'field_name' => '{field_name}',")
    lines.append(f"    'entity_type' => '{entity_type}',")
    lines.append(f"    'type' => '{field_type}',")
    lines.append(f"    'cardinality' => {cardinality},")
    lines.append(f"    'settings' => ['target_type' => '{target_entity_type}'],")
    lines.append(f"  ])->save();")
    lines.append(f"}}")
    lines.append(f"if (!FieldConfig::loadByName('{entity_type}', '{bundle}', '{field_name}')) {{")
    lines.append(f"  FieldConfig::create([")
    lines.append(f"    'field_name' => '{field_name}',")
    lines.append(f"    'entity_type' => '{entity_type}',")
    lines.append(f"    'bundle' => '{bundle}',")
    lines.append(f"    'label' => '{field_name}',")
    lines.append(f"  ])->save();")
    lines.append(f"}}")


async def main(campus_code: str) -> None:
    configs = load_config(yaml_path=Path("config.yaml"), env_path=Path(".env"))
    campus = campus_code.upper()

    site_configs = {k: v for k, v in configs.items() if not k.startswith("__")}
    if campus not in site_configs:
        print(f"Unknown campus: {campus}. Available: {', '.join(sorted(site_configs))}")
        sys.exit(1)

    exporter = SiteExporter(site_configs[campus])
    try:
        await exporter.login()
        structure = await exporter.export_all()

        # Write output
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

        # JSON inventory
        json_path = OUTPUT_DIR / "site_structure.json"
        with open(json_path, "w") as f:
            json.dump(structure, f, indent=2, default=str)
        logger.info("Wrote site structure to %s", json_path)

        # Drush PHP script
        php_path = OUTPUT_DIR / "recreate_structure.php"
        php_code = generate_drush_script(structure)
        with open(php_path, "w") as f:
            f.write(php_code)
        logger.info("Wrote Drush script to %s", php_path)

        # Summary
        print(f"\nExport complete!")
        print(f"  Node types:      {len(structure['node_types'])}")
        print(f"  Paragraph types: {len(structure['paragraph_types'])}")
        print(f"  Media types:     {len(structure['media_types'])}")
        print(f"  Taxonomies:      {len(structure['taxonomy_vocabularies'])}")
        print(f"  Total resource types: {len(structure['resource_types'])}")
        print(f"\nOutput:")
        print(f"  {json_path}")
        print(f"  {php_path}")
        print(f"\nNext: copy recreate_structure.php to your local Drupal and run:")
        print(f"  ddev drush php:script recreate_structure.php")

    finally:
        await exporter.close()


if __name__ == "__main__":
    code = sys.argv[1] if len(sys.argv) > 1 else "ELAC"
    asyncio.run(main(code))
