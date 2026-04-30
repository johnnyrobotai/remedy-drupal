"""Configuration for Remedy Drupal sites and LLM backends.

Loads per-campus Drupal site configs from YAML files with env-var overrides.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class DrupalAuthConfig:
    """Authentication credentials for a Drupal site."""

    auth_type: str = "cookie"  # "cookie" | "basic" | "oauth2"
    username: str = ""
    password: str = ""
    oauth_token_url: str = ""
    oauth_client_id: str = ""
    oauth_client_secret: str = ""


@dataclass(frozen=True)
class DrupalSiteConfig:
    """Configuration for a single campus Drupal site."""

    campus_code: str = ""
    base_url: str = ""
    auth: DrupalAuthConfig = field(default_factory=DrupalAuthConfig)
    http_auth_username: str = ""  # Acquia dev-shield basic auth
    http_auth_password: str = ""
    headless: bool = True
    verify_ssl: bool = True
    timeout: float = 30.0


@dataclass(frozen=True)
class LLMConfig:
    """Configuration for the LLM backend used by the remediator."""

    backend: str = "ollama"  # "ollama" | "openai"
    api_mode: str = "native"  # "native" | "openai_compat"
    base_url: str = "https://ollama.com/api"
    api_key: str = ""
    text_model: str = "kimi-k2.6:cloud"
    vision_model: str = "kimi-k2.6:cloud"
    max_concurrent: int = 4
    temperature: float = 0.0
    top_p: float = 1.0
    seed: int | None = None
    disable_thinking: bool = True
    text_max_tokens: int = 8192
    vision_max_tokens: int = 256


# ------------------------------------------------------------------
# Builders
# ------------------------------------------------------------------

def _build_site_config(campus_code: str, yml: dict[str, Any]) -> DrupalSiteConfig:
    """Build a DrupalSiteConfig from a YAML dict merged with env vars.

    Environment variables follow the pattern:
    - ``DRUPAL_{CODE}_USERNAME``
    - ``DRUPAL_{CODE}_PASSWORD``
    - ``DRUPAL_{CODE}_HTTP_USER``
    - ``DRUPAL_{CODE}_HTTP_PASS``
    """
    code = campus_code.upper()

    auth = DrupalAuthConfig(
        auth_type=yml.get("auth_type", "cookie"),
        username=os.environ.get(f"DRUPAL_{code}_USERNAME", yml.get("username", "")),
        password=os.environ.get(f"DRUPAL_{code}_PASSWORD", yml.get("password", "")),
        oauth_token_url=yml.get("oauth_token_url", ""),
        oauth_client_id=yml.get("oauth_client_id", ""),
        oauth_client_secret=yml.get("oauth_client_secret", ""),
    )

    return DrupalSiteConfig(
        campus_code=code,
        base_url=yml.get("base_url", "").rstrip("/"),
        auth=auth,
        http_auth_username=os.environ.get(
            f"DRUPAL_{code}_HTTP_USER", yml.get("http_auth_username", ""),
        ),
        http_auth_password=os.environ.get(
            f"DRUPAL_{code}_HTTP_PASS", yml.get("http_auth_password", ""),
        ),
        headless=yml.get("headless", True),
        verify_ssl=yml.get("verify_ssl", True),
        timeout=float(yml.get("timeout", 30.0)),
    )


def _build_llm_config(yml: dict[str, Any]) -> LLMConfig:
    """Build an LLMConfig from YAML + env vars."""
    seed_raw = yml.get("seed")
    return LLMConfig(
        backend=yml.get("backend", "ollama"),
        api_mode=yml.get("api_mode", "native"),
        base_url=os.environ.get(
            "OLLAMA_BASE_URL",
            yml.get("base_url", "https://ollama.com/api"),
        ).rstrip("/"),
        api_key=os.environ.get("OLLAMA_API_KEY", yml.get("api_key", "")),
        text_model=os.environ.get(
            "TEXT_MODEL",
            yml.get("text_model", "kimi-k2.6:cloud"),
        ),
        vision_model=os.environ.get(
            "VISION_MODEL",
            yml.get("vision_model", "kimi-k2.6:cloud"),
        ),
        max_concurrent=int(yml.get("max_concurrent", 4)),
        temperature=float(yml.get("temperature", 0.0)),
        top_p=float(yml.get("top_p", 1.0)),
        seed=int(seed_raw) if seed_raw is not None else None,
        disable_thinking=bool(yml.get("disable_thinking", True)),
        text_max_tokens=int(yml.get("text_max_tokens", 8192)),
        vision_max_tokens=int(yml.get("vision_max_tokens", 256)),
    )

# ------------------------------------------------------------------
# Public loader
# ------------------------------------------------------------------

def load_config(
    yaml_path: str | Path | None = None,
    env_path: str | Path | None = None,
) -> dict[str, DrupalSiteConfig]:
    """Load campus site configs from a YAML file and optional .env file.

    Parameters
    ----------
    yaml_path:
        Path to a YAML file with top-level keys ``sites`` (list of campus
        dicts) and optionally ``llm`` (LLM backend settings).  If *None*,
        looks for ``drupal_remedy.yml`` in the current directory.
    env_path:
        Path to a ``.env`` file.  If provided, variables are loaded into
        ``os.environ`` before building configs.

    Returns
    -------
    dict mapping campus_code (uppercased) to :class:`DrupalSiteConfig`.
    A special key ``"__llm__"`` holds the :class:`LLMConfig` if present.
    """
    # Load .env if provided.
    if env_path is not None:
        _load_dotenv(Path(env_path))

    # Load YAML.
    if yaml_path is None:
        yaml_path = Path("drupal_remedy.yml")
    else:
        yaml_path = Path(yaml_path)

    try:
        import yaml  # type: ignore[import-untyped]
    except ImportError:
        raise SystemExit("PyYAML is required: pip install pyyaml")

    if not yaml_path.exists():
        raise FileNotFoundError(f"Config file not found: {yaml_path}")

    with open(yaml_path) as fh:
        data = yaml.safe_load(fh) or {}

    configs: dict[str, Any] = {}
    for site_dict in data.get("sites", []):
        code = site_dict.get("campus_code", "").upper()
        if not code:
            continue
        configs[code] = _build_site_config(code, site_dict)

    if "llm" in data:
        configs["__llm__"] = _build_llm_config(data["llm"])

    return configs


def _load_dotenv(path: Path) -> None:
    """Minimal .env loader — sets KEY=VALUE lines into os.environ."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip("\"'")
        os.environ.setdefault(key, value)
