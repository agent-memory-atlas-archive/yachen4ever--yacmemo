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

import contextlib
import functools
import logging
import os
import posixpath
import re
import threading
import time
from datetime import UTC, date, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from rapidfuzz import fuzz

from .config import Config
from .detectors import (
    canonical_link_target,
    d1_scan,
    d3_scan,
    find_title_conflicts,
    parse_links,
    parse_observations,
)
from .embedding import EmbeddingClient
from .fs_utils import content_hash
from .git_snapshots import GitSnapshots
from .identity import AGENTS_PREFIX, Identity, in_agents_zone, visible, writable
from .index_db import IndexDB
from .vector import VectorStore

if TYPE_CHECKING:  # Iterator 只用于类型标注，运行期不 import collections.abc
    from collections.abc import Iterator

logger = logging.getLogger(__name__)

_ILLEGAL_FILENAME = re.compile(r'[\\/:*?"<>|]')
_HEADING_RE = re.compile(r"^(#{2,6})\s+(.+?)\s*$")
TOPICS_FILE = "TOPICS.md"
# 已结案提案的规范标记（引用行，search 据此把已结案提案从检索结果隐去；
# 显式 memory_read 仍可读——那是明确查阅）。打标时同步把「**状态：待裁决**」
# 行改为「**状态：已结案**」，人读机读一致。
PROPOSAL_SETTLED_MARKER = "> 状态：已结案"
# 用户画像与偏好：记忆层功能文件（不注册主题、不游离检测、memory_context 前置）
PROFILE_FILE = "PROFILE.md"
# 免注册区：不参与主题注册与游离检测的目录（agents/ 另有 identity 写守卫，
# 见 _require_agents_write——免注册 ≠ 任意可写）
FREE_ZONES = ("journal/", "archive/", "curator/", AGENTS_PREFIX)
_TOPIC_FIELD_RE = re.compile(r"^-\s*(卡|相关|现状|注册|状态|标签):\s*(.*)$")
# 盘符绝对路径（"C:/x/y.md"）。pathlib 判定它是绝对路径，`root / rel`
# 会直接把 root 丢弃——Windows 上的越界向量，靠末尾的包含性断言兜住，
# 这里先拦一次只为给出能读懂的报错。
_DRIVE_RE = re.compile(r"^[A-Za-z]:")

# memory_read 返回值里的附加信息标记：agent 把它们当文件内容抄进 old_string
# 时，拒绝消息要能直接点破（2026-09-16 TeleAgent 连续 4 次 edit 失败的根因）
_READ_DECOR_MARKERS = ("[正文开始", "[正文结束", "相关笔记", "(path: ",
                       "(vector)", "(via: ")


def _log_safe(value: object) -> str:
    """日志用值：把换行/回车折成字面量（可诊断性不丢，仍能看出原值）。

    与 vector._log_safe 同款，但**故意各留一份**：store 与 vector 之间没有
    依赖方向（vector 不 import store），从任一侧 import 都会造出新的耦合
    或循环导入风险，两行重复比一个 import 环便宜。

    笔记路径由用户/agent 决定，`notes/a\nERROR forged line.md` 这样一个
    路径就能在日志里伪造出第二行——伪造的 ERROR、伪造的处置痕迹都从这
    里来。异常消息同样可能回显路径（OSError 会带上完整路径），一起过。
    """
    return str(value).replace("\\", "\\\\").replace("\r", "\\r").replace("\n", "\\n")


def _d1_id(c: dict) -> str:
    """Stable issue id for a D1 title-duplicate pair (order-independent)."""
    return "D1:" + "|".join(sorted((c["a_title"], c["b_title"])))


def _d3_id(c: dict) -> str:
    return f"D3:{c['path']}|{c['link']}"


def _d4_id(path: str) -> str:
    return f"D4:{path}"


