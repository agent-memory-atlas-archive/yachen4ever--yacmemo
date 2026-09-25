import { describe, expect, it } from 'vitest'
import { parseFindings } from './proposal-parse.js'

const REPORT = `# 记忆质量提案（测试）

**状态：待裁决** —— 说明行。

## 总评
总体没问题。

## 提案（2 条）
1. **[medium] outdated** — 条目一的原因
   - 涉及: topics/a/abstract.md
   - 建议: 处理条目一
2. **[low] other** — 条目二的原因
   - 涉及: topics/b/abstract.md

> 裁决后在本行下追加执行记录；被采纳并执行的条目由 agent 在对应笔记中落实。
`

describe('parseFindings', () => {
  it('解析基础提案节（severity/type/reason/涉及/建议）', () => {
    const fs = parseFindings(REPORT)
    expect(fs).toHaveLength(2)
    expect(fs[0]).toMatchObject({
      index: 1, severity: 'medium', type: 'outdated',
      reason: '条目一的原因', paths: ['topics/a/abstract.md'],
      proposal: '处理条目一',
    })
  })

  it('复审节的条目同样可见（回归：2026-09-25 同日复审条目对 WebUI 隐身）', () => {
    // 同日重跑把复审节追加进同一份文件——节内条目必须照常解析
    const md = REPORT + `\n---\n\n## 复审（2026-09-25 10:00）\n\n`
      + `本次复审发现 2 条新问题，待裁决：\n`
      + `1. **[high] stale-card** — 复审条目一\n   - 涉及: topics/c/abstract.md\n`
      + `2. **[low] other** — 复审条目二\n`
    const fs = parseFindings(md)
    expect(fs).toHaveLength(4)
    // 全局序号跨节连续（复审打印序号从头计，不采用）
    expect(fs.map(f => f.index)).toEqual([1, 2, 3, 4])
    expect(fs[2].review).toBe('复审（2026-09-25 10:00）')
    expect(fs[3].review).toBe('复审（2026-09-25 10:00）')
  })

  it('类型名含连字符（stale-card）不丢条目（回归：统计 10≠14 漏解析）', () => {
    const md = `## 提案（3 条）\n`
      + `1. **[low] other** — 一\n`
      + `2. **[medium] stale-card** — 二\n`
      + `3. **[low] stale-card** — 三\n`
    const fs = parseFindings(md)
    expect(fs).toHaveLength(3)
    expect(fs[1].type).toBe('stale-card')
  })

  it('非标准加粗格式走兜底解析，不静默丢弃', () => {
    const md = `## 提案（1 条）\n1. **某条目没有方括号标注** — 原因文本\n`
    const fs = parseFindings(md)
    expect(fs).toHaveLength(1)
    expect(fs[0].severity).toBe('other')
  })

  it('提案节之外的编号行、引用行不计入', () => {
    const md = `## 总评\n1. **[low] other** — 这不是提案条目\n> 引用也不是\n\n`
      + `## 提案（1 条）\n1. **[low] other** — 真条目\n`
    expect(parseFindings(md)).toHaveLength(1)
  })

  it('空输入返回空数组', () => {
    expect(parseFindings('')).toEqual([])
    expect(parseFindings(undefined)).toEqual([])
  })
})
