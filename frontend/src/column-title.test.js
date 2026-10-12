// 哨兵：naive-ui DataTable 的列 title 只接受 string | () => VNodeChild。
// `title: computed(...)` 传入的是 ComputedRef 对象——naive 拼串渲染成整行
// 「[object Object]」（0.3.18 使用记录表实爆：此前该表因缺 h 导入整体崩溃，
// 崩溃修好后这个潜伏的表头 bug 才显形——渲染类问题 vitest/build 都测不出，
// 靠真跑页面才发现）。用源码扫描钉死这个错误写法。
import { readFileSync, readdirSync } from 'node:fs'
import { join, dirname } from 'node:path'
import { fileURLToPath } from 'node:url'
import { test, expect } from 'vitest'

const componentsDir = join(dirname(fileURLToPath(import.meta.url)), 'components')

test('no DataTable column uses `title: computed(` (renders as [object Object])', () => {
  const offenders = []
  for (const f of readdirSync(componentsDir)) {
    if (!f.endsWith('.vue')) continue
    const src = readFileSync(join(componentsDir, f), 'utf-8')
    if (src.includes('title: computed(')) offenders.push(f)
  }
  expect(offenders).toEqual([])
})
