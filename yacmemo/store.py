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

import functools
import logging
import os
import posixpath
import re
import threading
from datetime import UTC, datetime
from pathlib import Path

from rapidfuzz import fuzz

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
from .git_snapshots import GitSnapshots
from .index_db import IndexDB
from .vector import VectorStore

logger = logging.getLogger(__name__)

_ILLEGAL_FILENAME = re.compile(r'[\\/:*?"<>|]')
_HEADING_RE = re.compile(r"^(#{2,6})\s+(.+?)\s*$")
TOPICS_FILE = "TOPICS.md"
# 用户画像与偏好：记忆层功能文件（不注册主题、不游离检测、memory_context 前置）
PROFILE_FILE = "PROFILE.md"
# 免注册区：不参与主题注册与游离检测的目录
FREE_ZONES = ("journal/", "archive/", "curator/")
_TOPIC_FIELD_RE = re.compile(r"^-\s*(卡|相关|现状|注册|状态):\s*(.*)$")

# memory_read 返回值里的附加信息标记：agent 把它们当文件内容抄进 old_string
# 时，拒绝消息要能直接点破（2026-09-16 TeleAgent 连续 4 次 edit 失败的根因）
_READ_DECOR_MARKERS = ("[正文开始", "[正文结束", "相关笔记", "(path: ",
                       "(vector)", "(via: ")


def _d1_id(c: dict) -> str:
    """Stable issue id for a D1 title-duplicate pair (order-independent)."""
    return "D1:" + "|".join(sorted((c["a_title"], c["b_title"])))


def _d3_id(c: dict) -> str:
    return f"D3:{c['path']}|{c['link']}"


