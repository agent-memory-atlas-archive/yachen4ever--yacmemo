"""Hybrid search: FTS5(trigram) + LanceDB vectors, fused with Reciprocal Rank Fusion.

Two channels, one result list:
- fts:    SQLite FTS5 trigram (Chinese substring matching, BM25 order)
- vector: Qwen3-Embedding nearest notes (semantic)

The vector channel degrades to empty on any failure (embedding endpoint down,
index stale) and hybrid quietly falls back to FTS-only. RRF fuses by rank
only, so no score-scale alignment between channels is needed.
"""

from __future__ import annotations

import logging

from .config import Config
from .embedding import EmbeddingClient
from .index_db import IndexDB
from .vector import VectorStore

logger = logging.getLogger(__name__)


class Searcher:
    def __init__(self, config: Config, db: IndexDB,
                 emb: EmbeddingClient | None, vectors: VectorStore | None):
        self.config = config
        self.db = db
        self.emb = emb
        self.vectors = vectors
        # 最近一次 search() 的补充说明（向量通道降级 / 短查询提示），
        # MCP memory_search 与 WebUI 搜索页读取展示
        self.last_notice: str | None = None

    # ---------------------------------------------------------------- channels

    def fts_channel(self, query: str, limit: int) -> list[dict]:
        if len(query.strip()) < 3:
            # trigram 分词下 <3 字查询永不命中——LIKE 子串扫描兜底
            rows = self.db.like_search(query, limit)
            return [{"path": r["path"], "title": r["title"], "rank": i + 1,
                     "channels": ["fts"]} for i, r in enumerate(rows)]
        rows = self.db.fts_search(query, limit)
        return [{"path": r["path"], "title": r["title"], "rank": r["rank"],
                 "channels": ["fts"]} for r in rows]

    def vector_channel(self, query: str, limit: int) -> list[dict]:
        if not (self.emb and self.vectors):
            return []
        try:
            qv = self.emb.embed_one(query)
            hits = self.vectors.search_note_vectors(qv, limit)
        except Exception as e:
            self.last_notice = f"向量通道不可用（{e}），本次结果仅 FTS"
            logger.warning("Vector channel unavailable: %s", e)
            return []
        out = []
        for i, hit in enumerate(hits):
            path = hit["id"]
            out.append({"path": path, "title": self._title_of(path),
                        "rank": i + 1, "channels": ["vector"]})
        return out

    # ---------------------------------------------------------------- search

    def search(self, query: str, limit: int = 10,
               kind: str = "hybrid") -> list[dict]:
        if kind not in ("hybrid", "fts", "vector"):
            raise ValueError(f"未知检索类型: {kind}")
        self.last_notice = None
        channels = []
        if kind in ("hybrid", "fts"):
            channels.append(self.fts_channel(query, limit))
        if kind in ("hybrid", "vector"):
            channels.append(self.vector_channel(query, limit))

        if len(channels) == 2:
            merged = self._rrf(channels, limit)
        else:
            merged = self._take_first(channels, limit)

        if not merged and len(query.strip()) < 3 and not self.last_notice:
            # 短查询空结果：LIKE 兜底也没命中，提示换更长的关键词
            self.last_notice = "短于 3 字的查询无法被 FTS trigram 命中，请换更长的关键词"

        for r in merged:
            r["warnings"] = self._warnings_for(r["path"])
        return merged

    def _rrf(self, channels: list[list[dict]], limit: int) -> list[dict]:
        k = self.config.search.rrf_k
        scores: dict[str, float] = {}
        meta: dict[str, dict] = {}
        for ch in channels:
            for r in ch:
                scores[r["path"]] = scores.get(r["path"], 0.0) + 1.0 / (k + r["rank"])
                if r["path"] in meta:
                    meta[r["path"]]["channels"] = sorted(
                        set(meta[r["path"]]["channels"]) | set(r["channels"]))
                else:
                    meta[r["path"]] = dict(r)
        ordered = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)[:limit]
        return [dict(meta[p], score=round(s, 4)) for p, s in ordered]

    def _take_first(self, channels: list[list[dict]], limit: int) -> list[dict]:
        for ch in channels:
            if ch:
                return [dict(r, score=round(1.0 / (100 + r["rank"]), 4))
                        for r in ch[:limit]]
        return []

    # ---------------------------------------------------------------- helpers

    def _title_of(self, path: str) -> str:
        row = self.db.get_note(path)
        if row:
            return row["title"]
        stem = path.rsplit("/", 1)[-1]
        return stem.removesuffix(".md")

    def _warnings_for(self, path: str) -> list[str]:
        warnings = []
        for c in self.db.collisions_for(path):
            if c["a_path"] == path:
                other_path, other_text = c["b_path"], c["b_text"]
            else:
                other_path, other_text = c["a_path"], c["a_text"]
            other_title = self._title_of(other_path)
            warnings.append(
                f"⚠ 与 [[{other_title}]] 疑似重复（score {c['score']}）— "
                f"建议读两篇后用 memory_edit 合并。对方内容: {other_text[:50]}"
            )
        return warnings
