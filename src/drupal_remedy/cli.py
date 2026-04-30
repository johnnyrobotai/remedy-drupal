"""CLI entry point for Remedy Drupal."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import click


@click.group()
@click.option("--config", default="config.yaml", help="Path to config YAML.")
@click.option("--env", default=".env", help="Path to .env file.")
@click.pass_context
def cli(ctx: click.Context, config: str, env: str) -> None:
    """Drupal accessibility remediation tool."""
    ctx.ensure_object(dict)
    ctx.obj["config"] = config
    ctx.obj["env"] = env


@cli.command()
@click.argument("url")
@click.option("--campus", required=True, help="Campus code (e.g., ELAC).")
@click.pass_context
def scan(ctx: click.Context, url: str, campus: str) -> None:
    """Scan a page for WCAG 2.1 AA accessibility violations."""
    from drupal_remedy.client import DrupalClient
    from drupal_remedy.config import load_config

    configs = load_config(
        yaml_path=Path(ctx.obj["config"]),
        env_path=Path(ctx.obj["env"]),
    )
    code = campus.upper()
    if code not in configs:
        click.echo(f"Campus {code} not found in config.", err=True)
        sys.exit(1)

    async def _run() -> None:
        client = DrupalClient(configs[code])
        await client.start()
        try:
            result = await client.scan_page(url)
            click.echo(json.dumps(result, indent=2))
        finally:
            await client.close()

    asyncio.run(_run())


@cli.command()
@click.argument("nid", type=int)
@click.option("--campus", required=True, help="Campus code (e.g., ELAC).")
@click.pass_context
def trace(ctx: click.Context, nid: int, campus: str) -> None:
    """Trace accessibility violations to their Drupal source entities."""
    from drupal_remedy.client import DrupalClient
    from drupal_remedy.config import load_config
    from drupal_remedy.scan_payload import expand_scan_violations, is_template_noise_rule
    from drupal_remedy.tracer import ViolationTracer

    configs = load_config(
        yaml_path=Path(ctx.obj["config"]),
        env_path=Path(ctx.obj["env"]),
    )
    code = campus.upper()
    if code not in configs:
        click.echo(f"Campus {code} not found in config.", err=True)
        sys.exit(1)

    async def _run() -> None:
        client = DrupalClient(configs[code])
        await client.start()
        try:
            meta = await client.get_page_metadata(nid)
            url = meta.get("canonical_url") or await client.get_page_url(nid)

            scan = await client.scan_page(url)
            # Flatten: each axe rule can have multiple nodes (html_snippets).
            # The tracer expects one violation dict per node with an "html" key.
            violations = [
                v for v in expand_scan_violations(scan["violations"])
                if not is_template_noise_rule(v["id"])
            ]

            tracer = ViolationTracer(client, base_url=client.base_url)
            report = await tracer.trace_all(nid, violations)

            click.echo(f"Page: {url}")
            click.echo(f"Violations: {report.total_violations}")
            click.echo(f"Traced: {len(report.traced)}")
            click.echo(f"Untraced: {len(report.untraced)}")
            click.echo()

            for t in report.traced:
                click.echo(
                    f"  TRACED [{t.impact}] {t.violation_id} "
                    f"-> {t.entity_type}/{t.entity_id} "
                    f"field={t.field_name}"
                )
                click.echo(f"    Fix: {t.suggested_fix}")

            for u in report.untraced:
                click.echo(
                    f"  UNTRACED [{u.get('impact')}] {u.get('violation_id')} "
                    f"-> {u.get('likely_cause')}"
                )
        finally:
            await client.close()

    asyncio.run(_run())


@cli.command()
@click.option("--config", "cfg_path", default="config.yaml")
@click.option("--env", "env_path", default=".env")
def serve(cfg_path: str, env_path: str) -> None:
    """Start the MCP server (STDIO transport)."""
    from drupal_remedy.server import create_and_run_server

    asyncio.run(create_and_run_server(config_path=cfg_path, env_path=env_path))


@cli.command(name="serve-http")
@click.option("--config", "cfg_path", default="config.yaml")
@click.option("--env", "env_path", default=".env")
@click.option("--host", default="127.0.0.1")
@click.option("--port", default=8787, type=int)
@click.option("--runs-db", default=".remedy-drupal/runs.sqlite3", help="Path to the HTTP API SQLite run store.")
def serve_http(cfg_path: str, env_path: str, host: str, port: int, runs_db: str) -> None:
    """Start the HTTP API server for Drupal integration."""
    try:
        import uvicorn
    except ImportError:
        raise SystemExit("uvicorn is required for serve-http: pip install 'remedy-drupal[api]'")

    from drupal_remedy.http_api import create_app

    app = create_app(config_path=cfg_path, env_path=env_path, runs_db_path=runs_db)
    uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":
    cli()
