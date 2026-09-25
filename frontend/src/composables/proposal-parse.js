// 提案文件条目解析（纯函数，供 AuditPage 使用；测试见 proposal-parse.test.js）
// 与后端 store._proposal_findings_indices 同口径：提案节 + 同日复审节都含
// 条目，全局序号跨节连续（复审节打印序号从头计，不采用）。

export function parseFindings(markdown) {
  const lines = (markdown || '').split('\n')
  const findings = []
  let cur = null
  let seq = 0
  let section = ''
  for (const line of lines) {
    if (line.startsWith('## ')) {
      const head = line.slice(3).trim()
      section = head.startsWith('提案') || head.startsWith('复审') ? head : ''
      continue
    }
    if (!section || line.startsWith('> ')) continue
    // 类型名可含连字符（stale-card 等）——与后端条目解析同口径，
    // 只要求 "N. **[" 前缀，severity/type 尽力提取
    const m = line.match(/^(\d+)\.\s+\*\*\[(\w+)\]\s+([\w-]+)\*\*\s*[—-]\s*(.*)$/)
      || line.match(/^(\d+)\.\s+\*\*(.+?)\*\*\s*[—-]\s*(.*)$/)
    if (m) {
      seq += 1
      const review = section.startsWith('复审') ? section : ''
      if (m.length === 5) {
        cur = { index: seq, review, severity: m[2], type: m[3], reason: m[4], paths: [], proposal: '' }
      } else {
        const inner = m[2].match(/\[(\w+)\]\s*([\w-]+)/)
        cur = { index: seq, review, severity: inner?.[1] || 'other', type: inner?.[2] || 'other',
                reason: m[3], paths: [], proposal: '' }
      }
      findings.push(cur)
      continue
    }
    if (!cur) continue
    const p = line.match(/^\s+-\s+涉及:\s+(.*)$/)
    if (p) cur.paths.push(p[1])
    const s = line.match(/^\s+-\s+建议:\s+(.*)$/)
    if (s) cur.proposal = s[1]
  }
  return findings
}
