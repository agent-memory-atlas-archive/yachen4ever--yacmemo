"""Layer 3: Consistency checking — detect contradictions between facts."""

from __future__ import annotations

import logging

from .db import MemoryDB
from .embedding import EmbeddingClient
from .llm import LLMClient, LLMJSONError
from .vector import VectorStore

logger = logging.getLogger(__name__)


_CONSISTENCY_SYSTEM = """你是一致性校验助手。判断给定的旧事实和新事实是否矛盾。以JSON格式输出：
{
  "contradictory": true或false,
  "current": "A"或"B"（哪个是当前有效的）,
  "reason": "判断理由（简洁）",
  "confidence": 0.0到1.0的置信度
}"""


class ConsistencyChecker:
    """Layer 3 consistency checking."""

    def __init__(self, config, db: MemoryDB, vector: VectorStore,
                 llm: LLMClient, emb: EmbeddingClient):
        self.config = config
        self.db = db
        self.vector = vector
        self.llm = llm
        self.emb = emb

    def check_new_node(self, user_id: str, node_id: str):
        """Check a newly extracted node against existing nodes for contradictions."""
        node = self.db.get_node(user_id, node_id)
        if not node:
            return

        # Get embedding for this node
        node_text = f"{node['name']}: {node['summary']}"
        try:
            query_emb = self.emb.embed_one(node_text)
        except Exception as e:
            logger.warning("Embedding failed for consistency check on '%s': %s",
                           node['name'], e)
            return

        # Search for similar existing nodes (exclude self)
        similar = self.vector.search_nodes(query_emb, limit=5)
        threshold = self.config.consistency.similarity_threshold

        for match in similar:
            match_id = match.get("id")
            if match_id == node_id:
                continue

            # LanceDB returns _distance (L2 distance), convert to similarity
            distance = match.get("_distance", 999)
            # For normalized embeddings, similarity ≈ 1 - distance/2
            # But we use cosine distance directly as threshold
            if distance > (1 - threshold):
                continue

            old_node = self.db.get_node(user_id, match_id)
            if not old_node or not old_node["valid"]:
                continue

            # Judge contradiction via LLM
            result = self._judge(old_node, node)
            if result.get("contradictory"):
                confidence = result.get("confidence", 0.5)
                reason = result.get("reason", "")

                auto = (confidence >= self.config.consistency.confidence_threshold
                        and self.config.consistency.auto_invalidate)

                if auto:
                    self.db.invalidate_node(user_id, match_id,
                                            f"superseded_by:{node_id}: {reason}")
                    logger.info("Auto-invalidated node '%s' (confidence=%.2f): %s",
                                old_node['name'], confidence, reason)

                self.db.add_consistency_log(
                    user_id,
                    old_node_id=match_id,
                    new_node_id=node_id,
                    old_source_path=old_node.get("source_path", ""),
                    new_source_path=node.get("source_path", ""),
                    reason=reason,
                    confidence=confidence,
                    auto_invalidated=auto,
                )

    def check_all(self, user_id: str):
        """Full consistency scan of all valid nodes (cron triggered)."""
        nodes = self.db.get_all_valid_nodes(user_id)
        logger.info("Full consistency scan: %d valid nodes", len(nodes))

        for node in nodes:
            self.check_new_node(user_id, node["id"])

        logger.info("Full consistency scan complete")

    def _judge(self, old: dict, new: dict) -> dict:
        """Use LLM to judge if two facts contradict each other."""
        old_text = f"{old['name']}: {old.get('summary', '')}"
        new_text = f"{new['name']}: {new.get('summary', '')}"
        old_time = old.get("created_at", "unknown")
        new_time = new.get("created_at", "unknown")

        user_msg = (
            f"旧事实（记录时间: {old_time}）: {old_text}\n"
            f"新事实（记录时间: {new_time}）: {new_text}\n\n"
            f"请判断这两个事实是否矛盾。如果矛盾，哪个是当前有效的？"
        )

        try:
            result = self.llm.chat_json(
                system=_CONSISTENCY_SYSTEM,
                user=user_msg,
                max_tokens=self.config.llm.consistency_max_tokens,
                timeout=self.config.llm.consistency_timeout,
            )
            return result
        except LLMJSONError as e:
            logger.warning("Consistency judge JSON parse failed: %s", e)
            return {"contradictory": False, "confidence": 0.0, "reason": str(e)}
        except Exception as e:
            logger.warning("Consistency judge failed: %s", e)
            return {"contradictory": False, "confidence": 0.0, "reason": str(e)}
