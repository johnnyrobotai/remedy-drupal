from __future__ import annotations

from drupal_remedy.config import DrupalSiteConfig, LLMConfig, load_config


def test_load_config_merges_yaml_and_env_overrides(monkeypatch, tmp_path):
    for key in (
        "DRUPAL_ELAC_USERNAME",
        "DRUPAL_ELAC_PASSWORD",
        "DRUPAL_ELAC_HTTP_USER",
        "DRUPAL_ELAC_HTTP_PASS",
        "OLLAMA_API_KEY",
        "OLLAMA_BASE_URL",
        "TEXT_MODEL",
        "VISION_MODEL",
    ):
        monkeypatch.delenv(key, raising=False)

    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
sites:
  - campus_code: elac
    base_url: "https://example.edu/"
    auth_type: basic
    username: yaml-user
    password: yaml-pass
    http_auth_username: yaml-shield-user
    http_auth_password: yaml-shield-pass
    headless: false
    verify_ssl: false
    timeout: 12.5
llm:
  base_url: "https://llm.example/api/"
  api_key: yaml-llm-key
  text_model: yaml-text
  vision_model: yaml-vision
  max_concurrent: 2
  seed: 42
""",
        encoding="utf-8",
    )
    env_path = tmp_path / ".env"
    env_path.write_text(
        "\n".join(
            [
                "DRUPAL_ELAC_USERNAME=env-user",
                "DRUPAL_ELAC_PASSWORD=env-pass",
                "DRUPAL_ELAC_HTTP_USER=env-shield-user",
                "DRUPAL_ELAC_HTTP_PASS=env-shield-pass",
                "OLLAMA_API_KEY=env-llm-key",
                "OLLAMA_BASE_URL=https://env-llm.example/api/",
                "TEXT_MODEL=env-text",
                "VISION_MODEL=env-vision",
            ]
        ),
        encoding="utf-8",
    )

    configs = load_config(yaml_path=config_path, env_path=env_path)

    site = configs["ELAC"]
    assert isinstance(site, DrupalSiteConfig)
    assert site.base_url == "https://example.edu"
    assert site.auth.auth_type == "basic"
    assert site.auth.username == "env-user"
    assert site.auth.password == "env-pass"
    assert site.http_auth_username == "env-shield-user"
    assert site.http_auth_password == "env-shield-pass"
    assert site.headless is False
    assert site.verify_ssl is False
    assert site.timeout == 12.5

    llm = configs["__llm__"]
    assert isinstance(llm, LLMConfig)
    assert llm.base_url == "https://env-llm.example/api"
    assert llm.api_key == "env-llm-key"
    assert llm.text_model == "env-text"
    assert llm.vision_model == "env-vision"
    assert llm.max_concurrent == 2
    assert llm.seed == 42
