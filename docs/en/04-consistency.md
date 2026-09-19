> English | [简体中文](../04-consistency.md)

# Consistency Mechanism

> Core stance: **failure semantics trump detection semantics**. The system never deletes and never hides any memory; violations are visible, reversible, and countable.
> Deletion happens only on the user's explicit instruction (`memory_delete` / the WebUI delete button), and every deletion automatically produces a git snapshot with recoverable history — the system itself never deletes proactively, which is fundamentally different from v1's "auto-invalidation".
> Code locations: `store.py` (guards + D2 + stray detection), `detectors.py` (D1/D3 + normalization), `index_db.py` (collisions / guard_events).

## 1. Why consistency needs three lines of defense

The root of memory rot is that **conventions are probabilistic**: no matter how strong the model, there are times it fails to observe "search before writing, update in place" (overlong sessions, context compaction, small models). The failure-semantics ranking of the three responses:

1. Auto-invalidation (v1's approach): wrong kills happen silently and correct memories disappear — **the worst**;
2. Pure convention without detection: violations accumulate invisibly — **second worst**;
3. **API enforcement + deterministic detection + presentation**: violations are either blocked, or old and new coexist with annotations — **this design**.

## 2. The three lines of defense

### Layer 1: API enforcement (deterministic, write path)

| Tool | Enforced behavior |
|---|---|
| `memory_write` | Normalized-title fuzzy comparison ≥ 0.85 → refuse creation and point to `memory_edit` instead; journal/ exempt |
| `memory_edit` | `old_string` must exist and be unique, otherwise refuse and list the hit line numbers |
| `memory_edit_section` | The section heading must exist and be unique, otherwise list available sections/line numbers |
| `memory_move` | Refuse if the target already exists |

Rejection messages are all executable next-step instructions; the agent can self-correct as soon as it receives one.

**Two-stage force confirmation**: `force=true` is the only channel past the near-duplicate title guard, but once forced events within a 24-hour rolling window reach `force_confirm_threshold` (default 3), bare force is refused and both `force=true, force_confirm=true` are required to pass (explicit manual-confirmation semantics). The rejection message lists candidate existing notes. The whole process lands in `guard_events`:

- `refused` count: how often the guards intercept (the basis for tuning `title_similarity_threshold`);
- `forced` count: **a direct measure of the violation rate** (the P4 core metric);
- `uncovered` count: trigger count of the topic-registry hard block (added 2026-09-18, see 01-architecture §6.1).

### Layer 2: Deterministic detection (deterministic, zero LLM)

**D1 duplicate titles**: the guard only intercepts "at write time"; existing notes (historical writes / force bypasses / external creations) are covered by the audit's full pairwise scan (`detectors.d1_scan`, rapidfuzz after normalization, O(n²) string operations, millisecond-scale at personal scale).

**D2 observation semantic collisions** (incremental at write time, `store._d2_check`):

```
Each new observation vector → obs_vectors top-5 (excluding the same note)
→ compute the true cosine per candidate (no assumption that vectors are normalized)
→ cosine ≥ 0.86 → record open in the collisions table (with both sides' text and scores)
```

- Incremental: only newly written lines are compared, never a full run; old values are reused at zero cost via vec_cache;
- **Deliberately non-adjudicating**: it does not judge "whether they contradict each other" and does not decide "which one is valid" — it only marks "suspected to be talking about the same thing";
- Self-healing linkage: when an involved note is edited / externally modified / deleted, its collisions rows are removed and recomputed (`remove_collisions_involving` + the audit's hash-level resync); **clearance leaves a trace** (added 2026-09-19): old pairs that no longer match after re-indexing count as "auto-cleared", with the count reported in three places — appended to the success returns of `memory_edit` / `memory_edit_section` as "(automatically cleared N stale collision pairs)", the overview line of the audit overview and snapshot, and the `== auto-cleared stale collision pairs ==` line of the `memory_audit` output. Merge-type edits thereby gain a visible signal: **seeing the clearance count = this edit dissolved a semantic collision**;
- **Machine-produced zones stay out of the obs space**: journal/audit/ audit snapshots and curator/ proposal reports are system-derived outputs, not memories — their disposition lines `- [time] handled ...` would be treated by `parse_observations` as pseudo-observations (category = timestamp) and would be highly similar across snapshots, so `_index_note` skips obs indexing and D2 for machine-produced zones (`store._machine_zones`), and D1 candidates exclude them too (snapshot titles are isomorphic; proposal reports collide with each other once normalization strips the dates). Audit snapshots remain retrievable via FTS and note-level vector search.

**D3 dangling links**: a `[[target]]` matching no existing title → listed by audit (usually typos or notes still to be created). Machine-produced zones are exempt — audit snapshots quote the original text of the previous round's dangling links, and once the source note is deleted they must not call themselves out (same rule as D1/D2). Markers whose referenced target contains no text (e.g. the literature citation `[[1,28,28]]`) do not count as links and are filtered out.

**D4 stray files**: markdown belonging to no registered topic is called out by audit (the registry-free zones journal/, archive/, curator/ are exempt). This is the enforcement mechanism of the topic registry — no hiding place outside topics; the registry itself is described in [01-architecture.md](01-architecture.md) §13. **The write path is already hard-blocked (added 2026-09-17)**: the tool surface (memory_write / save creating new / move targets) can no longer manufacture strays, so D4 becomes the backstop — covering strays outside the tool surface, such as hand-created Obsidian files and leftovers from unregistration; the write interception and D4 share the same coverage test (`_path_covered`), so the criteria are always identical.

**D5 dangling topic cards**: the registry's `卡:` field points to a nonexistent abstract (leftovers from restructure / manual TOPICS.md edits). D3 only scans links inside note bodies, and the registry itself has no validation — audit fills the gap with call-outs; disposition means fixing the registry or rebuilding the card.

Known blind spot (accepted): D2 misses contradictions that are logically conflicting but far apart in wording (e.g. "sys_user has no role_color" vs "added a role_color field"). Closing it would cost a full LLM scan — v1's lesson; not doing it.

**Coverage boundary (deliberate)**: D2 acts only on observation lines (`- [category] text`), not on full note text — duplicate facts in body prose produce no ⚠ (the write convention of putting fact lines into observations exists precisely for this). The backstop for such duplicates: note-level vector retrieval recalls both notes together and leaves adjudication to the main model at read time, plus the audit's D1 title scan.

### Layer 3: Main-model adjudication (at read time)

`memory_search` results carry inline `⚠` annotations (from the collisions table):

```
1. 端口配置说明 (score 0.0281, fts+vector)
   path: 端口配置说明.md
   ⚠ Suspected duplicate with [[yacmemo部署配置]] (score 0.91) — suggested to read both notes and then merge with memory_edit. Other note's content: the service port is 9721
```

The moment of reading is the moment of repair: the agent reads both notes → merges with `memory_edit` → the collision pair disappears naturally. The strongest model in the house (the main model) makes the hardest call, and only when things are genuinely ambiguous; a human can run `memory_audit` at any time as the backstop.

## 3. Data model

```sql
collisions(id, kind,           -- obs / title
           a_path, b_path, a_text, b_text, score,
           detected_at, status)  -- open / resolved / dismissed

guard_events(id, ts, kind,     -- refused / forced / uncovered (not cleared by clear_all or reindex)
             attempted_title, matched_path)
```

## 4. Thresholds and tuning

| Parameter | Default | Meaning | Tuning basis |
|---|---|---|---|
| `title_similarity_threshold` | 0.85 | Normalized-title similarity interception line | High false-block ratio in refused logs → lower; missed blocks → raise |
| `collision_cosine_threshold` | 0.86 | D2 collision cosine line | Many audit false positives → raise; false negatives → lower |
| `force_confirm_threshold` | 3 | Number of force uses without confirmation within 24h | Relaxable when the model is good at keeping conventions |

## 5. Comparison with the v1 consistency layer

| | v1 (retired) | v2 |
|---|---|---|
| Judge | 1.3B small model self-reported confidence | Deterministic rules + main model (at read time) |
| Invalidation method | Auto-marks `valid=0` (can silently kill correct memories) | No deletion, no hiding; ⚠ annotation coexists |
| Coverage timing | Docs claimed per-node real-time; in practice a full scan on every write | Incremental at write + full at audit |
| Cost | O(all memories) LLM calls per write | Zero LLM |
| Violation visibility | Wrong kills invisible | Everything visible (⚠ annotations / audit reports) |
