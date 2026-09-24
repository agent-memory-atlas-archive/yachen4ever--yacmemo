"""Deterministic consistency utilities: parsing, title normalization, D1/D3 scans.

No LLM anywhere in this module — string math only. D2 (observation semantic
collision) lives in store.py because it is incremental and vector-based.
"""

from __future__ import annotations

import re

from rapidfuzz import fuzz

# Suffixes the title guard ignores: "-2", "(新)", "更新", dates, timestamps, "v1"
_SUFFIX_PATTERNS = [
    re.compile(r"[-–—]?\d{4}[-/.年]\d{1,2}[-/.月]\d{1,2}日?$"),
    re.compile(r"\d{8}$"),  # 紧凑 YYYYMMDD（2026-09-16 实测漏拦："笔记0916"绕过守卫）
    # 紧凑 MMDD：仅剥合法月日（01-12 月），"1972" 这类型号/年份结尾不受影响
    re.compile(r"(0[1-9]|1[0-2])(0[1-9]|[12]\d|3[01])$"),
    re.compile(r"[-–—]\d+$"),  # "-2" style dedup counters（含 "-0916" 等带分隔符数字）
    re.compile(r"[-–—]?v\d+$", re.IGNORECASE),
    re.compile(r"[-–—]?更新$"),
    re.compile(r"[-–—]?新$"),
    re.compile(r"[（(]\s*新\s*[）)]$"),
]
_NON_WORD = re.compile(r"[\W_]+", re.UNICODE)

_OBS_RE = re.compile(r"^\s*-\s*\[([^\]]{1,32})\]\s*(.+?)\s*$")
# GFM task-list items ("- [x] done") are checkboxes, not observations
_TASK_RE = re.compile(r"^\s*-\s*\[[ xX]\]\s")
_LINK_RE = re.compile(r"\[\[([^\[\]]+)\]\]")


def normalize_title(title: str) -> str:
    """Canonical form for duplicate detection: strip dates/suffixes/punct/case."""
    t = title.strip()
    for pat in _SUFFIX_PATTERNS:
        t = pat.sub("", t)
    return _NON_WORD.sub("", t).lower()


def title_similarity(a: str, b: str) -> float:
    na, nb = normalize_title(a), normalize_title(b)
    if not na or not nb:
        return 0.0
    return max(fuzz.ratio(na, nb), fuzz.token_sort_ratio(na, nb)) / 100.0


def find_title_conflicts(
    new_title: str,
    existing: list[dict],  # [{path, title}]
    threshold: float,
    exclude_path: str | None = None,
) -> list[dict]:
    """Return existing notes whose title is a near-duplicate of new_title."""
    conflicts = []
    for row in existing:
        if exclude_path and row["path"] == exclude_path:
            continue
        score = title_similarity(new_title, row["title"])
        if score >= threshold:
            conflicts.append({"path": row["path"], "title": row["title"],
                              "score": round(score, 3)})
    conflicts.sort(key=lambda c: c["score"], reverse=True)
    return conflicts


def d1_scan(all_titles: list[dict], threshold: float) -> list[dict]:
    """Full pairwise title-duplicate scan (audit backstop; O(n²) string math)."""
    out = []
    for i in range(len(all_titles)):
        for j in range(i + 1, len(all_titles)):
            a, b = all_titles[i], all_titles[j]
            score = title_similarity(a["title"], b["title"])
            if score >= threshold:
                out.append({"a_path": a["path"], "b_path": b["path"],
                            "a_title": a["title"], "b_title": b["title"],
                            "score": round(score, 3)})
    out.sort(key=lambda c: c["score"], reverse=True)
    return out


def parse_observations(content: str) -> list[dict]:
    """Parse Basic-Memory-style observation lines: `- [category] text #tag`.

    GFM task-list items (`- [x]` / `- [ ]`) are checkboxes, not observations,
    and are excluded so checklist-heavy notes don't generate collision noise.
    """
    out = []
    for lineno, line in enumerate(content.splitlines(), 1):
        if _TASK_RE.match(line):
            continue
        m = _OBS_RE.match(line)
        if m:
            out.append({"category": m.group(1).strip(), "text": m.group(2).strip(),
                        "line": lineno})
    return out


def parse_links(content: str) -> list[str]:
    """Unique [[wiki-link]] targets in order of first appearance."""
    seen = []
    for m in _LINK_RE.finditer(content):
        target = m.group(1).strip()
        if target and target not in seen:
            seen.append(target)
    return seen


def canonical_link_target(link: str) -> str:
    """Normalize a [[link]] target for path-form resolution（与 resolve() 同规则）."""
    return link.strip().replace("\\", "/").lstrip("/")


def d3_scan(contents: dict[str, str], valid_titles: set[str]) -> list[dict]:
    """Dangling [[links]]: targets that resolve to no existing note.

    两种合法形式，标题优先：
    - 标题形式：精确命中某笔记的标题（memory_move 保留的形式）
    - 路径形式：相对记忆根的路径，带不带 .md 都行——agent 从 memory_list
      拿到的就是路径，主题卡互链只能用它（标题从 H1 提取，会与主题名漂移）

    Citation-like targets with no letters/CJK (e.g. `[[1,28,28]]`, `[[...]]`)
    are not note references and are ignored.
    """
    path_forms = set()
    for p in contents:
        path_forms.add(p)
        if p.endswith(".md"):
            path_forms.add(p[:-3])
    out = []
    for path, content in contents.items():
        for link in parse_links(content):
            if not re.search(r"[\u4e00-\u9fffA-Za-z]", link):
                continue
            if link in valid_titles:
                continue
            if canonical_link_target(link) in path_forms:
                continue
            out.append({"path": path, "link": link})
    return out
