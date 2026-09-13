"""Configuration loading for yacmemo with multi-user support."""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib


@dataclass
class LLMConfig:
    base_url: str
    api_key: str
    model: str
    enable_thinking: bool = False
    extract_max_tokens: int = 2048
    consistency_max_tokens: int = 512
    extract_timeout: int = 60
    consistency_timeout: int = 30
    response_format: str = "json_object"


@dataclass
class EmbeddingConfig:
    base_url: str
    api_key: str
    model: str
    dimensions: int = 1024
    timeout: int = 30


@dataclass
class ScheduleConfig:
    extract_cron: str = "0 */6 * * *"
    consistency_cron: str = "0 3 * * *"


@dataclass
class ConsistencyConfig:
    similarity_threshold: float = 0.85
    confidence_threshold: float = 0.8
    auto_invalidate: bool = True


@dataclass
class ServerConfig:
    host: str = "0.0.0.0"          # 0.0.0.0 = listen on all interfaces (for remote WebUI access)
    port: int = 9721


@dataclass
class WebUIConfig:
    enabled: bool = True
    admin_token: str = ""           # empty = no auth (local only); set for remote access
    web_port: int = 9722           # reserved for future split; currently mounts at /admin on main port


@dataclass
class UserConfig:
    """Per-user configuration. Memory and indexes are fully isolated.

    LLM/embedding settings inherit from the global [llm]/[embedding] sections,
    but can be overridden per user (e.g. different API key).
    """
    id: str                          # unique user identifier (used in --user flag)
    display_name: str = ""           # human-friendly name
    memory_root: str = ""            # directory for this user's .md files
    llm_api_key: str = ""            # override global LLM api_key (empty = inherit)
    embedding_api_key: str = ""     # override global embedding api_key (empty = inherit)
    sqlite_path: str = ""            # override global sqlite path (empty = inherit)
    lancedb_path: str = ""           # override global lancedb path (empty = inherit)


@dataclass
class Config:
    """Top-level configuration with global defaults + per-user overrides."""
    llm: LLMConfig
    embedding: EmbeddingConfig
    storage: dict                    # raw [storage] for default paths
    schedule: ScheduleConfig = field(default_factory=ScheduleConfig)
    consistency: ConsistencyConfig = field(default_factory=ConsistencyConfig)
    server: ServerConfig = field(default_factory=ServerConfig)
    webui: WebUIConfig = field(default_factory=WebUIConfig)
    users: list[UserConfig] = field(default_factory=list)

    def get_user(self, user_id: str) -> UserConfig:
        """Find a user by id. Raises ValueError if not found."""
        for u in self.users:
            if u.id == user_id:
                return u
        raise ValueError(f"User not found: {user_id}")

    def resolve_llm(self, user: UserConfig) -> LLMConfig:
        """Return LLM config with user-level api_key override applied."""
        api_key = user.llm_api_key or self.llm.api_key
        return LLMConfig(
            base_url=self.llm.base_url,
            api_key=api_key,
            model=self.llm.model,
            enable_thinking=self.llm.enable_thinking,
            extract_max_tokens=self.llm.extract_max_tokens,
            consistency_max_tokens=self.llm.consistency_max_tokens,
            extract_timeout=self.llm.extract_timeout,
            consistency_timeout=self.llm.consistency_timeout,
            response_format=self.llm.response_format,
        )

    def resolve_embedding(self, user: UserConfig) -> EmbeddingConfig:
        """Return embedding config with user-level api_key override applied."""
        api_key = user.embedding_api_key or self.embedding.api_key
        return EmbeddingConfig(
            base_url=self.embedding.base_url,
            api_key=api_key,
            model=self.embedding.model,
            dimensions=self.embedding.dimensions,
            timeout=self.embedding.timeout,
        )

    def user_memory_root_abs(self, user: UserConfig) -> str:
        """Absolute path to this user's memory directory."""
        root = user.memory_root
        if os.path.isabs(root):
            return root
        base = os.environ.get("YACMEMO_HOME", os.getcwd())
        return str(Path(base) / root)

    def user_sqlite_abs(self, user: UserConfig) -> str:
        """Absolute path to this user's SQLite database."""
        memory_root = self.user_memory_root_abs(user)
        sqlite_rel = user.sqlite_path or self.storage.get("sqlite_path", ".index/memory.db")
        return os.path.join(os.path.dirname(memory_root), sqlite_rel)

    def user_lancedb_abs(self, user: UserConfig) -> str:
        """Absolute path to this user's LanceDB directory."""
        memory_root = self.user_memory_root_abs(user)
        lancedb_rel = user.lancedb_path or self.storage.get("lancedb_path", ".index/lancedb/")
        return os.path.join(os.path.dirname(memory_root), lancedb_rel)


def load_config(path: str | None = None) -> Config:
    """Load config from TOML file.

    Path can be overridden by YACMEMO_CONFIG env var.
    Falls back to config.toml in the current directory.
    """
    if path is None:
        path = os.environ.get("YACMEMO_CONFIG", "config.toml")

    with open(path, "rb") as f:
        data = tomllib.load(f)

    users = []
    for u in data.get("users", []):
        users.append(UserConfig(
            id=u["id"],
            display_name=u.get("display_name", u["id"]),
            memory_root=u.get("memory_root", ""),
            llm_api_key=u.get("llm_api_key", ""),
            embedding_api_key=u.get("embedding_api_key", ""),
            sqlite_path=u.get("sqlite_path", ""),
            lancedb_path=u.get("lancedb_path", ""),
        ))

    return Config(
        llm=LLMConfig(**data["llm"]),
        embedding=EmbeddingConfig(**data["embedding"]),
        storage=data.get("storage", {}),
        schedule=ScheduleConfig(**data.get("schedule", {})),
        consistency=ConsistencyConfig(**data.get("consistency", {})),
        server=ServerConfig(**data.get("server", {})),
        webui=WebUIConfig(**data.get("webui", {})),
        users=users,
    )
