#!/usr/bin/env python3
"""Run a media usage audit for a campus site.

Usage: python scripts/media_audit.py <campus_code>
"""

import asyncio
import json
import logging
import sys
from pathlib import Path

from drupal_remedy.config import load_config
from drupal_remedy.jsonapi_client import JsonApiClient

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


async def main(campus_code: str) -> None:
    configs = load_config(yaml_path=Path("config.yaml"), env_path=Path(".env"))
    campus = campus_code.upper()

    site_configs = {k: v for k, v in configs.items() if not k.startswith("__")}
    if campus not in site_configs:
        print(
            f"Unknown campus: {campus}. Available: {', '.join(sorted(site_configs))}"
        )
        sys.exit(1)

    client = JsonApiClient(site_configs[campus])
    await client.login()

    try:
        result = await client.audit_media_usage()
    finally:
        await client.close()

    output_path = Path(f"media_audit_{campus}.json")
    output_path.write_text(json.dumps(result, indent=2))

    s = result["summary"]
    print(f"\nMedia Usage Audit — {campus}")
    print("=" * 40)
    print(f"Total media entities: {s['total_media']}")
    print(f"Referenced by pages:  {s['referenced']}")
    print(f"Unreferenced:         {s['unreferenced']}")
    print()
    for mt, counts in s["by_type"].items():
        print(f"  {mt}: {counts['referenced']}/{counts['total']} referenced")
    print(f"\nResults saved to {output_path}")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python scripts/media_audit.py <campus_code>")
        sys.exit(1)
    asyncio.run(main(sys.argv[1]))