def _d5_id(title: str, card: str) -> str:
    return f"D5:{title}|{card}"


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
        # 最近一次审计结果（MCP memory_audit 与 WebUI 审计页共享，见 audit()）
        self.last_audit: dict | None = None
        # 最近一次 _index_note 自动清除的过期冲突对数（open 状态、重算后不再
        # 命中）——合并型编辑的可见信号，由 edit/edit_section 读取上报
        self._last_cleared_collisions = 0
        for name in self._MUTATING:
            fn = getattr(self, name)
            setattr(self, name,
                    functools.wraps(fn)(
                        lambda *a, _fn=fn, **kw: self._locked_call(_fn, *a, **kw)))

    def _locked_call(self, fn, *args, **kwargs):
        with self._lock:
            return fn(*args, **kwargs)

    # ------------------------------------------------------------------ paths

    def _norm_rel(self, path: str, *, what: str = "路径",
                  allow_empty: bool = False,
                  allow_leading_slash: bool = True) -> str:
        """唯一收口：把外部传入的相对路径规范化，并保证它留在 root 之内。

        所有外部路径入口（resolve / list / save / move / 标题 / 主题卡 /
        审计文件 / 提案文件）都必须走这里。规范化原先散在六处、各自一套
        标准——resolve 只 lstrip 前导斜杠不拒 ".."，而 save 拒 ".."，
        同一个库两套标准，越界只需挑那条松的。

        规则：反斜杠转正斜杠、拒盘符、折叠空段与 "."（否则 "topics//x.md"
        与 "topics/./x.md" 会把同一个物理文件索引成两个路径，产出 agent
        无法处置的假重复）、拒 ".." 段，最后以 resolve 后的包含性断言收尾
        （UNC 等漏网形态在这里被拦下）。

        越界一律以 StoreError 抛出：调用方普遍只 `except StoreError`，
        让 ValueError/OSError 逃出去等于把守卫绕成崩溃。
        allow_empty=True 时，只有斜杠/点的输入（"/"、"//"、"."）返回 ""
        ——那是"整个根目录"的合法写法（list_notes 用），不是越界。

        前导斜杠：默认**容忍**（"/topics/x.md" → "topics/x.md"），这是
        resolve/memory_read 的既有容忍度，测试钉住（test_path_guard.py）
        「收过头同样是回归」。容忍之所以安全，前提是调用方一律使用**返回值**
        而不是原串：返回值恒为相对路径，`self.root / 返回值` 必然落在 root
        内。2026-10-03 审查的 S1 就是死在这里——调用方把返回值丢掉、又拿
        原串去 join，pathlib 的 `/` 遇到绝对右操作数会直接丢弃左侧 root，
        守卫于是 fail-open。所以硬性要求是：
            rel = self._norm_rel(...)   ← 之后的每一次 self.root / rel
        派生数据入口（notes 表索引行、issue_id 里的提案文件）传
        allow_leading_slash=False：那里出现的绝对路径不是"少写了个斜杠"
        的笔误，而是索引损坏的证据，容忍它等于把损坏规范化成一个假的库内
        路径（fail-closed，与 ".." 行同等处置：隔离、不读盘、不删行）。
        """
        raw = (path or "").strip()
        rel = raw.replace("\\", "/")
        if _DRIVE_RE.match(rel):
            raise StoreError(f"{what}不允许盘符或绝对路径: {path}")
        if rel.startswith("/") and not allow_leading_slash:
            raise StoreError(f"{what}不允许绝对路径: {path}")
        parts = []
        for seg in rel.split("/"):
            if seg in ("", "."):
                continue
            if seg == "..":
                raise StoreError(f"{what}不允许路径穿越: {path}")
            parts.append(seg)
        rel = "/".join(parts)
        if not rel:
            if allow_empty:
                return ""
            raise StoreError(f"空的{what}。")
        # 终局断言：任何漏进上面的形态（UNC、平台怪癖）都在这里被挡住。
        # 两侧都取 resolve 后的 root 比对——root 自身是符号链接时（macOS 的
        # /var、/tmp）未解析的 self.root 会让每个合法路径都误判为越界。
        root = self.root.resolve()
        try:
            inside = (root / rel).resolve().is_relative_to(root)
        except (ValueError, OSError) as e:
            # 内嵌 NUL 等非法路径名：Path.resolve() 抛的是 ValueError，
            # 不是本守卫的 StoreError——调用方的 except StoreError 接不住，
            # 越界检查会被一条崩溃绕过。这里就地翻译成同一类拒绝。
            raise StoreError(
                f"{what}不是合法路径: {path!r}（{e}）") from e
        if not inside:
            raise StoreError(f"{what}越出记忆库根目录: {path}")
        return rel

    def _split_title(self, title: str) -> tuple[str, str]:
        """'projects/foo' -> ('projects', 'foo'); 规范化与越界判定见 _norm_rel。"""
        t = (title or "").strip()
        if not t:
            raise StoreError("标题不能为空。")
        t = self._norm_rel(t, what="标题")
        if "/" in t:
            dirpart, name = t.rsplit("/", 1)
            return dirpart, name
        return "", t

    def title_to_path(self, title: str) -> str:
        dirpart, name = self._split_title(title)
        name = _ILLEGAL_FILENAME.sub("_", name).strip(". ")
        # 尾部 .md 剥掉再统一追加——否则 "x.md" 会落成 "x.md.md"
        #（2026-09-28 实爆：memory_write(title="topics/zcodium/abstract.md")
        #  造出 abstract.md.md，与 topic_register 的真卡并存）
        if name.lower().endswith(".md"):
            name = name[:-3].strip(". ")
        if not name:
            raise StoreError(f"标题无法转为合法文件名: {title}")
        rel = f"{dirpart}/{name}.md" if dirpart else f"{name}.md"
        return rel

    def topic_name_map(self) -> dict[str, str]:
        """注册主题名 → 主题卡路径。主题名是 [[链接]] 的稳定引用——
        卡的索引标题从 H1 提取、会与主题名漂移，按主题名解析必须走注册表。"""
        return {t["title"]: t["card"] for t in self.load_topics() if t.get("card")}

    def resolve(self, path_or_title: str) -> str:
        """Resolve to an existing note: path first, then exact title."""
        p = (path_or_title or "").strip()
        if not p:
            raise StoreError("空的路径/标题。")
        rel = self._norm_rel(p, what="路径/标题")
        if (self.root / rel).is_file():
            return rel
        row = self.db.get_note_by_title(p)
        if row:
            # 索引行也要过 _norm_rel：notes 表是派生数据，库里可能还留着旧
            # 版本（有洞的）构建写进去的越界 path——那种行只靠一个标题就能
            # 把调用者带到 root 之外，而 read/edit/delete_note/move 拿到
            # 返回值后一律 `self.root / rel` 直接读改删。这里不做静默改写
            # 或删行：清理索引是数据决策，本守卫只负责拒绝（fail-closed）。
            stored = row["path"] or ""
            try:
                return self._norm_rel(stored, what="索引行路径",
                                      allow_leading_slash=False)
            except StoreError as e:
                raise StoreError(
                    f"索引行非法: 标题 {p!r} 在索引里指向记忆库根目录之外的路径 "
                    f"{stored!r}（{e}）。\n已拒绝一切操作。请人工核对该索引行"
                    "（疑似旧版本越界写入的残留）后清理或重建索引。") from e
        # tolerate path with/without .md
        if not rel.endswith(".md") and (self.root / (rel + ".md")).is_file():
            return rel + ".md"
        raise StoreError(f"未找到笔记: {path_or_title}（可先用 memory_list 浏览）")

    def _index_row_ok(self, stored: str, quarantine: list[dict]) -> str | None:
        """索引行 path 的唯一收口：返回**校验过的相对路径**，非法则登记隔离
        并返回 None。

        notes 表是派生数据，库里可能还留着旧版本（有洞的）构建写进去的越界
        path——整索引循环（audit 的正文取样、_resync_stale_notes 的自愈）拿到
        行后一律 `self.root / row["path"]` 直接读改删，是 resolve() 之外的
        同一类洞：一条被污染的行能让审计把记忆库根目录之外的文件内容读进
        FTS/向量库。

        2026-10-03 审查 S1：这个函数原来**只判不返**（返回 bool），调用方拿
        的还是 notes 表里的原串。绝对路径 fail-open 就是这么来的——
        `_norm_rel("/abs/x")` 剥掉前导斜杠、判定在 root 内、放行，调用方再
        `self.root / "/abs/x"` 时 pathlib 的 `/` 直接丢弃左侧 root，读的却是
        root 之外那个文件。守卫判的是一个串、用的是另一个串，等于没有守卫。
        所以返回值就是唯一可用的路径，调用方必须 `rel = self._index_row_ok(...)`
        之后再 `self.root / rel`；返回 None 的一律整行跳过。

        绝对路径按索引损坏处置（allow_leading_slash=False），与 ".." 行同等：
        把它"规范化"成 `abs/x.md` 是把损坏洗成一个假的库内路径，指向另一个
        真实文件或一个不存在的路径，两种都比报错更坏。

        隔离而非删除（失效语义优于检测语义：永不静默删除/隐藏）：
        - 不读盘——外部内容不得进索引/FTS/向量；
        - 不删行、不重写行——删掉就毁掉"谁在什么时候写进了越界 path"的
          证据，而清理索引是数据决策，不是守卫该替人做的动作；
        - 不写 audit_actions——那张表的语义是"人/agent 已处置某个
          D1/D3/D4"，且用于把已处置问题从后续报告里滤掉；损坏索引行是另一
          类东西，记进去会污染处置状态，还可能压掉一条真正待办的报告。

        非规范但仍在库内的行（"notes/./a.md"）不算隔离：返回规范化后的
        "notes/a.md"，调用方**文件系统访问**一律用这个返回值。索引键仍用
        行里的原 path——notes 表的键是那行自己的身份，改键等于给同一个物理
        文件凭空多插一条索引行（正是 _norm_rel 折叠 "." 想消灭的假重复），
        而隔离场景下根本走不到这里（原串越界的行已被丢弃，压根不落库）。
        """
        try:
            return self._norm_rel(stored or "", what="索引行路径",
                                  allow_leading_slash=False)
        except StoreError as e:
            quarantine.append({
                "path": stored,
                "reason": (f"notes 表该行 path 未通过路径守卫（{e}）；"
                           "已拒绝一切文件访问（未读盘、未删行）。"
                           "疑似旧版本越界写入的残留，处置手段：reindex() 重建索引"),
            })
            return None

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
              force_confirm: bool = False,
              identity: Identity | None = None) -> dict:
        rel = self.title_to_path(title)
        self._require_agents_write(rel, identity)
        self._require_covered(rel)
        _, name = self._split_title(title)
        is_journal = rel.startswith(self.config.journal_prefix)
        # agents/ 专属区的标题是文件名语义（必读/环境……），跨 identity 同构，
        # 与 journal 一样跳过全局唯一标题守卫（D1 审计侧同步排除）
        is_agent_zone = in_agents_zone(rel)

        # memory_write 全区只创建不覆盖：同名拦截必须放在近重名守卫之前——
        # exclude_path 会把 exact 匹配排除出候选，走到近重名守卫时拦截消息
        # 指向的是 -2 之类的近似笔记而非真正同名的那篇；force 不豁免，
        # 整篇重建的唯一通道是 memory_delete（仅用户明确要求时）
        abs_path = self.root / rel
        if abs_path.is_file():
            if is_agent_zone:
                raise StoreError(
                    f"写入被拦截: {rel} 已存在（agents/ 区 memory_write 只创建不覆盖）。\n"
                    "更新内容用 memory_edit / memory_edit_section 就地修改；"
                    "确要整篇重建请先 memory_delete 该路径（仅用户明确要求时）。")
            raise StoreError(
                f"写入被拦截: {rel} 已存在，memory_write 只创建不覆盖。\n"
                "更新内容用 memory_edit / memory_edit_section 就地修改；"
                "确要整篇重建请先 memory_delete 该路径（仅用户明确要求时）。")

        # The stored title is the topic name without any directory prefix —
        # directories are filing, not part of the note's identity.
        conflicts: list[dict] = []
        if not (is_journal or is_agent_zone):
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
        # 写时撞车即时回显：D2 在 _index_note 里增量检出，若不在此处
        # 带回，agent 要等到下次审计才知道自己制造了语义撞车
        new_cols = [c for c in self.db.collisions_for(rel, "open")
                    if c["b_path"] == rel]
        self.snapshots.commit(f"write: {rel}")
        return {"path": rel, "forced": bool(conflicts and force),
                "new_collisions": [{"with_path": c["a_path"],
                                    "score": c["score"],
                                    "text": c["b_text"]} for c in new_cols]}

    # ------------------------------------------------------------------ read

    def read(self, path_or_title: str,
             identity: Identity | None = None) -> dict:
        rel = self.resolve(path_or_title)
        self._require_agents_visible(rel, identity)
        content = (self.root / rel).read_text(encoding="utf-8")
        row = self.db.get_note(rel)
        title = row["title"] if row else self._title_from_content(rel, content)

        related = []
        seen_paths = {rel}
        topic_names = self.topic_name_map()
        for link in parse_links(content):
            target = self.db.get_note_by_title(link)
            if not target:
                # 主题名解析：卡标题会随 H1 漂移，注册表里的主题名才是
                # 稳定标识（[[工作规则与开发偏好]] → 其主题卡）
                card = topic_names.get(link.strip())
                target = self.db.get_note(card) if card else None
            if not target:
                # 路径形式兜底（与 d3_scan 同规则）：agent 从 memory_list
                # 拿到的是路径，[[topics/x/abstract.md]] 这类引用按路径解析
                tpath = canonical_link_target(link)
                target = (self.db.get_note(tpath)
                          or self.db.get_note(tpath + ".md"))
            if target and target["path"] not in seen_paths:
                # 链接目标同样来自 notes 表（派生数据）：一个被污染的行会让
                # memory_read 把 root 之外文件的第一条观察原样吐给 agent
                # （2026-10-03 审查 S2 实测泄漏）。这里过同一个收口，且只
                # 报告、不外泄——path 与 observation 都不进返回值。
                # 不并进 audit 的 quarantined_index_rows：read 是纯读路径，
                # 权威隔离清单由 audit() 产出（read 不该改审计状态），就地
                # 标 quarantined 让调用方看得见"这条链接有坏行"。
                trel = self._index_row_ok(target["path"], [])
                if trel is None:
                    seen_paths.add(target["path"])
                    related.append({
                        "title": link, "via": "link", "quarantined": True,
                        "note": ("该链接在索引里指向记忆库根目录之外的路径，"
                                 "已拒绝读取（不给出路径与内容）；"
                                 "详见 memory_audit 的 quarantined_index_rows")})
                    continue
                first_obs = self._first_observation(trel)
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
                        # 相关笔记同样遵守 identity 边界：其他专属区的笔记不出现
                        if not visible(hit["id"], identity):
                            continue
                        related.append({"title": self._title_of(hit["id"]),
                                        "path": hit["id"], "via": "vector",
                                        "score": round(1 - hit["_distance"] / 2, 3)})
                        seen_paths.add(hit["id"])
            except Exception as e:
                logger.warning("Related-vector lookup failed: %s", e)

        return {"path": rel, "title": title, "content": content, "related": related}

    # ------------------------------------------------------------------ edit

    def edit(self, path: str, old_string: str, new_string: str,
             identity: Identity | None = None) -> dict:
        rel = self.resolve(path)
        self._require_agents_write(rel, identity)
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
        return {"path": rel, "title": title,
                "before_hash": content_hash(content),
                "cleared_collisions": self._last_cleared_collisions}

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

    def edit_section(self, path: str, heading: str, new_content: str,
                     identity: Identity | None = None) -> dict:
        """Replace the body of one `##`-level (or deeper) section, keeping the heading."""
        rel = self.resolve(path)
        self._require_agents_write(rel, identity)
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
        if body:
            # new_content 约定不含标题行（标题行由本工具保留）——自带与目标
            # 同级同名的标题是最常见误用，此处剥除以免笔记长出双标题
            m = _HEADING_RE.match(body.splitlines()[0])
            if m and m.group(2).strip() == wanted and len(m.group(1)) == level:
                body = "\n".join(body.splitlines()[1:]).strip("\n")
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
        return {"path": rel, "heading": wanted,
                "before_hash": content_hash(content),
                "cleared_collisions": self._last_cleared_collisions}

    def save(self, path: str, content: str) -> dict:
        """Create-or-overwrite by exact path (WebUI editor, curator reports).
        Title follows the first `#` heading; index fully resynced."""
        rel = self._norm_rel(path)
        if not rel.endswith(".md"):
            rel += ".md"
        abs_path = self.root / rel
        if not abs_path.is_file():
            # Overwriting an existing file is the editor/report fast path and
            # stays unrestricted; only *creating* a file must respect the
            # registry, otherwise save() becomes a write-gate bypass.
            self._require_covered(rel)
        old_title = self._title_of(rel) if abs_path.is_file() else None
        before_hash = (content_hash(abs_path.read_text(encoding="utf-8"))
                       if abs_path.is_file() else "")
        abs_path.parent.mkdir(parents=True, exist_ok=True)
        abs_path.write_text(content, encoding="utf-8")
        title = self._title_from_content(rel, content) or old_title
        self._index_note(rel, title, content)
        self.snapshots.commit(f"save: {rel}")
        return {"path": rel, "title": title, "before_hash": before_hash}

    def delete_note(self, path: str,
                    identity: Identity | None = None) -> dict:
        """User-instructed deletion (WebUI / memory_delete tool): remove the
        file and all index rows. The store never deletes on its own
        initiative — this is an explicit human/agent action, equivalent to
        deleting the file in Obsidian. Git history keeps it recoverable."""
        rel = self.resolve(path)
        self._require_agents_write(rel, identity)
        abs_path = self.root / rel
        title = self._title_of(rel)
        before_hash = ""
        if abs_path.exists():
            before_hash = content_hash(abs_path.read_text(encoding="utf-8"))
            abs_path.unlink()
        self.db.remove_note(rel)
        self.db.remove_collisions_involving(rel)
        if self.vectors:
            self.vectors.delete_by_path(rel)
        # 删的是最近一次审计的快照 → 联动清缓存，否则审计页加载时
        # 会对着已删除的文件报"未找到笔记"（curator 过期清理同理）
        if (self.last_audit
                and self.last_audit.get("audit", {}).get("audit_file") == rel):
            self.last_audit = None
        self.snapshots.commit(f"delete: {rel}")
        return {"path": rel, "title": title, "deleted": True,
                "before_hash": before_hash}

    # ------------------------------------------------------------------ move

    def move(self, path: str, new_path: str,
             identity: Identity | None = None) -> dict:
        old_rel = self.resolve(path)
        self._require_agents_write(old_rel, identity)
        new_rel = self._norm_rel(new_path, what="目标路径")
        if not new_rel.endswith(".md"):
            new_rel += ".md"
        if new_rel == old_rel:
            raise StoreError("目标路径与原路径相同。")
        new_abs = self.root / new_rel
        if new_abs.exists():
            raise StoreError(f"目标已存在: {new_rel}")
        # A move that lands outside every registered topic is stray creation
        # by another name; archive_topic() lands in archive/ and is exempt.
        self._require_agents_write(new_rel, identity)
        self._require_covered(new_rel)

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

    def list_notes(self, sub: str = "", sort: str = "name",
                   identity: Identity | None = None) -> list[str]:
        # "/"、"//"、"." 规范化后为空 = 整个 root（收口前 sub.strip() 为假值
        # 时走的就是 root 分支，memory_list(path="/") 是合理调用，不能因为
        # 引入 _norm_rel 就报"空的路径"）。
        base = self.root / self._norm_rel(sub, what="子目录", allow_empty=True)
        if not base.is_dir():
            raise StoreError(f"目录不存在: {sub}")
        entries = []
        for p in base.rglob("*.md"):
            raw = p.relative_to(self.root).as_posix()
            if "/.index/" in f"/{raw}" or raw.startswith(".index"):
                continue
            # 符号链接形态的越界文件同样不进结果：read() 侧守卫会拒它读，
            # memory_list 却把它当库内笔记报出来，只会让 agent 撞一堵墙
            try:
                rel = self._norm_rel(raw, what="库内文件路径")
            except StoreError:
                continue
            if not visible(rel, identity):
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

    def _topic_card(self, val: str) -> tuple[str, str, str]:
        """注册表 `卡:` 字段的收口，返回 (card, card_invalid, card_error)。

        `卡:` 不是"库自己写的数据"而是**外部可写内容**：TOPICS.md 落在注册
        区内，匿名 agent 一次普通 memory_edit 就能把 `- 卡:` 改成任意串，
        而消费端（memory_context / archive_topic / 审计 D5 /
        curator.build_material）一律 `self.root / t["card"]` 裸拼。守卫
        （_norm_rel）在这里从来没被调用过——六处外部路径入口收口之后，
        唯一漏在守卫之外的一条（2026-10-03 审查 S3，实测
        `- 卡: ../OUTSIDE/secret.md` 让越界内容进了每次冷启动的
        memory_context 和 curator 的 LLM 提示词）。

        非法时**不抛**：load_topics() 在 memory_context() 里，而
        memory_context() 是每个 agent 冷启动都调的——因为注册表里一行字
        格式坏就把整个记忆层下线，代价远大于那一张卡。改成卡置空 + 留证
        （原值与理由进 card_invalid / card_error，审计另有 invalid_topic_cards
        上报）。

        card 置空后所有消费方都安全退化，这是本改动成立的前提（逐个核过）：
        topic_name_map（`if t.get("card")` 过滤掉）、_topic_of_path、
        memory_context、_path_covered、_near_miss_topics、审计 D5（都判假值）、
        archive_topic（整段移动逻辑被跳过，不抛 ValueError）、
        curator.build_material（`if t["card"] else None` → 读不到文件）。

        allow_leading_slash=False 与索引行同理：`- 卡:` 的写入方是
        topic_register，它落盘前已经过 _norm_rel，绝不会写出前导斜杠——
        所以注册表里出现 "/abs/…/x.md" 不是"少写个斜杠"，是被改坏/被塞进来
        的证据。容忍它等于把越界路径洗成一个**看起来在库内、实际指向别处**
        的假路径（实测 archive_topic 会拿着 "private/var/…/secret.md" 去归档，
        静默产生一条指向不存在文件的卡）。
        """
        raw = (val or "").strip()
        if not raw:
            return "", "", ""
        try:
            return self._norm_rel(raw, what="主题卡路径",
                                  allow_leading_slash=False), "", ""
        except StoreError as e:
            return "", raw, str(e)

    def load_topics(self) -> list[dict]:
        """Parse TOPICS.md registry: [{title, card, related[], status, registered}].

        `卡:` 字段在解析入口就过 _norm_rel（见 _topic_card）：非法值置空并
        记录在 card_invalid / card_error，而不是原样透传给消费端。
        """
        p = self.topics_file()
        if not p.is_file():
            return []
        topics, cur = [], None
        for line in p.read_text(encoding="utf-8").splitlines():
            if line.startswith("## "):
                if cur:
                    topics.append(cur)
                cur = {"title": line[3:].strip(), "card": "", "card_invalid": "",
                       "card_error": "", "related": [],
                       "status": "", "archived": False, "registered": "",
                       "tags": []}
            elif cur is not None:
                m = _TOPIC_FIELD_RE.match(line)
                if m:
                    key, val = m.group(1), m.group(2).strip()
                    if key == "卡":
                        (cur["card"], cur["card_invalid"],
                         cur["card_error"]) = self._topic_card(val)
                    elif key == "相关":
                        cur["related"] = [x.strip().rstrip("/")
                                          for x in val.split(",") if x.strip()]
                    elif key == "标签":
                        cur["tags"] = [x.strip() for x in val.split(",")
                                       if x.strip()]
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
                       related: str = "", card_path: str = "",
                       tags: str = "") -> dict:
        """Register a new topic: append to TOPICS.md and create the topic card.

        Called only on explicit user instruction (约定：用户明确要求时才注册).
        """
        title = (title or "").strip()
        if not title:
            raise StoreError("主题名不能为空。")
        if title in {t["title"] for t in self.load_topics()}:
            raise StoreError(f"主题已存在: {title}（如需更新请直接编辑主题卡）")

        if card_path:
            card_path = self._norm_rel(card_path, what="主题卡路径")
            if not (self.root / card_path).is_file():
                raise StoreError(f"指定的主题 abstract 不存在: {card_path}")
        else:
            # 生成的路径同样过收口：退化标题（如 ".."、"///"）在这里产出的
            # 只能是空串，拼成 "topics//abstract.md" —— 同一个物理文件两个
            # 索引路径，正是 _norm_rel 存在的理由，不能只查调用方给的参数
            folder = f"topics/{_ILLEGAL_FILENAME.sub('_', title).strip('. ')}"
            card_path = self._norm_rel(
                f"{folder}/abstract.md", what="主题卡路径")
            abs_card = self.root / card_path
            if abs_card.exists():
                raise StoreError(f"abstract 文件已存在: {card_path}")
            abs_card.parent.mkdir(parents=True, exist_ok=True)
            abs_card.write_text(f"# {title}\n\n{description or '（待补充现状）'}\n",
                                encoding="utf-8")

        from .fs_utils import content_hash as _ch  # noqa: F401 (kept for parity)
        now = datetime.now(UTC).isoformat(timespec="seconds")
        tlist = [x.strip() for x in tags.replace("，", ",").split(",") if x.strip()]
        tags_line = f"- 标签: {', '.join(tlist)}\n" if tlist else ""
        with open(self.topics_file(), "a", encoding="utf-8") as f:
            f.write(f"\n## {title}\n- 卡: {card_path}\n{tags_line}"
                    f"- 相关: {related}\n- 现状: {description}\n- 注册: {now}\n")

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

    # ---- 单篇笔记归档（archive/<主题名>/<文件>；整主题归档用 archive_topic）----

    @staticmethod
    def _topic_dir_name(title: str) -> str:
        """与 topic_register/archive_topic 同款：主题名 → 合法目录名。"""
        return _ILLEGAL_FILENAME.sub('_', title).strip('. ')

    def _topic_of_path(self, rel: str) -> dict | None:
        """按目录前缀找活跃注册主题（目录即归属）。"""
        for t in self.load_topics():
            if t.get("archived") or not t["card"]:
                continue
            d = posixpath.dirname(t["card"])
            if d and rel.startswith(d + "/"):
                return t
        return None

    def note_archive(self, path: str, reason: str = "",
                     identity: Identity | None = None) -> dict:
        """归档主题内的一篇笔记（不是整个主题）：移入
        archive/<主题名>/<文件名>。abstract 是主题卡，不允许单独归档；
        整主题归档用 archive_topic。可逆：note_unarchive 移回。"""
        rel = self.resolve(path)
        if posixpath.basename(rel) == "abstract.md":
            raise StoreError(
                "abstract 是主题卡，不能单独归档（整主题归档用 archive_topic）。")
        t = self._topic_of_path(rel)
        if t is None:
            raise StoreError(
                f"{rel} 不属于任何活跃注册主题目录，无需/无法按主题归档。")
        dest_dir = f"archive/{self._topic_dir_name(t['title'])}"
        dest = f"{dest_dir}/{posixpath.basename(rel)}"
        if (self.root / dest).exists():
            raise StoreError(f"归档目标已存在: {dest}")
        if reason.strip():
            abs_p = self.root / rel
            lines = abs_p.read_text(encoding="utf-8").splitlines()
            note = (f"> 状态：已归档（{date.today().isoformat()}）——{reason.strip()}")
            pos = 1 if (lines and lines[0].lstrip().startswith("#")) else 0
            lines.insert(pos, "\n" + note)
            abs_p.write_text("\n".join(lines) + "\n", encoding="utf-8")
            self._index_note(rel, self._title_of(rel),
                             abs_p.read_text(encoding="utf-8"))
        self.move(rel, dest, identity=identity)
        return {"archived": rel, "to": dest, "topic": t["title"]}

    def note_unarchive(self, path: str,
                       identity: Identity | None = None) -> dict:
        """取消单篇归档：archive/<主题名>/<文件> 移回 topics/<主题名>/。
        目录名按注册活跃主题反查；对不上（如自由目录 archive/memory-layer-design/）
        会报错——那些文件本就不是主题笔记。"""
        rel = self.resolve(path)
        parts = rel.split("/")
        if parts[0] != "archive" or len(parts) < 3:
            raise StoreError(f"{rel} 不是 archive/<主题>/<文件> 形态的归档笔记。")
        dir_name = parts[1]
        t = next((x for x in self.load_topics()
                  if not x.get("archived")
                  and self._topic_dir_name(x["title"]) == dir_name), None)
        if t is None:
            raise StoreError(
                f"目录 archive/{dir_name}/ 不对应任何活跃主题（可能是自由归档目录"
                "或主题已注销），无法自动取消归档——请用 memory_move 手动处理。")
        dest = f"{posixpath.dirname(t['card'])}/{parts[-1]}"
        if (self.root / dest).exists():
            raise StoreError(f"目标已存在: {dest}")
        self.move(rel, dest, identity=identity)
        return {"unarchived": rel, "to": dest, "topic": t["title"]}

    # ---- 主题标签（注册表 `- 标签:` 行；轻量可逆元数据）----

    @staticmethod
    def _parse_tags(raw: str) -> list[str]:
        return [x.strip() for x in raw.replace("，", ",").split(",") if x.strip()]

    def _set_topic_tags(self, title: str, tags: list[str]) -> None:
        """改写注册表中该主题块的 `- 标签:` 行（空列表 = 整行移除），
        重索引注册表并快照。"""
        p = self.topics_file()
        lines = p.read_text(encoding="utf-8").splitlines(keepends=True)
        start = next((i for i, ln in enumerate(lines)
                      if ln.rstrip("\r\n") == f"## {title}"), None)
        if start is None:
            raise StoreError(f"注册表中没有主题: {title}")
        end = next((i for i in range(start + 1, len(lines))
                    if lines[i].startswith("## ")), len(lines))
        block = [ln for ln in lines[start + 1:end]
                 if not re.match(r"^-\s*标签:", ln)]
        if tags:
            pos = next((k for k, ln in enumerate(block)
                        if ln.startswith("- 卡:")), -1)
            block.insert(pos + 1, f"- 标签: {', '.join(tags)}\n")
        p.write_text("".join(lines[:start + 1] + block + lines[end:]),
                     encoding="utf-8")
        self._index_note(TOPICS_FILE, "主题记忆注册表",
                         p.read_text(encoding="utf-8"))
        self.snapshots.commit(
            f"topic: tags {title} → {', '.join(tags) or '（清空）'}")

    def topic_status(self, title: str, status: str) -> dict:
        """更新注册表该主题的 `- 现状:` 行（一句话定位，非阶段流水）。
        abstract 现状变化时由 agent 同步调用——注册表与卡片两层各自
        一句话，都不复制细节。"""
        status = status.strip()
        if not status:
            raise StoreError("现状描述不能为空（一句话定位，不是进度流水）。")
        tmap = {t["title"]: t for t in self.load_topics()}
        if title not in tmap:
            known = "、".join(tmap) or "（空）"
            raise StoreError(f"注册表中没有主题: {title}。现有主题: {known}")
        p = self.topics_file()
        lines = p.read_text(encoding="utf-8").splitlines(keepends=True)
        start = next((i for i, ln in enumerate(lines)
                      if ln.rstrip("\r\n") == f"## {title}"), None)
        if start is None:
            raise StoreError(f"注册表中没有主题块: {title}")
        end = next((i for i in range(start + 1, len(lines))
                    if lines[i].startswith("## ")), len(lines))
        block = [ln for ln in lines[start + 1:end]
                 if not re.match(r"^-\s*现状:", ln)]
        pos = next((k for k, ln in enumerate(block)
                    if ln.startswith("- 卡:") or ln.startswith("- 标签:")), -1)
        block.insert(pos + 1, f"- 现状: {status}\n")
        p.write_text("".join(lines[:start + 1] + block + lines[end:]),
                     encoding="utf-8")
        self._index_note(TOPICS_FILE, "主题记忆注册表",
                         p.read_text(encoding="utf-8"))
        self.snapshots.commit(f"topic: status {title}")
        return {"title": title, "status": status}

    def topic_tag(self, title: str, add: str = "", remove: str = "") -> dict:
        """为主题增删标签（幂等，轻量可逆元数据）。返回该主题标签与
        全库标签清单——引导 agent 优先复用已有标签，避免同义词蔓延。"""
        tmap = {t["title"]: t for t in self.load_topics()}
        if title not in tmap:
            known = "、".join(tmap) or "（空）"
            raise StoreError(f"注册表中没有主题: {title}。现有主题: {known}")
        add_l = self._parse_tags(add)
        rm_l = self._parse_tags(remove)
        new = [x for x in (tmap[title].get("tags") or []) if x not in rm_l]
        for a in add_l:
            if a not in new:
                new.append(a)
        self._set_topic_tags(title, new)
        all_tags = sorted({x for t in self.load_topics()
                           for x in t.get("tags") or []})
        return {"title": title, "tags": new, "all_tags": all_tags}

    def tag_rename(self, old: str, new: str) -> dict:
        """重命名标签（全库批量改写；重名等价于合并）。"""
        old, new = old.strip(), new.strip()
        if not old or not new:
            raise StoreError("标签名不能为空")
        affected = [t["title"] for t in self.load_topics()
                    if old in (t.get("tags") or [])]
        if not affected:
            raise StoreError(f"标签不存在: {old}")
        for title in affected:
            tags = next(t["tags"] for t in self.load_topics()
                        if t["title"] == title)
            nt = []
            for x in tags:
                if x == old:
                    if new not in nt:
                        nt.append(new)      # 重名 = 合并
                else:
                    if x not in nt:
                        nt.append(x)
            self._set_topic_tags(title, nt)
        return {"renamed": f"{old} → {new}", "topics": affected}

    def tag_delete(self, tag: str) -> dict:
        """删除标签（从所有主题的标签行移除，主题本身不动）。"""
        tag = tag.strip()
        if not tag:
            raise StoreError("标签名不能为空")
        affected = []
        for t in self.load_topics():
            if tag in (t.get("tags") or []):
                self._set_topic_tags(
                    t["title"], [x for x in t["tags"] if x != tag])
                affected.append(t["title"])
        if not affected:
            raise StoreError(f"标签不存在: {tag}")
        return {"deleted": tag, "topics": affected}

    def archive_topic(self, title: str) -> dict:
        """Archive a topic (user-instructed): the WHOLE topic directory moves
        under archive/<topic>/（目录即归属——只移 abstract 会把主题内其余模块
        笔记留在 topics/ 成为游离文件），registry entry gets 状态: archived
        and its 卡: path rewritten (2026-09-17 实爆：卡路径不改写 → D5 每次
        必点名). Notes stay searchable; archived topics never count as stray
        (archive/ is a free zone). Reversible by hand (git history +
        registry edit)."""
        topics = self.load_topics()
        active = [t for t in topics if not t.get("archived")]
        t = next((x for x in active if x["title"] == title), None)
        if t is None:
            known = "、".join(x["title"] for x in active) or "（空）"
            raise StoreError(f"没有活跃主题: {title}。现有主题: {known}")

        dest_dir = f"archive/{_ILLEGAL_FILENAME.sub('_', title).strip('. ')}"
        new_card = t["card"]
        if t["card"]:
            old_dir = posixpath.dirname(t["card"])
            moved = False
            if old_dir and (self.root / old_dir).is_dir():
                for f in sorted((self.root / old_dir).rglob("*.md")):
                    raw = f.relative_to(self.root).as_posix()
                    # 主题目录内的符号链接越界文件不搬：move 会把它的内容
                    # 复制进 archive/，等于把 root 外的内容搬进记忆库
                    try:
                        rel = self._norm_rel(raw, what="库内文件路径")
                    except StoreError:
                        continue
                    sub = f.relative_to(self.root / old_dir).as_posix()
                    self.move(rel, f"{dest_dir}/{sub}")  # move() 逐个快照
                    moved = True
                # 清掉因移动而空掉的主题目录（目录即归属，不留空壳）
                for d in sorted((self.root / old_dir).rglob("*"), reverse=True):
                    if d.is_dir():
                        with contextlib.suppress(OSError):
                            d.rmdir()
                with contextlib.suppress(OSError):
                    (self.root / old_dir).rmdir()
            elif (self.root / t["card"]).is_file():
                # 卡不在主题目录内（注册表手工指定路径）：单移卡文件
                self.move(t["card"], f"{dest_dir}/{posixpath.basename(t['card'])}")
                moved = True
            if moved:
                new_card = f"{dest_dir}/{posixpath.basename(t['card'])}"

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
            changed = False
            if new_card != t["card"]:
                old_line = f"- 卡: {t['card']}"
                for i in range(start + 1, end):
                    if lines[i].rstrip("\r\n") == old_line:
                        lines[i] = f"- 卡: {new_card}\n"
                        changed = True
                        break
            if not any(ln.startswith("- 状态: ") for ln in lines[start:end]):
                k = start
                while k + 1 < end and lines[k + 1].startswith("- "):
                    k += 1
                lines.insert(k + 1, "- 状态: archived\n")
                changed = True
            if changed:
                p.write_text("".join(lines), encoding="utf-8")
                self._index_note(TOPICS_FILE, "主题记忆注册表",
                                 p.read_text(encoding="utf-8"))
                self.snapshots.commit(f"topic: archive {title}")
        return {"title": title, "card": new_card, "archived": True}

    def memory_context(self, card_lines: int = 12,
                       identity: Identity | None = None) -> str:
        """Cold-start context: user profile first, then registry + active
        abstracts; identity 专属必读追加在最后（agent 层 + 本机层）。"""
        topics = self.load_topics()
        tf = self.topics_file()
        profile = self.root / PROFILE_FILE
        active = [t for t in topics if not t.get("archived")]
        identity_parts = (self._identity_context(identity)
                          if identity and identity.agent else [])
        if (not topics and not tf.is_file() and not profile.is_file()
                and not identity_parts):
            return "（尚无主题记忆——用 topic_register 注册第一个主题）"
        parts = []
        if profile.is_file():
            parts.append(profile.read_text(encoding="utf-8"))
        else:
            # 与专属必读的「尚未创建」引导同款：让 agent 冷启动即感知画像缺失
            parts.append(
                "# 用户画像（尚未创建）\n"
                "- 用 update_user_preference 分节沉淀用户身份/偏好"
                "（写入 PROFILE.md，记忆层功能而非主题记忆）")
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
        parts.extend(identity_parts)
        return "\n\n".join(parts)

    def _identity_context(self, identity: Identity) -> list[str]:
        """专属必读注入：agent 层（agents/<agent>/shared/必读.md，同 agent
        跨设备共享）+ identity 层（agents/<agent>/<device>/必读.md，本机专属）。
        必读按约定是指针型短文——全量注入，单文件 80 行兜底。"""
        parts = []
        found = False
        for label, prefix in (
                (f"{identity.agent}（同 agent 跨设备共享）", identity.shared_prefix),
                (f"{identity.agent}@{identity.device}（本机专属）", identity.device_prefix)):
            rel = f"{prefix}必读.md"
            p = self.root / rel
            if not p.is_file():
                continue
            found = True
            head = "\n".join(p.read_text(encoding="utf-8").splitlines()[:80])
            section = f"# 专属必读·{label}｜{rel}\n{head}"
            if "占位模板" in head:
                # WebUI 建 identity 时预创建的占位模板：注入时持续点名，
                # 促使 agent 尽快用 memory_edit 填写（确保"去读且去写"）
                section += ("\n⚠ 本必读仍是占位模板——请用 memory_edit 就地"
                            "填写专属纪律，填写后移除模板标记行。")
            parts.append(section)
        if not found:
            parts.append(
                "# 专属必读（尚未创建）\n"
                f"- agent 层（同 agent 跨设备共享）："
                f"memory_write(title=\"{identity.shared_prefix}必读\", …)\n"
                f"- identity 层（本机专属）："
                f"memory_write(title=\"{identity.device_prefix}必读\", …)\n"
                "- 必读只放指针与纪律，事实一律进 topics/（与 user 层共享）")
        return parts

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

    # ------------------------------------------------- topic coverage (执法)

    def _path_covered(self, rel: str, topics: list[dict]) -> bool:
        """Registry coverage test shared by write-time gating and D4 stray
        detection — one semantics, two consumers. System files and free
        zones are always covered; anything else must be a registered
        topic's card/related file or live under such a file's directory
        (每主题一目录，目录即归属)."""
        if rel in (TOPICS_FILE, PROFILE_FILE) or rel.startswith(FREE_ZONES):
            return True
        # 免注册区（FREE_ZONES）刻意保持大小写敏感：把 Agents/… 漏判成
        # "不在免注册区"只会多走一层注册表覆盖检查（更严），不会把 agents/
        # 专属区放行。agents/ 的归属判定统一走 identity.in_agents_zone
        # （折大小写，见该函数注释）——那是安全边界，不与这里混用。
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
        if rel in covered_files:
            return True
        return any(rel.startswith(d + "/") for d in covered_dirs)

    # ------------------------------------------------- identity (agents/ 执法)

    def _require_agents_write(self, rel: str, identity: Identity | None) -> None:
        """agents/ 专属区的写守卫。identity=None（人类/WebUI/服务端内部）
        不受限；identity 为空 agent（MCP 无 token，ANONYMOUS）与其他 agent /
        其他设备的专属区一律拒绝。user 层路径不经过本守卫。"""
        if not in_agents_zone(rel):
            return
        if identity is None:  # 人类入口（WebUI/内部）：全库管理员
            return
        if not identity.agent:
            raise StoreError(
                f"写入被拦截: {rel} 属于 identity 专属区（agents/），当前连接"
                "未携带 identity token。\n请在 MCP 配置里加请求头 "
                "Authorization: Bearer <device>_<agent>（stdio 用环境变量 "
                "YACMEMO_TOKEN）；user 层（topics/、journal/）不受影响。")
        if not writable(rel, identity):
            raise StoreError(
                f"写入被拦截: {rel} 不在你的 identity 专属范围内。\n"
                f"你的身份: {identity.token}——可写 {identity.shared_prefix}"
                "（同 agent 跨设备共享子树）与 "
                f"{identity.device_prefix}（本机专属）子树；"
                "agents/<agent>/ 第一层平铺文件只读兼容（历史遗留），"
                "写入请进上述两类子树；其他 agent / 其他设备的专属区互相不可见。")

    def _require_agents_visible(self, rel: str, identity: Identity | None) -> None:
        """agents/ 专属区的读守卫：其他 identity 的专属区不可见。
        identity=None（人类）恒可见；ANONYMOUS 对 agents/ 全部不可见。"""
        if visible(rel, identity):
            return
        if identity is None or not identity.agent:  # None 不会走到这（visible 恒 True）
            raise StoreError(
                f"读取被拦截: {rel} 属于 identity 专属区（agents/），当前连接"
                "未携带 identity token。配置方式见拦截消息与 docs/05。")
        raise StoreError(
            f"读取被拦截: {rel} 属于其他 identity 的专属区，互相不可见。")

    def _require_covered(self, rel: str, attempted_title: str = "") -> None:
        """Write-time enforcement of the topic registry: tool-created notes
        must belong to a registered topic. Non-bypassable — force only
        covers title conflicts. D4 audit stays as the backstop for files
        that enter the store without the tools (Obsidian hand-edits,
        unregister leftovers)."""
        if rel in (TOPICS_FILE, PROFILE_FILE):
            raise StoreError(
                f"系统文件不允许通过写入创建/覆盖: {rel}\n"
                "主题注册请用 topic_register，画像/偏好请用 update_user_preference。")
        if rel.startswith(self.config.journal_prefix):
            return
        topics = self.load_topics()
        if self._path_covered(rel, topics):
            return
        self.db.add_guard_event("uncovered", attempted_title, rel, forced=False)
        raise StoreError(self._uncovered_error(rel, topics))

    def _uncovered_error(self, rel: str, topics: list[dict]) -> str:
        """拦截消息 = 行动指引 + 近失诊断 + 完整度明确的活跃主题列表。

        2026-09-19 TeleAgent 实测的教训：写入漏了 topics/ 前缀被拦后，
        旧消息的活跃主题列表静默截断到 8 个，恰好切掉刚注册的主题，
        agent 得出"注册表未同步"的错误假设，白烧一个推理块才自纠。
        诊断行让错误从死胡同变成一步修复；列表带总数且命中主题必显示。"""
        active = [t for t in topics if not t.get("archived")]
        near, matched = self._near_miss_topics(rel, active)
        lines = [
            f"写入被拦截: {rel} 不属于任何注册主题（主题注册制硬约束，force 不豁免）。",
            *near,
            "- 新主题：先征得用户同意后 topic_register 注册"
            "（会在 topics/<主题>/abstract.md 建卡），\n"
            "  之后把笔记写入 topics/<主题>/ 目录下；\n"
            "- 已有主题：写入该主题目录下的模块笔记，如 topics/<主题>/笔记名.md；\n"
            "  abstract 是摘要卡（保持一句话现状），详细内容请写成模块笔记；\n"
            "- journal/、archive/、curator/、agents/ 免注册区不受限"
            "（agents/ 另有 identity 专属守卫）。",
        ]
        titles = [t["title"] for t in active]
        if not titles:
            lines.append("当前活跃主题: （暂无）")
        else:
            shown = list(dict.fromkeys(titles[:8] + matched))
            listed = "、".join(f"《{t}》" for t in shown)
            if len(titles) > len(shown):
                lines.append(f"当前活跃主题（共 {len(titles)} 个，"
                             f"显示与本次写入最相关者，其余略）: {listed} …")
            else:
                lines.append(f"当前活跃主题（共 {len(titles)} 个）: {listed}")
        return "\n".join(lines)

    def _near_miss_topics(self, rel: str,
                          active: list[dict]) -> tuple[list[str], list[str]]:
        """未覆盖路径的近失诊断：写入意图最可能是某个已注册主题，只是路径
        缺 topics/ 前缀或目录名拼错。返回 (诊断行, 需在活跃列表点名的标题)。"""
        first = rel.split("/", 1)[0]
        name = posixpath.splitext(posixpath.basename(rel))[0]
        by_title = {t["title"]: t for t in active}
        t = None
        if by_title:
            best = max(by_title, key=lambda k: fuzz.ratio(first, k))
            if fuzz.ratio(first, best) >= 60:
                t = by_title[best]
        lines, matched = [], []
        if t is not None and t["card"]:
            d = posixpath.dirname(t["card"])
            if d:
                if t["title"] == first:
                    lead = f"⚠ 疑似路径前缀/目录名不对：主题「{t['title']}」已注册，目录 {d}/。"
                else:
                    lead = (f"⚠ 疑似路径/目录名不对：你想写的可能是主题"
                            f"「{t['title']}」（名称最接近），其目录 {d}/。")
                lines.append(
                    lead + f"\n"
                    f"  改用 title=\"{d}/{name}\" 即可写入；abstract 是摘要卡，"
                    f"详细内容建议写成 {d}/<笔记名>。")
                matched.append(t["title"])
        return lines, matched

    def _iter_in_root_md(self) -> Iterator[tuple[str, Path]]:
        """遍历 root 下的 *.md，只产出**经守卫判定仍在 root 内**的 (rel, 路径)。

        rglob 本身不看路径语义：root 内一个指向库外的符号链接文件
        （`notes/escape.md → /etc/passwd`）会被原样遍历出来，而消费端一律
        `p.read_text()` / `self._index_note(rel, …)` —— 守卫在 read() 侧
        拒掉的文件，审计却照样读进来进 FTS 与向量库（2026-10-03 审查 S5
        实测）。"库内的相对路径"与"库内的物理文件"在这里才划等号。
        （现代 Python 的 rglob 不跟随符号链接**目录**，暴露面是符号链接
        **文件**——这正是上面那种形态。）

        越界的一律跳过并记日志：内容不得进索引，日志里也不留越界路径的
        原文（_log_safe 防伪造日志行）。
        """
        for p in sorted(self.root.rglob("*.md")):
            rel = p.relative_to(self.root).as_posix()
            if rel.startswith(".index") or "/.index/" in f"/{rel}":
                continue
            if ".git" in p.parts:
                continue
            try:
                yield self._norm_rel(rel, what="库内文件路径"), p
            except StoreError as e:
                logger.warning("skipped out-of-root markdown (guard): %s (%s)",
                               _log_safe(rel), _log_safe(e))

    def _stray_files(self, topics: list[dict]) -> list[str]:
        """Markdown files outside any registered topic (and outside free zones)."""
        strays = []
        for rel, _p in self._iter_in_root_md():
            if self._path_covered(rel, topics):
                continue
            strays.append(rel)
        return strays

    # ------------------------------------------------------------------ audit

    def audit(self) -> dict:
        resynced, missing, resync_quarantined = self._resync_stale_notes()
        added = self._sync_new_files()
        titles = self.db.all_titles()
        # D1 只在**未隔离**的标题里配对：隔离行的 path 指向记忆库之外，让它
        # 参与配对会产出一条 a_path/b_path 越界的假"标题重复"待办——人会去
        # 处置它，处置记录还会写进 audit_actions，越界路径就此进入处置链。
        # 两条循环看的是同一张 notes 表，resync_quarantined 已覆盖全部非法行。
        _qpaths = {e["path"] for e in resync_quarantined}
        if _qpaths:
            titles = [t for t in titles if t["path"] not in _qpaths]
        d1 = d1_scan(titles, self.config.guard.title_similarity_threshold)
        pruned_stale = self.db.prune_stale_collisions()
        collisions = self.db.list_collisions(status="open")

        # 两条循环（_resync_stale_notes 的自愈、这里的正文取样）看的是同一张
        # notes 表，同一条越界行会被登记两次——按 path 去重、按路径序输出，
        # 审计报告才可复现。
        quarantined: list[dict] = list(resync_quarantined)
        contents = {}
        for row in titles:
            # 索引行过路径守卫并**取回校验过的相对路径**：越界行不读盘（外部
            # 文件内容绝不能进 FTS / 向量库），也不删行、也不写 audit_actions。
            # notes 表是派生数据，库里可能还留着旧版本（有洞的）构建写进去的
            # 越界 path——删行会毁掉证据，解析不了的行也不构成"文件被外部删除"
            # 的证据。p 必须用返回值拼：拿原串 join 时 pathlib 会丢弃 root。
            rel = self._index_row_ok(row["path"], quarantined)
            if rel is None:
                continue
            p = self.root / rel
            if p.is_file():
                contents[row["path"]] = p.read_text(encoding="utf-8")
        # 主题名也算合法链接目标（卡的索引标题会随 H1 漂移，
        # [[主题名]] 指向其主题卡——与 read() 的解析链同口径）
        dangling = d3_scan(contents,
                           {r["title"] for r in titles} | set(self.topic_name_map()))

        # 缺向量笔记自愈：embedding 端点故障期间写入的笔记 vector_ok=0，
        # hash 未变，外部变更自愈不会重试——审计补位重试 embedding，
        # 端点仍不可用时保持点名（下次审计再试）
        missing_vectors = []
        if self.emb and self.vectors:
            missing_vectors = self.db.notes_missing_vectors()
            if missing_vectors:
                # 隔离行不进向量库（越界内容永远不该被向量化），也不计入
                # "缺向量"——它每轮重试都不可能自愈，挂在那里只会训练人忽略审计
                qpaths = {e["path"] for e in quarantined}
                for row in missing_vectors:
                    if row["path"] in qpaths:
                        continue
                    content = contents.get(row["path"])
                    if content is not None:
                        self._index_note(row["path"], row["title"], content)
                missing_vectors = [r["path"] for r in self.db.notes_missing_vectors()
                                   if r["path"] not in qpaths]

        # 空白处置行自清（body 解析失败等事故产物，处置表没有删除接口）
        pruned_blank = self.db.prune_blank_audit_actions()

        topics = self.load_topics()
        stray = self._stray_files(topics)
        # 注册表里指向 root 之外的 `卡:`（load_topics 已置空防消费，见
        # _topic_card）。与 quarantined_index_rows 分开报：来源不同（markdown
        # 正文 vs 派生索引），且不可经 memory_audit_update 处置，也不写
        # audit_actions——那是人的 D1/D3/D4 处置状态，混进去会污染它。
        invalid_topic_cards = [{"title": t["title"],
                                "card": t["card_invalid"],
                                "reason": t["card_error"]}
                               for t in topics if t["card_invalid"]]

        # 机器产物不参与 D1（归一化剥日期后标题互相近似，必然假阳性：
        # 快照标题同构、提案-0916 与 提案-0917 都归一为"提案"）。
        # agents/ 专属区同样不参与：标题是文件名语义（必读/环境……），
        # 跨 identity 同构，D1 只会产出噪音
        d1 = [c for c in d1
              if not (c["a_path"].startswith(self._machine_zones)
                      or c["b_path"].startswith(self._machine_zones))
              and not (in_agents_zone(c["a_path"])
                       or in_agents_zone(c["b_path"]))]

        # 机器产物区同样不参与 D3：审计快照会引用上一轮悬空链接的原文，
        # 源笔记删除后快照自己被点名——审计追自己的尾巴（2026-09-18 实测）
        dangling = [d for d in dangling
                    if not d["path"].startswith(self._machine_zones)]

        # 人类已处置过的问题不再重放（D2 以 collisions.status 天然只列 open）
        disposed = {a["id"] for a in self.db.list_audit_actions()}
        d1 = [c for c in d1 if _d1_id(c) not in disposed]
        dangling = [c for c in dangling if _d3_id(c) not in disposed]
        stray = [p for p in stray if _d4_id(p) not in disposed]
        # D5：注册表指向不存在的 abstract（restructure/手工编辑 TOPICS.md 的遗留，
        # 2026-09-16 实例：notecalc-iced 的卡仍指向已移除的 projects/ 目录）
        dangling_cards = [_d5_id(t["title"], t["card"]) for t in topics
                          if t["card"] and not (self.root / t["card"]).is_file()
                          and _d5_id(t["title"], t["card"]) not in disposed]

        # 提案结案调和：文件已标已结案 = 人/agent 确认全部条目收口——
        # 为缺执行事件的条目补记 executed（覆盖事件机制上线前的执行、
        # 旧口径「已采纳」、以及无会话汇报通道的 agent 的手工打标）。
        # 复审失效标记的场景在 curator 追加复审节时即时撤标（见
        # curator.run_check），审计侧不重复撤标
        reconciled = self._reconcile_proposals()

        # 执行进度派生（判断与执行分离：人在 WebUI 判断，agent 经
        # memory_audit_update 汇报执行，审计只做最终验证——已执行且本轮
        # 不再报即复审通过，追加系统事件封口，下轮不再重放）
        exec_last = self.db.exec_last_status()
        current_ids = ({_d1_id(c) for c in d1}
                       | {f"D2:{c['id']}" for c in collisions}
                       | {_d3_id(c) for c in dangling}
                       | {_d4_id(p) for p in stray}
                       | set(dangling_cards))
        verified = []
        for iid, st in exec_last.items():
            # P 类（提案条目）没有确定性复审检查，复审封口只属于 D 类
            #
            # 必须以 executed 收口，否则"没修"和"修好了"在系统里长得一样：
            # 只报 executing 就停手的 issue 会被永久封成"复审通过"，且封口
            # 不可逆（下次 last event 变 verified，无条件跳过）——这一权失效
            # 时还不报错。blocked 同理：仍需人工，不能算复审通过。
            # 写成 != 而不是"跳过这几值"：将来新增事件类型默认不封口
            # （守卫要 fail-closed，不能是白名单）。
            if iid.startswith("P:") or iid in current_ids or st["event"] != "executed":
                continue
            self.db.add_exec_event(iid, st["kind"], "verified",
                                   note="复审通过：本轮审计不再报告此问题")
            verified.append(iid)
        verified.sort()
        exec_status = {i: s for i, s in exec_last.items() if i in current_ids}

        # Out-of-band changes just healed (externally added/edited/deleted
        # files): snapshot them so the repo stays git-clean.
        healed = len(resynced) + len(missing) + len(added)
        if healed:
            self.snapshots.commit(
                f"external: self-healed {healed} note(s) via audit")

        # 越界索引行去重（resync 与正文取样各登记过一次）
        _qseen: dict[str, dict] = {}
        for e in quarantined:
            _qseen.setdefault(e["path"], e)
        quarantined_index_rows = [_qseen[k] for k in sorted(_qseen)]

        # 审计快照落盘（journal/audit/ 免注册区，markdown 审计轨迹 + git 快照）
        audit_file = self._write_audit_snapshot(
            resynced, missing, added, d1, collisions, dangling, stray,
            dangling_cards, missing_vectors, pruned_blank, pruned_stale,
            verified, reconciled, quarantined_index_rows, invalid_topic_cards)

        res = {"title_duplicates": d1,
               "collisions": collisions,
               "dangling_links": dangling,
               "dangling_cards": dangling_cards,
               "resynced": resynced,
               "missing": missing,
               "added": added,
               "stray": stray,
               "missing_vectors": missing_vectors,
               "pruned_blank_actions": pruned_blank,
               "pruned_stale_collisions": pruned_stale,
               # 越界索引行：永远上报、不给 issue_id、不进 exec 封口集合
               # （它不可被 memory_audit_update 处置，也不该被复审"通过"掉）
               "quarantined_index_rows": quarantined_index_rows,
               # 注册表 `卡:` 越界：已置空防消费（见 _topic_card），这里只留证
               "invalid_topic_cards": invalid_topic_cards,
               "exec_status": exec_status,
               "verified": verified,
               "reconciled_proposals": reconciled,
               "git": self.snapshots.status_line(),
               "guard_stats": self.db.guard_stats(),
               "audit_file": audit_file}
        # 最近一次审计挂在 Store 上：MCP memory_audit 与 WebUI 审计页
        # 共享同一缓存，agent 跑完审计页面立即可见（2026-09-18 之前两入口
        # 各自为政，WebUI 看不到 MCP 刚跑的结果）
        self.last_audit = {"audit": res, "ts": int(time.time())}
        return res

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
                              dangling, stray, dangling_cards=(),
                              missing_vectors=None, pruned_blank=0,
                              pruned_stale=0, verified=(), reconciled=0,
                              quarantined=(), invalid_cards=()) -> str:
        """每日一份审计快照（journal/audit/<YYYYMMDD>.md），同日重跑以"复审"
        小节追加进当天文件——对齐 curator 的同日合并，标题天然唯一不撞 D1，
        且 journal/audit/ 不会随审计频率无界膨胀（过期文件由 curator 清理）。"""
        body = self._audit_body(resynced, missing, added, d1, collisions,
                                dangling, stray, dangling_cards,
                                missing_vectors, pruned_blank, pruned_stale,
                                verified, reconciled, quarantined,
                                invalid_cards)
        day = datetime.now().strftime("%Y%m%d")
        rel = f"{self._audit_dir}{day}.md"
        abs_path = self.root / rel
        if abs_path.is_file():
            old = abs_path.read_text(encoding="utf-8")
            stripped = body.strip("\n")
            review = (f"---\n\n## 复审（{datetime.now().strftime('%Y-%m-%d %H:%M')}）"
                      f"\n\n{stripped}\n")
            marker = "## 处置记录"
            if marker in old:  # 复审插在处置记录之前，处置行保持聚在文件末尾
                head, _, tail = old.partition(marker)
                content = f"{head.rstrip(chr(10))}\n\n{review}\n{marker}{tail}"
            else:
                content = f"{old.rstrip(chr(10))}\n\n{review}"
        else:
            head = (f"# 审计快照 {day}\n\n"
                    f"> 确定性审计 · {datetime.now(UTC).isoformat(timespec='seconds')} "
                    f"· git 快照: {self.snapshots.status_line()}\n")
            content = f"{head}{body}\n## 处置记录\n\n（暂无记录）\n"
        self.save(rel, content)
        return rel

    def _audit_body(self, resynced, missing, added, d1, collisions,
                    dangling, stray, dangling_cards,
                    missing_vectors=None, pruned_blank=0, pruned_stale=0,
                    verified=(), reconciled=0, quarantined=(),
                    invalid_cards=()) -> str:
        guard = self.db.guard_stats()

        def _sec(title, items):
            # 必须返回行列表：调用处是 lines += _sec(...)，返回字符串会被
            # 逐字符拆进列表（2026-09-17 生产实爆：D5 段一字一行）
            return ["", f"## {title}", ""] + [f"- {i}" for i in items] + [""]

        lines = ["## 概览", "",
                 f"- 新增文件（已入索引）：{len(added)}",
                 f"- 外部修改（已重建索引）：{len(resynced)}",
                 f"- 外部删除（已清理索引）：{len(missing)}",
                 f"- 越界索引行（隔离·未读盘未删行）：{len(quarantined or [])}",
                 f"- 注册表卡路径非法（已置空·未读盘）：{len(invalid_cards or [])}",
                 f"- 标题重复（D1）：{len(d1)}",
                 f"- 语义撞车（D2）：{len(collisions)}",
                 f"- 悬空链接（D3）：{len(dangling)}",
                 f"- 游离文件（D4）：{len(stray)}",
                 f"- 悬空主题卡（D5）：{len(dangling_cards)}",
                 f"- 缺向量笔记（已重试自愈）：{len(missing_vectors or [])}",
                 f"- 守卫：拒绝 {guard['refused']} / force {guard['forced']}"
                 f" / 未覆盖拦截 {guard['uncovered']}"]
        if verified:
            lines.append(f"- 复审通过（agent 已执行、本轮不再报告）：{len(verified)}")
        if reconciled:
            lines.append(f"- 提案结案补记：{reconciled} 条（文件已标结案，补记执行事件）")
        if pruned_blank:
            lines.append(f"- 已清理空白处置行：{pruned_blank}")
        if pruned_stale:
            lines.append(f"- 已自动清除过期冲突对：{pruned_stale}"
                         "（笔记已删除或重算后不再命中）")
        lines.append("")
        if added:
            lines += _sec("新增文件", added)
        if resynced:
            lines += _sec("外部修改", resynced)
        if missing:
            lines += _sec("外部删除", missing)
        if quarantined:
            lines += _sec("越界索引行（隔离·未读盘未删行）", [
                f"`{e['path']}` — {e['reason']}" for e in quarantined])
        if invalid_cards:
            # 原文照抄（含换行/反引号）会破坏快照结构，所以只留可诊断的摘要
            lines += _sec("注册表卡路径非法（已置空·未读盘）", [
                f"主题《{e['title']}》的 `卡:` = `{_log_safe(e['card'])}`"
                f" — {e['reason']}；该卡已置空，主题摘要/curator 材料均不再读它。"
                "处置手段：手工把 TOPICS.md 的 `卡:` 改回库内路径"
                for e in invalid_cards])
        if d1:
            lines += _sec("标题重复（D1）", [
                f"`{_d1_id(c)}` — `{c['a_path']}` ↔ `{c['b_path']}`"
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
        if dangling_cards:
            lines += _sec("悬空主题卡（D5）",
                          [f"`{cid}` — 注册表指向的 abstract 不存在" for cid in dangling_cards])
        if missing_vectors:
            lines += _sec("缺向量笔记（已重试自愈）",
                          [f"`{p}`" for p in missing_vectors])
        if verified:
            lines += _sec("复审通过（agent 已执行，本轮审计确认消除）",
                          [f"`{cid}`" for cid in verified])
        return "\n".join(lines) + "\n"

    def record_audit_action(self, audit_file: str, issue_id: str, action: str,
                            label: str, note: str = "") -> dict:
        """记录人类对某审计问题的处置（写入快照文件的处置记录 + 持久化）。

        - D2 撞车：同步 index 状态（open → resolved / dismissed），重跑不重放；
        - D1/D3/D4：写 audit_actions，重跑审计时不再列为待处理；
        - 处置行追加进当次快照文件（markdown 轨迹，git 自动快照）。
        """
        kind = issue_id.split(":", 1)[0]

        rel = self._norm_rel(audit_file, what="审计文件")
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
        # 快照先行：轨迹写盘成功后才同步 D2 状态与处置表，避免快照写失败
        # 时留下无轨迹的孤儿处置行（处置表没有删除接口，2026-09-18 实测）
        self.save(rel, content)
        if kind == "D2":
            cid = issue_id.split(":", 1)[1]
            rows = self.db.list_collisions(status=None)
            row = next((c for c in rows if c["id"] == cid), None)
            if row is not None:
                self.db.resolve_collision(
                    cid, "resolved" if action == "resolved" else "dismissed")
        self.db.record_audit_action(issue_id, kind, "", "", action, note)
        return rel

    def record_proposal_action(self, file: str, index: int, action: str,
                               type_: str = "", reason: str = "",
                               note: str = "") -> dict:
        """裁决 curator 提案条目（WebUI 人类裁决）：持久化 + 提案笔记留痕。

        - audit_actions 表记 P 类条目（id = P:<file>:<index>），前端据此
          展示裁决状态、重进页面不丢失；
        - 提案笔记追加「裁决记录」一节（git 自动快照）；执行仍由 agent
          按留痕进行——curator 铁律的延伸：系统与 WebUI 都只记录裁决，
          不直接改动任何笔记内容。
        """
        rel = self._norm_rel(file, what="提案文件")
        abs_path = self.root / rel
        if not abs_path.is_file():
            raise StoreError(f"提案文件不存在: {file}")
        if index < 1:
            raise StoreError("提案条目序号非法。")
        issue_id = f"P:{file}:{index}"
        verb = "已采纳" if action == "adopted" else "已忽略"
        ts = datetime.now(UTC).strftime("%Y-%m-%d %H:%M")
        line = f"- [{ts}] {verb} 第{index}条 [{type_}] {reason}"
        if note:
            line += f" 备注：{note}"
        content = abs_path.read_text(encoding="utf-8")
        marker = "## 裁决记录"
        if marker in content:
            head, _, tail = content.partition(marker)
            content = f"{head}{marker}{tail.rstrip()}\n{line}\n"
        else:
            content = content.rstrip() + f"\n\n{marker}\n\n{line}\n"
        self.db.record_audit_action(issue_id, "P", "", "", action, note or reason)
        self.save(rel, content)
        if action == "dismissed":
            self._mark_proposal_settled(rel)
        return {"path": rel, "issue_id": issue_id, "action": action}

    _EXEC_EVENTS = ("executing", "progress", "executed", "blocked")
    _EXEC_KINDS = ("D1", "D2", "D3", "D4", "D5", "P")

    def audit_exec_report(self, issue_id: str, event: str,
                          note: str = "", identity: str = "") -> dict:
        """agent 汇报审计问题的执行进度（追加事件时间线，只增不改）。

        issue_id 与审计报告、WebUI 处置表共用同一命名（D3:<path>|<link> /
        P:<file>:<index> 等）；时间线在 audit_exec_events 表，复审通过由
        audit() 在问题消除时自动追加系统事件，agent 不代劳。
        """
        issue_id = (issue_id or "").strip()
        kind = issue_id.split(":", 1)[0].upper()
        if kind not in self._EXEC_KINDS or ":" not in issue_id:
            raise StoreError(
                f"issue_id 非法: {issue_id!r}——应为审计报告里的 id，"
                "形如 D3:topics/x/abstract.md|[[link]] 或 P:journal/curator/x.md:1")
        if event not in self._EXEC_EVENTS:
            raise StoreError(
                f"event 非法: {event!r}（可用：{'/'.join(self._EXEC_EVENTS)}）")
        # P 类的 issue_id 藏着第二条外部路径入口：P:<file>:<index> 的
        # <file> 由 agent 提供，下游 _proposal_findings_indices /
        # _mark_proposal_settled 直接 self.root / rel 读提案正文。写侧
        # 已被 save() 挡住，读侧必须在这里自己收口——否则
        # memory_audit_update(issue_id="P:../outside/secret.md:1")
        # 就能把 root 之外的文件读进来解析成提案条目。
        #
        # 收口必须**先于**落事件（2026-10-03 审查 S1b）：原来校验排在
        # add_exec_event 之后，一个被拒的越界 issue_id 照样在事件表里留下
        # 一条"已执行/已忽略"，之后 exec_last_status 会拿它参与复审封口
        # 与调和——守卫拒掉的东西不该在系统里看起来像发生过。
        # 绝对路径同样拒（allow_leading_slash=False）：见 _norm_rel 文档。
        # 返回值即下游唯一可用路径（prop_file），不再拿 issue_id 原串。
        prop_file = None
        if kind == "P":
            prop_file = self._norm_rel(issue_id[2:].rsplit(":", 1)[0],
                                       what="issue_id 中的提案文件",
                                       allow_leading_slash=False)
        e = self.db.add_exec_event(issue_id, kind, event, (note or "").strip(),
                                   identity or "")
        if kind == "P":
            # P 类事件可能补全结案条件——汇报后顺手检查是否全部条目已结案
            self._mark_proposal_settled(prop_file)
        return {"event": e, "timeline": self.db.list_exec_events(issue_id)}

    # ---- proposal settlement（提案全部条目执行/忽略后对 agent 隐去）----

    def _reconcile_proposals(self) -> int:
        """文件已标已结案 = 人/agent 断言全部条目收口：为缺执行事件且未被
        忽略的条目补记 executed。覆盖事件机制上线前的执行、旧口径「已采纳」、
        以及无会话汇报通道（旧客户端会话拿不到 memory_audit_update）的
        agent 的手工打标——0.3.5 承诺的"两条路都算数"。幂等。
        复审失效标记在 curator 追加复审节时即时撤标，此处不再撤。
        """
        imported = 0
        d = self.root / "curator"
        if not d.is_dir():
            return 0
        for p in sorted(d.glob("提案-*.md")):
            raw = p.relative_to(self.root).as_posix()
            try:
                rel = self._norm_rel(raw, what="库内文件路径")
            except StoreError:
                continue          # 越界的提案文件不读、不解析条目
            try:
                content = p.read_text(encoding="utf-8")
            except OSError:
                continue
            if PROPOSAL_SETTLED_MARKER not in content:
                continue
            indices = self._proposal_findings_indices(rel)
            if not indices:
                continue
            prefix = f"P:{rel}:"
            has_event = set(self.db.exec_last_status().keys())
            dismissed = {a["id"] for a in self.db.list_audit_actions()
                         if a["id"].startswith(prefix) and a["action"] == "dismissed"}
            for i in indices:
                iid = f"{prefix}{i}"
                if iid in has_event or iid in dismissed:
                    continue
                self.db.add_exec_event(
                    iid, "P", "executed", identity="reconcile",
                    note="结案补记：提案文件已标记已结案（条目在事件机制上线前完成或经其他渠道处置）")
                imported += 1
        return imported

    def _proposal_findings_indices(self, rel: str) -> list[int]:
        """提案文件里的条目序号（1..N，跨原提案与同日复审节**连续编号**——
        P:<file>:<index> 的唯一性依赖它；复审节从头重新打印的序号不采用）。
        文件缺失或无提案节返回空表。"""
        p = self.root / rel
        if not p.is_file():
            return []
        indices, seq, in_section = [], 0, False
        for line in p.read_text(encoding="utf-8").splitlines():
            if line.startswith("## "):
                head = line[3:].strip()
                in_section = head.startswith("提案") or head.startswith("复审")
                continue
            if in_section:
                m = re.match(r"^(\d+)\.\s+\*\*\[", line)
                if m:
                    seq += 1
                    indices.append(seq)
        return indices

    def _mark_proposal_settled(self, rel: str) -> bool:
        """全部条目已执行或忽略 → 文件头部打已结案标记（幂等，单向）。

        条目结案 = 对应 P 条目被人类忽略，或执行事件最新状态为
        executed/verified（复审封口由审计负责，P 类无自动复审）。
        历史「已采纳」行不算结案——采纳只是旧口径的派发意图，必须
        由执行事件或显式忽略收口。"""
        indices = self._proposal_findings_indices(rel)
        if not indices:
            return False
        abs_path = self.root / rel
        if not abs_path.is_file():
            return False
        content = abs_path.read_text(encoding="utf-8")
        if PROPOSAL_SETTLED_MARKER in content:
            return False
        prefix = f"P:{rel}:"
        done = {a["id"] for a in self.db.list_audit_actions()
                if a["id"].startswith(prefix) and a["action"] == "dismissed"}
        for iid, st in self.db.exec_last_status().items():
            if iid.startswith(prefix) and st["event"] in ("executed", "verified"):
                done.add(iid)
        if not all(f"{prefix}{i}" in done for i in indices):
            return False
        marker = (f"{PROPOSAL_SETTLED_MARKER}（{datetime.now(UTC).strftime('%Y-%m-%d')}）"
                  f"—— 全部 {len(indices)} 条提案已执行或忽略，agent 无需重复处理")
        lines = content.splitlines()
        if lines and lines[0].lstrip().startswith("#"):
            body = [lines[0], "", marker, *lines[1:]]
        else:
            body = [marker, *lines]
        content = "\n".join(body) + "\n"
        content = content.replace("**状态：待裁决**", "**状态：已结案**", 1)
        self.save(rel, content)
        logger.info("proposal settled: %s (%d findings)",
                    _log_safe(rel), len(indices))
        return True

    def proposal_is_settled(self, rel: str) -> bool:
        """提案文件是否带已结案标记（实时读盘，不缓存）。

        结案不是单向状态：复审给提案追加新条目时标记会被撤销——缓存会把
        已复活的提案当已结案，agent 就永远看不到它了（searcher 0.3.6 的
        同款教训）。rel 须是 list_notes 产出的已规范化库内相对路径，这里
        仍过一遍 _norm_rel 作防御纵深（S3 教训：公开方法不该假设调用方
        已守卫）；越界/缺失/不可读一律按未结案处理——宁可多读一份，
        不漏一份活提案。"""
        try:
            p = self.root / self._norm_rel(rel, what="库内文件路径")
        except StoreError:
            return False
        if not p.is_file():
            return False
        try:
            return PROPOSAL_SETTLED_MARKER in p.read_text(encoding="utf-8")
        except OSError:
            return False

    def _sync_new_files(self) -> list[str]:
        """Index .md files that exist on disk but were never ingested
        (created out-of-band before the server saw them)."""
        added = []
        for rel, p in self._iter_in_root_md():
            if self.db.get_note(rel) is not None:
                continue
            try:
                content = p.read_text(encoding="utf-8")
                title = self._title_from_content(rel, content)
                self._index_note(rel, title, content)
                added.append(rel)
            except Exception as e:
                logger.warning("audit: indexing new file %s failed: %s",
                               _log_safe(rel), _log_safe(e))
        return added

    def _resync_stale_notes(self) -> tuple[list[str], list[str], list[dict]]:
        """Self-healing: reconcile the index with out-of-band file changes.

        - externally edited (disk hash != notes.content_hash): rebuild that
          note's index entry; embeddings come from vec_cache for unchanged
          observation lines; collisions involving it are recomputed.
        - externally deleted: drop its index rows (the user's deletion is the
          source of truth; the store itself never deletes files).
        - index row whose path fails the guard: quarantined, never touched.

        第三项不是"外部删除"的子类：notes 表是派生数据，库里可能还留着旧
        版本（有洞的）构建写进去的越界 path。解析不了的行不能当"文件已被
        用户删掉"的证据——落进下面的删行分支就再也看不到是谁写进去的，
        证据也随之消失。返回的隔离名单由 audit() 汇总上报。
        """
        resynced, missing, quarantined = [], [], []
        for row in self.db.list_notes():
            # 整索引循环同样要过路径守卫（与 resolve() 同一类洞）：越界行不
            # stat、不读、不删、不向量化，只登记隔离上报；非规范但在库内的行
            # 返回规范化路径，p 必须用它拼（拿原串拼时 pathlib 会丢弃 root）。
            # 详见 _index_row_ok——它返回值而不只是判真假正是为了这里。
            rel = self._index_row_ok(row["path"], quarantined)
            if rel is None:
                continue
            p = self.root / rel
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
        return resynced, missing, quarantined

    # ------------------------------------------------------------------ reindex

    def reindex(self) -> dict:
        self.db.clear_all()
        if self.vectors:
            self.vectors.wipe()
        failed = []
        count = 0
        for rel, p in self._iter_in_root_md():
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

        self._last_cleared_collisions = 0
        if not (self.emb and self.vectors):
            return
        pre_open = len(self.db.collisions_for(rel))
        try:
            # Stale vectors/collisions from a previous version of this note
            # must go before re-adding (obs ids are content-addressed, so
            # changed observation lines would otherwise leave orphans).
            self.db.remove_collisions_involving(rel)
            self.vectors.delete_by_path(rel)

            note_vec = self._embed_cached(f"{title}\n{content}")
            self.vectors.upsert_note_vector(rel, f"{title}", note_vec)
            # note 级向量落库即算"不缺向量"（缺向量点名指 note 向量；
            # 两个早退分支在后面，标记必须在此之前打上）
            self.db.set_vector_ok(rel, True)

            def _settle():
                # 重索引后不再命中的旧冲突对即"自动清除"——合并型编辑的
                # 可见信号（2026-09-19 atlas 评审指出清除转换无留痕）
                self._last_cleared_collisions = max(
                    0, pre_open - len(self.db.collisions_for(rel)))

            if rel.startswith(self._machine_zones):
                # 机器产物不是记忆：处置行 "- [时间] 已处理 ..." 会被解析为
                # 伪 observation 且跨快照高度相似，入 obs 空间必然产生 D2 假阳性
                _settle()
                return
            obs_list = parse_observations(content)
            if not obs_list:
                _settle()
                return
            obs_vecs = []
            for obs in obs_list:
                vec = self._embed_cached(obs["text"])
                obs_vecs.append(vec)
                self.vectors.upsert_obs_vector(rel, obs["text"], vec)
            self._d2_check(rel, obs_list, obs_vecs)
            self.db.set_vector_ok(rel, True)
            _settle()
        except Exception as e:
            # 故障期写入的笔记标记缺向量：hash 未变，外部变更自愈不会重试，
            # 由审计补位重试（见 audit 的 missing_vectors 自愈）
            self._last_cleared_collisions = pre_open
            self.db.set_vector_ok(rel, False)
            logger.warning("Vector indexing failed for %s: %s",
                           _log_safe(rel), _log_safe(e))

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
