"""Shared fixtures for the yacmemo v2 test suite."""

from __future__ import annotations

import socket as _socket
import threading as _threading
import time as _time

import pytest
import uvicorn as _uvicorn

from yacmemo.config import (
    Config,
    EmbeddingConfig,
    GuardConfig,
    MemoryConfig,
    SearchConfig,
    load_config,
)
from yacmemo.index_db import IndexDB
from yacmemo.search import Searcher
from yacmemo.store import Store
from yacmemo.vector import VectorStore


@pytest.fixture
def cfg(tmp_path) -> Config:
    return Config(
        memory=MemoryConfig(root=str(tmp_path / "memory")),
        embedding=EmbeddingConfig(),  # endpoints unused; Store gets a fake client
        search=SearchConfig(),
        guard=GuardConfig(),
    )


@pytest.fixture
def db(cfg):
    d = IndexDB(cfg.sqlite_path)
    yield d
    d.close()


class FakeEmbedding:
    """Deterministic keyword-bucket embedding (1024-dim, matching LanceDB schema).

    Texts containing the same keyword land in the same direction (cosine 1.0);
    unrelated texts land in orthogonal directions (cosine 0.0). Same text always
    yields the identical vector.
    """

    BUCKETS = {"端口": 0, "备份": 1, "服务器": 2, "网络": 3}

    def __init__(self, dim: int = 1024):
        self.dim = dim
        self.calls = 0

    def embed_one(self, text: str) -> list[float]:
        self.calls += 1
        v = [0.0] * self.dim
        hit = False
        for key, idx in self.BUCKETS.items():
            if key in text:
                v[idx] = 1.0
                hit = True
        if not hit:
            # orthogonal junk bucket, deterministic per text
            v[100 + (abs(hash(text)) % 800)] = 1.0
        return v

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [self.embed_one(t) for t in texts]


@pytest.fixture
def emb():
    return FakeEmbedding()


@pytest.fixture
def vectors(cfg):
    return VectorStore(cfg.lancedb_path, dimensions=1024)


@pytest.fixture
def store(cfg, db, emb, vectors) -> Store:
    return Store(cfg, db, emb, vectors)


@pytest.fixture
def searcher(cfg, db, emb, vectors) -> Searcher:
    return Searcher(cfg, db, emb, vectors)


@pytest.fixture
def tstore(store: Store) -> Store:
    """Store with TOPICS.md pre-seeded (one topic covering notes/)."""
    (store.root / "notes").mkdir(exist_ok=True)
    (store.root / "notes" / "a.md").write_text("# a\n内容A\n", encoding="utf-8")
    (store.root / "notes" / "b.md").write_text("# b\n内容B\n", encoding="utf-8")
    store.reindex()
    (store.root / "TOPICS.md").write_text(
        "# 主题记忆注册表\n\n## 笔记主题\n- 卡: notes/a.md\n"
        "- 相关: notes/b.md\n- 现状: 测试主题\n- 注册: 2026-09-14\n",
        encoding="utf-8",
    )
    store._index_note("TOPICS.md", "主题记忆注册表",
                      (store.root / "TOPICS.md").read_text(encoding="utf-8"))
    return store


# ---- HTTP server fixture (shared by test_server / test_webui) ----


def _free_port() -> int:
    s = _socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture
def http_server(tmp_path):
    """Two-user HTTP server (FTS-only: no embedding endpoint configured)."""
    config_file = tmp_path / "config.toml"
    config_file.write_text(
        f"""
[embedding]
base_url = ""
model = ""

[server]
data_dir = "{(tmp_path / "server-data").as_posix()}"

[[users]]
id = "alice"
root = "{(tmp_path / "alice").as_posix()}"

[[users]]
id = "bob"
root = "{(tmp_path / "bob").as_posix()}"
""",
        encoding="utf-8",
    )
    from yacmemo.server import create_app

    config = load_config(str(config_file))
    app = create_app(config)

    port = _free_port()
    server = _uvicorn.Server(_uvicorn.Config(app, host="127.0.0.1", port=port,
                                             log_level="error"))
    thread = _threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(100):
        if server.started:
            break
        _time.sleep(0.1)
    assert server.started, "uvicorn did not start"

    yield port

    server.should_exit = True
    thread.join(timeout=5)
