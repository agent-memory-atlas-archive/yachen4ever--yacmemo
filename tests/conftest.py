"""Shared fixtures for the yacmemo v2 test suite."""

from __future__ import annotations

import pytest

from yacmemo.config import (
    Config,
    EmbeddingConfig,
    GuardConfig,
    MemoryConfig,
    SearchConfig,
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
