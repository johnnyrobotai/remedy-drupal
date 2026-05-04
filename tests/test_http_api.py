from __future__ import annotations

from fastapi.testclient import TestClient

from drupal_remedy.http_api import create_app


def _empty_config(tmp_path):
    config_path = tmp_path / "config.yaml"
    env_path = tmp_path / ".env"
    config_path.write_text("sites: []\n", encoding="utf-8")
    env_path.write_text("", encoding="utf-8")
    return config_path, env_path


def test_health_endpoint_starts_with_empty_config(tmp_path):
    config_path, env_path = _empty_config(tmp_path)
    app = create_app(
        config_path=str(config_path),
        env_path=str(env_path),
        runs_db_path=str(tmp_path / "runs.sqlite3"),
        api_token="test-token",
    )

    with TestClient(app) as client:
        response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_bearer_auth_is_required_when_token_configured(tmp_path):
    config_path, env_path = _empty_config(tmp_path)
    app = create_app(
        config_path=str(config_path),
        env_path=str(env_path),
        runs_db_path=str(tmp_path / "runs.sqlite3"),
        api_token="test-token",
    )

    with TestClient(app) as client:
        missing = client.get("/v1/runs/missing")
        wrong = client.get("/v1/runs/missing", headers={"Authorization": "Bearer wrong"})
        allowed = client.get(
            "/v1/runs/missing",
            headers={"Authorization": "Bearer test-token"},
        )

    assert missing.status_code == 401
    assert wrong.status_code == 401
    assert allowed.status_code == 404


def test_auth_is_disabled_when_no_token_is_configured(tmp_path, monkeypatch):
    monkeypatch.delenv("REMEDY_DRUPAL_API_TOKEN", raising=False)
    monkeypatch.delenv("DRUPAL_REMEDY_API_TOKEN", raising=False)
    config_path, env_path = _empty_config(tmp_path)
    app = create_app(
        config_path=str(config_path),
        env_path=str(env_path),
        runs_db_path=str(tmp_path / "runs.sqlite3"),
        api_token="",
    )

    with TestClient(app) as client:
        response = client.get("/v1/runs/missing")

    assert response.status_code == 404
