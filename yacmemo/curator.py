"""Memory quality curator: periodic review -> proposal report. Never auto-applies.

    yacmemo-curator --config config.toml [--user yachen] [--dry-run]

Runs (typically via systemd timer, off-peak): gathers the topic registry,
topic cards, audit results and guard stats, asks a configured LLM for
quality findings, and writes a PROPOSAL report note into `curator/`. It has
no write power over memories — approved proposals are executed by the agent
or by hand, exactly like any other human decision.
"""

from __future__ import annotations

import argparse
import json
import logging
from datetime import date, datetime

import httpx

from yacmemo.config import Config, UserEntry, load_config

logger = logging.getLogger("yacmemo.curator")

_SYSTEM_PROMPT = """你是个人记忆体系的质量审查员。
你收到：主题注册表、各主题卡（现状手册）、审计结果。
你的任务是发现记忆体系的质量问题并输出"提案"——你只提案，绝不执行，也绝不修改任何记忆。

审查维度：
- duplicate：同一主题的多份拷贝/快照
  （注意：archive/ 与 OV 分片备份按设计只读保留，不要建议整理它们）
- outdated：现状笔记内容明显落后（结合文中提到的日期与"已取代"线索判断）
- stray：游离在所有注册主题之外的文件（建议归入哪个主题或删除）
- stale-card：主题卡的"现状"描述与正文明显不一致
- merge：两个主题应合并
- forget：纯过程性记录，建议遗忘（删除，git 可恢复）

输出严格 JSON（不要 markdown 代码块）：
{"summary": "总体评价（2-3 句）",
 "findings": [{"type": "duplicate|outdated|stray|stale-card|merge|forget|other",
               "severity": "high|medium|low",
               "paths": ["涉及笔记路径"],
               "reason": "判断依据",
               "proposal": "具体建议动作"}]}

若无发现，findings 返回空数组。severity 从严：只有确有把握才标 high。"""


def default_llm_call(config: Config):
    """Build an OpenAI-compatible chat caller from [curator] config."""
    cfg = config.curator
    if not cfg.base_url or not cfg.model:
        raise RuntimeError("curator 未配置 LLM 端点（[curator].base_url / model）")

    def call(system: str, user: str) -> str:
        headers = {"Content-Type": "application/json"}
        if cfg.api_key:
            headers["Authorization"] = f"Bearer {cfg.api_key}"
        resp = httpx.post(
            f"{cfg.base_url}/chat/completions",
            headers=headers,
            json={
                "model": cfg.model,
                "messages": [{"role": "system", "content": system},
                             {"role": "user", "content": user}],
                "temperature": 0.2,
                "max_tokens": cfg.max_tokens,
            },
            timeout=cfg.timeout,
        )
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"]

    return call


def build_material(store, max_chars_per_card: int = 2000) -> str:
    """Registry + topic cards + audit, assembled into the reviewer prompt."""
    topics = store.load_topics()
    cards = []
    for t in topics:
        p = store.root / t["card"] if t["card"] else None
        if t["card"] and p and p.is_file():
            body = p.read_text(encoding="utf-8")[:max_chars_per_card]
            cards.append(f"## {t['title']}（{t['card']}）\n{body}")
    audit = store.audit()
    material = (
        "### 主题注册表\n" +
        (store.topics_file().read_text(encoding="utf-8")
         if store.topics_file().is_file() else "（空）")
        + "\n\n### 各主题卡\n" + ("\n\n".join(cards) or "（无）")
        + "\n\n### 审计结果\n" + json.dumps(audit, ensure_ascii=False)[:4000]
    )
    return material[:30000]


def parse_proposal(raw: str) -> dict:
    """Parse the LLM's JSON proposal (tolerates markdown code fences)."""
    text = raw.strip()
    if text.startswith("```"):
        text = text.split("```")[1]
        if text.startswith("json"):
            text = text[4:]
    return json.loads(text.strip())


def render_report(proposal: dict, user_id: str) -> str:
    lines = [
        f"# 记忆质量提案（{user_id}，{date.today().isoformat()}）",
        "",
        "**状态：待裁决** —— 本报告由 curator 生成，仅含提案。"
        "批准的条目请让 agent 执行或手工处理；执行后可在审计页复核。",
        "",
        "## 总评",
        proposal.get("summary", ""),
        "",
    ]
    findings = proposal.get("findings", [])
    lines.append(f"## 提案（{len(findings)} 条）")
    if not findings:
        lines.append("无。")
    for i, f in enumerate(findings, 1):
        lines.append(
            f"{i}. **[{f.get('severity', '?')}] {f.get('type', '?')}** — {f.get('reason', '')}")
        for p in f.get("paths", []):
            lines.append(f"   - 涉及: {p}")
        lines.append(f"   - 建议: {f.get('proposal', '')}")
    lines.append("")
    lines.append("> 裁决后在本行下追加执行记录；被采纳并执行的条目由 agent 在对应笔记中落实。")
    return "\n".join(lines)


def run_check(config: Config, user: UserEntry, dry_run: bool = False,
              llm_call=None) -> str:
    """One curator pass for one user. Returns the proposal report markdown."""
    from yacmemo.index_db import IndexDB
    from yacmemo.store import Store
    from yacmemo.vector import VectorStore

    root = config.user_root_abs(user)
    db = IndexDB(str(root / ".index" / "index.db"))
    emb, vectors = None, None
    if config.embedding.base_url and config.embedding.model:
        from yacmemo.embedding import EmbeddingClient

        emb = EmbeddingClient(base_url=config.embedding.base_url,
                              api_key=config.embedding.api_key,
                              model=config.embedding.model,
                              dimensions=config.embedding.dimensions,
                              timeout=config.embedding.timeout)
        vectors = VectorStore(str(root / ".index" / "lancedb"),
                              config.embedding.dimensions)
    store = Store(config, db, emb, vectors, root=root)

    material = build_material(store)
    call = llm_call or default_llm_call(config)
    raw = call(_SYSTEM_PROMPT, material)
    proposal = parse_proposal(raw)
    report = render_report(proposal, user.id)

    if not dry_run:
        base = f"curator/提案-{date.today().isoformat()}"
        path = base + ".md"
        if (store.root / path).is_file():
            # 同日重跑不覆盖（旧报告可能已带裁决记录）
            path = f"{base}-{datetime.now().strftime('%H%M')}.md"
        store.save(path, report)
    db.close()
    return report


def main():
    parser = argparse.ArgumentParser(description="yacmemo memory quality curator")
    parser.add_argument("--config", default=None)
    parser.add_argument("--user", default=None, help="single user (default: all)")
    parser.add_argument("--dry-run", action="store_true",
                        help="print the proposal instead of saving it")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")
    config = load_config(args.config)
    users = config.users
    if args.user:
        users = [u for u in users if u.id == args.user]
        if not users:
            raise SystemExit(f"未知用户: {args.user}")

    for user in users:
        try:
            report = run_check(config, user, dry_run=args.dry_run)
            logger.info("[%s] curator report generated (%d chars)%s",
                        user.id, len(report), " (dry-run)" if args.dry_run else "")
            if args.dry_run:
                print(report)
        except Exception as e:
            logger.error("[%s] curator check failed: %s", user.id, e)


if __name__ == "__main__":
    main()
