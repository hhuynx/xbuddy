"""Local configuration. Secrets are never serialized into API responses."""
from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]


def endpoint_is_safe(url: str) -> bool:
    parsed = urlparse(url)
    return bool(parsed.hostname and not parsed.username and not parsed.password
                and not parsed.query and not parsed.fragment
                and (parsed.scheme == "https" or
                     (parsed.scheme == "http" and parsed.hostname in {"localhost", "127.0.0.1", "::1"})))


@dataclass(frozen=True)
class Settings:
    data_dir: Path = ROOT / "data"
    database_url: str = field(default="", repr=False)
    redis_url: str = field(default="", repr=False)
    embedded_worker: bool = True
    tool_concurrency: int = 2
    fuyao_base_url: str = "https://fuyao.aicubes.cn"
    fuyao_api_key: str = field(default="", repr=False)
    ifind_mcp_url: str = ""
    ifind_api_key: str = field(default="", repr=False)
    ifind_auth_header: str = "Authorization"
    ifind_auth_prefix: str = "Bearer"
    ifind_transport: str = "streamable_http"
    ifind_allowlist: tuple[str, ...] = ()
    timeout_seconds: float = 20
    max_tool_attempts: int = 30
    llm_base_url: str = ""
    llm_model: str = ""
    llm_api_key: str = field(default="", repr=False)
    llm_allow_no_key: bool = False
    model_timeout_seconds: float = 90
    model_max_output_tokens: int = 12000
    llm_reasoning_effort: str = 'auto'
    cookie_secure: bool = False
    allowed_hosts: tuple[str, ...] = ("localhost", "127.0.0.1", "testserver")

    @property
    def db_path(self) -> Path:
        return self.data_dir / "xbuddy.sqlite3"

    def provider_fingerprint(self, provider: str) -> str:
        if provider == 'documents':
            return hashlib.sha256(b'user_documents_v1').hexdigest()
        parts = (self.fuyao_base_url, self.fuyao_api_key) if provider == "fuyao" else (
            self.ifind_mcp_url, self.ifind_api_key, self.ifind_transport)
        return hashlib.sha256("\0".join(parts).encode()).hexdigest()

    @classmethod
    def load(cls) -> "Settings":
        load_dotenv(ROOT / ".env", override=False)
        return cls(
            database_url=os.getenv("XBUDDY_DATABASE_URL", ""),
            redis_url=os.getenv("XBUDDY_REDIS_URL", ""),
            embedded_worker=os.getenv("XBUDDY_EMBEDDED_WORKER", "true").lower() == "true",
            tool_concurrency=min(3, max(1, int(os.getenv("XBUDDY_TOOL_CONCURRENCY", "2")))),
            data_dir=Path(os.getenv("XBUDDY_DATA_DIR") or str(ROOT / "data")).resolve(),
            fuyao_base_url=os.getenv("FUYAO_BASE_URL", "https://fuyao.aicubes.cn").rstrip("/"),
            fuyao_api_key=os.getenv("FUYAO_API_KEY", ""),
            ifind_mcp_url=os.getenv("IFIND_MCP_URL", ""),
            ifind_api_key=os.getenv("IFIND_API_KEY", ""),
            ifind_auth_header=os.getenv("IFIND_AUTH_HEADER", "Authorization"),
            ifind_auth_prefix=os.getenv("IFIND_AUTH_PREFIX", "Bearer"),
            ifind_transport=os.getenv("IFIND_TRANSPORT", "streamable_http"),
            ifind_allowlist=tuple(x.strip() for x in os.getenv("IFIND_TOOL_ALLOWLIST", "").split(",") if x.strip()),
            timeout_seconds=min(60, max(1, float(os.getenv("PROBE_TIMEOUT_SECONDS", "20")))),
            llm_base_url=os.getenv("LLM_BASE_URL", "").rstrip("/"),
            llm_model=os.getenv("LLM_MODEL", ""),
            llm_api_key=os.getenv("LLM_API_KEY", ""),
            llm_allow_no_key=os.getenv("LLM_ALLOW_NO_KEY", "false").lower() == "true",
            model_timeout_seconds=min(180, max(10, float(os.getenv('LLM_TIMEOUT_SECONDS', '90')))),
            model_max_output_tokens=min(24000, max(2000, int(os.getenv('LLM_MAX_OUTPUT_TOKENS', '12000')))),
            llm_reasoning_effort=os.getenv('LLM_REASONING_EFFORT', 'auto').lower(),
            cookie_secure=os.getenv("XBUDDY_COOKIE_SECURE", "false").lower() == "true",
            allowed_hosts=tuple(x.strip() for x in os.getenv("XBUDDY_ALLOWED_HOSTS", "localhost,127.0.0.1,testserver").split(",") if x.strip()),
        )

    @property
    def secrets(self) -> tuple[str, ...]:
        return tuple(x for x in (self.fuyao_api_key, self.ifind_api_key, self.llm_api_key, os.getenv("LLM_API_KEY", ""),
            self.database_url, self.redis_url, urlparse(self.database_url).password, urlparse(self.redis_url).password) if x)
