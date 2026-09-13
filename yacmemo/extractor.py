"""Layer 2: Extract entities/events from .md files using LLM."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone

from .db import MemoryDB
from .embedding import EmbeddingClient
from .llm import LLMClient, LLMJSONError
from .vector import VectorStore
from .fs_utils import content_hash, file_hash, get_split_dir, to_rel_path

logger = logging.getLogger(__name__)


@dataclass
class ExtractResult:
    path: str
    status: str  # success / skipped / failed / incremental
    split_count: int = 0
    entity_count: int = 0
    event_count: int = 0
    errors: list[str] = field(default_factory=list)


# ---- Prompts ----

_EXTRACT_SYSTEM = """你是一个记忆提取助手。请将给定的工作记录按独立事项拆分为多个文件，并为每个文件提取关联的实体和事件。
以JSON格式输出，格式如下：
{
  "files": [
    {
      "name": "文件名（不含.md后缀，用英文kebab-case）",
      "title": "标题（中文，简明描述这个事项）",
      "content": "文件正文内容（简洁摘要，保留关键细节）",
      "entities": [{"name": "实体名称", "type": "实体类型", "summary": "一句话描述"}],
      "events": [{"date": "日期(YYYY-MM-DD或留空)", "type": "事件类型", "description": "事件描述"}]
    }
  ]
}"""

_INCREMENT_HINT = """
以下是上次的拆分结果，请做增量更新：
- 新增的内容 → 创建新文件
- 修改的内容 → 更新对应文件
- 未变的内容 → 保持不变
- 删除的内容 → 不再包含

