"""Configuration loading for memory-enhancer."""

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
class StorageConfig:
    memory_root: str
    sqlite_path: str
    lancedb_path: str


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
    host: str = "127.0.0.1"
    port: int = 9721


@dataclass
class Config:
    llm: LLMConfig
    embedding: EmbeddingConfig
    storage: StorageConfig
    schedule: ScheduleConfig = field(default_factory=ScheduleConfig)
    consistency: ConsistencyConfig = field(default_factory=ConsistencyConfig)
    server: ServerConfig = field(default_factory=ServerConfig)

    @property
    def memory_root_abs(self) -> str:
        """Return memory_root as absolute path, resolved relative to workspace."""
        root = self.storage.memory_root
        if os.path.isabs(root):
            return root
        workspace = os.environ.get("TELEAGENT_WORKSPACE", os.getcwd())
        return str(Path(workspace) / root)

    @property
    def sqlite_abs(self) -> str:
        return os.path.join(os.path.dirname(self.memory_root_abs), self.storage.sqlite_path)

    @property
    def lancedb_abs(self) -> str:
        return os.path.join(os.path.dirname(self.memory_root_abs), self.storage.lancedb_path)


def load_config(path: str | None = None) -> Config:
    """Load config from TOML file. Path can be overridden by MEMORY_CONFIG env var."""
    if path is None:
        path = os.environ.get("MEMORY_CONFIG", "config.toml")

    with open(path, "rb") as f:
        data = tomllib.load(f)

    return Config(
        llm=LLMConfig(**data["llm"]),
        embedding=EmbeddingConfig(**data["embedding"]),
        storage=StorageConfig(**data["storage"]),
        schedule=ScheduleConfig(**data.get("schedule", {})),
        consistency=ConsistencyConfig(**data.get("consistency", {})),
        server=ServerConfig(**data.get("server", {})),
    )
