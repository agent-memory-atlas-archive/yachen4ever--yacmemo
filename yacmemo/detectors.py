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
    re.compile(r"[-–—]\d{6,}$"),
    re.compile(r"[-–—]\d+$"),  # "-2" style dedup counters
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


def d3_scan(contents: dict[str, str], valid_titles: set[str]) -> list[dict]:
    """Dangling [[links]]: targets that match no existing note title.

    Citation-like targets with no letters/CJK (e.g. `[[1,28,28]]`, `[[...]]`)
    are not note references and are ignored.
    """
    out = []
    for path, content in contents.items():
        for link in parse_links(content):
            if not re.search(r"[\u4e00-\u9fffA-Za-z]", link):
                continue
            if link not in valid_titles:
                out.append({"path": path, "link": link})
    return out