上次拆分结果：
"""


class Extractor:
    """Layer 2 extraction pipeline."""

    def __init__(self, config, db: MemoryDB, vector: VectorStore,
                 llm: LLMClient, emb: EmbeddingClient):
        self.config = config
        self.db = db
        self.vector = vector
        self.llm = llm
        self.emb = emb

    def process_file(self, md_path: str) -> ExtractResult:
        """Process a single .md file: extract → split → index."""
        memory_root = self.config.memory_root_abs
        rel_path = to_rel_path(md_path, memory_root)

        # Step 1: Read file and compute hash
        try:
            with open(md_path, "r", encoding="utf-8") as f:
                content = f.read()
        except FileNotFoundError:
            return ExtractResult(path=rel_path, status="failed",
                                 errors=["File not found"])

        chash = content_hash(content)

        # Step 2: Check if already processed
        prev = self.db.get_processed_file(rel_path)
        if prev and prev["content_hash"] == chash and prev["status"] == "success":
            return ExtractResult(path=rel_path, status="skipped")

        is_incremental = prev is not None

        # Step 3: Determine split directory
        split_dir = get_split_dir(md_path, memory_root)
        split_dir_rel = to_rel_path(split_dir, memory_root) if os.path.isdir(split_dir) else None

        # Step 4: Get previous split results for incremental extraction
        prev_splits = None
        if is_incremental and split_dir and os.path.isdir(split_dir):
            prev_splits = self._read_prev_splits(split_dir)

        # Step 5: Call LLM for extraction
        try:
            result_json = self._extract(content, prev_splits)
        except (LLMJSONError, Exception) as e:
            logger.error("Extraction failed for %s: %s", rel_path, e)
            self.db.record_processed_file(rel_path, chash, split_dir_rel or "", "failed", str(e))
            return ExtractResult(path=rel_path, status="failed", errors=[str(e)])

        files = result_json.get("files", [])
        if not files:
            logger.info("No content extracted from %s", rel_path)
            self.db.record_processed_file(rel_path, chash, split_dir_rel or "", "success",
                                           split_file_count=0)
            return ExtractResult(path=rel_path, status="success", split_count=0)

        # Step 6: Write split files
        self._write_split_files(split_dir, files, rel_path)

        # Step 7: Clear old data for this source (re-extraction)
        for split_file in self._list_split_files(split_dir, [f["name"] for f in files]):
            split_rel = to_rel_path(split_file, memory_root)
            self.db.delete_nodes_by_source(split_rel)
            self.db.delete_events_by_source(split_rel)
            self.db.delete_edges_by_source(split_rel)
            self.vector.delete_by_source(split_rel)

        # Step 8: Index new entities/events
        total_entities = 0
        total_events = 0
        all_new_node_ids = []

        for f in files:
            split_file_path = os.path.join(split_dir, f["name"] + ".md")
            split_rel = to_rel_path(split_file_path, memory_root)
            shash = file_hash(split_file_path)

            # Index entities
            for ent in f.get("entities", []):
                name = ent.get("name", "")
                if not name:
                    continue
                ent_type = ent.get("type", "unknown")
                ent_summary = ent.get("summary", "")
                node_text = f"{name}: {ent_summary}"
                node_id = self.db.upsert_node(
                    name=name, type_=ent_type, summary=ent_summary,
                    source_path=split_rel, source_hash=shash,
                    original_path=rel_path,
                )
                # Vectorize
                try:
                    emb_vec = self.emb.embed_one(node_text)
                    self.vector.upsert_node_vector(node_id, node_text, emb_vec, split_rel)
                except Exception as e:
                    logger.warning("Embedding failed for entity '%s': %s", name, e)
                total_entities += 1
                all_new_node_ids.append(node_id)

            # Index events
            for evt in f.get("events", []):
                date = evt.get("date", "")
                evt_type = evt.get("type", "")
                description = evt.get("description", "")
                evt_text = f"{date} {evt_type}: {description}"
                event_id = self.db.upsert_event(
                    date=date, type_=evt_type, summary=description,
                    details="", source_path=split_rel,
                    source_hash=shash, original_path=rel_path,
                )
                try:
                    emb_vec = self.emb.embed_one(evt_text)
                    self.vector.upsert_event_vector(event_id, evt_text, emb_vec, split_rel)
                except Exception as e:
                    logger.warning("Embedding failed for event '%s': %s", description[:50], e)
                total_events += 1

        # Step 9: Update processed_files
        status = "incremental" if is_incremental else "success"
        self.db.record_processed_file(rel_path, chash, split_dir_rel or "", status,
                                       split_file_count=len(files))

        logger.info("Extracted %s: %d files, %d entities, %d events (%s)",
                    rel_path, len(files), total_entities, total_events, status)

        return ExtractResult(
            path=rel_path, status=status, split_count=len(files),
            entity_count=total_entities, event_count=total_events,
        )

    def _extract(self, content: str, prev_splits: list[dict] | None) -> dict:
        """Call LLM to extract entities/events from content."""
        user_msg = content
        if prev_splits:
            user_msg += "\n\n" + _INCREMENT_HINT + "\n".join(
                f"- {s['name']}: {s['title']}" for s in prev_splits
            )
        return self.llm.chat_json(
            system=_EXTRACT_SYSTEM,
            user=user_msg,
            max_tokens=self.config.llm.extract_max_tokens,
            timeout=self.config.llm.extract_timeout,
        )

    def _read_prev_splits(self, split_dir: str) -> list[dict]:
        """Read previous split file metadata for incremental extraction."""
        result = []
        for f in os.listdir(split_dir):
            if not f.endswith(".md") or f.startswith("."):
                continue
            name = f[:-3]
            filepath = os.path.join(split_dir, f)
            try:
                with open(filepath, "r", encoding="utf-8") as fh:
                    first_line = fh.readline()
                    # Extract title from first heading
                    title = first_line.lstrip("# ").strip() if first_line.startswith("#") else name
                result.append({"name": name, "title": title})
            except Exception:
                result.append({"name": name, "title": name})
        return result

    def _write_split_files(self, split_dir: str, files: list[dict], original_path: str):
        """Write split .md files to the split directory."""
        os.makedirs(split_dir, exist_ok=True)
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        for f in files:
            name = f.get("name", "untitled")
            title = f.get("title", name)
            content = f.get("content", "")
            entities = f.get("entities", [])
            events = f.get("events", [])

            # Build frontmatter
            lines = [
                "---",
                f"source: {os.path.basename(original_path)}",
                f"created: {now}",
                f"updated: {now}",
                "---",
                "",
                f"# {title}",
                "",
                content,
            ]

            # Add entity references section
            if entities:
                lines.append("")
                lines.append("## 关联实体")
                for ent in entities:
                    ent_name = ent.get("name", "")
                    ent_type = ent.get("type", "")
                    if ent_name:
                        lines.append(f"- {ent_name}: {ent_type}")

            # Add events section
            if events:
                lines.append("")
                lines.append("## 事件")
                for evt in events:
                    date = evt.get("date", "")
                    desc = evt.get("description", "")
                    lines.append(f"- {date}: {desc}" if date else f"- {desc}")

            filepath = os.path.join(split_dir, name + ".md")
            with open(filepath, "w", encoding="utf-8") as fh:
                fh.write("\n".join(lines))

    def _list_split_files(self, split_dir: str, expected_names: list[str]) -> list[str]:
        """List all .md files in split_dir that match expected names."""
        if not os.path.isdir(split_dir):
            return []
        result = []
        expected_set = {n + ".md" for n in expected_names}
        for f in os.listdir(split_dir):
            if f in expected_set:
                result.append(os.path.join(split_dir, f))
        return result
