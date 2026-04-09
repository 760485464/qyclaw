from __future__ import annotations

from functools import lru_cache
from pathlib import Path
import os

import yaml
from pydantic import BaseModel, ConfigDict, Field


class AppSection(BaseModel):
    model_config = ConfigDict(extra="ignore")

    name: str = "qyclaw-backend"
    locale: str = "en"
    env: str = "development"
    host: str = "0.0.0.0"
    port: int = 8000
    debug: bool = True
    frontend_base_url: str = "http://localhost:8080/frontend"


class DatabaseSection(BaseModel):
    model_config = ConfigDict(extra="ignore")

    host: str = "localhost"
    port: int = 5432
    user: str = "qyclaw"
    password: str = "qyclaw_dev_password"
    name: str = "qyclaw"

    @property
    def sqlalchemy_url(self) -> str:
        host = "127.0.0.1" if self.host == "localhost" else self.host
        return f"postgresql+psycopg://{self.user}:{self.password}@{host}:{self.port}/{self.name}"


class SmtpSection(BaseModel):
    model_config = ConfigDict(extra="ignore")

    enabled: bool = False
    host: str = "smtp.example.com"
    port: int = 587
    username: str = ""
    password: str = ""
    from_email: str = "noreply@qyclaw.local"
    from_name: str = "Qyclaw"
    use_tls: bool = True
    use_ssl: bool = False
    timeout_seconds: int = 15


class AuthSection(BaseModel):
    model_config = ConfigDict(extra="ignore")

    expose_password_reset_debug: bool = False


class SkillStorageSection(BaseModel):
    model_config = ConfigDict(extra="ignore")

    userskills_dir: str = "./userskills"
    preskills_dir: str = "./preskills"
    skills_dir: str = "./skills"
    agentskills_dir: str = "./agentskills"
    conversationskills_dir: str = "./conversationskills"


class BackendRoutingSection(BaseModel):
    model_config = ConfigDict(extra="ignore")

    default_backend: str = "deepagents"
    rollout_backend: str = "claude"
    rollout_percent: int = 0
    user_overrides: dict[str, str] = Field(default_factory=dict)
    enable_fallback: bool = True
    fallback_backend: str = "deepagents"


class ClaudeAgentSection(BaseModel):
    model_config = ConfigDict(extra="ignore")

    enabled: bool = True
    model: str = "claude-sonnet-4-20250514"
    cli_path: str | None = None
    fallback_model: str | None = None
    permission_mode: str = "default"
    max_turns: int | None = None
    max_thinking_tokens: int | None = None
    effort: str | None = None
    base_url: str | None = None
    api_key: str | None = None


class SecuritySection(BaseModel):
    model_config = ConfigDict(extra="ignore")

    debug_exec_enabled: bool = True
    debug_exec_admin_only: bool = True
    terminal_policy: str = "strict"
    terminal_allowed_roots: list[str] = Field(default_factory=lambda: ["/workspace", "/tmp"])


class HindsightSection(BaseModel):
    model_config = ConfigDict(extra="ignore")

    enabled: bool = False
    base_url: str = "http://127.0.0.1:8888"
    api_key: str | None = None
    timeout: float = 30.0
    user_bank_prefix: str = "user"
    recall_budget: str = "mid"
    recall_max_tokens: int = 2000
    retain_async: bool = False
    reflect_every_turns: int = 20


class Settings(BaseModel):
    model_config = ConfigDict(extra="allow")

    app: AppSection = Field(default_factory=AppSection)
    database: DatabaseSection = Field(default_factory=DatabaseSection)
    smtp: SmtpSection = Field(default_factory=SmtpSection)
    auth: AuthSection = Field(default_factory=AuthSection)
    skill_storage: SkillStorageSection = Field(default_factory=SkillStorageSection)
    backend_routing: BackendRoutingSection = Field(default_factory=BackendRoutingSection)
    claude_agent: ClaudeAgentSection = Field(default_factory=ClaudeAgentSection)
    security: SecuritySection = Field(default_factory=SecuritySection)
    hindsight: HindsightSection = Field(default_factory=HindsightSection)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    config_path = Path(os.getenv("QYCLAW_CONFIG", "config.yaml"))
    if not config_path.exists():
        raise FileNotFoundError(f"Config file not found: {config_path}")

    with config_path.open("r", encoding="utf-8") as fp:
        raw = yaml.safe_load(fp) or {}

    db_overrides = {
        "host": os.getenv("QYCLAW_DB_HOST"),
        "port": os.getenv("QYCLAW_DB_PORT"),
        "user": os.getenv("QYCLAW_DB_USER"),
        "password": os.getenv("QYCLAW_DB_PASSWORD"),
        "name": os.getenv("QYCLAW_DB_NAME"),
    }
    if any(db_overrides.values()):
        database = dict(raw.get("database") or {})
        for key, value in db_overrides.items():
            if value is None:
                continue
            if key == "port":
                try:
                    database[key] = int(value)
                except ValueError:
                    continue
            else:
                database[key] = value
        raw["database"] = database

    frontend_url = os.getenv("QYCLAW_FRONTEND_BASE_URL")
    if frontend_url:
        app_cfg = dict(raw.get("app") or {})
        app_cfg["frontend_base_url"] = frontend_url
        raw["app"] = app_cfg

    return Settings.model_validate(raw)