def _d4_id(path: str) -> str:
    return f"D4:{path}"


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
    # Methods serialized under the instance lock: the HTTP server runs tools
    # in a threadpool, and mutating ops must not interleave.
    _MUTATING = ("write", "edit", "edit_section", "move", "read", "audit", "reindex")

    def __init__(self, config: Config, db: IndexDB,
                 emb: EmbeddingClient | None = None,
                 vectors: VectorStore | None = None,
                 root: str | Path | None = None,
                 git_user: str = "", git_email: str = ""):
        self.config = config
        self.db = db
        self.emb = emb
        self.vectors = vectors
        # Explicit root for multi-user servers; falls back to the single-user
        # [memory].root. Never derived lazily — the boundary must be fixed at
        # construction time or two users could share one directory.
        self.root = (Path(root).expanduser().resolve() if root
                     else config.root_abs)
        self.root.mkdir(parents=True, exist_ok=True)
        self.snapshots = GitSnapshots(self.root,
                                      enabled=config.memory.git_snapshots,
                                      user_name=git_user, user_email=git_email)
        self._lock = threading.RLock()
        for name in self._MUTATING:
            fn = getattr(self, name)
            setattr(self, name,
                    functools.wraps(fn)(
                        lambda *a, _fn=fn, **kw: self._locked_call(_fn, *a, **kw)))

    def _locked_call(self, fn, *args, **kwargs):
        with self._lock:
            return fn(*args, **kwargs)

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
        self.snapshots.commit(f"write: {rel}")
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
                f"old_string 在 {rel} 中未找到。{self._edit_miss_hint(content, old_string)}")
        if n > 1:
            raise AnchorError(
                f"old_string 在 {rel} 中命中 {n} 处（约行 {positions[:5]}），需要唯一。"
                "请扩展上下文使锚点唯一。")

        new_content = content.replace(old_string, new_string, 1)
        abs_path.write_text(new_content, encoding="utf-8")
        title = self._title_of(rel, new_content)
        self._index_note(rel, title, new_content)
        self.snapshots.commit(f"edit: {rel}")
        return {"path": rel, "title": title}

    def _edit_miss_hint(self, content: str, old: str) -> str:
        """未命中锚点的确定性诊断：拒绝消息必须是可执行的下一步指令
        （04-consistency §一），点破原因并交还可复制的逐字原文。"""
        deco = sorted({m for m in _READ_DECOR_MARKERS if m in old})
        if deco:
            return ("old_string 里混有 memory_read 返回值的附加信息（"
                    + "、".join(deco) + "）——相关笔记/path 等标注不是文件内容，"
                    "锚点请只用 [正文开始]/[正文结束] 块内的文字。")

        def _norm(text: str) -> list[tuple[str, int]]:
            """每行 rstrip + 连续空行折叠为一行；返回 (行文本, 原始行号)。"""
            out, blanks = [], 0
            for idx, ln in enumerate(text.splitlines()):
                s = ln.rstrip()
                if not s:
                    blanks += 1
                    if blanks >= 2:
                        continue
                else:
                    blanks = 0
                out.append((s, idx))
            return out

        orig = content.splitlines()
        hay, needle = _norm(content), _norm(old)
        if not any(s for s, _ in needle):
            return "old_string 为空白，无法定位。"
        span = len(needle)
        hits = [i for i in range(len(hay) - span + 1)
                if [t for t, _ in hay[i:i + span]] == [t for t, _ in needle]]
        if len(hits) == 1:
            region = orig[hay[hits[0]][1]:hay[hits[0] + span - 1][1] + 1]
            for ln in region:  # 首选唯一单行锚点（实测单行逐字复制成功率最高）
                if ln.strip():
                    if content.count(ln) == 1 and len(ln.strip()) >= 12:
                        return ("old_string 与文件内容仅空白不一致（空行数量/行尾空格）。"
                                "可改用下面这行文件原文作锚点：\n" + ln)
                    break
            verbatim = "\n".join(region)
            if len(verbatim) > 600:
                verbatim = verbatim[:600] + "…（截断，请用 memory_read 核对全段）"
            return ("old_string 与文件内容仅空白不一致（空行数量/行尾空格）。"
                    "该位置逐字原文如下，请整段复制：\n" + verbatim)
        if len(hits) > 1:
            return (f"old_string 归一化空白后仍命中 {len(hits)} 处"
                    "——请扩展上下文使锚点唯一。")
        best_score, best_line = 0, ""
        for s, _ in hay:
            if not s:
                continue
            for nl, _ in needle:
                if nl:
                    r = fuzz.ratio(s, nl)
                    if r > best_score:
                        best_score, best_line = r, s
        if best_score >= 75:
            return ("old_string 与文件内容有实质差异。最接近的原文行（相似度 "
                    f"{best_score}%）：\n{best_line}\n请先 memory_read 核对实际内容再重试。")
        return "文件中无相似内容——该段可能尚不存在，请 memory_read 核对后决定改法。"

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
        self.snapshots.commit(f"edit: {rel}")
        return {"path": rel, "heading": wanted}

    def save(self, path: str, content: str) -> dict:
        """Create-or-overwrite by exact path (WebUI editor, curator reports).
        Title follows the first `#` heading; index fully resynced."""
        rel = path.replace("\\", "/").strip("/")
        if not rel or ".." in rel.split("/"):
            raise StoreError(f"非法路径: {path}")
        if not rel.endswith(".md"):
            rel += ".md"
        abs_path = self.root / rel
        old_title = self._title_of(rel) if abs_path.is_file() else None
        abs_path.parent.mkdir(parents=True, exist_ok=True)
        abs_path.write_text(content, encoding="utf-8")
        title = self._title_from_content(rel, content) or old_title
        self._index_note(rel, title, content)
        self.snapshots.commit(f"save: {rel}")
        return {"path": rel, "title": title}

    def delete_note(self, path: str) -> dict:
        """User-instructed deletion (WebUI / memory_delete tool): remove the
        file and all index rows. The store never deletes on its own
        initiative — this is an explicit human/agent action, equivalent to
        deleting the file in Obsidian. Git history keeps it recoverable."""
        rel = self.resolve(path)
        abs_path = self.root / rel
        title = self._title_of(rel)
        if abs_path.exists():
            abs_path.unlink()
        self.db.remove_note(rel)
        self.db.remove_collisions_involving(rel)
        if self.vectors:
            self.vectors.delete_by_path(rel)
        self.snapshots.commit(f"delete: {rel}")
        return {"path": rel, "title": title, "deleted": True}

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
        self.snapshots.commit(f"move: {old_rel} -> {new_rel}")
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

    # ------------------------------------------------------------------ topics

    def topics_file(self) -> Path:
        return self.root / TOPICS_FILE

    def load_topics(self) -> list[dict]:
        """Parse TOPICS.md registry: [{title, card, related[], status, registered}]."""
        p = self.topics_file()
        if not p.is_file():
            return []
        topics, cur = [], None
        for line in p.read_text(encoding="utf-8").splitlines():
            if line.startswith("## "):
                if cur:
                    topics.append(cur)
                cur = {"title": line[3:].strip(), "card": "", "related": [],
                       "status": "", "archived": False, "registered": ""}
            elif cur is not None:
                m = _TOPIC_FIELD_RE.match(line)
                if m:
                    key, val = m.group(1), m.group(2).strip()
                    if key == "卡":
                        cur["card"] = val
                    elif key == "相关":
                        cur["related"] = [x.strip().rstrip("/")
                                          for x in val.split(",") if x.strip()]
                    elif key == "现状":
                        cur["status"] = val
                    elif key == "注册":
                        cur["registered"] = val
                    elif key == "状态":
                        cur["archived"] = "archived" in val
        if cur:
            topics.append(cur)
        return topics

    def topic_register(self, title: str, description: str = "",
                       related: str = "", card_path: str = "") -> dict:
        """Register a new topic: append to TOPICS.md and create the topic card.

        Called only on explicit user instruction (约定：用户明确要求时才注册).
        """
        title = (title or "").strip()
        if not title:
            raise StoreError("主题名不能为空。")
        if title in {t["title"] for t in self.load_topics()}:
            raise StoreError(f"主题已存在: {title}（如需更新请直接编辑主题卡）")

        if card_path:
            card_path = card_path.replace("\\", "/").lstrip("/")
            if not (self.root / card_path).is_file():
                raise StoreError(f"指定的主题 abstract 不存在: {card_path}")
        else:
            folder = f"topics/{_ILLEGAL_FILENAME.sub('_', title).strip('. ')}"
            card_path = f"{folder}/abstract.md"
            abs_card = self.root / card_path
            if abs_card.exists():
                raise StoreError(f"abstract 文件已存在: {card_path}")
            abs_card.parent.mkdir(parents=True, exist_ok=True)
            abs_card.write_text(f"# {title}\n\n{description or '（待补充现状）'}\n",
                                encoding="utf-8")

        from .fs_utils import content_hash as _ch  # noqa: F401 (kept for parity)
        now = datetime.now(UTC).isoformat(timespec="seconds")
        with open(self.topics_file(), "a", encoding="utf-8") as f:
            f.write(f"\n## {title}\n- 卡: {card_path}\n- 相关: {related}\n"
                    f"- 现状: {description}\n- 注册: {now}\n")

        # index the new/updated files so search sees them immediately
        self._index_note(card_path, title,
                         (self.root / card_path).read_text(encoding="utf-8"))
        self._index_note(TOPICS_FILE, "主题记忆注册表",
                         self.topics_file().read_text(encoding="utf-8"))
        self.snapshots.commit(f"topic: register {title} ({card_path})")
        return {"title": title, "card": card_path, "registered": now}

    def topic_unregister(self, title: str) -> dict:
        """Remove a topic from TOPICS.md (user-instructed). Notes are untouched:
        they become stray files (D4) pending an explicit follow-up decision."""
        p = self.topics_file()
        if not p.is_file():
            raise StoreError("尚无主题注册表。")
        lines = p.read_text(encoding="utf-8").splitlines(keepends=True)
        start = None
        for i, ln in enumerate(lines):
            if ln.rstrip("\r\n") == f"## {title}":
                start = i
                break
        if start is None:
            known = "、".join(t["title"] for t in self.load_topics()) or "（空）"
            raise StoreError(f"注册表中没有主题: {title}。现有主题: {known}")
        end = len(lines)
        for j in range(start + 1, len(lines)):
            if lines[j].startswith("## "):
                end = j
                break
        removed_card = ""
        for ln in lines[start:end]:
            if ln.startswith("- 卡: "):
                removed_card = ln[len("- 卡: "):].strip()
        del lines[start:end]
        p.write_text("".join(lines), encoding="utf-8")
        self._index_note(TOPICS_FILE, "主题记忆注册表",
                         p.read_text(encoding="utf-8"))
        self.snapshots.commit(f"topic: unregister {title}")
        return {"title": title, "card": removed_card}

    def archive_topic(self, title: str) -> dict:
        """Archive a topic (user-instructed): the abstract moves under
        archive/<topic>/, the registry entry gets 状态: archived (kept for
        lookup, out of active lists and memory_context). Notes stay
        searchable; archived topics never count as stray (archive/ is a free
        zone). Reversible by hand (git history + registry edit)."""
        topics = self.load_topics()
        active = [t for t in topics if not t.get("archived")]
        t = next((x for x in active if x["title"] == title), None)
        if t is None:
            known = "、".join(x["title"] for x in active) or "（空）"
            raise StoreError(f"没有活跃主题: {title}。现有主题: {known}")

        new_card = t["card"]
        if t["card"] and (self.root / t["card"]).is_file():
            new_card = (f"archive/{_ILLEGAL_FILENAME.sub('_', title).strip('. ')}"
                        "/abstract.md")
            self.move(t["card"], new_card)  # move() snapshots "move: ..."

        p = self.topics_file()
        lines = p.read_text(encoding="utf-8").splitlines(keepends=True)
        start = None
        for i, ln in enumerate(lines):
            if ln.rstrip("\r\n") == f"## {title}":
                start = i
                break
        if start is not None:
            end = len(lines)
            for j in range(start + 1, len(lines)):
                if lines[j].startswith("## "):
                    end = j
                    break
            if not any(ln.startswith("- 状态: ") for ln in lines[start:end]):
                k = start
                while k + 1 < end and lines[k + 1].startswith("- "):
                    k += 1
                lines.insert(k + 1, "- 状态: archived\n")
                p.write_text("".join(lines), encoding="utf-8")
                self._index_note(TOPICS_FILE, "主题记忆注册表",
                                 p.read_text(encoding="utf-8"))
                self.snapshots.commit(f"topic: archive {title}")
        return {"title": title, "card": new_card, "archived": True}

    def memory_context(self, card_lines: int = 12) -> str:
        """Cold-start context: user profile first, then registry + active abstracts."""
        topics = self.load_topics()
        tf = self.topics_file()
        profile = self.root / PROFILE_FILE
        active = [t for t in topics if not t.get("archived")]
        if not topics and not tf.is_file() and not profile.is_file():
            return "（尚无主题记忆——用 topic_register 注册第一个主题）"
        parts = []
        if profile.is_file():
            parts.append(profile.read_text(encoding="utf-8"))
        if tf.is_file():
            parts.append(tf.read_text(encoding="utf-8"))
        cards = []
        for t in active:
            p = self.root / t["card"] if t["card"] else None
            if t["card"] and p and p.is_file():
                head = "\n".join(p.read_text(encoding="utf-8").splitlines()[:card_lines])
                cards.append(f"### {t['title']}（{t['card']}）\n{head}")
        if cards:
            parts.append("\n# 主题摘要（abstract）\n" + "\n\n".join(cards))
        return "\n\n".join(parts)

    # -------------------------------------------------------------- profile

    def _find_profile_section(self, text: str, section: str) -> tuple[int, int] | None:
        """Span (start, end) of one ## section's body, or None if absent."""
        for m in re.finditer(r"^##\s+(.+?)\s*$", text, re.MULTILINE):
            if m.group(1).strip() == section:
                start = m.end()
                mm = re.search(r"^##\s+", text[start:], re.MULTILINE)
                end = start + mm.start() if mm else len(text)
                return start, end
        return None

    def get_preference(self, section: str = "") -> str:
        """Whole PROFILE.md or one ## section of it (memory-layer function)."""
        p = self.root / PROFILE_FILE
        if not p.is_file():
            return "（尚无用户画像/偏好记录——用 update_user_preference 建立）"
        text = p.read_text(encoding="utf-8")
        if not section.strip():
            return text
        wanted = section.strip()
        span = self._find_profile_section(text, wanted)
        if span is None:
            raise StoreError(
                f"PROFILE.md 中没有小节 '{wanted}'，可用 update_user_preference 创建")
        return text[span[0]:span[1]].strip("\n") or "（该小节为空）"

    def update_preference(self, section: str, content: str) -> dict:
        """Create-or-replace one ## section of PROFILE.md. The agent-maintained
        user profile & preferences: not a topic, never registered, first thing
        every session sees via memory_context."""
        wanted = (section or "").strip()
        if not wanted:
            raise StoreError("小节名不能为空。")
        body = (content or "").strip("\n")
        p = self.root / PROFILE_FILE
        if not p.is_file():
            text = f"# 用户画像与偏好\n\n## {wanted}\n\n{body}\n"
            p.write_text(text, encoding="utf-8")
            self._index_note(PROFILE_FILE, "用户画像与偏好", text)
            self.snapshots.commit(f"profile: init '{wanted}'")
            return {"path": PROFILE_FILE, "section": wanted, "created": True}
        text = p.read_text(encoding="utf-8")
        if self._find_profile_section(text, wanted) is not None:
            r = self.edit_section(PROFILE_FILE, wanted, body)  # snapshots "edit:"
            return {"path": PROFILE_FILE, "section": wanted,
                    "heading": r["heading"]}
        new_text = text.rstrip("\n") + f"\n\n## {wanted}\n\n{body}\n"
        p.write_text(new_text, encoding="utf-8")
        title = self._title_of(PROFILE_FILE, new_text)
        self._index_note(PROFILE_FILE, title, new_text)
        self.snapshots.commit(f"profile: add '{wanted}'")
        return {"path": PROFILE_FILE, "section": wanted, "created": True}

    def _stray_files(self, topics: list[dict]) -> list[str]:
        """Markdown files outside any registered topic (and outside free zones)."""
        covered_files, covered_dirs = set(), set()
        for t in topics:
            if t["card"]:
                covered_files.add(t["card"])
                d = posixpath.dirname(t["card"])
                if d:
                    covered_dirs.add(d)
            for r in t["related"]:
                covered_files.add(r)
                d = posixpath.dirname(r)
                if d:
                    covered_dirs.add(d)
        strays = []
        for p in sorted(self.root.rglob("*.md")):
            rel = p.relative_to(self.root).as_posix()
            if rel in (TOPICS_FILE, PROFILE_FILE) or rel.startswith(FREE_ZONES):
                continue
            if "/.index/" in f"/{rel}" or ".git" in p.parts:
                continue
            if rel in covered_files:
                continue
            if any(rel.startswith(d + "/") for d in covered_dirs):
                continue
            strays.append(rel)
        return strays

    # ------------------------------------------------------------------ audit

    def audit(self) -> dict:
        resynced, missing = self._resync_stale_notes()
        added = self._sync_new_files()
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
        topics = self.load_topics()
        stray = self._stray_files(topics)

        # 机器产物不参与 D1（归一化剥日期后标题互相近似，必然假阳性：
        # 快照标题同构、提案-0916 与 提案-0917 都归一为"提案"）
        d1 = [c for c in d1
              if not (c["a_path"].startswith(self._machine_zones)
                      or c["b_path"].startswith(self._machine_zones))]

        # 人类已处置过的问题不再重放（D2 以 collisions.status 天然只列 open）
        disposed = {a["id"] for a in self.db.list_audit_actions()}
        d1 = [c for c in d1 if _d1_id(c) not in disposed]
        dangling = [c for c in dangling if _d3_id(c) not in disposed]
        stray = [p for p in stray if _d4_id(p) not in disposed]

        # Out-of-band changes just healed (externally added/edited/deleted
        # files): snapshot them so the repo stays git-clean.
        healed = len(resynced) + len(missing) + len(added)
        if healed:
            self.snapshots.commit(
                f"external: self-healed {healed} note(s) via audit")

        # 审计快照落盘（journal/audit/ 免注册区，markdown 审计轨迹 + git 快照）
        audit_file = self._write_audit_snapshot(
            resynced, missing, added, d1, collisions, dangling, stray)

        return {"title_duplicates": d1,
                "collisions": collisions,
                "dangling_links": dangling,
                "resynced": resynced,
                "missing": missing,
                "added": added,
                "stray": stray,
                "git": self.snapshots.status_line(),
                "guard_stats": self.db.guard_stats(),
                "audit_file": audit_file}

    # ---- audit snapshots & dispositions ----

    @property
    def _audit_dir(self) -> str:
        return f"{self.config.journal_prefix}audit/"

    @property
    def _machine_zones(self) -> tuple[str, ...]:
        """机器产物区（系统派生输出，不是记忆）：journal/audit/ 快照与 curator/
        提案报告。不参与 obs 索引（见 _index_note），不参与 D1/D2 候选——
        归一化剥日期后快照/报告标题互相近似，处置行则是伪 observation。"""
        return (self._audit_dir, "curator/")

    def _write_audit_snapshot(self, resynced, missing, added, d1, collisions,
                              dangling, stray) -> str:
        ts = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
        git_line = self.snapshots.status_line()
        guard = self.db.guard_stats()

        def _sec(title, items):
            return ["", f"## {title}", ""] + [f"- {i}" for i in items] + [""]

        lines = [f"# 审计快照 {ts}", "",
                 f"> 确定性审计 · {datetime.now(UTC).isoformat(timespec='seconds')} "
                 f"· git 快照: {git_line}", ""]
        lines += ["## 概览", ""]
        lines += [f"- 新增文件（已入索引）：{len(added)}",
                  f"- 外部修改（已重建索引）：{len(resynced)}",
                  f"- 外部删除（已清理索引）：{len(missing)}",
                  f"- 标题重复（D1）：{len(d1)}",
                  f"- 语义撞车（D2）：{len(collisions)}",
                  f"- 悬空链接（D3）：{len(dangling)}",
                  f"- 游离文件（D4）：{len(stray)}",
                  f"- 守卫：拒绝 {guard['refused']} / force {guard['forced']}", ""]
        if added:
            lines += _sec("新增文件", added)
        if resynced:
            lines += _sec("外部修改", resynced)
        if missing:
            lines += _sec("外部删除", missing)
        if d1:
            lines += _sec("标题重复（D1）", [
                f"`{_d1_id(c)}` — `{c['a_title']}` ↔ `{c['b_title']}`"
                f"（score {c['score']}）" for c in d1])
        if collisions:
            lines += _sec("语义撞车（D2）", [
                f"`D2:{c['id']}` — `{c['a_path']}` ↔ `{c['b_path']}`"
                f"（score {c['score']}）" for c in collisions])
        if dangling:
            lines += _sec("悬空链接（D3）", [
                f"`{_d3_id(c)}` — `{c['path']}`: [[{c['link']}]]" for c in dangling])
        if stray:
            lines += _sec("游离文件（D4）", [f"`{_d4_id(p)}` — `{p}`" for p in stray])
        lines += ["## 处置记录", "", "（暂无记录）", ""]
        rel = f"{self._audit_dir}{ts}.md"
        self.save(rel, "\n".join(lines))
        return rel

    def record_audit_action(self, audit_file: str, issue_id: str, action: str,
                            label: str, note: str = "") -> dict:
        """记录人类对某审计问题的处置（写入快照文件的处置记录 + 持久化）。

        - D2 撞车：同步 index 状态（open → resolved / dismissed），重跑不重放；
        - D1/D3/D4：写 audit_actions，重跑审计时不再列为待处理；
        - 处置行追加进当次快照文件（markdown 轨迹，git 自动快照）。
        """
        kind = issue_id.split(":", 1)[0]
        if kind == "D2":
            cid = issue_id.split(":", 1)[1]
            rows = self.db.list_collisions(status=None)
            row = next((c for c in rows if c["id"] == cid), None)
            if row is not None:
                self.db.resolve_collision(
                    cid, "resolved" if action == "resolved" else "dismissed")
        self.db.record_audit_action(issue_id, kind, "", "", action, note)

        rel = audit_file.replace("\\", "/").strip("/")
        content = ""
        abs_path = self.root / rel
        if abs_path.is_file():
            content = abs_path.read_text(encoding="utf-8")
        ts = datetime.now(UTC).strftime("%Y-%m-%d %H:%M")
        verb = "已处理" if action == "resolved" else "已忽略"
        line = f"- [{ts}] {verb} {label}（`{issue_id}`）"
        if note:
            line += f" 备注：{note}"
        marker = "## 处置记录"
        if marker in content:
            head, _, tail = content.partition(marker)
            if "（暂无记录）" in tail:
                tail = tail.replace("（暂无记录）", "", 1)
            content = f"{head}{marker}{tail.rstrip()}\n{line}\n"
        else:
            content = content.rstrip() + f"\n\n{marker}\n{line}\n"
        self.save(rel, content)
        return rel

    def _sync_new_files(self) -> list[str]:
        """Index .md files that exist on disk but were never ingested
        (created out-of-band before the server saw them)."""
        added = []
        for p in sorted(self.root.rglob("*.md")):
            rel = p.relative_to(self.root).as_posix()
            if rel.startswith(".index") or "/.index/" in f"/{rel}":
                continue
            if self.db.get_note(rel) is not None:
                continue
            try:
                content = p.read_text(encoding="utf-8")
                title = self._title_from_content(rel, content)
                self._index_note(rel, title, content)
                added.append(rel)
            except Exception as e:
                logger.warning("audit: indexing new file %s failed: %s", rel, e)
        return added

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
                self._index_note(rel, title, content)
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

    def _index_note(self, rel: str, title: str, content: str):
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

            if rel.startswith(self._machine_zones):
                # 机器产物不是记忆：处置行 "- [时间] 已处理 ..." 会被解析为
                # 伪 observation 且跨快照高度相似，入 obs 空间必然产生 D2 假阳性
                return
            obs_list = parse_observations(content)
            if not obs_list:
                return
            obs_vecs = []
            for obs in obs_list:
                vec = self._embed_cached(obs["text"])
                obs_vecs.append(vec)
                self.vectors.upsert_obs_vector(rel, obs["text"], vec)
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
