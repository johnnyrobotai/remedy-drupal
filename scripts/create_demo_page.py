#!/usr/bin/env python3
"""Create a demo page on ELAC with intentional WCAG 2.1 AA violations.

Demonstrates all 7 traceable violation types:
  1. color-contrast   – low contrast text
  2. image-alt        – image missing alt text
  3. image-redundant-alt – image alt duplicates surrounding text
  4. empty-heading    – heading with no content
  5. heading-order    – skipped heading levels
  6. frame-title      – iframe without title
  7. link-name        – link with no discernible text
"""

import asyncio
import sys
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from drupal_remedy.config import load_config

import httpx

DEMO_BODY_HTML = """
<h1>Welcome to the ELAC Accessibility Demo Page</h1>

<p>This page was created intentionally with WCAG 2.1 AA violations
to demonstrate automated accessibility remediation.</p>

<!-- VIOLATION 1: color-contrast — very low contrast ratio -->
<p style="color: #999999; background-color: #ffffff;">
This light gray text on a white background fails the 4.5:1 contrast ratio
required by WCAG 2.1 AA for normal-sized text. It is very hard to read
for users with low vision.
</p>

<!-- VIOLATION 2: image-alt — image with no alt attribute -->
<img src="/sites/default/files/2024-01/elac-campus-aerial.jpg" width="600">

<!-- VIOLATION 3: image-redundant-alt — alt text duplicates surrounding text -->
<p>A photo of the ELAC campus quad with students walking</p>
<img src="/sites/default/files/2024-01/elac-quad.jpg"
     alt="A photo of the ELAC campus quad with students walking" width="400">

<!-- VIOLATION 4: empty-heading — heading element with no text -->
<h2></h2>

<!-- VIOLATION 5: heading-order — skips from h1 directly to h4 -->
<h4>Important Resources for Students</h4>
<p>Students can find tutoring support, financial aid information, and
counseling services through the links below.</p>

<!-- VIOLATION 6: frame-title — iframe with no title attribute -->
<iframe src="https://www.youtube.com/embed/dQw4w9WgXcQ"
        width="560" height="315" frameborder="0"
        allow="accelerometer; autoplay; clipboard-write; encrypted-media; gyroscope; picture-in-picture"
        allowfullscreen></iframe>

<!-- VIOLATION 7: link-name — link with no discernible accessible name -->
<p>For more information: <a href="/admissions"><img src="/sites/default/files/arrow-icon.png"></a></p>

<!-- Extra violation: another link-name issue with empty link -->
<p>Visit our <a href="/financial-aid"></a> page for details.</p>

<!-- Extra violation: more color-contrast issues -->
<div style="background-color: #336699;">
  <p style="color: #6688aa;">This blue text on a blue background
  is extremely difficult to distinguish for anyone.</p>
</div>
"""


async def main():
    env_path = Path(__file__).resolve().parent.parent / ".env"
    yaml_path = Path(__file__).resolve().parent.parent / "config.yaml"

    configs = load_config(yaml_path=yaml_path, env_path=env_path)
    site = configs.get("ELAC")
    if not site:
        print("ERROR: No ELAC site config found")
        return

    auth = None
    if site.http_auth_username:
        auth = httpx.BasicAuth(site.http_auth_username, site.http_auth_password)

    async with httpx.AsyncClient(
        base_url=site.base_url,
        auth=auth,
        timeout=30.0,
        verify=site.verify_ssl,
    ) as client:
        # Step 1: Log in
        print(f"Logging in to {site.base_url}...")
        login_resp = await client.post(
            "/user/login?_format=json",
            json={"name": site.auth.username, "pass": site.auth.password},
            headers={"Content-Type": "application/json"},
        )
        if login_resp.status_code != 200:
            print(f"Login failed: {login_resp.status_code} {login_resp.text[:200]}")
            return
        print("Logged in successfully.")

        # Step 2: Get CSRF token
        csrf_resp = await client.get("/session/token")
        csrf_token = csrf_resp.text.strip()

        # Step 3: Create the page node
        payload = {
            "data": {
                "type": "node--page",
                "attributes": {
                    "title": "WCAG 2.1 AA Accessibility Demo — Violations for Remediation",
                    "body": {
                        "value": DEMO_BODY_HTML,
                        "format": "rich_text",
                    },
                    "status": True,
                },
            }
        }

        print("Creating demo page...")
        create_resp = await client.post(
            "/jsonapi/node/page",
            json=payload,
            headers={
                "Accept": "application/vnd.api+json",
                "Content-Type": "application/vnd.api+json",
                "X-CSRF-Token": csrf_token,
            },
        )

        if create_resp.status_code in (200, 201):
            data = create_resp.json()["data"]
            nid = data["attributes"]["drupal_internal__nid"]
            alias = data["attributes"].get("path", {}).get("alias", "")
            url = f"{site.base_url}/node/{nid}"
            print(f"\nDemo page created successfully!")
            print(f"  NID:   {nid}")
            print(f"  URL:   {url}")
            if alias:
                print(f"  Alias: {site.base_url}{alias}")
            print(f"\nViolations included:")
            print(f"  1. color-contrast  — low contrast text (2 instances)")
            print(f"  2. image-alt       — image missing alt text")
            print(f"  3. image-redundant-alt — alt duplicates surrounding text")
            print(f"  4. empty-heading   — empty <h2> element")
            print(f"  5. heading-order   — h1 jumps to h4, skipping h2/h3")
            print(f"  6. frame-title     — iframe without title attribute")
            print(f"  7. link-name       — links with no accessible name (2 instances)")
        else:
            print(f"Create failed: {create_resp.status_code}")
            print(create_resp.text[:500])


if __name__ == "__main__":
    asyncio.run(main())
