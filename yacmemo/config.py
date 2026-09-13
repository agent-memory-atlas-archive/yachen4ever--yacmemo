"""Configuration loading for yacmemo with multi-user support."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path


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
    web_port: int = 9722           # reserved for future split; mounts at /admin on main port


@dataclass
class UserConfig:
    """Per-user configuration stored in the users table.

    LLM/embedding settings inherit from the global [llm]/[embedding] sections,
    but can be overridden per user (e.g. different API key).
    """
    id: str                          # unique user identifier (used in --user flag)
    display_name: str = ""           # human-friendly name
    memory_root: str = ""            # directory for this user's .md files
    llm_api_key: str = ""            # override global LLM api_key (empty = inherit)
    embedding_api_key: str = ""      # override global embedding api_key (empty = inherit)


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

    @property
    def sqlite_abs(self) -> str:
        """System-level SQLite path."""
        sqlite_rel = self.storage.get("sqlite_path", "data/system.db")
        base = os.environ.get("YACMEMO_HOME", os.getcwd())
        return str(Path(base) / sqlite_rel) if not os.path.isabs(sqlite_rel) else sqlite_rel

    def user_memory_root_abs(self, user: UserConfig) -> str:
        """Absolute path to this user's memory directory."""
        root = user.memory_root
        if os.path.isabs(root):
            return root
        base = os.environ.get("YACMEMO_HOME", os.getcwd())
        return str(Path(base) / root)

    def user_lancedb_abs(self, user: UserConfig) -> str:
        """Absolute path to this user's LanceDB directory."""
        lancedb_base = self.storage.get("lancedb_path", "data/lancedb/")
        base = os.environ.get("YACMEMO_HOME", os.getcwd())
        lancedb_root = (
            str(Path(base) / lancedb_base)
            if not os.path.isabs(lancedb_base) else lancedb_base
        )
        return os.path.join(lancedb_root, user.id)

    def sync_users_to_db(self, db) -> list[UserConfig]:
        """Sync config.toml [[users]] into the database.

        - Users in config.toml that don't exist in DB are added.
        - Users in DB that aren't in config.toml are kept (runtime additions via WebUI).
        - Existing users' fields are updated from config.toml.
        Returns the full user list from the database (as UserConfig objects).
        """
        {u.id for u in self.users}

        # Add/update users from config.toml
        for u in self.users:
            existing = db.get_user(u.id)
            if existing:
                db.update_user(u.id,
                               display_name=u.display_name,
                               memory_root=u.memory_root,
                               llm_api_key=u.llm_api_key,
                               embedding_api_key=u.embedding_api_key)
            else:
                db.add_user(
                    id=u.id,
                    display_name=u.display_name,
                    memory_root=u.memory_root,
                    llm_api_key=u.llm_api_key,
                    embedding_api_key=u.embedding_api_key,
                )

        # Load all users from DB (includes runtime-added ones)
        db_users = db.list_users()
        return [UserConfig(
            id=u["id"],
            display_name=u["display_name"],
            memory_root=u["memory_root"],
            llm_api_key=u["llm_api_key"],
            embedding_api_key=u["embedding_api_key"],
        ) for u in db_users]


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
