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
from .identity import Identity, visible
from .index_db import IndexDB
from .store import PROPOSAL_SETTLED_MARKER, in_archive_zone
from .vector import VectorStore

logger = logging.getLogger(__name__)


class Searcher:
    def __init__(self, config: Config, db: IndexDB,
                 emb: EmbeddingClient | None, vectors: VectorStore | None):
        self.config = config
        self.db = db
        self.emb = emb
        self.vectors = vectors
        # 最近一次 search() 的补充说明（向量通道降级 / 短查询提示）
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
               kind: str = "hybrid",
               identity: Identity | None = None,
               include_archived: bool = True,
               notice_out: list[str] | None = None) -> list[dict]:
        """include_archived=False 供 MCP 工具层使用：归档笔记（archive/）
        对客户端不可见。默认 True——WebUI 检索与人的视野不受影响。

        notice_out：调用方传入 list，本次检索的提示（归档/已结案隐去条数、
        降级说明）逐条追加。last_notice 是实例状态，无状态 HTTP 并发下会被
        其他请求覆盖或读走（2026-10-11 审计 P2）——MCP/WebUI 通道一律用
        notice_out；last_notice 仅为兼容旧消费方保留。"""
        if kind not in ("hybrid", "fts", "vector"):
            raise ValueError(f"未知检索类型: {kind}")
        # 负数/0 会让 SQLite 的 LIMIT 语义翻转（负 = 无上限，候选全量）
        limit = max(1, int(limit))
        self.last_notice = None
        notices: list[str] = []
        # identity / 归档过滤在融合后做：通道多取 3 倍候选，防止不可见结果
        # 挤占限额。identity 非 None 即过滤（含 ANONYMOUS——无 token 的
        # agents/ 区不可见，与 memory_list/memory_read 同口径；identity=None
        # 是人类/服务端内部语义，恒可见）
        overfetch = (identity is not None or not include_archived)
        fetch = limit * 3 if overfetch else limit
        channels = []
        if kind in ("hybrid", "fts"):
            channels.append(self.fts_channel(query, fetch))
        if kind in ("hybrid", "vector"):
            channels.append(self.vector_channel(query, fetch))
        if self.last_notice:
            # 通道级提示（向量降级等）并入逐次返回的 notices——只写
            # last_notice 会在入口被清、被并发覆盖（2026-10-11 审计二轮 P2）
            notices.append(self.last_notice)
            self.last_notice = None

        if len(channels) == 2:
            merged = self._rrf(channels, fetch)
        else:
            merged = self._take_first(channels, fetch)

        if identity is not None:
            # scoped search：user 层 + 本 identity 专属区（identity.py 的
            # visible 是唯一权威）。过滤在前、截断在后——先截断再过滤会让
            # 结果少于 limit（overfetch 3x 只是缓解，不是保证）。
            merged = [r for r in merged if visible(r["path"], identity)]

        if not include_archived:
            merged = self._drop_archived(merged, notices)
        merged = self._drop_settled_proposals(merged, notices)
        merged = merged[:limit]

        if not merged and len(query.strip()) < 3:
            # 短查询空结果：LIKE 兜底也没命中，提示换更长的关键词
            notices.append("短于 3 字的查询无法被 FTS trigram 命中，请换更长的关键词")

        for r in merged:
            r["warnings"] = self._warnings_for(
                r["path"], identity, mask_archive=not include_archived)
        if notices:
            self.last_notice = "；".join(notices)
            if notice_out is not None:
                notice_out.extend(notices)
        return merged

    def _drop_archived(self, results: list[dict],
                       notices: list[str]) -> list[dict]:
        """归档笔记（archive/）不对 MCP 客户端可见（0.3.17）：检索结果里
        全部隐去——归档是人的记忆收纳，agent 不该再把归档内容当活记忆引用。
        WebUI 检索不带本开关，人的视野不受影响；按归档路径的显式
        memory_read 仍可读（那是明确查阅，与已结案提案同款）。"""
        kept, dropped = [], 0
        for r in results:
            if in_archive_zone(r["path"]):
                dropped += 1
                continue
            kept.append(r)
        if dropped:
            notices.append(f"已隐去 {dropped} 条归档笔记"
                           "（archive/ 对客户端不可见；WebUI 已归档分组仍可见）")
        return kept

    def _drop_settled_proposals(self, results: list[dict],
                                notices: list[str]) -> list[dict]:
        """已结案提案（全部条目执行/忽略）默认不对 agent 可见：
        文件头部有已结案标记的 curator/ 报告从结果中隐去——执行类工作
        不该被重复派发；显式 memory_read 仍可读（那是明确查阅）。
        注意结案不是单向状态（复审撤标后回到未结案），不做正结果缓存。"""
        kept, dropped = [], 0
        for r in results:
            if r["path"].startswith("curator/") and self._is_settled(r["path"]):
                dropped += 1
                continue
            kept.append(r)
        if dropped:
            notices.append(f"已隐去 {dropped} 条已结案提案"
                           "（全部条目已执行/忽略，无需重复处理）")
        return kept

    def _is_settled(self, path: str) -> bool:
        body = self.db.fts_body(path)
        return bool(body) and PROPOSAL_SETTLED_MARKER in body

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

    def _warnings_for(self, path: str, identity: Identity | None = None,
                      mask_archive: bool = False) -> list[str]:
        warnings = []
        # identity 非 None 即受控（含 ANONYMOUS——agents/ 区对其不可见）；
        # identity=None 是人类/服务端内部语义
        restricted = identity is not None
        for c in self.db.collisions_for(path):
            if c["a_path"] == path:
                other_path, other_text = c["b_path"], c["b_text"]
            else:
                other_path, other_text = c["a_path"], c["a_text"]
            # warnings 是内容通道：⚠ 会携带对方标题与 50 字原文——对方不可见
            # （其他专属区；MCP 通道下的已归档笔记）时只报存在，不外泄
            # （2026-10-11 审计 P1：归档隐形被 ⚠ 原文打穿）
            if in_archive_zone(other_path) and mask_archive:
                warnings.append(
                    f"⚠ 存在一条疑似重复笔记（score {c['score']}）——"
                    "对象已归档，对客户端不可见。")
                continue
            if restricted and not visible(other_path, identity):
                warnings.append(
                    f"⚠ 存在一条疑似重复笔记（score {c['score']}）——"
                    "对象对当前身份不可见，建议让有权限的会话处理。")
                continue
            other_title = self._title_of(other_path)
            warnings.append(
                f"⚠ 与 [[{other_title}]] 疑似重复（score {c['score']}）— "
                f"建议读两篇后用 memory_edit 合并。对方内容: {other_text[:50]}"
            )
        return warnings
