"""Markdown store: CRUD + write-path guards + synchronous index maintenance.

Design invariants (docs/06-lean-architecture.md):
- File first, index second: a crash can only leave the index stale, never
  corrupt the source of truth.
- Every mutating call updates all indexes synchronously (<300ms typical);
  no daemons, no queues, no cron.
- The store never deletes user content. Guards refuse; force is explicit and
  logged; collisions are annotated, never auto-resolved.
- Embedding is the only model call and may fail: content is still written,
  vectors/D2 catch up on the next write or `reindex`.
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path

from .config import Config
from .detectors import (
    d1_scan,
    d3_scan,
    find_title_conflicts,
    parse_links,
    parse_observations,
)
from .embedding import EmbeddingClient
from .fs_utils import content_hash
from .index_db import IndexDB
from .vector import VectorStore

logger = logging.getLogger(__name__)

_ILLEGAL_FILENAME = re.compile(r'[\\/:*?"<>|]')
_HEADING_RE = re.compile(r"^(#{2,6})\s+(.+?)\s*$")


class StoreError(Exception):
    """Tool-facing error; the message is meant to be shown to the agent."""


class TitleConflict(StoreError):
    """Write refused because an existing note has a near-duplicate title."""

    def __init__(self, message: str, matches: list[dict]):
        super().__init__(message)
        self.matches = matches


class AnchorError(StoreError):
    """Edit refused: old_string not found or not unique."""


class Store:
    def __init__(self, config: Config, db: IndexDB,
                 emb: EmbeddingClient | None = None,
                 vectors: VectorStore | None = None):
        self.config = config
        self.db = db
        self.emb = emb
        self.vectors = vectors
        self.root = config.root_abs
        self.root.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------ paths

    def _split_title(self, title: str) -> tuple[str, str]:
        """'projects/foo' -> ('projects', 'foo'); reject traversal."""
        t = (title or "").strip().replace("\\", "/").strip("/")
        if not t:
            raise StoreError("标题不能为空。")
        if ".." in t.split("/"):
            raise StoreError(f"标题不允许包含路径穿越: {title}")
        if "/" in t:
            dirpart, name = t.rsplit("/", 1)
            return dirpart, name
        return "", t

    def title_to_path(self, title: str) -> str:
        dirpart, name = self._split_title(title)
        name = _ILLEGAL_FILENAME.sub("_", name).strip(". ")
        if not name:
            raise StoreError(f"标题无法转为合法文件名: {title}")
        rel = f"{dirpart}/{name}.md" if dirpart else f"{name}.md"
        return rel

    def resolve(self, path_or_title: str) -> str:
        """Resolve to an existing note: path first, then exact title."""
        p = (path_or_title or "").strip()
        if not p:
            raise StoreError("空的路径/标题。")
        rel = p.replace("\\", "/").lstrip("/")
        if (self.root / rel).is_file():
            return rel
        row = self.db.get_note_by_title(p)
        if row:
            return row["path"]
        # tolerate path with/without .md
        if not rel.endswith(".md") and (self.root / (rel + ".md")).is_file():
            return rel + ".md"
        raise StoreError(f"未找到笔记: {path_or_title}（可先用 memory_list 浏览）")

    # ------------------------------------------------------------------ guard

    def check_title_conflicts(self, title: str,
                              exclude_path: str | None = None) -> list[dict]:
        return find_title_conflicts(
            title, self.db.all_titles(),
            self.config.guard.title_similarity_threshold,
            exclude_path=exclude_path,
        )

    # ------------------------------------------------------------------ write

    def write(self, title: str, content: str, force: bool = False,
              force_confirm: bool = False) -> dict:
        rel = self.title_to_path(title)
        _, name = self._split_title(title)
        is_journal = rel.startswith(self.config.journal_prefix)

        # The stored title is the topic name without any directory prefix —
        # directories are filing, not part of the note's identity.
        conflicts: list[dict] = []
        if not is_journal:
            conflicts = self.check_title_conflicts(name, exclude_path=rel)
            if conflicts and not force:
                self.db.add_guard_event("refused", name,
                                        conflicts[0]["path"], forced=False)
                top = "\n".join(
                    f"  - [[{c['title']}]] ({c['path']}, 相似度 {c['score']})"
                    for c in conflicts[:5]
                )
                raise TitleConflict(
                    f"已存在近似标题笔记，拒绝新建：\n{top}\n"
                    f"更新内容请用 memory_edit / memory_edit_section；"
                    f"确属新主题请 memory_write(force=true)。",
                    conflicts,
                )
        if conflicts and force:
            # Two-step confirmation ladder: frequent force usage requires an
            # explicit force_confirm=true on top of force=true (human-confirm
            # semantics, deterministic and fully counted in guard_events).
            recent = self.db.count_forced_since(hours=24)
            threshold = self.config.guard.force_confirm_threshold
            if recent >= threshold and not force_confirm:
                raise StoreError(
                    f"force 近 24 小时已被使用 {recent} 次（阈值 {threshold}），"
                    "需要人工确认。\n"
                    f"候选已有笔记：[[{conflicts[0]['title']}]] "
                    f"({conflicts[0]['path']}, 相似度 {conflicts[0]['score']})。\n"
                    "若已确认这确实是不同主题，请同时传 force=true 和 "
                    "force_confirm=true 重试。"
                )
            self.db.add_guard_event("forced", name, conflicts[0]["path"], forced=True)

        abs_path = self.root / rel
        abs_path.parent.mkdir(parents=True, exist_ok=True)
        abs_path.write_text(content, encoding="utf-8")

        self._index_note(rel, name, content)
        return {"path": rel, "forced": bool(conflicts and force)}

    # ------------------------------------------------------------------ read

    def read(self, path_or_title: str) -> dict:
        rel = self.resolve(path_or_title)
        content = (self.root / rel).read_text(encoding="utf-8")
        row = self.db.get_note(rel)
        title = row["title"] if row else self._title_from_content(rel, content)

        related = []
        seen_paths = {rel}
        for link in parse_links(content):
            target = self.db.get_note_by_title(link)
            if target and target["path"] not in seen_paths:
                first_obs = self._first_observation(target["path"])
                related.append({"title": link, "path": target["path"],
                                "via": "link", "note": first_obs or ""})
                seen_paths.add(target["path"])
            elif not target:
                related.append({"title": link, "via": "link", "missing": True})

        if self.emb and self.vectors:
            try:
                qv = self._embed_cached(f"{title}\n{content}")
                for hit in self.vectors.search_note_vectors(qv, 3):
                    if hit["id"] not in seen_paths:
                        related.append({"title": self._title_of(hit["id"]),
                                        "path": hit["id"], "via": "vector",
                                        "score": round(1 - hit["_distance"] / 2, 3)})
                        seen_paths.add(hit["id"])
            except Exception as e:
                logger.warning("Related-vector lookup failed: %s", e)

        return {"path": rel, "title": title, "content": content, "related": related}

    # ------------------------------------------------------------------ edit

    def edit(self, path: str, old_string: str, new_string: str) -> dict:
        rel = self.resolve(path)
        abs_path = self.root / rel
        content = abs_path.read_text(encoding="utf-8")

        positions = [i + 1 for i, line in enumerate(content.splitlines())
                     if old_string in line]
        n = content.count(old_string)
        if n == 0:
            raise AnchorError(
                f"old_string 在 {rel} 中未找到。请先用 memory_read 确认内容。")
        if n > 1:
            raise AnchorError(
                f"old_string 在 {rel} 中命中 {n} 处（约行 {positions[:5]}），需要唯一。"
                "请扩展上下文使锚点唯一。")

        new_content = content.replace(old_string, new_string, 1)
        abs_path.write_text(new_content, encoding="utf-8")
        title = self._title_of(rel, new_content)
        self._index_note(rel, title, new_content)
        return {"path": rel, "title": title}

    def edit_section(self, path: str, heading: str, new_content: str) -> dict:
        """Replace the body of one `##`-level (or deeper) section, keeping the heading."""
        rel = self.resolve(path)
        abs_path = self.root / rel
        content = abs_path.read_text(encoding="utf-8")
        lines = content.splitlines()

        wanted = (heading or "").strip()
        targets: list[tuple[int, int]] = []
        available: list[str] = []
        for i, line in enumerate(lines):
            m = _HEADING_RE.match(line)
            if not m:
                continue
            text = m.group(2).strip()
            available.append(text)
            if text == wanted:
                targets.append((i, len(m.group(1))))

        if not targets:
            raise StoreError(
                f"未找到小节标题 '{wanted}'（仅匹配 ## 及更深层标题，"
                "# 一级标题是笔记本身，请用 memory_edit）。\n"
                f"现有小节: {', '.join(available) if available else '（无）'}"
            )
        if len(targets) > 1:
            nos = [t[0] + 1 for t in targets]
            raise StoreError(
                f"小节标题 '{wanted}' 命中 {len(targets)} 处（行 {nos}），需要唯一。")

        idx, level = targets[0]
        end = len(lines)
        for j in range(idx + 1, len(lines)):
            m = _HEADING_RE.match(lines[j])
            if m and len(m.group(1)) <= level:
                end = j
                break

        body = (new_content or "").strip("\n")
        new_lines = lines[:idx + 1]
        if body:
            new_lines += ["", *body.splitlines()]
        if end < len(lines):
            new_lines += [""]
        new_lines += lines[end:]
        new_text = "\n".join(new_lines).rstrip("\n") + "\n"

        abs_path.write_text(new_text, encoding="utf-8")
        title = self._title_of(rel, new_text)
        self._index_note(rel, title, new_text)
        return {"path": rel, "heading": wanted}

    # ------------------------------------------------------------------ move

    def move(self, path: str, new_path: str) -> dict:
        old_rel = self.resolve(path)
        new_rel = new_path.strip().replace("\\", "/").strip("/")
        if ".." in new_rel.split("/"):
            raise StoreError(f"目标路径不允许路径穿越: {new_path}")
        if not new_rel.endswith(".md"):
            new_rel += ".md"
        if new_rel == old_rel:
            raise StoreError("目标路径与原路径相同。")
        new_abs = self.root / new_rel
        if new_abs.exists():
            raise StoreError(f"目标已存在: {new_rel}")

        old_abs = self.root / old_rel
        content = old_abs.read_text(encoding="utf-8")
        title = self._title_of(old_rel, content)

        new_abs.parent.mkdir(parents=True, exist_ok=True)
        os.rename(old_abs, new_abs)

        self.db.remove_collisions_involving(old_rel)
        if self.vectors:
            self.vectors.delete_by_path(old_rel)
        self.db.move_note(old_rel, new_rel)
        # re-index under the new path; embeddings come from vec_cache (content
        # unchanged), so this makes no embedding API calls.
        self._index_note(new_rel, title, content)
        return {"old_path": old_rel, "new_path": new_rel, "title": title}

    # ------------------------------------------------------------------ list

    def list_notes(self, sub: str = "", sort: str = "name") -> list[str]:
        base = self.root / sub.strip("/").replace("\\", "/") if sub.strip() else self.root
        if not base.is_dir():
            raise StoreError(f"目录不存在: {sub}")
        entries = []
        for p in base.rglob("*.md"):
            rel = p.relative_to(self.root).as_posix()
            if "/.index/" in f"/{rel}" or rel.startswith(".index"):
                continue
            mtime = p.stat().st_mtime
            entries.append((rel, mtime))
        if sort == "mtime":
            entries.sort(key=lambda e: e[1], reverse=True)
        else:
            entries.sort()
        return [e[0] for e in entries]

    # ------------------------------------------------------------------ audit

    def audit(self) -> dict:
        resynced, missing = self._resync_stale_notes()
        titles = self.db.all_titles()
        d1 = d1_scan(titles, self.config.guard.title_similarity_threshold)
        self.db.prune_stale_collisions()
        collisions = self.db.list_collisions(status="open")

        contents = {}
        for row in titles:
            p = self.root / row["path"]
            if p.is_file():
                contents[row["path"]] = p.read_text(encoding="utf-8")
        dangling = d3_scan(contents, {r["title"] for r in titles})

        return {"title_duplicates": d1,
                "collisions": collisions,
                "dangling_links": dangling,
                "resynced": resynced,
                "missing": missing,
                "guard_stats": self.db.guard_stats()}

    def _resync_stale_notes(self) -> tuple[list[str], list[str]]:
        """Self-healing: reconcile the index with out-of-band file changes.

        - externally edited (disk hash != notes.content_hash): rebuild that
          note's index entry; embeddings come from vec_cache for unchanged
          observation lines; collisions involving it are recomputed.
        - externally deleted: drop its index rows (the user's deletion is the
          source of truth; the store itself never deletes files).
        """
        resynced, missing = [], []
        for row in self.db.list_notes():
            p = self.root / row["path"]
            if not p.is_file():
                self.db.remove_note(row["path"])
                self.db.remove_collisions_involving(row["path"])
                if self.vectors:
                    self.vectors.delete_by_path(row["path"])
                missing.append(row["path"])
                continue
            content = p.read_text(encoding="utf-8")
            if content_hash(content) != row["content_hash"]:
                title = self._title_from_content(row["path"], content)
                self._index_note(row["path"], title, content)
                resynced.append(row["path"])
        return resynced, missing

    # ------------------------------------------------------------------ reindex

    def reindex(self) -> dict:
        self.db.clear_all()
        if self.vectors:
            self.vectors.wipe()
        failed = []
        count = 0
        for p in sorted(self.root.rglob("*.md")):
            rel = p.relative_to(self.root).as_posix()
            if rel.startswith(".index") or "/.index/" in f"/{rel}":
                continue
            try:
                content = p.read_text(encoding="utf-8")
                title = self._title_from_content(rel, content)
                self._index_note(rel, title, content, collect_d2=False)
                count += 1
            except Exception as e:
                failed.append({"path": rel, "error": str(e)})
        return {"indexed": count, "failed": failed}

    # ------------------------------------------------------------------ internals

    def _title_of(self, rel: str, content: str | None = None) -> str:
        row = self.db.get_note(rel)
        if row:
            return row["title"]
        c = content if content is not None else self._safe_read(rel)
        return self._title_from_content(rel, c or "")

    def _safe_read(self, rel: str) -> str | None:
        p = self.root / rel
        if p.is_file():
            return p.read_text(encoding="utf-8")
        return None

    @staticmethod
    def _title_from_content(rel: str, content: str) -> str:
        for line in content.splitlines():
            if line.startswith("# "):
                return line[2:].strip()
        return Path(rel).stem

    def _first_observation(self, rel: str) -> str | None:
        p = self.root / rel
        if not p.is_file():
            return None
        obs = parse_observations(p.read_text(encoding="utf-8"))
        return obs[0]["text"] if obs else None

    def _embed_cached(self, text: str) -> list[float]:
        if not self.emb:
            raise RuntimeError("embedding client not configured")
        chash = content_hash(text)
        cached = self.db.get_cached_vector(chash)
        if cached:
            return cached
        vec = self.emb.embed_one(text)
        self.db.put_cached_vector(chash, vec)
        return vec

    def _index_note(self, rel: str, title: str, content: str,
                    collect_d2: bool = True):
        """Synchronously sync every index for one note. File must be written already."""
        chash = content_hash(content)
        self.db.upsert_note(rel, title, chash)
        self.db.fts_replace(rel, title, content)

        if not (self.emb and self.vectors):
            return
        try:
            # Stale vectors/collisions from a previous version of this note
            # must go before re-adding (obs ids are content-addressed, so
            # changed observation lines would otherwise leave orphans).
            self.db.remove_collisions_involving(rel)
            self.vectors.delete_by_path(rel)

            note_vec = self._embed_cached(f"{title}\n{content}")
            self.vectors.upsert_note_vector(rel, f"{title}", note_vec)

            obs_list = parse_observations(content)
            if not obs_list:
                return
            obs_vecs = []
            for obs in obs_list:
                vec = self._embed_cached(obs["text"])
                obs_vecs.append(vec)
                self.vectors.upsert_obs_vector(rel, obs["text"], vec)
            if collect_d2:
                self._d2_check(rel, obs_list, obs_vecs)
        except Exception as e:
            logger.warning("Vector indexing failed for %s: %s", rel, e)

    def _d2_check(self, rel: str, obs_list: list[dict], obs_vecs: list[list[float]]):
        """Incremental D2: compare new observations against existing ones."""
        import numpy as np

        threshold = self.config.guard.collision_cosine_threshold
        topk = self.config.guard.obs_topk
        for obs, vec in zip(obs_list, obs_vecs, strict=True):
            try:
                hits = self.vectors.search_obs_vectors(vec, topk)
            except Exception as e:
                logger.warning("D2 vector search failed: %s", e)
                return
            a = np.asarray(vec, dtype=np.float32)
            a_norm = float(np.linalg.norm(a)) or 1.0
            for hit in hits:
                other_path = hit.get("source_path", "")
                if other_path == rel:
                    continue
                other_vec = np.asarray(hit.get("vector") or [], dtype=np.float32)
                if other_vec.size != a.size:
                    continue
                denom = a_norm * (float(np.linalg.norm(other_vec)) or 1.0)
                score = float(np.dot(a, other_vec) / denom)
                if score >= threshold:
                    self.db.add_collision(
                        kind="obs", a_path=other_path, b_path=rel,
                        a_text=hit.get("text", ""), b_text=obs["text"],
                        score=round(score, 3),
                    )
