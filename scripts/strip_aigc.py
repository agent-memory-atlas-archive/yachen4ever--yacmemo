#!/usr/bin/env python3
"""删除 TeleAgent 自动注入的 AIGC watermark 头（markdown frontmatter）。

TeleAgent 本地 AIGC hook 会在 agent 创建/修改 .md 文件后，在文件头部自动
插入一段 YAML frontmatter（`AIGC:` 节）。本工具检测并删除这种头，保持仓库
文档纯净、可 diff。

约定：任何修改 markdown 文件之后调用本工具（见 yacmemo 工作规则记忆）。

用法：
    strip_aigc.py 文件.md [更多.md...]   # 清理指定文件
    strip_aigc.py -r 目录                # 递归清理目录下 *.md
    strip_aigc.py -r 目录 --dry-run      # 只打印将清理的文件，不改动
    strip_aigc.py -r 目录 --check        # 有 AIGC 头则退出码 1（CI 用）

退出码：0 = 全部清理完成（或无需清理）；1 = --check 模式下发现残留。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# hook 注入的 frontmatter 第二行固定为 `AIGC:`（键顶格）
_MARKER = "AIGC:"

# 递归时跳过的目录
_SKIP_DIRS = {".git", ".temp", "node_modules", "dist", "__pycache__", ".venv",
              ".uv-cache", ".index"}


def strip_aigc(text: str) -> tuple[str, bool]:
    """删除开头的 AIGC frontmatter。返回 (新文本, 是否发生了删除)。"""
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        return text, False
    if len(lines) < 2 or not lines[1].lstrip().startswith(_MARKER):
        return text, False
    end = None
    for i in range(2, len(lines)):
        if lines[i].strip() == "---":
            end = i
            break
    if end is None:
        return text, False  # 没有闭合的 frontmatter，不碰
    rest = lines[end + 1:]
    # 头与正文之间通常有一个空行，顺手吃掉一个
    if rest and rest[0].strip() == "":
        rest = rest[1:]
    return "".join(rest), True


def strip_file(path: Path, dry_run: bool) -> tuple[bool, bool]:
    """返回 (是否被修改, 是否出错)。"""
    try:
        text = path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError) as e:
        print(f"  ! 读取失败 {path}: {e}", file=sys.stderr)
        return False, True
    new_text, changed = strip_aigc(text)
    if changed:
        print(f"  - 删除 AIGC 头: {path}")
        if not dry_run:
            path.write_text(new_text, encoding="utf-8")
    return changed, False


def main() -> int:
    ap = argparse.ArgumentParser(description="删除 markdown 的 AIGC 头")
    ap.add_argument("paths", nargs="*", help="文件或目录（-r 时递归）")
    ap.add_argument("-r", "--recursive", action="store_true",
                    help="递归处理目录下的 *.md")
    ap.add_argument("--dry-run", action="store_true",
                    help="只打印将清理的文件")
    ap.add_argument("--check", action="store_true",
                    help="发现残留则退出码 1（CI 检查用）")
    args = ap.parse_args()

    targets: list[Path] = []
    for p in args.paths:
        path = Path(p)
        if path.is_file() and path.suffix == ".md":
            targets.append(path)
        elif path.is_dir() and args.recursive:
            for f in sorted(path.rglob("*.md")):
                if not any(part in _SKIP_DIRS for part in f.parts):
                    targets.append(f)
        else:
            print(f"  ! 忽略（不是 md 或需要 -r）: {p}", file=sys.stderr)

    if not targets:
        print("无文件需要处理（用法见 --help）", file=sys.stderr)
        return 0

    found = 0
    errors = 0
    for t in targets:
        changed, err = strip_file(t, args.dry_run)
        if err:
            errors += 1
        elif changed:
            found += 1

    if args.check:
        print(f"检查完成：{len(targets)} 个文件，{found} 个带 AIGC 头"
              f"{'（--check 模式，未改动文件）' if args.dry_run else ''}")
        return 1 if found else 0

    print(f"完成：检查 {len(targets)} 个文件，清理 {found} 个"
          + ("（dry-run，未改动）" if args.dry_run else "")
          + (f"，{errors} 个读取失败" if errors else ""))
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())