"""HTTP API for Drupal module integration."""

from __future__ import annotations

import asyncio
import os
import secrets
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Literal

from drupal_remedy.content_accuracy import ContentAccuracyChecker
from drupal_remedy.client import DrupalClient
from drupal_remedy.config import load_config
from drupal_remedy.run_store import PersistentRunStore
from drupal_remedy.scan_payload import build_scan_payload, make_run_id, utc_now_iso
from drupal_remedy.server import _make_llm_chat, _make_vision_chat


def create_app(
    config_path: str | None = None,
    env_path: str | None = None,
    *,
    runs_db_path: str | None = None,
    api_token: str | None = None,
) -> Any:
    """Create the FastAPI app lazily so FastAPI remains optional."""
    try:
        from fastapi import Depends, FastAPI, Header, HTTPException
        from pydantic import BaseModel, Field
    except ImportError as exc:
        raise RuntimeError("fastapi is required for the HTTP API") from exc

    store = PersistentRunStore(runs_db_path or ".remedy-drupal/runs.sqlite3")
    required_token = api_token if api_token is not None else (
        os.environ.get("REMEDY_DRUPAL_API_TOKEN")
        or os.environ.get("DRUPAL_REMEDY_API_TOKEN", "")
    )

    @asynccontextmanager
    async def lifespan(app: Any) -> Any:
        configs = load_config(
            yaml_path=Path(config_path or "config.yaml"),
            env_path=Path(env_path or ".env"),
        )
        llm_cfg = configs.pop("__llm__", None)
        configs.pop("__pdf__", None)
        clients: dict[str, DrupalClient] = {}
        for code, site_cfg in configs.items():
            clients[code] = DrupalClient(site_cfg)
            await clients[code].start()
        app.state.clients = clients
        app.state.llm_cfg = llm_cfg
        try:
            yield
        finally:
            tasks = list(app.state.background_tasks)
            for task in tasks:
                task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            for client in app.state.clients.values():
                await client.close()

    app = FastAPI(title="Remedy Drupal HTTP API", version="0.1.0", lifespan=lifespan)
    app.state.clients = {}
    app.state.llm_cfg = None
    app.state.store = store
    app.state.background_tasks = set()

    class PageRef(BaseModel):
        entity_type: str = "node"
        entity_id: int = Field(..., ge=1)
        canonical_url: str | None = None

    class ScanOptions(BaseModel):
        include_content_accuracy: bool = False
        interaction_profile: str = "default"

    class ScanRequest(BaseModel):
        site_code: str = Field(..., min_length=1)
        page: PageRef
        options: ScanOptions = Field(default_factory=ScanOptions)

    class FixRequest(BaseModel):
        site_code: str = Field(..., min_length=1)
        page: PageRef
        mode: Literal["traced", "selected_issues", "body"] = "traced"

    async def _require_auth(authorization: str | None = Header(default=None)) -> None:
        if not required_token:
            return
        scheme, _, token = (authorization or "").partition(" ")
        if scheme.lower() != "bearer" or not secrets.compare_digest(token, required_token):
            raise HTTPException(
                status_code=401,
                detail="Unauthorized",
                headers={"WWW-Authenticate": "Bearer"},
            )

    auth_dependency = Depends(_require_auth)

    def _get_client(site_code: str) -> DrupalClient:
        code = site_code.upper()
        client = app.state.clients.get(code)
        if client is None:
            raise HTTPException(status_code=404, detail=f"Unknown site code: {code}")
        return client

    def _queue_task(coro: Any) -> None:
        task = asyncio.create_task(coro)
        app.state.background_tasks.add(task)
        task.add_done_callback(app.state.background_tasks.discard)

    async def _resolve_url(client: DrupalClient, entity_type: str, entity_id: int, provided_url: str | None) -> str:
        if provided_url:
            return provided_url
        if entity_type == "node":
            return await client.get_page_url(entity_id)
        raise HTTPException(status_code=400, detail="canonical_url is required for non-node pages")

    async def _run_scan(run_id: str, site_code: str, entity_type: str, entity_id: int, canonical_url: str, include_content_accuracy: bool) -> None:
        started_at = utc_now_iso()
        store.mark_running(run_id, started_at=started_at)
        client = _get_client(site_code)
        try:
            content_accuracy_report = None
            if include_content_accuracy:
                if entity_type != "node":
                    raise ValueError("Content accuracy checks are only supported for node pages")
                if not app.state.llm_cfg:
                    raise ValueError("No llm section configured for content accuracy checks")
                vision = await _make_vision_chat(app.state.llm_cfg)
                if vision is None:
                    raise ValueError("No vision model configured for content accuracy checks")
                checker = ContentAccuracyChecker(client, vision)
                content_accuracy_report = await checker.check_page(entity_id)

            payload = await build_scan_payload(
                client,
                site_code=site_code,
                entity_type=entity_type,
                entity_id=entity_id,
                canonical_url=canonical_url,
                run_id=run_id,
                trigger_type="manual",
                started_at=started_at,
                include_content_accuracy=include_content_accuracy,
                content_accuracy_report=content_accuracy_report,
            )
            store.complete(run_id, payload=payload, completed_at=payload["run"]["completed_at"])
            store.set_latest_scan(site_code.upper(), entity_type, entity_id, run_id)
        except Exception as exc:
            store.fail(run_id, str(exc))

    async def _run_fix(run_id: str, site_code: str, entity_type: str, entity_id: int, mode: str) -> None:
        started_at = utc_now_iso()
        store.mark_running(run_id, started_at=started_at)
        client = _get_client(site_code)
        llm_cfg = app.state.llm_cfg
        if entity_type != "node":
            store.fail(run_id, "Only node fixes are supported in v1")
            return
        if not llm_cfg:
            store.fail(run_id, "No llm section configured")
            return

        try:
            from drupal_remedy.remediator import Remediator

            llm = await _make_llm_chat(llm_cfg)
            vision = await _make_vision_chat(llm_cfg)
            rem = Remediator(client, llm, vision_chat=vision)
            if mode in {"selected_issues", "traced"}:
                result = await rem.remediate_traced(entity_id)
            else:
                result = await rem.remediate_body(entity_id)
            verify_payload = await build_scan_payload(
                client,
                site_code=site_code,
                entity_type=entity_type,
                entity_id=entity_id,
                canonical_url=await client.get_page_url(entity_id),
                run_id=make_run_id("scan"),
                trigger_type="post_fix_verify",
                started_at=utc_now_iso(),
                completed_at=utc_now_iso(),
            )
            store.create(
                verify_payload["run"]["run_id"],
                run_type="scan",
                site_code=site_code.upper(),
                entity_type=entity_type,
                entity_id=entity_id,
                canonical_url=verify_payload["page"]["canonical_url"],
                status="completed",
                created_at=verify_payload["run"]["started_at"],
            )
            store.complete(
                verify_payload["run"]["run_id"],
                payload=verify_payload,
                completed_at=verify_payload["run"]["completed_at"],
            )
            store.complete(run_id, result={
                "fix_result": result,
                "verify_payload": verify_payload,
            })
            store.set_latest_scan(site_code.upper(), entity_type, entity_id, verify_payload["run"]["run_id"])
        except Exception as exc:
            store.fail(run_id, str(exc))

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/ready")
    async def ready() -> dict[str, Any]:
        store.ping()
        if not app.state.clients:
            raise HTTPException(status_code=503, detail="No Drupal clients configured")
        return {"status": "ready", "site_count": len(app.state.clients)}

    @app.post("/v1/pages/scan", dependencies=[auth_dependency])
    async def scan_page(request: ScanRequest) -> dict[str, Any]:
        site_code = request.site_code.upper()
        entity_type = request.page.entity_type
        entity_id = request.page.entity_id
        canonical_url = request.page.canonical_url

        client = _get_client(site_code)
        resolved_url = await _resolve_url(client, entity_type, entity_id, canonical_url)

        run_id = make_run_id("scan")
        store.create(
            run_id,
            run_type="scan",
            site_code=site_code,
            entity_type=entity_type,
            entity_id=entity_id,
            canonical_url=resolved_url,
        )
        _queue_task(_run_scan(
            run_id,
            site_code,
            entity_type,
            entity_id,
            resolved_url,
            request.options.include_content_accuracy,
        ))
        return {"run_id": run_id, "status": "queued"}

    @app.get("/v1/runs/{run_id}", dependencies=[auth_dependency])
    async def get_run(run_id: str) -> dict[str, Any]:
        run = store.get(run_id)
        if run is None:
            raise HTTPException(status_code=404, detail="Run not found")
        return run

    @app.get("/v1/pages/{entity_type}/{entity_id}/latest", dependencies=[auth_dependency])
    async def latest_scan(entity_type: str, entity_id: int, site_code: str) -> dict[str, Any]:
        run = store.get_latest_scan(site_code.upper(), entity_type, entity_id)
        if run is None or run.get("payload") is None:
            raise HTTPException(status_code=404, detail="No completed scan payload found")
        return run["payload"]

    @app.post("/v1/pages/fix", dependencies=[auth_dependency])
    async def fix_page(request: FixRequest) -> dict[str, Any]:
        site_code = request.site_code.upper()
        entity_type = request.page.entity_type
        entity_id = request.page.entity_id
        mode = request.mode

        _get_client(site_code)
        run_id = make_run_id("fix")
        store.create(run_id, run_type="fix", site_code=site_code, entity_type=entity_type, entity_id=entity_id)
        _queue_task(_run_fix(run_id, site_code, entity_type, entity_id, mode))
        return {"fix_run_id": run_id, "status": "queued"}

    return app
