"""Configuration for the yacmemo lean memory layer (v2).

Everything derives from one memory_root: markdown files live there, and the
rebuildable index (SQLite + LanceDB) lives in `<root>/.index/`.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class MemoryConfig:
    root: str = "memory"
    # Timeline notes live here; exempt from the title guard
    journal_dir: str = "journal"


@dataclass
class EmbeddingConfig:
    base_url: str = "http://localhost:11235/v1"
    api_key: str = ""
    model: str = ""
    dimensions: int = 1024
    timeout: int = 30


@dataclass
class SearchConfig:
    rrf_k: int = 60
    # Reserved: jieba shadow column if trigram recall proves weak (P0 decision point)
    fts_seg_fallback: bool = False


@dataclass
class GuardConfig:
    title_similarity_threshold: float = 0.85
    collision_cosine_threshold: float = 0.86
    obs_topk: int = 5
    # When >= this many force-bypasses happened in the last 24h, force also
    # requires force_confirm=True (two-step human-confirmation semantics)
    force_confirm_threshold: int = 3


@dataclass
class CuratorConfig:
    # Periodic quality reviewer (proposal-only; never auto-applies)
    enabled: bool = False
    schedule: str = "0 4 * * 6"  # systemd timer is deployed separately; kept for reference
    base_url: str = ""           # OpenAI-compatible endpoint (e.g. the main model on m2ultra)
    api_key: str = ""
    model: str = ""
    max_tokens: int = 4096
    timeout: int = 180


@dataclass
class ServerConfig:
    host: str = "0.0.0.0"  # LAN-exposed so any machine's agent can reach it
    port: int = 9721
    data_dir: str = "data"  # server-level state (usage log); relative to cwd


@dataclass
class UserEntry:
    """One mounted memory root in the HTTP server; id must be URL-safe."""
    id: str
    root: str


# ids that would shadow server routes
_RESERVED_IDS = {"api", "ui", "health"}


@dataclass
class Config:
    memory: MemoryConfig = field(default_factory=MemoryConfig)
    embedding: EmbeddingConfig = field(default_factory=EmbeddingConfig)
    search: SearchConfig = field(default_factory=SearchConfig)
    guard: GuardConfig = field(default_factory=GuardConfig)
    server: ServerConfig = field(default_factory=ServerConfig)
    curator: CuratorConfig = field(default_factory=CuratorConfig)
    users: list[UserEntry] = field(default_factory=list)
    config_path: str | None = None  # set by load_config when a TOML file was used

    @property
    def root_abs(self) -> Path:
        return Path(self.memory.root).expanduser().resolve()

    @property
    def index_dir(self) -> Path:
        return self.root_abs / ".index"

    @property
    def sqlite_path(self) -> str:
        return str(self.index_dir / "index.db")

    @property
    def lancedb_path(self) -> str:
        return str(self.index_dir / "lancedb")

    @property
    def journal_prefix(self) -> str:
        return self.memory.journal_dir.replace("\\", "/").strip("/") + "/"

    def user_root_abs(self, user: UserEntry) -> Path:
        """Absolute memory root for an HTTP-server user entry."""
        return Path(user.root).expanduser().resolve()


def load_config(path: str | None = None) -> Config:
    """Load config.toml; every section/key is optional and falls back to defaults."""
    import re

    data: dict = {}
    if path:
        with open(path, "rb") as f:
            data = tomllib.load(f)

    mem = data.get("memory", {})
    emb = data.get("embedding", {})
    search = data.get("search", {})
    guard = data.get("guard", {})
    srv = data.get("server", {})
    cur = data.get("curator", {})
    users_raw = data.get("users", [])

    users = []
    for u in users_raw:
        uid = str(u.get("id", "")).strip()
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,32}", uid):
            raise ValueError(
                f"用户 id 非法（需 [A-Za-z0-9_-]，1-32 位，用作 URL 路径）: {uid!r}")
        if uid in _RESERVED_IDS:
            raise ValueError(f"用户 id 不能是保留字: {uid!r}")
        users.append(UserEntry(id=uid, root=str(u.get("root", ""))))

    cfg = Config(
        memory=MemoryConfig(
            root=mem.get("root", MemoryConfig.root),
            journal_dir=mem.get("journal_dir", MemoryConfig.journal_dir),
        ),
        embedding=EmbeddingConfig(
            base_url=emb.get("base_url", EmbeddingConfig.base_url),
            api_key=emb.get("api_key", EmbeddingConfig.api_key),
            model=emb.get("model", EmbeddingConfig.model),
            dimensions=emb.get("dimensions", EmbeddingConfig.dimensions),
            timeout=emb.get("timeout", EmbeddingConfig.timeout),
        ),
        search=SearchConfig(
            rrf_k=search.get("rrf_k", SearchConfig.rrf_k),
            fts_seg_fallback=search.get("fts_seg_fallback", SearchConfig.fts_seg_fallback),
        ),
        guard=GuardConfig(
            title_similarity_threshold=guard.get(
                "title_similarity_threshold", GuardConfig.title_similarity_threshold
            ),
            collision_cosine_threshold=guard.get(
                "collision_cosine_threshold", GuardConfig.collision_cosine_threshold
            ),
            obs_topk=guard.get("obs_topk", GuardConfig.obs_topk),
            force_confirm_threshold=guard.get(
                "force_confirm_threshold", GuardConfig.force_confirm_threshold
            ),
        ),
        server=ServerConfig(
            host=srv.get("host", ServerConfig.host),
            port=srv.get("port", ServerConfig.port),
            data_dir=srv.get("data_dir", ServerConfig.data_dir),
        ),
        curator=CuratorConfig(
            enabled=cur.get("enabled", CuratorConfig.enabled),
            schedule=cur.get("schedule", CuratorConfig.schedule),
            base_url=cur.get("base_url", CuratorConfig.base_url),
            api_key=cur.get("api_key", CuratorConfig.api_key),
            model=cur.get("model", CuratorConfig.model),
            max_tokens=cur.get("max_tokens", CuratorConfig.max_tokens),
            timeout=cur.get("timeout", CuratorConfig.timeout),
        ),
        users=users,
    )
    cfg.config_path = path
    return cfg
